"""Worker must never book twice after a booking reached the server."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engines import KsaCardDeclinedError, KsaError, KsaPaymentStateError
from macro_worker import EngineWorker, format_result_message

ROOM = {"vesselid": "v", "mastertime": "m", "onlinecnt": 3, "ispossible": 1,
        "classes": "일반실", "vessel": "배", "departuretime": "10:00"}


class FakeEngine:
    def __init__(self, outcomes, reservations=None):
        self.outcomes = list(outcomes)
        self.book_calls = 0
        self.reservations = list(reservations or [])  # get_reservations 호출마다 하나씩 반환

    def get_reservations(self):
        item = self.reservations.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def login(self, *a):
        return {"member": "x"}

    def initial_listing(self, *a):
        return []

    unsupported = ([], 0)

    def unsupported_virtual_sailings(self, *a):
        if isinstance(self.unsupported, Exception):
            raise self.unsupported
        return self.unsupported

    def search_departures(self, *a):
        return [{"departuretime": "10:00", "vesselid": "v", "mastertime": "m"}], [ROOM]

    def book(self, **kwargs):
        self.book_calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def run_worker(outcomes, send=None, reservations=None):
    worker = EngineWorker({"ksa_id": "a", "ksa_pw": "b", "f_portid": "1", "t_portid": "2",
                           "date": "2030-01-01", "auto_pay": False, "refresh_interval": 0})
    worker.engine = FakeEngine(outcomes, reservations or [KsaError("no data")] * 2)
    sent = []
    worker.telegram.send_message = send or sent.append
    worker.run()
    return worker.engine.book_calls, sent


class WorkerTests(unittest.TestCase):
    def test_notification_failure_does_not_rebook(self):
        def broken_send(text):
            raise RuntimeError("telegram down")
        calls, _ = run_worker([{"status": "VA_RESERVED", "va_amount": 3800}, AssertionError("rebooked")],
                              send=broken_send)
        self.assertEqual(calls, 1)

    def test_uncertain_payment_stops_macro(self):
        calls, sent = run_worker([KsaPaymentStateError("결과 불명"), AssertionError("rebooked")])
        self.assertEqual(calls, 1)
        self.assertIn("매크로 중지", sent[0])

    def test_uncertain_payment_reports_new_reservation(self):
        _, sent = run_worker([KsaPaymentStateError("결과 불명")],
                             reservations=[[{"groupid": 1}], [{"groupid": 1}, {"groupid": 2}]])
        self.assertIn("새 항목 1건", sent[0])

        _, sent = run_worker([KsaPaymentStateError("결과 불명")],
                             reservations=[[{"groupid": 1}], [{"groupid": 1}]])
        self.assertIn("새 항목이 없습니다", sent[0])

        # 기준선 조회가 실패하면 "새 내역"으로 오판하지 않습니다.
        _, sent = run_worker([KsaPaymentStateError("결과 불명")],
                             reservations=[KsaError("down"), [{"groupid": 9}]])
        self.assertIn("자동 대조를 할 수 없습니다", sent[0])

    def test_seat_available_alert_stops_and_links_booking_page(self):
        calls, sent = run_worker([{"status": "SEAT_AVAILABLE", "vessel": "배", "remaining": 3, "fare": 3800},
                                  AssertionError("rebooked")])
        self.assertEqual(calls, 1)
        self.assertIn("빈자리 발견", sent[0])
        self.assertIn("/page/booking", sent[0])

    def test_start_warns_about_unsupported_virtual_sailings(self):
        FakeEngine.unsupported = (["고군산카훼리호 12:20"], 2)
        try:
            warnings = []
            worker = EngineWorker({"ksa_id": "a", "ksa_pw": "b", "f_portid": "1", "t_portid": "2",
                                   "date": "2030-01-01", "auto_pay": False, "refresh_interval": 0})
            worker.engine = FakeEngine([{"status": "COMPLETED", "fare": 1}], [KsaError("x")] * 2)
            sent = []
            worker.telegram.send_message = sent.append
            worker.warning_signal.connect(warnings.append)
            worker.run()
            self.assertIn("2편 중 1편", warnings[0])
            self.assertIn("가상계좌 미지원 선사 경고", sent[0])

            # 자동 결제가 켜져 있으면 확인하지 않습니다.
            worker = EngineWorker({"ksa_id": "a", "ksa_pw": "b", "f_portid": "1", "t_portid": "2",
                                   "date": "2030-01-01", "auto_pay": True, "card_info": {"cardno": "1"},
                                   "refresh_interval": 0})
            worker.engine = FakeEngine([{"status": "COMPLETED", "fare": 1}], [KsaError("x")] * 2)
            warnings.clear()
            worker.warning_signal.connect(warnings.append)
            worker.telegram.send_message = lambda t: None
            worker.run()
            self.assertEqual(warnings, [])
        finally:
            FakeEngine.unsupported = ([], 0)

    def test_plain_error_retries(self):
        calls, _ = run_worker([KsaError("좌석 없음"), {"status": "COMPLETED", "fare": 1}])
        self.assertEqual(calls, 2)

    def test_message_escapes_server_values(self):
        text = format_result_message({"status": "VA_RESERVED", "vessel": "<배&>", "va_account": "1<2",
                                      "va_amount": 3800, "fare": 3800, "passengers": ["a&b"]})
        self.assertIn("&lt;배&amp;&gt;", text)
        self.assertIn("1&lt;2", text)
        self.assertIn("3,800원", text)

    def test_room_filter_needs_seat_for_every_non_infant(self):
        worker = EngineWorker({})
        rooms = [
            {"vesselid": "v", "mastertime": "m", "classes": "일반실A", "onlinecnt": "1", "ispossible": "1"},
            {"vesselid": "v", "mastertime": "m", "classes": "일반실B", "onlinecnt": None, "ispossible": "Y"},
            {"vesselid": "v", "mastertime": "m", "classes": "일반실C", "onlinecnt": "5.0", "ispossible": 1},
        ]
        room = worker._filter_room(rooms, "v", "m", "일반객실만", seats_needed=3)
        self.assertEqual(room["classes"], "일반실C")
        self.assertIsNone(worker._filter_room(rooms, "v", "m", "일반객실만", seats_needed=6))

    def test_card_decline_stops_worker(self):
        worker = EngineWorker({"ksa_id": "a", "ksa_pw": "b", "f_portid": "1", "t_portid": "2",
                               "date": "2030-01-01", "auto_pay": True, "card_info": {"cardno": "1"},
                               "refresh_interval": 0})
        worker.engine = FakeEngine([KsaCardDeclinedError("거절"), AssertionError("retried")], [KsaError("x")] * 2)
        sent = []
        worker.telegram.send_message = sent.append
        worker.run()
        self.assertEqual(worker.engine.book_calls, 1)
        self.assertIn("카드 승인 거절", sent[0])


if __name__ == "__main__":
    unittest.main()
