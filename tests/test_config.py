"""Secrets must not land in the plain config file."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config_manager
from telegram_remote import TelegramBot


class ConfigTests(unittest.TestCase):
    def test_token_is_kept_out_of_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            with patch.object(config_manager, "CONFIG_FILE", path), \
                    patch.object(config_manager.keyring, "set_password"), \
                    patch.object(config_manager, "clear_card_info"):
                config_manager.save_config({"ksa_id": "id", "ksa_pw": "pw", "tg_token": "123:SECRET",
                                            "card_info": {"cardno": ""}, "date": "2026-09-30"})
            saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("tg_token", saved)
        self.assertNotIn("ksa_pw", saved)
        self.assertEqual(saved["date"], "2026-09-30")

    def _updates(self, chat_ids):
        class Resp:
            status_code = 200

            def json(self_inner):
                return {"ok": True, "result": [{"message": {"chat": {"id": c}}} for c in chat_ids]}
        return Resp()

    def test_pairing_refuses_when_several_chats_wrote_to_bot(self):
        bot = TelegramBot("t", "")
        with patch("telegram_remote.requests.get", return_value=self._updates([1, 1])):
            self.assertEqual(bot.get_last_chat_id(), "1")
        with patch("telegram_remote.requests.get", return_value=self._updates([1, 2])):
            self.assertIsNone(bot.get_last_chat_id())

    def test_corrupt_files_are_backed_up_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg, hist = Path(tmp) / "config.json", Path(tmp) / "history.json"
            cfg.write_text("{broken", encoding="utf-8")
            hist.write_text('{"not": "a list"}', encoding="utf-8")
            with patch.object(config_manager, "CONFIG_FILE", cfg), \
                    patch.object(config_manager, "HISTORY_FILE", hist), \
                    patch.object(config_manager.keyring, "get_password", return_value=None):
                config_manager.load_config()
                self.assertEqual(config_manager.load_booking_history(), [])
                config_manager.save_booking_record({"ticketingkey": "k"})
            self.assertEqual(next(Path(tmp).glob("config.json.*.bak")).read_text(encoding="utf-8"), "{broken")
            self.assertIn("not", next(Path(tmp).glob("history.json.*.bak")).read_text(encoding="utf-8"))
            self.assertEqual(json.loads(hist.read_text(encoding="utf-8")), [{"ticketingkey": "k"}])


class KeyringTests(unittest.TestCase):
    def test_keyring_failures_are_reported_and_old_password_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = {("KsaMacro_KSA", "old"): "pw"}
            deleted = []

            def get(service, user):
                if service == "KsaMacro_Card":
                    raise RuntimeError("locked")
                return store.get((service, user))

            def set_(service, user, value):
                if service == "KsaMacro_Telegram":
                    raise RuntimeError("denied")
                store[(service, user)] = value

            with patch.object(config_manager, "CONFIG_FILE", Path(tmp) / "c.json"),                     patch.object(config_manager.keyring, "get_password", side_effect=get),                     patch.object(config_manager.keyring, "set_password", side_effect=set_),                     patch.object(config_manager.keyring, "delete_password",
                                 side_effect=lambda s, u: deleted.append((s, u))):
                (Path(tmp) / "c.json").write_text('{"ksa_id": "old"}', encoding="utf-8")
                cfg = config_manager.load_config()
                self.assertEqual(cfg["ksa_pw"], "pw")
                config_manager.save_config({**cfg, "ksa_id": "new", "ksa_pw": "pw2", "tg_token": "t"})
                warnings = config_manager.pop_keyring_warnings()

        self.assertIn(("KsaMacro_KSA", "old"), deleted)
        self.assertEqual(store[("KsaMacro_KSA", "new")], "pw2")
        self.assertTrue(any("카드 정보 불러오기" in w for w in warnings))
        self.assertTrue(any("텔레그램 토큰 저장" in w for w in warnings))
        self.assertEqual(config_manager.pop_keyring_warnings(), [])


class BackgroundTaskTests(unittest.TestCase):
    def test_background_errors_go_to_error_callback_and_callback_errors_are_contained(self):
        from concurrent.futures import Future
        from ksa_macro_main import KsaMainWindow
        logs, errors = [], []
        window = type("W", (), {"log_msg": lambda self, lvl, text: logs.append((lvl, text))})()

        failed = Future()
        failed.set_exception(RuntimeError("network down"))
        KsaMainWindow._on_bg_finished(window, failed, None, errors.append)
        self.assertEqual(str(errors[0]), "network down")

        ok = Future()
        ok.set_result([1])
        KsaMainWindow._on_bg_finished(window, ok, lambda r: 1 / 0, None)  # 콜백 예외가 앱을 죽이지 않음
        self.assertEqual(logs[0][0], "ERROR")



class ConfigSafetyTests(unittest.TestCase):
    def test_unreadable_config_is_not_overwritten_and_empty_password_does_not_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "config.json"
            cfg_path.write_text('{"ksa_id": "me", "passengers": [{"name": "A", "idnumber": "1"}]}', encoding="utf-8")
            deleted = []
            real_open = open

            def locked_open(path, *a, **k):
                if Path(path) == cfg_path and "r" in (a[0] if a else k.get("mode", "r")):
                    raise PermissionError("locked")
                return real_open(path, *a, **k)

            with patch.object(config_manager, "CONFIG_FILE", cfg_path), \
                    patch.object(config_manager, "_config_unreadable", False), \
                    patch.object(config_manager.keyring, "get_password", return_value=None), \
                    patch.object(config_manager.keyring, "set_password"), \
                    patch.object(config_manager.keyring, "delete_password",
                                 side_effect=lambda s, u: deleted.append(u)), \
                    patch("builtins.open", side_effect=locked_open):
                cfg = config_manager.load_config()
                config_manager.save_config({**cfg, "ksa_id": "me", "ksa_pw": ""})
            config_manager.pop_keyring_warnings()
            self.assertIn("passengers", cfg_path.read_text(encoding="utf-8"))  # 원본 유지
            self.assertFalse(list(Path(tmp).glob("*.bak")))  # 잠긴 정상 파일을 옮기지 않음
            self.assertEqual(deleted, [])  # 빈 비밀번호 칸 때문에 저장된 비밀번호를 지우지 않음


if __name__ == "__main__":
    unittest.main()
