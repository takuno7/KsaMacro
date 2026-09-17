# -*- coding: utf-8 -*-
"""KSA 여객선 매크로 워커 스레드.

QThread 기반으로 백그라운드에서 KSA 여객선 잔여석을 지속적으로 감시하고,
조건에 맞는 좌석이 발견되는 즉시 예약 및 자동 결제를 수행합니다.
"""

import logging
import time
from typing import Any, Dict, List, Optional
from PyQt6.QtCore import QThread, pyqtSignal

from engines import KsaEngine, KsaError, KsaLoginError
from telegram_remote import TelegramBot

logger = logging.getLogger(__name__)


class EngineWorker(QThread):
    """KSA 매크로 백그라운드 폴링 워커."""

    log_signal = pyqtSignal(str, str)          # level, message
    finished_signal = pyqtSignal()
    success_signal = pyqtSignal(dict)          # 예매 성공 정보
    listing_signal = pyqtSignal(list)          # 최초 조회 스케줄 목록
    soldout_signal = pyqtSignal()              # 첫 매진 안내
    paired_signal = pyqtSignal(bool, str)      # telegram pair

    def __init__(self, settings: Dict[str, Any], parent=None):
        super().__init__(parent)
        self.settings = settings
        self.engine = KsaEngine()
        self.is_running = True
        self.search_count = 0
        self.first_soldout_notified = False
        self.telegram = TelegramBot(settings.get("tg_token"), settings.get("tg_chat_id"))

    def log(self, level: str, msg: str):
        self.log_signal.emit(level, msg)

    def stop(self):
        """매크로를 안전하게 중지합니다."""
        self.is_running = False

    def test_telegram(self):
        """텔레그램 연결을 테스트하고 chat_id를 자동 감지합니다."""
        if not self.telegram.chat_id:
            self.log("INFO", "텔레그램 대화 기록을 확인 중입니다...")
            found_id = self.telegram.get_last_chat_id()
            if not found_id:
                self.log("INFO", "대화 기록이 없습니다. 봇에게 아무 메시지나 보내고 15초 내에 대기합니다...")
                st = time.time()
                while time.time() - st < 15 and self.is_running:
                    found_id = self.telegram.get_last_chat_id()
                    if found_id:
                        break
                    time.sleep(1)
            if found_id:
                self.telegram.chat_id = found_id
                self.settings["tg_chat_id"] = found_id
                self.paired_signal.emit(True, found_id)
                self.log("SUCCESS", f"텔레그램 연동 성공! (Chat ID: {found_id})")
                self.telegram.send_message("⚓ <b>KsaMacro</b>: 텔레그램 연동이 완료되었습니다.")
                return True
            else:
                self.paired_signal.emit(False, "")
                self.log("ERROR", "텔레그램 연동 실패: 봇에게 메시지를 전송했는지 확인해주세요.")
                return False
        else:
            ok = self.telegram.send_message("⚓ <b>KsaMacro</b>: 텔레그램 알림 테스트 성공!")
            if ok:
                self.log("SUCCESS", "텔레그램 테스트 메시지가 전송되었습니다.")
            else:
                self.log("ERROR", "텔레그램 메시지 전송 실패. 토큰을 확인해주세요.")
            return ok

    def _filter_room(self, all_rooms: List[Dict[str, Any]], vessel_id: str, master_time: str, preference: str) -> Optional[Dict[str, Any]]:
        """선호 조건에 맞는 잔여석이 있는 객실을 선택합니다."""
        # 해당 선박/시간의 객실 필터
        rooms = [
            r for r in all_rooms
            if r.get("vesselid") == vessel_id and r.get("mastertime") == master_time
        ]

        # 잔여석이 있는 객실 (onlinecnt > 0, ispossible == 1)
        available = []
        for r in rooms:
            cnt = int(float(r.get("onlinecnt", 0)))
            is_pos = int(r.get("ispossible", 0))
            if is_pos == 1 and cnt > 0:
                available.append(r)

        if not available:
            return None

        # 선호도 필터링
        # 1. 일반객실: 이름에 '일반' or '2등' or '3등' 포함
        general_rooms = [
            r for r in available
            if any(k in r.get("classes", "") for k in ["일반", "2등", "3등", "평실"])
        ]

        if preference == "일반객실만":
            return general_rooms[0] if general_rooms else None
        elif preference == "일반객실 우선":
            if general_rooms:
                return general_rooms[0]
            return available[0]
        else:  # 전체 객실
            return available[0]

    def run(self):
        """매크로 실행 루프."""
        self.log("INFO", "🚀 KsaMacro 매크로가 시작되었습니다.")

        action = self.settings.get("action")
        if action == "test_telegram":
            self.test_telegram()
            self.finished_signal.emit()
            return
        elif action == "test_login":
            ksa_id = self.settings.get("ksa_id", "")
            ksa_pw = self.settings.get("ksa_pw", "")
            try:
                self.log("INFO", f"KSA 로그인 중... ({ksa_id})")
                login_info = self.engine.login(ksa_id, ksa_pw)
                self.log("SUCCESS", f"로그인 성공: {login_info.get('member')} 님")
            except Exception as e:
                self.log("ERROR", f"로그인 실패: {e}")
            self.finished_signal.emit()
            return

        # 1. 로그인
        ksa_id = self.settings.get("ksa_id", "")
        ksa_pw = self.settings.get("ksa_pw", "")

        if not ksa_id or not ksa_pw:
            self.log("ERROR", "KSA 회원 아이디 또는 비밀번호가 설정되지 않았습니다.")
            self.finished_signal.emit()
            return

        try:
            self.log("INFO", f"KSA 로그인 중... ({ksa_id})")
            login_info = self.engine.login(ksa_id, ksa_pw)
            self.log("SUCCESS", f"로그인 성공: {login_info.get('member')} 님")
        except Exception as e:
            self.log("ERROR", f"로그인 실패: {e}")
            self.finished_signal.emit()
            return

        # 2. 조회 조건 파싱
        f_port = {
            "port": self.settings.get("f_port", ""),
            "portid": self.settings.get("f_portid", ""),
            "portsubid": self.settings.get("f_portsubid", "0")
        }
        t_port = {
            "t_port": self.settings.get("t_port", ""),
            "t_portid": self.settings.get("t_portid", ""),
            "t_portsubid": self.settings.get("t_portsubid", "0")
        }
        date_str = self.settings.get("date", "")
        time_start = self.settings.get("time_start", "00:00")
        time_end = self.settings.get("time_end", "23:59")
        preference = self.settings.get("room_preference", "일반객실 우선")
        passengers = self.settings.get("passengers", [])
        card_info = self.settings.get("card_info") if self.settings.get("auto_pay") else None
        interval = float(self.settings.get("refresh_interval", 2.0))

        if not f_port["portid"] or not t_port["t_portid"] or not date_str:
            self.log("ERROR", "출발항, 도착항 또는 날짜 설정이 올바르지 않습니다.")
            self.finished_signal.emit()
            return

        self.log("INFO", f"구간: {f_port['port']} → {t_port['t_port']} | 날짜: {date_str} ({time_start}~{time_end})")
        self.log("INFO", f"선호 객실: {preference} | 승객 수: {len(passengers)}명 | 자동결제: {'켜짐' if card_info else '꺼짐(좌석선점만)'}")

        # 3. 최초 목록 조회
        try:
            init_lines = self.engine.initial_listing(f_port, t_port, date_str, time_start, time_end)
            self.listing_signal.emit(init_lines)
            for line in init_lines:
                self.log("INFO", line)
        except Exception as e:
            self.log("WARNING", f"최초 일정 조회 실패: {e}")

        # 4. 폴링 루프
        search_data_obj = {
            "journeylen": 1,
            "portList": [{
                "f_port": f_port["port"],
                "f_portid": f_port["portid"],
                "f_portsubid": f_port["portsubid"],
                "t_port": t_port["t_port"],
                "t_portid": t_port["t_portid"],
                "t_portsubid": t_port["t_portsubid"],
                "departuretype": "all",
                "bookingtype": "normal"
            }],
            "dateList": [{
                "date": date_str,
                "fulldatename": date_str.replace("-", ".")
            }]
        }

        while self.is_running:
            self.search_count += 1
            try:
                schedules, all_rooms = self.engine.search_departures(
                    f_port["portid"], f_port["portsubid"],
                    t_port["t_portid"], t_port["t_portsubid"],
                    date_str
                )

                found_target_room: Optional[Dict[str, Any]] = None

                for s in schedules:
                    dep_time = s.get("departuretime", "")
                    time_part = dep_time.split(" ")[-1] if " " in dep_time else dep_time
                    if not (time_start <= time_part <= time_end):
                        continue

                    # 객실 매칭
                    v_id = s.get("vesselid", "")
                    m_time = s.get("mastertime", "")
                    matched = self._filter_room(all_rooms, v_id, m_time, preference)
                    if matched:
                        found_target_room = matched
                        break

                if found_target_room:
                    v_name = found_target_room.get("vessel", "")
                    c_name = found_target_room.get("classes", "")
                    d_time = found_target_room.get("departuretime", "")
                    cnt = int(float(found_target_room.get("onlinecnt", 0)))
                    self.log("SUCCESS", f"⚡ 잔여석 발견! [{v_name}] {d_time} {c_name} (잔여 {cnt}석) - 즉시 예매 시도!")

                    # 예매 및 결제 실행
                    try:
                        res = self.engine.book(
                            search_data=search_data_obj,
                            selected_room=found_target_room,
                            passengers=passengers,
                            card_info=card_info,
                            log_fn=self.log
                        )

                        # 성공 처리
                        self.success_signal.emit(res)
                        msg_text = (
                            f"🎉 <b>KSA 여객선 예매 성공!</b>\n\n"
                            f"🚢 <b>선박</b>: {res.get('vessel')}\n"
                            f"⏰ <b>출발시간</b>: {res.get('departure_time')}\n"
                            f"💺 <b>객실/좌석</b>: {res.get('classes')} ({res.get('seats')})\n"
                            f"💰 <b>결제금액</b>: {res.get('fare', 0):,}원\n"
                            f"👥 <b>승객</b>: {', '.join(res.get('passengers', []))}\n"
                            f"🔖 <b>티켓키</b>: {res.get('ticketingkey')}"
                        )
                        self.telegram.send_message(msg_text)
                        break

                    except Exception as e:
                        self.log("ERROR", f"예매 진행 중 오류 발생: {e}")

                else:
                    if not self.first_soldout_notified:
                        self.first_soldout_notified = True
                        self.soldout_signal.emit()
                        self.log("INFO", f"현재 조건({preference})의 잔여석이 없어 매크로 감시를 계속합니다...")

                    if self.search_count % 10 == 0:
                        self.log("INFO", f"조회 중... ({self.search_count}회 시도)")

            except Exception as e:
                err_str = str(e)
                self.log("WARNING", f"조회 요청 오류 ({self.search_count}회): {err_str}")
                # 세션 문제 의심 시 재로그인
                if "세션" in err_str or "로그인" in err_str or "401" in err_str:
                    try:
                        self.engine.relogin()
                    except Exception:
                        pass

            # 대기
            slept = 0.0
            while slept < interval and self.is_running:
                time.sleep(0.1)
                slept += 0.1

        self.log("INFO", "🛑 매크로 작업이 종료되었습니다.")
        self.finished_signal.emit()
