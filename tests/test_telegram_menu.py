"""Telegram menu regression checks."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ksa_macro_main import KsaMainWindow
from telegram_remote import TelegramRemoteManager, validate_card_field


class FakeMessage:
    def __init__(self, text):
        self.text = text
        self.replies = []
        self.reply_options = []
        self.deleted = False

    async def delete(self):
        self.deleted = True

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        self.reply_options.append(kwargs)


class FakeUpdate:
    def __init__(self, text, chat_id="123"):
        self.effective_chat = type("Chat", (), {"id": chat_id})()
        self.message = FakeMessage(text)


class FakeContext:
    def __init__(self):
        self.user_data = {}


class TelegramMenuTests(unittest.TestCase):
    def test_journey_button_requests_query_and_returns_results(self):
        manager = TelegramRemoteManager("token", "123")
        requested = []
        manager.request_journey_check.connect(lambda: requested.append(True))
        update = FakeUpdate("🚢 여정 즉시 조회")

        asyncio.run(manager.handle_message(update, None))

        self.assertEqual(requested, [True])
        self.assertEqual(update.message.replies, [])

        sent = []
        window = type("Window", (), {
            "tg_remote": type("Remote", (), {
                "send_log": lambda self, level, text, **kw: sent.append((level, text))
            })(),
            "_on_query_click": lambda self, then=None: then(["15:40 출항", "잔여 3석"]),
        })()
        KsaMainWindow._on_telegram_query(window)
        self.assertEqual(sent, [("INFO", "15:40 출항\n잔여 3석")])

    def test_setting_menu_selects_ports_and_emits_updates(self):
        manager = TelegramRemoteManager("token", "123")
        manager.set_port_options(
            [{"port": "인천", "portid": "1010", "portsubid": "0"}],
            [{"t_port": "백령", "t_portid": "1002", "t_portsubid": "0"}],
        )
        requested = []
        manager.request_update_setting.connect(lambda key, value: requested.append((key, value)))
        context = FakeContext()

        asyncio.run(manager.handle_message(FakeUpdate("⚙️ 예매 설정"), context))
        asyncio.run(manager.handle_message(FakeUpdate("🚢 출발항"), context))
        search = FakeUpdate("인")
        asyncio.run(manager.handle_message(search, context))
        keyboard = search.message.reply_options[0]["reply_markup"].keyboard
        self.assertEqual([[button.text for button in row] for row in keyboard], [["인천"], ["↩️ 설정 메뉴"]])
        asyncio.run(manager.handle_message(FakeUpdate("인천"), context))

        asyncio.run(manager.handle_message(FakeUpdate("🏁 도착항"), context))
        asyncio.run(manager.handle_message(FakeUpdate("백령"), context))

        self.assertEqual(requested, [
            ("f_port", {"port": "인천", "portid": "1010", "portsubid": "0"}),
            ("t_port", {"t_port": "백령", "t_portid": "1002", "t_portsubid": "0"}),
        ])

    def test_setting_inputs_are_validated_and_blocked_while_running(self):
        manager = TelegramRemoteManager("token", "123")
        requested = []
        manager.request_update_setting.connect(lambda key, value: requested.append((key, value)))
        context = FakeContext()

        asyncio.run(manager.handle_message(FakeUpdate("📅 날짜"), context))
        invalid = FakeUpdate("내일")
        asyncio.run(manager.handle_message(invalid, context))
        self.assertIn("YYYY-MM-DD", invalid.message.replies[0])

        asyncio.run(manager.handle_message(FakeUpdate("2030-01-02"), context))
        asyncio.run(manager.handle_message(FakeUpdate("🕐 시작 시간"), context))
        invalid_time = FakeUpdate("06:15")
        asyncio.run(manager.handle_message(invalid_time, context))
        self.assertIn("30분", invalid_time.message.replies[0])
        asyncio.run(manager.handle_message(FakeUpdate("06:30"), context))
        asyncio.run(manager.handle_message(FakeUpdate("💳 자동 결제"), context))
        asyncio.run(manager.handle_message(FakeUpdate("끄기"), context))
        self.assertEqual(requested, [("date", "2030-01-02"), ("time_start", "06:30"), ("auto_pay", False)])

        manager.set_macro_running(True)
        blocked = FakeUpdate("⚙️ 예매 설정")
        asyncio.run(manager.handle_message(blocked, context))
        self.assertIn("실행 중", blocked.message.replies[0])

    def test_gui_applies_setting_saves_and_replies_with_current_status(self):
        class CheckBox:
            checked = True

            def setChecked(self, value):
                self.checked = value

        class Remote:
            def __init__(self):
                self.settings = None
                self.logs = []

            def set_current_settings(self, settings):
                self.settings = settings

            def set_port_options(self, ports, pairs):
                pass

            def send_log(self, level, text, **kw):
                self.logs.append((level, text))

            def format_settings_text(self):
                return "갱신된 상태"

        remote = Remote()
        window = type("Window", (), {
            "worker": None,
            "auto_pay_chk": CheckBox(),
            "tg_remote": remote,
            "_all_ports": [],
            "_current_pairs": [],
            "_auto_save_settings": lambda self: None,
            "_gather_settings_from_ui": lambda self: {"auto_pay": self.auto_pay_chk.checked}, "_report_tg_settings": KsaMainWindow._report_tg_settings,
        })()

        KsaMainWindow._on_telegram_setting_update(window, "auto_pay", False)

        self.assertFalse(window.auto_pay_chk.checked)
        self.assertEqual(remote.settings, {"auto_pay": False})
        self.assertEqual(remote.logs, [("SUCCESS", "예매 설정이 변경되었습니다.\n갱신된 상태")])

    def test_card_field_validation(self):
        self.assertEqual(validate_card_field("cardno", "1234-5678-9012-3456"), "1234567890123456")
        self.assertIsNone(validate_card_field("cardno", "1234"))
        self.assertEqual(validate_card_field("validdate", "0928"), "0928")
        self.assertIsNone(validate_card_field("validdate", "1328"))
        self.assertEqual(validate_card_field("password", "12"), "12")
        self.assertIsNone(validate_card_field("password", "1a"))
        self.assertEqual(validate_card_field("requestno", "900101"), "900101")
        self.assertIsNone(validate_card_field("requestno", "901301"))

    def test_card_menu_emits_update_and_deletes_input(self):
        manager = TelegramRemoteManager("token", "123")
        requested = []
        manager.request_update_setting.connect(lambda key, value: requested.append((key, value)))
        context = FakeContext()

        asyncio.run(manager.handle_message(FakeUpdate("🪪 카드 정보"), context))
        asyncio.run(manager.handle_message(FakeUpdate("카드번호"), context))
        bad = FakeUpdate("12")
        asyncio.run(manager.handle_message(bad, context))
        good = FakeUpdate("1234567890123456")
        asyncio.run(manager.handle_message(good, context))

        self.assertTrue(bad.message.deleted and good.message.deleted)
        self.assertIn("형식", bad.message.replies[0])
        self.assertEqual(requested, [("card_info", ("cardno", "1234567890123456"))])
        manager.set_current_settings({"card_info": {"cardno": "1234567890123456"}})
        self.assertIn("****3456", manager.format_settings_text())

    def test_card_input_is_deleted_even_while_macro_running(self):
        manager = TelegramRemoteManager("token", "123")
        context = FakeContext()
        context.user_data["waiting_for"] = "card:cardno"
        manager.set_macro_running(True)
        update = FakeUpdate("1234567890123456")

        asyncio.run(manager.handle_message(update, context))

        self.assertTrue(update.message.deleted)
        self.assertIn("변경할 수 없습니다", update.message.replies[0])

    def test_gui_applies_card_field(self):
        class LineEdit:
            def __init__(self):
                self.value = ""

            def setText(self, value):
                self.value = value

        remote = type("Remote", (), {
            "set_current_settings": lambda self, s: None,
            "set_port_options": lambda self, a, b: None,
            "send_log": lambda self, level, text, **kw: None,
            "format_settings_text": lambda self: "",
        })()
        window = type("Window", (), {
            "worker": None, "tg_remote": remote, "_all_ports": [], "_current_pairs": [],
            "card_no_input": LineEdit(), "card_valid_input": LineEdit(),
            "card_pw_input": LineEdit(), "card_req_input": LineEdit(),
            "_gather_settings_from_ui": lambda self: {}, "_report_tg_settings": KsaMainWindow._report_tg_settings,
        })()

        KsaMainWindow._on_telegram_setting_update(window, "card_info", ("validdate", "0928"))

        self.assertEqual(window.card_valid_input.value, "0928")

    def test_start_block_and_stop_are_reported_to_telegram(self):
        from unittest.mock import patch
        sent = []
        remote = type("Remote", (), {
            "isRunning": lambda self: True,
            "send_log": lambda self, level, text, **kw: sent.append((level, text)),
            "set_macro_running": lambda self, running: None,
        })()
        button = type("Btn", (), {"setEnabled": lambda self, v: None})()
        window = type("Window", (), {"tg_remote": remote, "start_btn": button, "stop_btn": button, "worker": None,
                                     "log_msg": lambda self, level, text: None})()

        with patch("ksa_macro_main.QMessageBox.warning") as popup:
            KsaMainWindow._warn_start(window, "카드번호 오류", "카드번호를 확인하세요.")
        popup.assert_called_once()
        KsaMainWindow._on_worker_finished(window)

        self.assertEqual(sent[0][0], "WARNING")
        self.assertIn("시작하지 못했습니다 - 카드번호 오류", sent[0][1])
        self.assertIn("중지", sent[1][1])


    def test_passenger_dialog_requires_distinct_emergency_contact(self):
        from unittest.mock import patch
        from PyQt6.QtWidgets import QApplication
        from ksa_macro_main import PassengerDialog
        app = QApplication.instance() or QApplication([])

        def fill(dlg, emtel):
            dlg.name_input.setText("홍길동")
            dlg.idnumber_input.setText("19900101")
            dlg.tel_input.setText("01011112222")
            dlg.emtel_input.setText(emtel)

        warned = []
        with patch("ksa_macro_main.QMessageBox.warning", lambda *a, **k: warned.append(a[2])):
            for emtel in ("", "01011112222"):  # 미입력, 본인 번호와 동일
                dlg = PassengerDialog()
                fill(dlg, emtel)
                dlg.accept_data()
                self.assertNotIn("emtel", dlg.passenger_data)
            dlg = PassengerDialog()
            fill(dlg, "01033334444")
            dlg.accept_data()
        self.assertEqual(dlg.passenger_data["emtel"], "01033334444")
        self.assertEqual(len(warned), 2)
        self.assertIn("비상연락처", warned[0])
        self.assertIn("본인 연락처와 달라야", warned[1])
        app.processEvents()

    def test_settings_text_escapes_user_values(self):
        manager = TelegramRemoteManager("token", "123")
        manager.set_current_settings({"f_port": "<A&B>", "passengers": [{"name": "<b>x</b>"}]})
        text = manager.format_settings_text()
        self.assertIn("&lt;A&amp;B&gt;", text)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)
        self.assertIn("<b>[현재 KsaMacro 상태]</b>", text)  # 의도한 서식은 유지

    def test_late_finish_of_old_worker_does_not_reset_new_worker(self):
        calls = []
        running = type("W", (), {"isRunning": lambda self: True})()
        window = type("Window", (), {"worker": running, "tg_remote": None,
                                     "start_btn": type("B", (), {"setEnabled": lambda self, v: calls.append(v)})(),
                                     "stop_btn": None, "log_msg": lambda self, *a: calls.append(a)})()
        KsaMainWindow._on_worker_finished(window)
        self.assertEqual(calls, [])


    def test_send_log_goes_direct_while_reconnecting(self):
        from unittest.mock import patch
        manager = TelegramRemoteManager("token", "123")  # 세션 미가동 (_live False)
        sent = []
        with patch("telegram_remote.TelegramBot.send_message", lambda self, text: sent.append(text)), \
                patch("telegram_remote.threading.Thread",
                      lambda target, args, daemon: type("T", (), {"start": lambda s: target(*args)})()):
            manager.send_log("ERROR", "a<b")
            manager.send_log("", "<b>굵게</b>", html_mode=True)
        self.assertEqual(sent, ["[ERROR] a&lt;b", "<b>굵게</b>"])


if __name__ == "__main__":
    unittest.main()
