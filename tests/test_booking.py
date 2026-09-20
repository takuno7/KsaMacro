"""Offline booking checks: no credentials, network, or real payments."""

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engines import KsaCardDeclinedError, KsaEngine, KsaError, KsaPaymentStateError


ROOM = {
    "rf_portid": "3017", "rt_portid": "3043", "rsectionid": "0",
    "f_portid": "3017", "f_portsubid": "0", "f_port": "말도",
    "t_portid": "3043", "t_portsubid": "0", "t_port": "장자도",
    "vesselid": "TEST", "vessel": "테스트선", "salecompanyid": "TEST",
    "mastertime": "2026-09-22 10:00", "departuretime": "2026-09-22 10:00",
    "classesid": "1", "masterclassesid": "0", "classes": "일반",
    "farekind": "1", "fare": 7700, "usemap": "0",
}
PASSENGER = {"name": "테스트", "idnumber": "19900101", "tel": "01000000000"}
CARD = {"cardno": "0000000000000000", "validdate": "2912",
        "password": "00", "requestno": "900101"}
FARE = {"fare1": 7700, "bond1": 3900, "terminalfare1": 700,
        "farekind1": "1", "newticketid1": "1", "newdcid1": "1F"}


class BookingTests(unittest.TestCase):
    def run_booking(self, overrides=None, count=1, card=CARD):
        self.calls = []
        self.original = {"journeylen": 1, "masterdate": "2026-09-22"}
        self.logs = []
        engine = KsaEngine()
        engine.user_id = "test-member"
        engine.member_seq = "1"
        replies = {
            "selectCheckJourney": {"result": True, "data": {"errcode": 0}},
            "selectDepartureTicketDC": {"result": True, "data": {
                "tdResult": [{"ticketid": "1", "dcid": "1F"}]}},
            "checkPassengerValidation": {"result": True, "data": {
                "failed": False, "checkkey": "check-test",
                "result": [{"result": "TRUE"}], "checked": []}},
            "checkInsert": {"result": True},
            "booking": {"result": True},
            "selectFareUpdate": {"result": True},
            "selectFareList": {"result": True, "data": {
                "result": [dict(FARE) for _ in range(count)]}},
            "selectIsCompleteCheck": {"result": True},
            "ticketCompleteParamsCheck": {"result": True, "data": {
                "result": [{"errcode": 0, "ticketingkey": "ticket-test"}]}},
            "checkCapacitySeat": {"result": True, "errCode": 0, "data": {
                "result": [{"seatno": i, "printseatno": str(i), "ticketno": i}
                           for i in range(count)]}},
            "cardapprove": {"result": True, "data": {
                "errCode": 0, "cardgroupid": "group-test", "logtime": "time-test"}},
            "ticketComplete": {"result": True, "data": {"result": [{"errcode": 0}]}},
            "ticketRollBack": {"result": True},
            "selectvirtualinfo": {"result": True, "data": {
                "errCode": 0, "banks": [{"bankname": "테스트은행", "bankcode": "088"}]}},
            "vaapprove": {"result": True, "data": {
                "errCode": 0, "rgroupid": 77, "logtime": "va-time",
                "rctelegram": {"r_bank_nm": "테스트은행", "r_account_no": "123-456", "r_amount": "3800"},
                "sdtelegram": {"expiredatetime": "2026-09-21 23:59"}}},
            "vacancel": {"result": True, "data": {"errCode": 0}},
            "cardcancel": {"result": True, "data": {"errCode": 0}},
        }
        replies.update(overrides or {})

        def post(url, data, timeout):
            endpoint = url.rsplit("/", 1)[-1]
            self.calls.append((endpoint, copy.deepcopy(data)))
            if isinstance(replies[endpoint], Exception):
                raise replies[endpoint]
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(replies[endpoint]).encode()
            return response

        with patch.object(engine.session, "post", side_effect=post), patch(
            "requests.sessions.Session.send", side_effect=AssertionError("Network forbidden")
        ):
            return engine.book(self.original, ROOM, [dict(PASSENGER) for _ in range(count)],
                               card, lambda level, message: self.logs.append(message))

    def test_saved_original_preserves_payment_context(self):
        self.run_booking()
        payload = dict(self.calls)["checkInsert"]
        original = json.loads(payload["original"])
        self.assertEqual(original["checkkey"], "check-test")
        self.assertEqual(original["journeyList"][0]["rf_portid"], "3017")
        self.assertEqual(original["journeyList"][0]["farekind"], "1")
        self.assertEqual(original["journeyList"][0]["salecompanyid"], "TEST")
        self.assertEqual(original["passengerList"], json.loads(payload["passengerlist"]))
        self.assertEqual(original["cargoList"], [])
        self.assertNotIn("checkkey", self.original)  # caller's polling input is reusable

    def test_approval_amount_does_not_add_terminal_fee_twice(self):
        result = self.run_booking(count=2)
        approvals = [data for name, data in self.calls if name == "cardapprove"]
        self.assertEqual([data["amount"] for data in approvals], [7600])
        self.assertEqual(result["fare"], 7600)

    def test_null_decline_is_not_retried_and_releases_seats(self):
        with self.assertRaisesRegex(KsaError, "결제금액오류"):
            self.run_booking({"cardapprove": {
                "result": False, "data": None, "message": "결제금액오류", "errCode": -200}})
        self.assertEqual([name for name, _ in self.calls].count("cardapprove"), 1)
        self.assertEqual(self.calls[-1][0], "ticketRollBack")
        self.assertNotIn("ticketComplete", dict(self.calls))

    def test_incomplete_fares_stop_before_seat_allocation(self):
        for result in ([], [dict(FARE)]):
            with self.subTest(result=result), self.assertRaises(KsaError):
                self.run_booking({"selectFareList": {"result": True, "data": {
                    "result": result}}}, count=2)
            self.assertNotIn("checkCapacitySeat", dict(self.calls))

    def test_failed_context_or_fare_update_stops(self):
        for endpoint in ("checkInsert", "selectFareUpdate", "selectFareList"):
            with self.subTest(endpoint=endpoint), self.assertRaises(KsaError):
                self.run_booking({endpoint: {"result": False, "data": None,
                                             "message": "서버 검증 실패"}})
            self.assertNotIn("checkCapacitySeat", dict(self.calls))

    def test_rejected_passenger_stops(self):
        with self.assertRaisesRegex(KsaError, "승객 검증 실패"):
            self.run_booking({"checkPassengerValidation": {"result": True, "data": {
                "checkkey": "check-test", "failed": False,
                "result": [{"result": "FALSE", "resultmessage": "승객 검증 실패"}]}}})
        self.assertNotIn("checkInsert", dict(self.calls))

    def test_invalid_fares_stop_before_seat_allocation(self):
        for fare, bond in ((None, 0), ("bad", 0), (-1, 0), (100, -1), (100, 101)):
            with self.subTest(fare=fare, bond=bond), self.assertRaises(KsaError):
                self.run_booking({"selectFareList": {"result": True, "data": {
                    "result": [{**FARE, "fare1": fare, "bond1": bond}]}}})
            self.assertNotIn("checkCapacitySeat", dict(self.calls))

    def test_no_card_completes_virtual_account_reservation(self):
        result = self.run_booking(card=None)
        names = [name for name, _ in self.calls]
        self.assertNotIn("cardapprove", names)
        self.assertLess(names.index("selectvirtualinfo"), names.index("checkCapacitySeat"))
        self.assertEqual(names[-2:], ["vaapprove", "ticketComplete"])
        va = dict(self.calls)["vaapprove"]
        self.assertEqual((va["amount"], va["bank_cd"], va["companyidlist"], va["ticketingkey"]),
                         (3800, "088", "TEST", "ticket-test"))
        ticket = json.loads(dict(self.calls)["ticketComplete"]["jsonstring"])[0]
        self.assertEqual((ticket["approvekind"], ticket["ispresale"], ticket["rgroupid"], ticket["groupid"]),
                         ("1", "0", "77", "0"))
        self.assertEqual((result["status"], result["va_account"], result["va_expire"]),
                         ("VA_RESERVED", "123-456", "2026-09-21 23:59"))

    def test_unsupported_virtual_sailings_lists_only_unsupported_in_window(self):
        engine = KsaEngine()
        schedules = [
            {"vessel": "A호", "vesselid": "4322175806", "mastertime": "m1", "departuretime": "2026-09-30 12:20"},
            {"vessel": "A호", "vesselid": "4322175806", "mastertime": "m2", "departuretime": "2026-09-30 15:00"},
            {"vessel": "B호", "vesselid": "9603000001", "mastertime": "m3", "departuretime": "2026-09-30 13:00"},
            {"vessel": "C호", "vesselid": "1111000001", "mastertime": "m4", "departuretime": "2026-09-30 22:00"},
        ]
        asked = []

        def info(company, mastertime):
            asked.append(company)
            return {"errCode": 0, "banks": [{"bankcode": "004"}]} if company == "9603" else {"errCode": -2, "banks": []}

        with patch.object(engine, "search_departures", return_value=(schedules, [])),                 patch.object(engine, "_virtual_info", side_effect=info):
            unsupported, total = engine.unsupported_virtual_sailings(
                {"portid": "1"}, {"t_portid": "2"}, "2026-09-30", "12:00", "16:00")
        self.assertEqual((unsupported, total), (["A호 12:20", "A호 15:00"], 3))
        self.assertEqual(asked, ["4322", "9603"])  # 선사별 1회, 범위 밖(C호)은 조회 안 함

    def test_virtual_uses_first_bank_and_expiry_fallback(self):
        result = self.run_booking({"selectvirtualinfo": {"result": True, "data": {
            "errCode": 0, "expiredatetime": "2026-09-22 23:00:00",
            "banks": [{"bankname": "국민은행", "bankcode": "004"}, {"bankname": "농협중앙회", "bankcode": "011"}]}},
            "vaapprove": {"result": True, "data": {"errCode": 0, "rgroupid": 5, "logtime": "t"}}}, card=None)
        self.assertEqual(dict(self.calls)["vaapprove"]["bank_cd"], "004")
        self.assertEqual(result["va_expire"], "2026-09-22 23:00:00")

    def test_virtual_unsupported_only_reports_seat_without_locking(self):
        result = self.run_booking({"selectvirtualinfo": {"result": True, "data": {
            "errCode": -2, "errMsg": "미지원 선사", "banks": []}}}, card=None)
        self.assertEqual((result["status"], result["fare"]), ("SEAT_AVAILABLE", 3800))
        for endpoint in ("ticketCompleteParamsCheck", "checkCapacitySeat", "vaapprove", "ticketComplete"):
            self.assertNotIn(endpoint, dict(self.calls))

    def test_virtual_failures_release_seat_and_account(self):
        with self.assertRaisesRegex(KsaError, "가상계좌 발급 실패"):
            self.run_booking({"vaapprove": {"result": True, "data": {"errCode": -9, "errMsg": "채번 실패"}}}, card=None)
        self.assertEqual(self.calls[-1][0], "ticketRollBack")
        self.assertEqual(self.calls[-1][1]["Method"], "CapacitySeat")

        with self.assertRaises(KsaError):
            self.run_booking({"ticketComplete": {"result": True, "data": {"result": [{"errcode": -1}]}}}, card=None)
        self.assertEqual([name for name, _ in self.calls][-2:], ["vacancel", "ticketRollBack"])
        self.assertEqual(dict(self.calls)["vacancel"], {"groupid": "77"})

        with self.assertRaisesRegex(KsaPaymentStateError, "입금하지 말고"):
            self.run_booking({"ticketComplete": {"result": False},
                              "vacancel": {"result": True, "data": {"errCode": -3}}}, card=None)
        self.assertNotIn("ticketRollBack", dict(self.calls))

    def test_uncertain_payment_states_are_not_retryable(self):
        timeout = requests.Timeout("응답 지연")
        cases = [
            ({"cardapprove": timeout}, CARD),
            ({"ticketComplete": timeout}, CARD),
            ({"ticketComplete": {"result": False, "message": "발권 실패"}}, CARD),
            ({"vaapprove": timeout}, None),
            ({"ticketComplete": timeout}, None),
        ]
        for overrides, card in cases:
            with self.subTest(overrides=overrides, card=bool(card)), self.assertRaises(KsaPaymentStateError):
                self.run_booking(overrides, card=card)
            # 결과를 모르는 상태에서 가상계좌를 취소하지 않습니다.
            self.assertNotIn("vacancel", dict(self.calls))

    def test_virtual_success_accepts_string_errcode_and_formatted_amount(self):
        result = self.run_booking({
            "ticketComplete": {"result": True, "data": {"result": [{"errcode": "0"}]}},
            "vaapprove": {"result": True, "data": {
                "errCode": "0", "rgroupid": 77, "logtime": "t",
                "rctelegram": {"r_amount": "3,800"}, "sdtelegram": {}}},
        }, card=None)
        self.assertEqual((result["status"], result["va_amount"]), ("VA_RESERVED", 3800))
        self.assertNotIn("vacancel", dict(self.calls))

    def test_diagnostic_transport_blocks_financial_and_seat_requests(self):
        try:
            from diagnose_booking import DiagnosticSession
        except ImportError:
            self.skipTest("diagnose_booking 모듈이 저장소에 없음")
        with DiagnosticSession() as session, patch.object(requests.Session, "request") as send:
            for endpoint in ("cardapprove", "ticketComplete", "checkCapacitySeat", "unknown"):
                with self.subTest(endpoint=endpoint), self.assertRaises(RuntimeError):
                    session.post("https://island.theksa.co.kr/booking/" + endpoint, data={})
            with self.assertRaises(RuntimeError):
                session.get("https://example.com/page/booking")
            with self.assertRaises(PreviewReady), patch("builtins.print"):
                session.post("https://island.theksa.co.kr/booking/ticketCompleteParamsCheck",
                             data={"jsonstring": '[{"fare":7700,"bond":3900}]'})
            send.assert_not_called()

    def test_logs_do_not_include_card_details_or_raw_passenger_responses(self):
        self.run_booking({"ticketCompleteParamsCheck": {"result": True, "data": {
            "result": [{"errcode": 0, "ticketingkey": "ticket-test"}],
            "resultData": [{"sidnumber": "private-passenger-data"}]}}})
        logs = "\n".join(self.logs)
        for private in (CARD["cardno"], CARD["validdate"], CARD["requestno"],
                        "private-passenger-data"):
            self.assertNotIn(private, logs)

    def test_string_zero_card_approval_is_success(self):
        result = self.run_booking({"cardapprove": {"result": True, "data": {
            "errCode": "0", "cardgroupid": "g", "logtime": "t"}}})
        self.assertEqual(result["status"], "COMPLETED")
        self.assertNotIn("ticketRollBack", dict(self.calls))

    def test_card_decline_is_not_retryable(self):
        with self.assertRaises(KsaCardDeclinedError):
            self.run_booking({"cardapprove": {"result": False, "data": {"errCode": -5, "errMsg": "한도초과"}}})
        self.assertEqual(self.calls[-1][0], "ticketRollBack")

    def test_card_approval_without_code_is_uncertain(self):
        with self.assertRaises(KsaPaymentStateError):
            self.run_booking({"cardapprove": {"result": True, "data": {"cardgroupid": "g"}}})
        self.assertNotIn("ticketRollBack", dict(self.calls))

    def test_card_ticket_row_error_is_not_success(self):
        with self.assertRaises(KsaPaymentStateError):
            self.run_booking({"ticketComplete": {"result": True, "data": {"result": [{"errcode": -1}]}}})

    def test_card_ticket_failure_cancels_payment_then_releases(self):
        with self.assertRaisesRegex(KsaPaymentStateError, "자동 취소했습니다"):
            self.run_booking({"ticketComplete": {"result": False, "message": "발권 오류"}})
        names = [n for n, _ in self.calls]
        self.assertEqual(names[-2:], ["cardcancel", "ticketRollBack"])
        cancel = dict(self.calls)["cardcancel"]
        self.assertEqual((cancel["cardgroupid"], cancel["usedate"], cancel["cancelamount"]),
                         ("group-test", "time-test", 3800))
        self.assertEqual(self.calls[-1][1]["Method"], "CardRecord|Ticket|CapacitySeat")

    def test_card_cancel_failure_keeps_seat_and_says_so(self):
        with self.assertRaisesRegex(KsaPaymentStateError, "자동 취소도 실패"):
            self.run_booking({"ticketComplete": {"result": False},
                              "cardcancel": {"result": True, "data": {"errCode": -1}}})
        self.assertNotIn("ticketRollBack", dict(self.calls))

    def test_ticket_response_without_rows_is_unknown_not_success(self):
        for card in (CARD, None):
            with self.subTest(card=bool(card)), self.assertRaises(KsaPaymentStateError):
                self.run_booking({"ticketComplete": {"result": True}}, card=card)
            self.assertNotIn("cardcancel", dict(self.calls))
            self.assertNotIn("vacancel", dict(self.calls))

    def test_virtual_info_error_is_retryable_not_unsupported(self):
        with self.assertRaisesRegex(KsaError, "가상계좌 정보 조회 실패") as ctx:
            self.run_booking({"selectvirtualinfo": requests.Timeout("slow")}, card=None)
        self.assertNotIsInstance(ctx.exception, KsaPaymentStateError)
        self.assertNotIn("checkCapacitySeat", dict(self.calls))

    def test_invalid_seat_rows_are_rolled_back(self):
        with self.assertRaises(KsaError):
            self.run_booking({"checkCapacitySeat": {"result": True, "errCode": 0, "data": {
                "result": [{"seatno": -1, "printseatno": ""}]}}})
        self.assertEqual(self.calls[-1][0], "ticketRollBack")

    def test_partial_seat_allocation_is_rolled_back(self):
        with self.assertRaises(KsaError):
            self.run_booking({"checkCapacitySeat": {"result": True, "errCode": 0, "data": {
                "result": [{"seatno": 1, "printseatno": "1"}]}}}, count=2)
        self.assertEqual(self.calls[-1][0], "ticketRollBack")
        self.assertNotIn("cardapprove", dict(self.calls))


    def test_each_passenger_type_gets_its_own_discount_code(self):
        infant = {**PASSENGER, "ticketid": "5"}
        self.calls = []
        engine = KsaEngine()
        engine.user_id = "m"
        replies = {"selectCheckJourney": {"result": True, "data": {"errcode": 0}},
                   "selectDepartureTicketDC": {"result": True, "data": {"tdResult": [
                       {"ticketid": "1", "dcid": "1F"}, {"ticketid": "5", "dcid": "5F"}]}},
                   "checkPassengerValidation": {"result": False, "message": "stop"}}

        def post(url, data, timeout):
            endpoint = url.rsplit("/", 1)[-1]
            self.calls.append((endpoint, copy.deepcopy(data)))
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(replies[endpoint]).encode()
            return response

        with patch.object(engine.session, "post", side_effect=post), self.assertRaises(KsaError):
            engine.book(self.original if hasattr(self, "original") else {"journeylen": 1}, ROOM,
                        [dict(PASSENGER), infant], None, None)
        sent = json.dumps(dict(self.calls)["checkPassengerValidation"], ensure_ascii=False)
        self.assertIn("1F", sent)
        self.assertIn("5F", sent)


    def test_special_discount_event_is_passed_to_ticket_params(self):
        event = [{"eventkey": "military", "unit": "U1", "rank": "R", "name": "N", "idnumber": "20000101"}]
        self.run_booking({"checkPassengerValidation": {"result": True, "data": {
            "failed": False, "checkkey": "check-test", "result": [{"result": "TRUE"}],
            "checked": [{"vaindex": 0, "seqstring": json.dumps(event)}]}}})
        params = json.loads(dict(self.calls)["ticketCompleteParamsCheck"]["jsonstring"])
        self.assertEqual(params[0]["eventcontents"], "military-U1-R-N-20000101")
        inserted = json.loads(dict(self.calls)["checkInsert"]["passengerlist"])
        self.assertEqual(inserted[0]["eventcontents"], event)

    def test_card_seat_failure_rolls_back_twice_like_site(self):
        with self.assertRaises(KsaError):
            self.run_booking({"checkCapacitySeat": {"result": False, "message": "정원 초과"}})
        rollbacks = [d for n, d in self.calls if n == "ticketRollBack"]
        self.assertEqual(len(rollbacks), 2)
        with self.assertRaises(KsaError):
            self.run_booking({"checkCapacitySeat": {"result": False}}, card=None)
        self.assertEqual(len([n for n, _ in self.calls if n == "ticketRollBack"]), 1)

    def test_virtual_issue_error_rolls_back_with_issued_group(self):
        with self.assertRaises(KsaError):
            self.run_booking({"vaapprove": {"result": True, "data": {"errCode": -9, "rgroupid": 55}}}, card=None)
        self.assertEqual(self.calls[-1][1]["GroupID"], "55")


if __name__ == "__main__":
    unittest.main(verbosity=2)
