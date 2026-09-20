# -*- coding: utf-8 -*-
"""KSA 여객선 매크로 워커 스레드.

QThread 기반으로 백그라운드에서 KSA 여객선 잔여석을 지속적으로 감시하고,
조건에 맞는 좌석이 발견되는 즉시 예약 및 자동 결제를 수행합니다.
"""

import html
import logging
import time
from typing import Any, Dict, List, Optional
from PyQt6.QtCore import QThread, pyqtSignal

from engines import BASE_URL, KsaCardDeclinedError, KsaEngine, KsaError, KsaLoginError, KsaPaymentStateError
from telegram_remote import TelegramBot

BOOKING_PAGE_URL = f"{BASE_URL}/page/booking"
RESERVATION_PAGE_URL = f"{BASE_URL}/page/payment_confirm"

logger = logging.getLogger(__name__)


def format_result_message(res: Dict[str, Any]) -> str:
    """예매 결과를 텔레그램 HTML 메시지로 만듭니다. 서버 값은 HTML 이스케이프합니다."""
    e = lambda v: html.escape(str(v if v is not None else ""))
    details = (
        f"🚢 <b>선박</b>: {e(res.get('vessel'))}\n"
        f"⏰ <b>출발시간</b>: {e(res.get('departure_time'))}\n"
        f"💺 <b>객실/좌석</b>: {e(res.get('classes'))} ({e(res.get('seats'))})\n"
        f"💰 <b>금액</b>: {int(res.get('fare') or 0):,}원\n"
        f"👥 <b>승객</b>: {e(', '.join(res.get('passengers') or []))}\n"
        f"🔖 <b>관리번호</b>: {e(res.get('ticketingkey'))} (입력할 곳 없음 · 고객센터 문의 시 참고용)"
    )
    if res.get("status") == "VA_RESERVED":
        return (
            f"🏦 <b>가상계좌 예약 완료 (입금 대기)</b>\n\n{details}\n\n"
            f"🏦 <b>입금은행</b>: {e(res.get('va_bank'))}\n"
            f"💳 <b>계좌번호</b>: <code>{e(res.get('va_account'))}</code>\n"
            f"💰 <b>입금액</b>: {int(res.get('va_amount') or 0):,}원\n"
            f"⏳ <b>입금기한</b>: {e(res.get('va_expire'))}\n\n"
            f"입금기한까지 정확한 금액을 입금해야 예매가 완료되며, 미입금 시 예약은 자동 취소됩니다.\n"
            f'👉 <a href="{RESERVATION_PAGE_URL}">KSA 예매내역 확인</a>'
        )
    if res.get("status") == "SEAT_AVAILABLE":
        return (
            f"🔔 <b>빈자리 발견 (예약 안 됨)</b>\n\n"
            f"🚢 <b>선박</b>: {e(res.get('vessel'))}\n"
            f"⏰ <b>출발시간</b>: {e(res.get('departure_time'))}\n"
            f"💺 <b>객실</b>: {e(res.get('classes'))} (잔여 {e(res.get('remaining'))}석)\n"
            f"💰 <b>금액</b>: {int(res.get('fare') or 0):,}원\n"
            f"👥 <b>승객</b>: {e(', '.join(res.get('passengers') or []))}\n\n"
            f"이 운항편은 가상계좌 예약을 쓸 수 없어 좌석을 잡지 않았습니다. "
            f"다른 사람이 먼저 예매할 수 있으니 서둘러 직접 예매해 주세요. 매크로는 멈췄습니다.\n"
            f"자동으로 예매하려면 💳 자동 결제를 켜고 카드 정보를 입력하세요.\n"
            f'👉 <a href="{BOOKING_PAGE_URL}">KSA 예매 페이지</a>'
        )
    return f"🎉 <b>KSA 여객선 예매 성공!</b>\n\n{details}"


class EngineWorker(QThread):
    """KSA 매크로 백그라운드 폴링 워커."""

    log_signal = pyqtSignal(str, str)          # level, message
    finished_signal = pyqtSignal()
    success_signal = pyqtSignal(dict)          # 예매 성공 정보
    listing_signal = pyqtSignal(list)          # 최초 조회 스케줄 목록
    soldout_signal = pyqtSignal()              # 첫 매진 안내
    warning_signal = pyqtSignal(str)           # 시작 시 사전 경고 (GUI 팝업)
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
                self.log("ERROR", f"텔레그램 연동 실패: {self.telegram.last_error or '봇에게 메시지를 전송했는지 확인해주세요.'}")
                return False
        else:
            ok = self.telegram.send_message("⚓ <b>KsaMacro</b>: 텔레그램 알림 테스트 성공!")
            if ok:
                self.log("SUCCESS", "텔레그램 테스트 메시지가 전송되었습니다.")
            else:
                self.log("ERROR", "텔레그램 메시지 전송 실패. 토큰을 확인해주세요.")
            self.paired_signal.emit(ok, self.telegram.chat_id if ok else "")
            return ok

    def _warn_unsupported_virtual(self, f_port, t_port, date_str, time_start, time_end):
        """자동 결제가 꺼진 채 시작할 때 가상계좌 미지원 운항편을 미리 경고합니다 (매크로는 계속 진행)."""
        try:
            unsupported, total = self.engine.unsupported_virtual_sailings(
                f_port, t_port, date_str, time_start, time_end)
        except Exception as e:
            self.log("WARNING", f"가상계좌 지원 여부를 미리 확인하지 못했습니다: {e}")
            return
        if not unsupported:
            if total:
                self.log("INFO", f"조회 범위의 운항편 {total}편 모두 가상계좌 예약을 지원합니다.")
            return
        scope = "모든 운항편이" if len(unsupported) == total else f"{total}편 중 {len(unsupported)}편이"
        text = (f"자동 결제가 꺼져 있는데, 조회 범위의 {scope} 가상계좌 미지원 선사입니다.\n"
                f"미지원 운항편: {', '.join(unsupported)}\n"
                "이 운항편에서 빈자리가 나면 예약하지 않고 빈자리 알림만 보냅니다. "
                "자동으로 예매하려면 매크로를 중지하고 자동 결제를 켠 뒤 카드 정보를 입력하세요.")
        self.log("WARNING", text.replace("\n", " "))
        self.warning_signal.emit(text)
        self.telegram.send_message(f"⚠️ <b>가상계좌 미지원 선사 경고</b>\n\n{html.escape(text)}")

    def _relogin_if_expired(self):
        """요청이 실패하면 실제 로그인 상태를 확인해 세션이 만료됐을 때만 재로그인합니다."""
        try:
            if not self.engine.is_logged_in():
                self.log("WARNING", "로그인 세션이 만료되어 다시 로그인합니다.")
                self.engine.relogin()
                self.log("SUCCESS", "재로그인 성공")
        except Exception as e:
            self.log("WARNING", f"재로그인 실패: {e}")

    def _reservation_group_ids(self) -> Optional[set]:
        """현재 KSA 예매내역의 groupid 집합. 조회 실패 시 None."""
        try:
            return {str(g.get("groupid")) for g in self.engine.get_reservations() if isinstance(g, dict)}
        except Exception as e:
            self.log("WARNING", f"예매내역 기준선 조회 실패 (결과 불명 시 자동 대조 불가): {e}")
            return None

    def _check_new_reservation(self, baseline: Optional[set]) -> str:
        """결과 불명 예매 후 예매내역을 다시 조회해 새 항목이 생겼는지 알려 줍니다."""
        if baseline is None:
            return "시작 시 예매내역을 조회하지 못해 자동 대조를 할 수 없습니다. KSA 예매내역을 직접 확인하세요."
        now = self._reservation_group_ids()
        if now is None:
            return "예매내역 재조회에 실패했습니다. KSA 예매내역을 직접 확인하세요."
        new = now - baseline
        if new:
            return f"예매내역에 새 항목 {len(new)}건이 생겼습니다 (그룹 {', '.join(sorted(new))}). 예약/결제가 처리된 것으로 보입니다."
        return ("예매내역에 새 항목이 없습니다. 처리되지 않았을 가능성이 높지만, 반영이 늦을 수 있으니 "
                "몇 분 뒤 KSA 예매내역을 확인하세요. 잡혀 있던 좌석은 약 20분 뒤 자동으로 풀립니다.")

    def _filter_room(self, all_rooms: List[Dict[str, Any]], vessel_id: str, master_time: str, preference: str,
                     seats_needed: int = 1) -> Optional[Dict[str, Any]]:
        """선호 조건에 맞는 잔여석이 있는 객실을 선택합니다."""
        # 해당 선박/시간의 객실 필터
        rooms = [
            r for r in all_rooms
            if r.get("vesselid") == vessel_id and r.get("mastertime") == master_time
        ]

        # 승객 전원이 앉을 수 있는 객실 (onlinecnt >= 필요 좌석, ispossible == 1)
        available = []
        for r in rooms:
            try:
                cnt = int(float(r.get("onlinecnt") or 0))
                is_pos = int(float(r.get("ispossible") or 0))
            except (TypeError, ValueError):
                continue  # 값이 이상한 객실 하나 때문에 전체 조회가 실패하지 않도록 건너뜀
            if is_pos == 1 and cnt >= max(seats_needed, 1):
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

        # 결과 불명 예매가 생기면 비교할 예매내역 기준선 (조회 실패 시 None)
        baseline_groups = self._reservation_group_ids()

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
        # 유아(ticketid 5)는 좌석을 차지하지 않습니다.
        seats_needed = sum(1 for p in passengers if str(p.get("ticketid", "")) != "5")

        if not f_port["portid"] or not t_port["t_portid"] or not date_str:
            self.log("ERROR", "출발항, 도착항 또는 날짜 설정이 올바르지 않습니다.")
            self.finished_signal.emit()
            return

        self.log("INFO", f"구간: {f_port['port']} → {t_port['t_port']} | 날짜: {date_str} ({time_start}~{time_end})")
        self.log("INFO", f"선호 객실: {preference} | 승객 수: {len(passengers)}명 | 자동결제: {'켜짐' if card_info else '꺼짐(가상계좌 예약)'}")

        # 3. 최초 목록 조회
        try:
            init_lines = self.engine.initial_listing(f_port, t_port, date_str, time_start, time_end)
            self.listing_signal.emit(init_lines)
            for line in init_lines:
                self.log("INFO", line)
        except Exception as e:
            self.log("WARNING", f"최초 일정 조회 실패: {e}")

        if not card_info:
            self._warn_unsupported_virtual(f_port, t_port, date_str, time_start, time_end)

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
                    matched = self._filter_room(all_rooms, v_id, m_time, preference, seats_needed)
                    if matched:
                        found_target_room = matched
                        break

                if found_target_room:
                    v_name = found_target_room.get("vessel", "")
                    c_name = found_target_room.get("classes", "")
                    d_time = found_target_room.get("departuretime", "")
                    cnt = found_target_room.get("onlinecnt", "?")
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
                    except KsaPaymentStateError as e:
                        # 결제/예약이 이미 반영됐을 수 있어 재시도하면 중복 결제·예약이 됩니다.
                        verdict = self._check_new_reservation(baseline_groups)
                        self.log("ERROR", f"{e} 중복 결제를 막기 위해 매크로를 중지합니다. {verdict}")
                        self.telegram.send_message(
                            f"⚠️ <b>예매 상태 확인 필요 - 매크로 중지</b>\n\n{html.escape(str(e))}\n\n"
                            f"🔎 {html.escape(verdict)}\n\n"
                            f"결제 또는 예약이 이미 처리됐을 수 있어 중복을 막기 위해 매크로를 멈췄습니다.\n"
                            f'👉 <a href="{RESERVATION_PAGE_URL}">KSA 예매내역 확인</a>'
                        )
                        break
                    except KsaCardDeclinedError as e:
                        self.log("ERROR", f"{e} 같은 카드로 반복 시도하지 않도록 매크로를 중지합니다.")
                        self.telegram.send_message(
                            f"❌ <b>카드 승인 거절 - 매크로 중지</b>\n\n{html.escape(str(e))}\n\n"
                            f"잡았던 좌석은 되돌렸습니다. 카드 정보·한도를 확인한 뒤 다시 시작하세요.")
                        break
                    except Exception as e:
                        self.log("ERROR", f"예매 진행 중 오류 발생: {e}")
                        self._relogin_if_expired()
                    else:
                        # 예매가 끝났으므로 알림이 실패해도 반드시 감시를 멈춥니다 (재예매 방지).
                        self.success_signal.emit(res)
                        try:
                            self.telegram.send_message(format_result_message(res))
                        except Exception as e:
                            self.log("WARNING", f"텔레그램 결과 알림 작성 실패: {e}")
                        break

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
                self._relogin_if_expired()

            # 대기
            slept = 0.0
            while slept < interval and self.is_running:
                time.sleep(0.1)
                slept += 0.1

        self.log("INFO", "🛑 매크로 작업이 종료되었습니다.")
        self.finished_signal.emit()
