# -*- coding: utf-8 -*-
"""KSA (한국해운조합 - 가보고 싶은 섬) 여객선 예매 엔진 어댑터.

브라우저 없이도 초고속 밀리초(ms) 단위로 KSA 웹 백엔드와 직접 통신하여
로그인, 항구 조회, 여정 조회, 실시간 잔여석 확인, 승객 등록, 카드 자동 결제 및 발권까지
전체 예매 프로세스를 수행합니다.
"""

import json
import logging
import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://island.theksa.co.kr"

COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/page/booking",
    "X-Requested-With": "XMLHttpRequest",
    "Accept": "*/*",
    "Accept-Language": "ko,en-US;q=0.9,en;q=0.8",
}


class KsaError(Exception):
    """KSA API 관련 일반 에러."""
    pass


class KsaLoginError(KsaError):
    """로그인 실패 에러."""
    pass


class KsaSoldOutError(KsaError):
    """잔여 좌석 없음."""
    pass


def _to_won(value: Any, default: int) -> int:
    """'3,800', '3800.0', 3800 같은 금액 응답을 정수로 바꿉니다."""
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return default


class KsaCardDeclinedError(KsaError):
    """카드사가 승인을 거절함. 재시도하면 같은 거절이 반복되고 카드 잠금 위험이 있어 중지해야 함."""
    pass


def _ok(code: Any) -> bool:
    """사이트 JS의 느슨한 비교(errcode == 0)와 같게 0/"0"을 성공으로 봅니다."""
    return str(code) == "0"


def _ticket_outcome(comp_json: Dict[str, Any]) -> str:
    """ticketComplete 응답 판정: "ok" / "failed"(명확한 실패) / "unknown"(판정 불가).

    사이트는 result == false 또는 result[0].errcode != 0 이면 실패로 보고 결제를 취소합니다.
    결과 행이나 errcode가 없으면 성공으로 단정할 수 없으므로 "unknown"입니다.
    """
    if not comp_json.get("result", False):
        return "failed"
    rows = (comp_json.get("data") or {}).get("result") or []
    if not rows or any(not isinstance(r, dict) or "errcode" not in r for r in rows):
        return "unknown"
    return "ok" if all(_ok(r["errcode"]) for r in rows) else "failed"


def _event_contents(mode: str, event: Any) -> str:
    """사이트 payment.js fn_eventContents와 같은 특수할인 파라미터 문자열을 만듭니다."""
    if not isinstance(event, dict) or not event:
        return ""
    key = event.get("eventkey")
    idx = 0 if mode == "1" else 1
    if key == "badaro":
        return f"{key}-{event.get('seq')}-{event.get('farebase1' if mode == '1' else 'farebase2')}"
    if key in ("outhometown", "homecoming"):
        return f"{key}-{event.get('seq')}"
    if key == "military":
        return f"{key}-{event.get('unit')}-{event.get('rank')}-{event.get('name')}-{event.get('idnumber')}"
    if key == "companypromotion":
        return f"{key}-{event.get('promotionid')}"
    if key == "militarypassenger":
        try:
            return f"{key}-{event['iscommander'][idx]}-{event['pblno'][idx]}"
        except (KeyError, IndexError, TypeError):
            return ""
    return ""


class KsaPaymentStateError(KsaError):
    """결제/예약이 서버에 이미 반영됐을 수 있어 자동 재시도하면 중복 결제·예약이 되는 오류."""
    pass


class KsaEngine:
    """KSA 여객선 예매 엔진 클라이언트."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(COMMON_HEADERS)
        self.user_id: Optional[str] = None
        self.user_pw: Optional[str] = None
        self.user_name: str = ""
        self.member_seq: str = "-99"
        self._cached_ports: List[Dict[str, Any]] = []

    # ----------------------------------------------------------------------
    # 1. 로그인 및 세션 관리
    # ----------------------------------------------------------------------
    def login(self, user_id: str, pw: str) -> Dict[str, Any]:
        """KSA 사이트에 로그인합니다.
        
        Args:
            user_id: 아이디 또는 휴대폰번호 ('-' 포함 또는 미포함)
            pw: 비밀번호
            
        Returns:
            Dict[str, Any]: 로그인 결과 정보 (회원명, memberid 등)
        """
        self.user_id = user_id.replace("-", "").strip()
        self.user_pw = pw

        payload = {
            "jsondata": json.dumps({
                "method": "login",
                "memberid": self.user_id,
                "passcode": self.user_pw,
                "snstype": "",
                "snsid": "",
                "accesstoken": "",
                "tel": ""
            })
        }

        url = f"{BASE_URL}/useraccount/doLogin"
        try:
            resp = self.session.post(url, data=payload, timeout=10)
            resp.raise_for_status()
            res_json = resp.json()
        except Exception as e:
            raise KsaLoginError(f"로그인 요청 중 네트워크 오류가 발생했습니다: {e}") from e

        if not res_json.get("result", False):
            msg = res_json.get("message") or "로그인에 실패했습니다."
            raise KsaLoginError(msg)

        data = res_json.get("data", {})
        err_code = data.get("errcode", -1)
        if err_code != 0:
            err_msg = data.get("errmsg") or "아이디 또는 비밀번호가 올바르지 않습니다."
            raise KsaLoginError(err_msg)

        self.user_name = data.get("member", "")
        # 세션 검증을 위해 booking 페이지 1회 호출
        try:
            chk_resp = self.session.get(f"{BASE_URL}/page/booking", timeout=10)
            m = re.search(r'loginSessionInfo\.seq\s*=\s*[\'"](.*?)[\'"]', chk_resp.text)
            if m:
                self.member_seq = m.group(1)
        except Exception:
            pass

        return {
            "success": True,
            "member": self.user_name,
            "member_id": self.user_id,
            "seq": self.member_seq
        }

    def relogin(self) -> Dict[str, Any]:
        """세션이 만료된 경우 재로그인을 수행합니다."""
        if not self.user_id or not self.user_pw:
            raise KsaLoginError("저장된 로그인 자격 증명이 없습니다.")
        return self.login(self.user_id, self.user_pw)

    def is_logged_in(self) -> bool:
        """현재 로그인 상태인지 확인합니다."""
        try:
            resp = self.session.get(f"{BASE_URL}/page/booking", timeout=7)
            return 'loginSessionInfo.seq = "-99"' not in resp.text
        except Exception:
            return False

    # ----------------------------------------------------------------------
    # 2. 항구 (출발지 / 도착지) 조회
    # ----------------------------------------------------------------------
    def get_ports(self, refresh: bool = False) -> List[Dict[str, Any]]:
        """전체 출발 항구 목록을 조회합니다."""
        if self._cached_ports and not refresh:
            return self._cached_ports

        url = f"{BASE_URL}/booking/selectPortList"
        try:
            resp = self.session.post(url, data={"portkind": "all"}, timeout=10)
            resp.raise_for_status()
            res_json = resp.json()
            ports = res_json.get("data", {}).get("result", [])
            # 정렬
            self._cached_ports = sorted(ports, key=lambda x: x.get("port", ""))
            return self._cached_ports
        except Exception as e:
            logger.error(f"출발항 목록 조회 실패: {e}")
            return []

    def get_pair_ports(self, f_portid: str, f_portsubid: str = "0") -> List[Dict[str, Any]]:
        """선택된 출발지에서 갈 수 있는 도착지 목록을 조회합니다."""
        url = f"{BASE_URL}/booking/selectPairPortList"
        payload = {
            "portid": f_portid,
            "portsubid": f_portsubid,
            "bundleport": "",
            "portkind": "fport",
            "ismain": "0"
        }
        try:
            resp = self.session.post(url, data=payload, timeout=10)
            resp.raise_for_status()
            res_json = resp.json()
            return res_json.get("data", {}).get("result", [])
        except Exception as e:
            logger.error(f"도착지 목록 조회 실패: {e}")
            return []

    # ----------------------------------------------------------------------
    # 3. 여정 및 잔여석 조회
    # ----------------------------------------------------------------------
    def search_departures(
        self,
        f_portid: str,
        f_portsubid: str,
        t_portid: str,
        t_portsubid: str,
        date_str: str  # YYYY-MM-DD
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """여정 목록 및 객실별 잔여석을 조회합니다.
        
        Returns:
            Tuple[List[Dict], List[Dict]]: (운항 스케줄 목록, 객실별 상세 잔여석 목록)
        """
        url = f"{BASE_URL}/booking/selectDepartureList"
        payload = {
            "f_portidlist": f_portid,
            "f_portsubidlist": f_portsubid,
            "t_portidlist": t_portid,
            "t_portsubidlist": t_portsubid,
            "masterdate": date_str
        }

        try:
            resp = self.session.post(url, data=payload, timeout=10)
            resp.raise_for_status()
            res_json = resp.json()
        except Exception as e:
            raise KsaError(f"여정 조회 요청 중 오류가 발생했습니다: {e}") from e

        data = res_json.get("data") or {}
        schedule_list = data.get("result") or []
        all_rooms = data.get("resultAll") or []

        return schedule_list, all_rooms

    def initial_listing(
        self,
        f_port: Dict[str, Any],
        t_port: Dict[str, Any],
        date_str: str,
        time_start: str = "00:00",
        time_end: str = "23:59"
    ) -> List[str]:
        """최초 조회 시 GUI 및 텔레그램 안내용 텍스트 라인들을 생성합니다."""
        f_id = f_port["portid"]
        f_sub = f_port.get("portsubid", "0")
        t_id = t_port["t_portid"]
        t_sub = t_port.get("t_portsubid", "0")

        schedules, all_rooms = self.search_departures(f_id, f_sub, t_id, t_sub, date_str)
        if not schedules:
            return [f"[{date_str}] 해당 구간({f_port.get('port')}→{t_port.get('t_port')})의 운항 일정이 없습니다."]

        lines = []
        for s in schedules:
            dep_time_full = s.get("departuretime", "")
            # dep_time_full format: 'YYYY-MM-DD HH:MM' or 'HH:MM'
            time_part = dep_time_full.split(" ")[-1] if " " in dep_time_full else dep_time_full
            if not (time_start <= time_part <= time_end):
                continue

            vessel = s.get("vessel", "여객선")
            vessel_id = s.get("vesselid", "")
            master_time = s.get("mastertime", "")

            # 객실 잔여석 집계
            matching_rooms = [
                r for r in all_rooms
                if r.get("vesselid") == vessel_id and r.get("mastertime") == master_time
            ]

            room_descs = []
            for r in matching_rooms:
                r_name = r.get("classes", "객실")
                cnt = int(float(r.get("onlinecnt", 0)))
                fare = r.get("fare", 0)
                is_pos = int(r.get("ispossible", 0))
                status = f"✅{cnt}석 잔여" if (is_pos == 1 and cnt > 0) else "❌매진"
                room_descs.append(f"{r_name}({status}, {fare:,}원)")

            room_str = " | ".join(room_descs) if room_descs else "객실 정보 없음"
            lines.append(f"🚢 [{vessel}] {time_part}출발 -> {room_str}")

        if not lines:
            lines.append(f"{time_start}~{time_end} 사이에 운항하는 여객선이 없습니다.")
        return lines

    # ----------------------------------------------------------------------
    # 4. 예약 트랜잭션 (선택 -> 승객등록 -> 좌석배정 -> 결제 -> 발권)
    # ----------------------------------------------------------------------
    def book(
        self,
        search_data: Dict[str, Any],
        selected_room: Dict[str, Any],
        passengers: List[Dict[str, Any]],
        card_info: Optional[Dict[str, str]] = None,
        log_fn: Optional[Any] = None
    ) -> Dict[str, Any]:
        """선택된 객실과 승객 정보로 자동 예약 및 결제를 실행합니다.
        
        Args:
            search_data: 기본 검색 조건 데이터
            selected_room: `resultAll` 중 사용자가 선택한 객실 항목
            passengers: 승객 목록 (성명, 주민번호, 성별, 전화번호, 할인구분 등)
            card_info: 카드 결제 정보 (cardno, validdate, password, requestno). 없으면 가상계좌 예약, 가상계좌 미지원 운항편은 좌석을 잡지 않고 SEAT_AVAILABLE 반환.
            log_fn: 로그 콜백 함수 (level, message)
            
        Returns:
            Dict[str, Any]: 예매 결과 정보
        """
        def _log(lvl: str, msg: str):
            if log_fn:
                log_fn(lvl, msg)
            logger.info(f"[{lvl}] {msg}")

        _log("INFO", f"선택 객실: {selected_room.get('vessel')} {selected_room.get('classes')} "
                     f"({selected_room.get('departuretime')} 출발)")

        # 1. selectCheckJourney (ksa.util.journeyChkMaker 공식 규격)
        check_item = {
            "rf_portid": selected_room.get("rf_portid", selected_room.get("t_portid")),
            "rt_portid": selected_room.get("rt_portid", selected_room.get("f_portid")),
            "rsectionid": selected_room.get("rsectionid", "0"),
            "f_portid": selected_room.get("f_portid"),
            "f_portsubid": selected_room.get("f_portsubid", "0"),
            "t_portid": selected_room.get("t_portid"),
            "t_portsubid": selected_room.get("t_portsubid", "0"),
            "vesselid": selected_room.get("vesselid"),
            "mastertime": selected_room.get("mastertime"),
            "departuretime": selected_room.get("departuretime"),
            "pairtime": selected_room.get("pairtime", ""),
            "total": 1,
            "step": 0,
            "classesList": [{
                "_vesselid": selected_room.get("vesselid"),
                "_mastertime": selected_room.get("mastertime"),
                "masterclassesid": selected_room.get("masterclassesid", "0"),
                "classesid": selected_room.get("classesid")
            }],
            "classeslen": 1
        }
        check_journey_payload = [check_item]

        url_chk_journey = f"{BASE_URL}/booking/selectCheckJourney"
        resp = self.session.post(url_chk_journey, data={"jsonstring": json.dumps(check_journey_payload)}, timeout=10)
        try:
            chk_res = resp.json()
        except Exception as e:
            raise KsaError(f"여정 선택 확인 응답 오류 (HTTP {resp.status_code}): {resp.text[:100]}") from e

        if chk_res.get("data", {}).get("errcode", -1) != 0:
            err_msg = chk_res.get("data", {}).get("errmsg") or "여정 선택 확인 실패"
            raise KsaError(err_msg)

        _log("INFO", "여정 확인 완료. 승객 정보를 검증 및 등록합니다...")

        # 1-1. selectDepartureTicketDC (유효한 ticketid / dcid 조회)
        default_dcid = "01"
        dcid_by_ticket: Dict[str, str] = {}
        try:
            dc_req = [{
                "rf_portid": check_item["rf_portid"],
                "rt_portid": check_item["rt_portid"],
                "rsectionid": check_item["rsectionid"],
                "f_portid": check_item["f_portid"],
                "f_portsubid": check_item["f_portsubid"],
                "t_portid": check_item["t_portid"],
                "t_portsubid": check_item["t_portsubid"],
                "mastertime": check_item["mastertime"],
                "vesselid": check_item["vesselid"],
                "classesid": check_item["classesList"][0]["classesid"],
                "masterclassesid": check_item["classesList"][0]["masterclassesid"]
            }]
            dc_resp = self.session.post(f"{BASE_URL}/booking/selectDepartureTicketDC", data={"JsonString": json.dumps(dc_req), "PromotionID": ""}, timeout=5)
            td_list = (dc_resp.json().get("data") or {}).get("tdResult") or []
            for td in td_list:
                # 권종(대인·소아·유아 등)마다 첫 번째 할인코드를 기본값으로 씁니다.
                if td.get("dcid"):
                    dcid_by_ticket.setdefault(str(td.get("ticketid")), str(td.get("dcid")))
            default_dcid = dcid_by_ticket.get("1", default_dcid)
        except Exception:
            pass

        # 2. checkPassengerValidation
        # 승객 리스트 포맷 생성
        formatted_passengers = []
        for i, p in enumerate(passengers):
            # ticketid 1: 대인, 3: 소아, 5: 유아
            ticket_id = p.get("ticketid", "1")
            dc_id = p.get("dcid")
            if not dc_id or dc_id == "100":
                dc_id = dcid_by_ticket.get(str(ticket_id), default_dcid)

            clean_id = p["idnumber"].replace("-", "").strip()
            clean_tel = p["tel"].replace("-", "").strip()
            
            p_detail = {
                "name": p["name"].strip(),
                "idnumber": clean_id,
                "sex": p.get("sex", "M"),
                "tel": clean_tel,
                "ticketid": ticket_id,
                "ticketID1": ticket_id,
                "ticketID2": ticket_id,
                "dcid": dc_id,
                "dcID1": dc_id,
                "dcID2": dc_id,
                "mode": "1",
                "manCnt": len(passengers),
                "index": i,
                "cargoCnt": 0,
                "manCheck": f"{ticket_id}-{dc_id}"
            }
            formatted_passengers.append(p_detail)

        # 결제 페이지와 selectFareUpdate는 checkInsert의 original을 다시 읽습니다.
        # journeyMaker처럼 운임 종류, 구간, 판매 회사 및 객실 정보를 보존합니다.
        journey_list_for_validation = [{
            **selected_room,
            "classesList": [dict(selected_room)],
            "classeslen": 1,
        }]
        search_data = {
            **search_data,
            "journeylen": 1,
            "journeyList": journey_list_for_validation,
            "passengerList": formatted_passengers,
            "cargoList": [],
        }

        url_val = f"{BASE_URL}/booking/checkPassengerValidation"
        val_payload = {
            "original": json.dumps(search_data, ensure_ascii=False),
            "journeylist": json.dumps(journey_list_for_validation, ensure_ascii=False),
            "passengerlist": json.dumps(formatted_passengers, ensure_ascii=False),
            "cargolist": "[]",
            "additional": json.dumps({}),
            "browser": COMMON_HEADERS["User-Agent"]
        }

        val_resp = self.session.post(url_val, data=val_payload, timeout=10)
        val_json = val_resp.json()
        val_data = val_json.get("data") or {}
        if not val_json.get("result") or not val_data or val_data.get("failed"):
            err_m = val_data.get("message") or val_json.get("message") or "승객 유효성 검사에 실패했습니다."
            raise KsaError(err_m)
        for row in val_data.get("result", []):
            if row.get("result") != "TRUE":
                raise KsaError(row.get("resultmessage") or "승객 유효성 검사에 실패했습니다.")

        checkkey = val_data.get("checkkey")
        if not checkkey:
            raise KsaError("승객 유효성 검사 키(checkkey) 획득 실패")

        search_data["checkkey"] = checkkey
        for checked in val_data.get("checked") or []:
            if not isinstance(checked, dict) or not checked.get("seqstring"):
                continue
            for fp in formatted_passengers:
                if str(fp.get("index")) == str(checked.get("vaindex")):
                    try:
                        fp["eventcontents"] = json.loads(checked["seqstring"])
                    except (TypeError, ValueError):
                        pass
        _log("INFO", "승객 검증 완료. 승객 등록 진행...")

        # 3. checkInsert
        url_insert = f"{BASE_URL}/booking/checkInsert"
        ins_payload = {
            "original": json.dumps(search_data, ensure_ascii=False),
            "passengerlist": json.dumps(formatted_passengers, ensure_ascii=False),
            "checkkey": checkkey
        }
        ins_resp = self.session.post(url_insert, data=ins_payload, timeout=10)
        ins_resp.raise_for_status()
        ins_json = ins_resp.json()
        if not ins_json.get("result"):
            raise KsaError(ins_json.get("message") or "승객 정보 저장 실패")

        # 3-1. booking_payment 페이지 세션 활성화 및 컨텍스트 바인딩
        page_resp = self.session.post(f"{BASE_URL}/page/booking", data={"pagename": "booking_payment", "checkkey": checkkey}, timeout=10)
        page_resp.raise_for_status()

        # 4. selectFareList (상세 운임 계산)
        crud_fare_list = []
        try:
            update_resp = self.session.post(f"{BASE_URL}/booking/selectFareUpdate", data={"checkkey": checkkey}, timeout=5)
            update_resp.raise_for_status()
            update_json = update_resp.json()
            if not update_json.get("result"):
                raise KsaError(update_json.get("message") or "저장된 여정의 운임 갱신 실패")
            fare_query_list = []
            for i, p in enumerate(formatted_passengers):
                fare_query_list.append({
                    "mode": 1,
                    "rf_portid1": check_item["rf_portid"],
                    "rt_portid1": check_item["rt_portid"],
                    "rsectionid1": check_item["rsectionid"],
                    "f_portid1": check_item["f_portid"],
                    "f_portsubid1": check_item["f_portsubid"],
                    "t_portid1": check_item["t_portid"],
                    "t_portsubid1": check_item["t_portsubid"],
                    "vesselid1": check_item["vesselid"],
                    "mastertime1": check_item["mastertime"],
                    "farekind1": selected_room.get("farekind", "1"),
                    "ticketid1": p["ticketID1"],
                    "classesid1": selected_room.get("classesid"),
                    "dcid1": p["dcID1"],
                    "promotionid1": p.get("promotionid", ""),
                    "dcprice1": 0,
                    "dcpercent1": 0,
                    "isadd1": "",
                    "isvat1": "",
                    "rf_portid2": "",
                    "rt_portid2": "",
                    "rsectionid2": "",
                    "f_portid2": "",
                    "f_portsubid2": "",
                    "t_portid2": "",
                    "t_portsubid2": "",
                    "vesselid2": "",
                    "mastertime2": "",
                    "farekind2": "",
                    "ticketid2": "",
                    "classesid2": "",
                    "dcid2": "",
                    "promotionid2": "",
                    "dcprice2": 0,
                    "dcpercent2": 0,
                    "isadd2": "",
                    "isvat2": "",
                    "isdriver": "",
                    "cargocnt": p.get("cargoCnt", "0"),
                    "manindex": i,
                    "couponselectyn": "N",
                    "flag": "Internet",
                    "agencyid": "",
                    "mancheck": p.get("manCheck", "0"),
                    "badakind": "",
                    "companyid": selected_room.get("salecompanyid") or selected_room.get("companyid", ""),
                    "iscommander": "0",
                    "iscommander2": "0"
                })

            url_fare_list = f"{BASE_URL}/booking/selectFareList"
            fare_resp = self.session.post(url_fare_list, data={"jsonstring": json.dumps(fare_query_list, ensure_ascii=False)}, timeout=10)
            fare_resp.raise_for_status()
            fare_json = fare_resp.json()
            if not fare_json.get("result"):
                raise KsaError(fare_json.get("message") or "상세 운임 조회 실패")
            fare_data = fare_json.get("data", {}) or {}
            crud_fare_list = fare_data.get("resultData") or fare_data.get("result") or []
            if not crud_fare_list or len(crud_fare_list) != len(formatted_passengers):
                raise KsaError("승객 수와 상세 운임 수가 일치하지 않습니다.")
            _log("INFO", f"상세 운임 계산 완료 (수신: {len(crud_fare_list)}건)")
        except KsaError:
            raise
        except Exception as e:
            raise KsaError("상세 운임 계산 요청 실패") from e

        # 5. ticketCompleteParamsCheck
        has_card = bool(card_info and card_info.get("cardno"))
        pay_type = "2" if has_card else "1"
        is_presale = "0" if pay_type == "1" else "1"
        # 카드가 없으면 사이트의 가상계좌 예약(payType 1) 흐름으로 끝까지 진행합니다.
        company_ids = str(selected_room.get("vesselid", ""))[:4]
        va_bank = None if has_card else self._select_virtual_bank(
            company_ids, selected_room.get("mastertime"), _log)

        params_list = []
        total_fare = 0
        for i, p in enumerate(formatted_passengers):
            f_item = crud_fare_list[i]
            try:
                f_fare = int(f_item["fare1"])
                f_bond = int(f_item["bond1"])
            except (KeyError, TypeError, ValueError) as e:
                raise KsaError("상세 운임 또는 지원금이 올바르지 않습니다.") from e
            if f_fare < 0 or f_bond < 0 or f_bond > f_fare:
                raise KsaError("상세 운임 또는 지원금이 올바르지 않습니다.")
            # payment.fn_setFare의 oneWayAmount: 터미널 이용료는 fare에 포함됩니다.
            total_fare += f_fare - f_bond

            params_list.append({
                "mode": "1",
                "rf_portid": check_item["rf_portid"],
                "rt_portid": check_item["rt_portid"],
                "rsectionid": check_item["rsectionid"],
                "f_portid": check_item["f_portid"],
                "f_portsubid": check_item["f_portsubid"],
                "t_portid": check_item["t_portid"],
                "t_portsubid": check_item["t_portsubid"],
                "mastertime": check_item["mastertime"],
                "departuretime": check_item["departuretime"],
                "vesselid": check_item["vesselid"],
                "classesid": selected_room.get("classesid"),
                "farekind": str(f_item.get("farekind1", selected_room.get("farekind", "1"))),
                "groupid": "0",
                "groupid2": "0",
                "rgroupid": "0",
                "rgroupid2": "0",
                "userid": "Internet",
                "companyid": selected_room.get("salecompanyid") or selected_room.get("companyid", ""),
                "issale": "0",
                "salekind": "2",
                "approvekind": pay_type,
                "isbatch": "0",
                "ticketid": str(f_item.get("newticketid1") or p["ticketID1"]),
                "dcid": str(f_item.get("newdcid1") or p["dcID1"]),
                "seatno": 0,
                "printseatno": "",
                "fare": f_item.get("fare1", selected_room.get("fare", 0)),
                "dcpercent": str(f_item.get("newdcpercent1", "0")),
                "dcprice": str(f_item.get("newdcprice1", "0")),
                "bond": str(f_item.get("bond1", "0")),
                "terminalfare": str(f_item.get("terminalfare1", "0")),
                "f_passfee": str(f_item.get("f_passfee1", "0")),
                "t_passfee": str(f_item.get("t_passfee1", "0")),
                "addfare": str(f_item.get("addfare1", "0")),
                "consignfee": str(f_item.get("consignfee1", "0")),
                "vat": str(f_item.get("vat1", "0")),
                "mname": formatted_passengers[0]["name"],
                "midnumber": formatted_passengers[0]["idnumber"],
                "mtel": formatted_passengers[0]["tel"],
                "sname": p["name"],
                "sidnumber": p["idnumber"],
                "tel1": p["tel"],
                "tel2": p.get("emtel", ""),
                "note": "",
                "agencyid": "0000",
                "cargocode": "",
                "cnumber": "",
                "cidname": "",
                "ctel": "",
                "cfare": "0",
                "cshipping": "0",
                "cdischarge": "0",
                "memberid": self.user_id or "Default",
                "ispresale": is_presale,
                "engname": "",
                "countrycode": "",
                "isforeigner": p.get("isforeigner", "0"),
                "identityno": "",
                "passportno": "",
                "sex": p["sex"],
                "passengertel": "",
                "address": p.get("address", ""),
                "point": "0",
                "nbond": str(f_item.get("nbond1", "0")),
                "eventcontents": _event_contents("1", (p.get("eventcontents") or [None])[0]
                                                 if isinstance(p.get("eventcontents"), list) else p.get("eventcontents")),
                "logtime": "",
                "searchdataseq": i,
                "sinfo": "",
                "checkkey": checkkey,
                "ismobile": "0",
                "toregist": "false"
            })

        # 카드도 가상계좌도 쓸 수 없으면 좌석을 잡지 않고 빈자리 발견만 알립니다.
        # (선점하면 약 20분간 본인도 그 좌석을 직접 예매할 수 없음)
        if not has_card and not va_bank:
            _log("INFO", "가상계좌를 쓸 수 없는 운항편이라 좌석을 잡지 않고 빈자리 알림만 보냅니다.")
            return {
                "success": True,
                "status": "SEAT_AVAILABLE",
                "vessel": selected_room.get("vessel"),
                "departure_time": selected_room.get("departuretime"),
                "classes": selected_room.get("classes"),
                "remaining": int(float(selected_room.get("onlinecnt") or 0)),
                "fare": total_fare,
                "passengers": [p["name"] for p in formatted_passengers],
            }

        # 4-1. selectIsCompleteCheck (결제 진행 확인)
        try:
            self.session.post(f"{BASE_URL}/booking/selectIsCompleteCheck", data={"CheckKey": checkkey}, timeout=5)
        except Exception:
            pass

        url_params_chk = f"{BASE_URL}/booking/ticketCompleteParamsCheck"
        chk_p_resp = self.session.post(url_params_chk, data={
            "jsonstring": json.dumps(params_list, ensure_ascii=False),
            "checkkey": checkkey
        }, timeout=10)
        chk_p_json = chk_p_resp.json()
        chk_res_list = (chk_p_json.get("data") or {}).get("result") or []
        if not chk_res_list or not all(_ok(r.get("errcode")) for r in chk_res_list):
            err_msg = chk_p_json.get("message") or "티켓 파라미터 검증 실패"
            raise KsaError(err_msg)

        _log("INFO", f"티켓 파라미터 검증 완료 (수신: {len(chk_res_list)}건)")

        ticketingkey = chk_res_list[0].get("ticketingkey")
        _log("INFO", f"티켓 키 발급 성공 (ticketingkey: {ticketingkey}). 좌석을 배정합니다...")

        # 5. checkCapacitySeat (좌석 배정 및 락)
        url_cap = f"{BASE_URL}/booking/checkCapacitySeat"
        try:
            cap_json = self.session.post(url_cap, data={"TicketingKey": ticketingkey}, timeout=10).json()
        except Exception as e:
            self._rollback_seat(ticketingkey, has_card)
            raise KsaError(f"좌석 배정 응답 오류: {e}") from e
        _log("INFO", f"좌석 배정 응답: result={cap_json.get('result')}, errCode={cap_json.get('errCode')}")

        seats = (cap_json.get("data") or {}).get("result") or []

        def _seat_ok(seat) -> bool:
            try:
                return int(seat.get("seatno")) >= 0 and bool(str(seat.get("printseatno") or ""))
            except (TypeError, ValueError, AttributeError):
                return False

        if (not cap_json.get("result", False) or not _ok(cap_json.get("errCode"))
                or len(seats) != len(params_list) or not all(_seat_ok(x) for x in seats)):
            err_msg = cap_json.get("message") or f"좌석 할당 실패 (배정 {len(seats)}석 / 필요 {len(params_list)}석)"
            self._rollback_seat(ticketingkey, has_card)
            raise KsaError(err_msg)

        for i, p in enumerate(params_list):
            if i < len(seats):
                p["seatno"] = seats[i].get("seatno", 0)
                p["printseatno"] = seats[i].get("printseatno", "")

        assigned_seat_names = [s.get("printseatno", "") for s in seats if s.get("printseatno")]
        seat_desc = ", ".join(assigned_seat_names) if assigned_seat_names else "배정 완료"
        _log("SUCCESS", f"🎉 좌석 배정 완료! 좌석: {seat_desc} (총 {total_fare:,}원)")

        room_info = {
            "vessel": selected_room.get("vessel"),
            "departure_time": selected_room.get("departuretime"),
            "classes": selected_room.get("classes"),
            "seats": seat_desc,
            "fare": total_fare,
            "passengers": [p["name"] for p in formatted_passengers],
        }
        if va_bank:
            return self._complete_virtual(params_list, ticketingkey, total_fare, company_ids,
                                          formatted_passengers[0], va_bank, room_info, _log)

        # 6. cardapprove (카드 승인)
        _log("INFO", "카드 자동 결제를 요청합니다...")
        card_no = card_info["cardno"].replace("-", "").strip()
        valid_raw = card_info["validdate"].replace("/", "").replace("-", "").strip()
        # MMYY(예: 0928)로 입력된 유효기간을 KSA 카드 승인 규격인 YYMM(2809)으로 변환
        if len(valid_raw) == 4 and valid_raw.isdigit():
            p_month = int(valid_raw[:2])
            if 1 <= p_month <= 12:
                valid_date = f"{valid_raw[2:]}{valid_raw[:2]}"
            else:
                valid_date = valid_raw
        else:
            valid_date = valid_raw
        card_pw = card_info["password"].strip()[:2]  # 앞 2자리
        req_no = card_info["requestno"].replace("-", "").strip()  # 생년월일 6자리

        approve_params = {
            "cardno": card_no,
            "validdate": valid_date,
            "password": card_pw,
            "requestno": req_no,
            "installment": card_info.get("installment", "00"),
            "amount": total_fare,
            "cardgroupid1": "0",
            "regmycard": "0",
            "ticketingkey": ticketingkey,
            "accesskey": checkkey,
            "cardenc": "",
            "modeorder": "1"
        }

        url_approve = f"{BASE_URL}/booking/cardapprove"
        _log("INFO", f"cardapprove 요청 (금액: {total_fare}원)")
        try:
            app_json = self.session.post(url_approve, data=approve_params, timeout=15).json()
        except Exception as e:
            raise KsaPaymentStateError(f"카드 승인 결과를 확인할 수 없습니다 ({e}). 결제되었을 수 있습니다.") from e
        app_data = app_json.get("data") or {}
        _log("INFO", f"카드 승인 응답: result={app_json.get('result')}, errCode={app_data.get('errCode', app_json.get('errCode'))}")

        err_code = app_data.get("errCode", app_json.get("errCode"))
        if app_json.get("result", False) and err_code is None:
            # 성공 여부를 판정할 코드가 없으면 승인됐을 수 있으므로 롤백·재시도하지 않습니다.
            raise KsaPaymentStateError("카드 승인 응답에 결과 코드가 없어 승인 여부를 알 수 없습니다.")
        if not app_json.get("result", False) or not _ok(err_code):
            err_msg = app_data.get("errMsg") or app_json.get("message") or "응답 없음"
            # 카드 실패 시 롤백 요청
            self._rollback("CapacitySeat", ticketingkey, "0", "presale")
            raise KsaCardDeclinedError(f"카드 결제 승인 실패: {err_msg}")

        card_group_id = str(app_json.get("data", {}).get("cardgroupid", "0"))
        log_time = str(app_json.get("data", {}).get("logtime", ""))
        _log("SUCCESS", f"카드 결제 승인 완료! (승인번호/그룹ID: {card_group_id})")

        # 7. ticketComplete (최종 발권)
        # Params에 groupid 및 좌석번호 반영
        for i, p in enumerate(params_list):
            p["groupid"] = card_group_id
            p["groupid2"] = card_group_id
            p["logtime"] = log_time
            p["ticketingkey"] = ticketingkey
            if i < len(seats):
                p["seatno"] = seats[i].get("seatno")
                p["printseatno"] = seats[i].get("printseatno")

        # 카드 승인 이후 실패는 이미 결제된 상태이므로 재시도 금지
        url_complete = f"{BASE_URL}/booking/ticketComplete"
        try:
            comp_json = self.session.post(url_complete, data={
                "jsonstring": json.dumps(params_list, ensure_ascii=False),
                "fcmtoken": ""
            }, timeout=15).json()
        except Exception as e:
            raise KsaPaymentStateError(f"카드 결제 후 발권 결과를 확인할 수 없습니다 ({e}).") from e

        outcome = _ticket_outcome(comp_json)
        if outcome == "unknown":
            raise KsaPaymentStateError("카드 결제 후 발권 응답에 결과가 없어 발권 여부를 알 수 없습니다.")
        if outcome == "failed":
            err_msg = comp_json.get("message") or "최종 발권에 실패했습니다."
            # 사이트(fn_cardRecordCancel)와 같이 결제를 자동 취소한 뒤 좌석·기록을 되돌립니다.
            if self._cancel_card(log_time, card_group_id, total_fare):
                self._rollback("CardRecord|Ticket|CapacitySeat", ticketingkey, card_group_id, "presale")
                raise KsaPaymentStateError(f"발권에 실패해 카드 결제를 자동 취소했습니다 ({err_msg}). 카드 취소 내역을 확인하세요.")
            raise KsaPaymentStateError(
                f"카드 결제는 승인됐지만 발권에 실패했고 자동 취소도 실패했습니다 ({err_msg}). KSA 고객센터에 취소를 요청하세요.")

        _log("SUCCESS", f"🎊 축하합니다! 최종 여객선 예매 및 발권이 완료되었습니다! (티켓키: {ticketingkey})")

        return {
            "success": True,
            "status": "COMPLETED",
            "ticketingkey": ticketingkey,
            "cardgroupid": card_group_id,
            "vessel": selected_room.get("vessel"),
            "departure_time": selected_room.get("departuretime"),
            "classes": selected_room.get("classes"),
            "seats": seat_desc,
            "fare": total_fare,
            "passengers": [p["name"] for p in formatted_passengers]
        }

    def _rollback(self, method: str, ticketingkey: str, group_id: str, approvekind: str):
        """payment.js fn_ticketRollBack 규격으로 좌석/티켓 점유를 되돌립니다 (best effort)."""
        try:
            self.session.post(f"{BASE_URL}/booking/ticketRollBack", data={
                "Method": method,
                "TicketingKey": ticketingkey,
                "GroupID": group_id,
                "ApproveKind": approvekind,
            }, timeout=5)
        except Exception:
            pass

    def _virtual_info(self, company_ids: str, mastertime: str) -> Dict[str, Any]:
        """payment/selectvirtualinfo 조회 (좌석을 잡지 않는 조회 전용 호출)."""
        resp = self.session.post(f"{BASE_URL}/payment/selectvirtualinfo", data={
            "companyidlist": company_ids,
            "mastertime": mastertime,
        }, timeout=10)
        return resp.json().get("data") or {}

    @staticmethod
    def _virtual_supported(data: Dict[str, Any]) -> bool:
        return str(data.get("errCode")) == "0" and bool(data.get("banks"))

    def unsupported_virtual_sailings(self, f_port: Dict[str, Any], t_port: Dict[str, Any],
                                     date_str: str, time_start: str, time_end: str) -> Tuple[List[str], int]:
        """조회 시간 범위 운항편 중 가상계좌 미지원 운항편 목록과 전체 운항편 수를 반환합니다.

        선사(vesselid 앞 4자리)별로 한 번만 조회합니다. 조회 실패 시 KsaError.
        """
        schedules, _ = self.search_departures(f_port["portid"], f_port.get("portsubid", "0"),
                                              t_port["t_portid"], t_port.get("t_portsubid", "0"), date_str)
        supported: Dict[str, bool] = {}
        unsupported, total = [], 0
        for s in schedules:
            dep = s.get("departuretime", "")
            time_part = dep.split(" ")[-1] if " " in dep else dep
            if not (time_start <= time_part <= time_end):
                continue
            total += 1
            company = str(s.get("vesselid", ""))[:4]
            if company not in supported:
                try:
                    supported[company] = self._virtual_supported(self._virtual_info(company, s.get("mastertime")))
                except Exception as e:
                    raise KsaError(f"가상계좌 지원 여부 조회 실패: {e}") from e
            if not supported[company]:
                unsupported.append(f"{s.get('vessel', '여객선')} {time_part}")
        return unsupported, total

    def _rollback_seat(self, ticketingkey: str, has_card: bool):
        """좌석 배정 실패 롤백. 사이트는 카드 경로에서 'CapacitySeat-CapacitySeat'(2회), 가상계좌는 1회 호출합니다."""
        self._rollback("CapacitySeat", ticketingkey, "0", "presale" if has_card else "reserve")
        if has_card:
            self._rollback("CapacitySeat", ticketingkey, "0", "presale")

    def _cancel_card(self, usedate: str, cardgroupid: str, amount: int) -> bool:
        """payment/cardcancel (사이트 fn_cardRecordCancel). 취소가 확인되면 True."""
        if not usedate or not cardgroupid or str(cardgroupid) == "0" or amount <= 0:
            return False
        try:
            res = self.session.post(f"{BASE_URL}/payment/cardcancel", data={
                "usedate": usedate, "cardgroupid": cardgroupid, "cancelamount": amount}, timeout=15).json()
        except Exception:
            return False
        return bool(res.get("result")) and _ok((res.get("data") or {}).get("errCode"))

    def _select_virtual_bank(self, company_ids: str, mastertime: str, log) -> Optional[Dict[str, Any]]:
        """가상계좌 발급 은행을 고릅니다. 선사가 가상계좌를 지원하지 않으면 None.

        가상계좌는 어느 은행에서든 이체할 수 있어 선사 목록의 첫 은행을 씁니다.
        """
        try:
            data = self._virtual_info(company_ids, mastertime)
        except Exception as e:
            # 일시적 오류를 "미지원 선사"로 오판하면 빈자리 알림만 보내고 멈추므로, 좌석을 잡기 전에 실패시켜 재시도합니다.
            raise KsaError(f"가상계좌 정보 조회 실패: {e}") from e
        banks = data.get("banks") or []
        if not self._virtual_supported(data):
            log("WARNING", f"가상계좌 예약을 쓸 수 없는 운항편입니다. ({data.get('errMsg', '')})")
            return None
        return {**banks[0], "expiredatetime": data.get("expiredatetime", "")}

    def _complete_virtual(self, params_list, ticketingkey, total_fare, company_ids,
                          representative, va_bank, room_info, log) -> Dict[str, Any]:
        """payment.js fn_ticketComplete_Virtual: vaapprove(계좌 발급) -> ticketComplete(예약 확정)."""
        log("INFO", f"가상계좌 발급을 요청합니다... (입금은행: {va_bank.get('bankname')})")
        try:
            va_json = self.session.post(f"{BASE_URL}/booking/vaapprove", data={
                "amount": total_fare,
                "user_nm": representative["name"],
                "user_phone1": representative["tel"],
                "user_mail": "",
                "bank_cd": va_bank.get("bankcode"),
                "companyidlist": company_ids,
                "ticketingkey": ticketingkey,
            }, timeout=15).json()
        except Exception as e:
            raise KsaPaymentStateError(f"가상계좌 발급 결과를 확인할 수 없습니다 ({e}).") from e
        va_data = va_json.get("data") or {}
        if not va_json.get("result", False) or str(va_data.get("errCode")) != "0":
            err_msg = va_data.get("errMsg") or va_json.get("message") or "응답 없음"
            # result가 true인데 errCode가 실패면 사이트는 응답의 rgroupid로 롤백합니다.
            group = str(va_data.get("rgroupid") or "0") if va_json.get("result", False) else "0"
            self._rollback("CapacitySeat", ticketingkey, group, "reserve")
            raise KsaError(f"가상계좌 발급 실패: {err_msg}")

        rgroupid = str(va_data.get("rgroupid", "0"))
        for p in params_list:
            p.update(rgroupid=rgroupid, rgroupid2=rgroupid,
                     logtime=str(va_data.get("logtime", "")), ticketingkey=ticketingkey)

        try:
            comp_json = self.session.post(f"{BASE_URL}/booking/ticketComplete", data={
                "jsonstring": json.dumps(params_list, ensure_ascii=False),
                "fcmtoken": ""
            }, timeout=15).json()
        except Exception as e:
            # 서버에서 예약이 확정됐을 수도 있으므로 취소하지 않고 재시도도 막습니다.
            raise KsaPaymentStateError(f"가상계좌 발급 후 예약 확정 결과를 확인할 수 없습니다 ({e}).") from e
        outcome = _ticket_outcome(comp_json)
        if outcome == "unknown":
            raise KsaPaymentStateError("가상계좌 발급 후 예약 확정 응답에 결과가 없어 예약 여부를 알 수 없습니다.")
        if outcome == "failed":
            err_msg = comp_json.get("message") or "예약 확정(ticketComplete)에 실패했습니다."
            # 사이트(fn_virtualCancel)와 같이 가상계좌 취소가 확인된 경우에만 좌석을 되돌리고 재시도를 허용합니다.
            cancelled = False
            if rgroupid not in ("", "0"):
                try:
                    res = self.session.post(f"{BASE_URL}/payment/vacancel", data={"groupid": rgroupid}, timeout=10).json()
                    cancelled = bool(res.get("result")) and _ok((res.get("data") or {}).get("errCode"))
                except Exception:
                    cancelled = False
            if not cancelled:
                raise KsaPaymentStateError(
                    f"예약 확정에 실패했고 발급된 가상계좌를 취소하지 못했습니다 ({err_msg}). 입금하지 말고 KSA 예매내역을 확인하세요.")
            self._rollback("CashRecord2|Reserve|CapacitySeat", ticketingkey, rgroupid, "reserve")
            raise KsaError(err_msg)

        rc = va_data.get("rctelegram") or {}
        sd = va_data.get("sdtelegram") or {}
        result = {
            "success": True,
            "status": "VA_RESERVED",
            "ticketingkey": ticketingkey,
            "rgroupid": rgroupid,
            **room_info,
            "va_bank": rc.get("r_bank_nm") or va_bank.get("bankname"),
            "va_account": rc.get("r_account_no", ""),
            "va_amount": _to_won(rc.get("r_amount"), total_fare),
            "va_expire": sd.get("expiredatetime") or va_bank.get("expiredatetime", ""),
        }
        log("SUCCESS", f"🎊 가상계좌 예약 완료! {result['va_bank']} {result['va_account']} "
                       f"/ {result['va_amount']}원 / 입금기한 {result['va_expire']}")
        return result

    # ----------------------------------------------------------------------
    # 5. 예매 내역 조회
    # ----------------------------------------------------------------------
    def get_reservations(self, group_id: str = "") -> List[Dict[str, Any]]:
        """회원의 예매 목록을 조회합니다."""
        url = f"{BASE_URL}/payment_confirm/selectPaymentGroupPaging"
        headers = {
            "Referer": f"{BASE_URL}/page/payment_confirm",
            "X-Requested-With": "XMLHttpRequest"
        }
        params: Dict[str, Any] = {
            "DateFrom": "",
            "DateTo": "",
            "PayStatus": "A",
            "Page": 1,
            "Tel": "",
            "PresalePassword": "",
            "Name": "",
            "GroupID": group_id,
            "MasterDate": ""
        }
        # 실패를 빈 목록과 구분해야 결과 불명 예매의 사후 확인이 거짓 판정을 내리지 않습니다.
        try:
            resp = self.session.post(url, data={"SearchData": json.dumps(params)}, headers=headers, timeout=10)
            resp.raise_for_status()
            return (resp.json().get("data") or {}).get("result") or []
        except Exception as e:
            raise KsaError(f"예매 내역 조회 실패: {e}") from e
