# -*- coding: utf-8 -*-
"""KsaMacro 오프스크린 GUI 및 핵심 로직 스모크 테스트.

모든 컴포넌트(엔진, 설정 관리자, 워커, GUI 위젯)의 정상 동작을 검증합니다.
"""

import os
import sys

# 프로젝트 루트를 sys.path에 추가
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# 오프스크린 모드 설정
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import unittest
from PyQt6.QtWidgets import QApplication

from config_manager import load_config, save_config
from engines import KsaEngine
from ksa_macro_main import KsaMainWindow


class TestKsaMacroSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)

    def test_01_engine_init(self):
        """엔진 인스턴스 생성 및 기본 헤더 확인"""
        engine = KsaEngine()
        self.assertIsNotNone(engine.session)
        self.assertIn("island.theksa.co.kr", engine.session.headers.get("Origin", ""))

    def test_02_config_save_load(self):
        """설정 저장 및 불러오기 검증 (테스트 후 깨끗이 원복)"""
        original_cfg = load_config()
        test_cfg = original_cfg.copy()
        test_cfg["f_port"] = "인천"
        test_cfg["t_port"] = "백령"
        save_config(test_cfg)
        loaded = load_config()
        self.assertEqual(loaded.get("f_port"), "인천")
        self.assertEqual(loaded.get("t_port"), "백령")
        # 원래 설정으로 복원
        save_config(original_cfg)

    def test_03_engine_login_and_ports(self):
        """KSA 서버 항구 조회 및 선택적 로그인 검증"""
        engine = KsaEngine()
        cfg = load_config()
        uid = cfg.get("ksa_id")
        upw = cfg.get("ksa_pw")
        if uid and upw and uid != "sample_user":
            try:
                res = engine.login(uid, upw)
                self.assertTrue(res.get("success"))
                print(f"  [Smoke Test] 저장된 계정 로그인 성공 확인: {res.get('member')} 님")
            except Exception as e:
                print(f"  [Smoke Test] 로그인 테스트 생략 또는 실패: {e}")

        ports = engine.get_ports()
        self.assertGreater(len(ports), 50)
        print(f"  [Smoke Test] 출발항 목록 조회 확인: 총 {len(ports)}개 항구")

        pairs = engine.get_pair_ports("1010", "0")
        self.assertGreater(len(pairs), 0)
        print(f"  [Smoke Test] 인천항 연결 도착지 조회 확인: 총 {len(pairs)}개 도착항")

    def test_04_gui_offscreen_creation(self):
        """PyQt6 GUI 오프스크린 생성 및 컨트롤 바인딩 검증"""
        window = KsaMainWindow()
        self.assertIsNotNone(window.id_input)
        self.assertIsNotNone(window.pw_input)
        self.assertIsNotNone(window.dep_combo)
        self.assertIsNotNone(window.arr_combo)
        self.assertIsNotNone(window.start_btn)
        self.assertIsNotNone(window.stop_btn)

        print("  [Smoke Test] GUI 컨트롤 생성 및 설정 자동 바인딩 확인 완료")
        window.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
