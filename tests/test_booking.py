"""Offline booking checks: no credentials, network, or real payments."""

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engines import KsaEngine, KsaError
from diagnose_booking import DiagnosticSession, PreviewReady


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
    def run_booking(self, overrides=None, count=1):
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
            "ticketComplete": {"result": True},
            "ticketRollBack": {"result": True},
        }
        replies.update(overrides or {})

        def post(url, data, timeout):
            endpoint = url.rsplit("/", 1)[-1]
            self.calls.append((endpoint, copy.deepcopy(data)))
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps(replies[endpoint]).encode()
            return response

        with patch.object(engine.session, "post", side_effect=post), patch(
            "requests.sessions.Session.send", side_effect=AssertionError("Network forbidden")
        ):
            return engine.book(self.original, ROOM, [dict(PASSENGER) for _ in range(count)],
                               CARD, lambda level, message: self.logs.append(message))

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

    def test_diagnostic_transport_blocks_financial_and_seat_requests(self):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
