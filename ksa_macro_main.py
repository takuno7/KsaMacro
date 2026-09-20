# -*- coding: utf-8 -*-
"""KsaMacro — 한국해운조합(KSA) 여객선 예매 매크로 메인 GUI 애플리케이션.

TrainMacro의 검증된 아키텍처와 디자인을 계승하여:
1. 텔레그램 대화형 키보드 메뉴 버튼(원격 제어) 완벽 지원
2. 승객 등록 시 생년월일 8자리(YYYYMMDD) 정밀 지원
3. '여객 추가'와 '유아 추가'를 분리 지원하는 직관적인 승객 관리 시스템
4. 로그인 정보 영구 기억 및 시작 시 자동 로그인 복원
5. 깔끔하고 정갈한 표준 라이트 테마
"""

import copy
import datetime
import html
from concurrent.futures import Future, ThreadPoolExecutor
import logging
import os
import sys
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QDate, QRegularExpression, QSize, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QFont, QIcon, QRegularExpressionValidator, QTextCharFormat, QTextCursor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from config_manager import (clear_card_info, load_config, save_config, save_booking_record, load_booking_history,
                            pop_keyring_warnings)
from engines import KsaEngine
from macro_worker import EngineWorker
from telegram_remote import TelegramBot, TelegramRemoteManager

# Windows 작업표시줄에서 고유한 배 아이콘이 정상 표시되도록 AppUserModelID 등록
if sys.platform == "win32":
    import ctypes
    try:
        myappid = "KsaMacro.FerryBooking.Application.v1"
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
# httpx(텔레그램 라이브러리)는 INFO에서 봇 토큰이 들어간 요청 URL을 그대로 기록하므로 올리지 않습니다.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

VERSION = "1.2.0"


def resource_path(relative_path: str) -> str:
    """PyInstaller 번들 내부 리소스 및 로컬 경로를 반환합니다."""
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)


class PassengerDialog(QDialog):
    """승객 상세 정보 입력 다이얼로그 (여객 / 유아 분리 지원)."""

    def __init__(self, passenger_data: Optional[Dict[str, Any]] = None, is_infant: bool = False, parent=None):
        super().__init__(parent)
        self.is_infant = is_infant
        title_type = "유아(24개월 미만)" if is_infant else "일반 여객(대인/소아/경로)"
        self.setWindowTitle(f"{title_type} 정보 입력")
        self.resize(400, 300)
        self.passenger_data = (passenger_data or {}).copy()

        layout = QFormLayout(self)
        layout.setSpacing(10)

        self.name_input = QLineEdit(self.passenger_data.get("name", ""))
        self.name_input.setPlaceholderText("성명 입력")

        self.idnumber_input = QLineEdit(self.passenger_data.get("idnumber", ""))
        self.idnumber_input.setPlaceholderText("생년월일 8자리 (예: 19850101, 20240512)")
        self.idnumber_input.setMaxLength(8)
        self.idnumber_input.setValidator(QRegularExpressionValidator(QRegularExpression(r"^[0-9]*$"), self))

        self.sex_combo = QComboBox()
        self.sex_combo.addItems(["남 (M)", "여 (F)"])
        if self.passenger_data.get("sex") == "F":
            self.sex_combo.setCurrentIndex(1)

        self.tel_input = QLineEdit(self.passenger_data.get("tel", ""))
        self.tel_input.setPlaceholderText("연락처 10~11자리 (예: 01012345678)")
        self.tel_input.setMaxLength(11)
        self.tel_input.setValidator(QRegularExpressionValidator(QRegularExpression(r"^[0-9]*$"), self))

        self.emtel_input = QLineEdit(self.passenger_data.get("emtel", ""))
        self.emtel_input.setPlaceholderText("비상연락처 10~11자리 (본인 번호와 다르게)")
        self.emtel_input.setMaxLength(11)
        self.emtel_input.setValidator(QRegularExpressionValidator(QRegularExpression(r"^[0-9]*$"), self))

        if is_infant:
            self.type_lbl = QLabel("유아 (만 2세 미만, 무임)")
            self.type_lbl.setStyleSheet("color: #2e7d32; font-weight: bold;")
            self.discount_lbl = QLabel("일반발매 (기본)")
        else:
            self.ticket_type_combo = QComboBox()
            self.ticket_type_combo.addItems(["대인 (만 13세 이상)", "소아 (만 2세~12세)", "경로 (만 65세 이상)"])
            tid = self.passenger_data.get("ticketid", "1")
            if tid == "3":
                self.ticket_type_combo.setCurrentIndex(1)
            elif tid == "7":
                self.ticket_type_combo.setCurrentIndex(2)
            self.discount_lbl = QLabel("일반발매 (기본)")

        layout.addRow("성명:", self.name_input)
        layout.addRow("생년월일(8자리):", self.idnumber_input)
        layout.addRow("성별:", self.sex_combo)
        layout.addRow("연락처:", self.tel_input)
        layout.addRow("비상연락처:", self.emtel_input)
        if is_infant:
            layout.addRow("승객 구분:", self.type_lbl)
        else:
            layout.addRow("승객 구분:", self.ticket_type_combo)
        layout.addRow("할인 종류:", self.discount_lbl)

        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btn_box.accepted.connect(self.accept_data)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    def accept_data(self):
        name = self.name_input.text().strip()
        birth = self.idnumber_input.text().strip().replace("-", "")
        tel = self.tel_input.text().strip().replace("-", "")
        emtel = self.emtel_input.text().strip().replace("-", "")

        if not name:
            QMessageBox.warning(self, "입력 오류", "승객 성명을 올바르게 입력해주세요.")
            self.name_input.setFocus()
            return

        if len(birth) != 8 or not birth.isdigit():
            QMessageBox.warning(
                self,
                "생년월일 규격 오류",
                "생년월일은 반드시 8자리 숫자(YYYYMMDD)로 입력해야 합니다.\n(예: 19850101, 20240512)"
            )
            self.idnumber_input.setFocus()
            return

        # 생년월일 유효 날짜 검증
        try:
            y = int(birth[:4])
            m = int(birth[4:6])
            d = int(birth[6:8])
            b_date = datetime.date(y, m, d)
        except Exception:
            QMessageBox.warning(
                self,
                "생년월일 날짜 오류",
                f"입력하신 생년월일 '{birth}'은(는) 달력에 존재하지 않는 날짜입니다.\n올바른 생년월일을 입력해주세요. (예: 19900512)"
            )
            self.idnumber_input.setFocus()
            return

        today = datetime.date.today()
        if b_date > today:
            QMessageBox.warning(self, "생년월일 오류", f"생년월일이 오늘 날짜({today})보다 미래일 수 없습니다.")
            self.idnumber_input.setFocus()
            return
        if y < 1900:
            QMessageBox.warning(self, "생년월일 오류", "생년월일 연도는 1900년 이후여야 합니다.")
            self.idnumber_input.setFocus()
            return

        # 만 나이 계산
        age_years = today.year - b_date.year - ((today.month, today.day) < (b_date.month, b_date.day))

        if self.is_infant:
            if age_years >= 2:
                QMessageBox.warning(
                    self,
                    "승객 구분 오류",
                    f"입력하신 생년월일({birth}) 기준 승객은 만 {age_years}세로, 만 2세(24개월) 이상입니다.\n\n"
                    "유아(무임) 대상이 아니므로 메인 화면의 [➕ 여객 추가 (대인/소아/경로)]로 등록해주세요."
                )
                self.idnumber_input.setFocus()
                return
        else:
            if age_years < 2:
                reply = QMessageBox.question(
                    self,
                    "유아 확인 안내",
                    f"입력하신 승객은 만 2세 미만({age_years}세)입니다.\n"
                    "유아는 무임(0원) 혜택이 적용되는 [👶 유아 추가]로 등록하시는 것을 권장합니다.\n\n"
                    "그래도 일반 여객(좌석 점유)으로 계속 등록하시겠습니까?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return

        if not tel or len(tel) < 9 or not tel.isdigit():
            QMessageBox.warning(
                self,
                "연락처 입력 오류",
                "연락처(휴대전화번호)를 올바르게 입력해주세요.\n(예: 01012345678, 숫자만 입력)"
            )
            self.tel_input.setFocus()
            return

        # KSA는 운항편 설정(requiredemtel)에 따라 비상연락처를 요구하며, 조회한 노선들은 모두 필수였습니다.
        if not emtel or len(emtel) < 9 or not emtel.isdigit():
            QMessageBox.warning(
                self,
                "비상연락처 입력 오류",
                "비상연락처(보호자 등 다른 사람의 휴대전화번호)를 올바르게 입력해주세요.\n(예: 01087654321, 숫자만 입력)"
            )
            self.emtel_input.setFocus()
            return
        if emtel == tel:
            QMessageBox.warning(
                self,
                "비상연락처 중복",
                "비상연락처는 본인 연락처와 달라야 합니다.\nKSA는 승객들의 연락처와 비상연락처가 모두 서로 다를 것을 요구합니다."
            )
            self.emtel_input.setFocus()
            return

        self.passenger_data["name"] = name
        self.passenger_data["idnumber"] = birth  # KSA에서는 idnumber에 8자리 생년월일 전송
        self.passenger_data["sex"] = "M" if self.sex_combo.currentIndex() == 0 else "F"
        self.passenger_data["tel"] = tel
        self.passenger_data["emtel"] = emtel

        if self.is_infant:
            self.passenger_data["ticketid"] = "5"  # 5: 유아
            self.passenger_data["ticket_desc"] = "유아"
        else:
            t_map = {0: ("1", "대인"), 1: ("3", "소아"), 2: ("7", "경로")}
            tid, tdesc = t_map[self.ticket_type_combo.currentIndex()]
            self.passenger_data["ticketid"] = tid
            self.passenger_data["ticket_desc"] = tdesc

        self.passenger_data["dcid"] = "100"  # 일반발매
        self.accept()


class KsaMainWindow(QMainWindow):
    # 백그라운드 작업 완료를 GUI 스레드로 넘기는 신호 (future, 성공 콜백, 실패 콜백)
    _bg_finished = pyqtSignal(object, object, object)
    """KsaMacro 메인 창 (TrainMacro 스타일)."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"KsaMacro — KSA 여객선 예매 매크로  v{VERSION}")
        self.resize(700, 920)
        self.setMinimumSize(660, 880)

        # 배/여객선 아이콘 적용
        ico_file = resource_path("ship_icon.ico")
        if os.path.exists(ico_file):
            self.setWindowIcon(QIcon(ico_file))

        self.engine = KsaEngine()
        self.worker: Optional[EngineWorker] = None
        self.util_worker: Optional[EngineWorker] = None
        self.tg_remote: Optional[TelegramRemoteManager] = None
        self.is_tg_paired = False
        self.tg_chat_id = ""
        # 네트워크 호출은 이 단일 스레드에서 순서대로 실행합니다 (GUI 멈춤 방지, 같은 세션 동시 사용 방지).
        self._bg = ThreadPoolExecutor(max_workers=1)
        self._bg_finished.connect(self._on_bg_finished)
        self._keyring_warned: set = set()
        self._old_tg_remotes: List[TelegramRemoteManager] = []  # 종료 중인 이전 텔레그램 연결 (참조 유지)
        self.config = load_config()

        self._all_ports: List[Dict[str, Any]] = []
        self._current_pairs: List[Dict[str, Any]] = []
        self._is_loading_config = True

        self._init_ui()
        self._apply_styles()
        self._load_ports_initial()
        self._load_settings_to_ui()
        self._connect_setting_signals()
        self._is_loading_config = False

        self.log_msg("INFO", f"KsaMacro v{VERSION}가 시작되었습니다. 정보를 확인해 주세요.")
        self._report_keyring_warnings()

        # 시작 시 텔레그램 연동 및 백그라운드 자동 로그인 즉시 가동 (TrainMacro 방식)
        self._init_telegram_remote()
        self._try_auto_login()

    # ----------------------------------------------------------------------
    # UI 생성 (TrainMacro 스타일)
    # ----------------------------------------------------------------------
    def _init_ui(self):
        main_widget = QWidget()
        main_layout = QVBoxLayout(main_widget)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(15, 12, 15, 12)

        # 1. 계정 정보 그룹
        account_group = QGroupBox("계정 정보")
        account_layout = QGridLayout(account_group)
        account_layout.setVerticalSpacing(8)
        account_layout.setHorizontalSpacing(8)

        self.id_input = QLineEdit()
        self.id_input.setPlaceholderText("휴대폰번호 ('-' 제외 입력)")
        self.pw_input = QLineEdit()
        self.pw_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.pw_input.setPlaceholderText("비밀번호 입력")
        self.login_test_btn = QPushButton("로그인 확인")
        self.login_test_btn.setMinimumWidth(120)
        self.login_test_btn.clicked.connect(self._on_login_click)

        self.login_status_lbl = QLabel("미로그인")
        self.login_status_lbl.setStyleSheet("color: #e65100; font-weight: bold;")

        account_layout.addWidget(QLabel("회원번호:"), 0, 0)
        account_layout.addWidget(self.id_input, 0, 1)
        account_layout.addWidget(QLabel("비밀번호:"), 0, 2)
        account_layout.addWidget(self.pw_input, 0, 3)
        account_layout.addWidget(self.login_test_btn, 0, 4)

        account_layout.addWidget(QLabel("접속 상태:"), 1, 0)
        account_layout.addWidget(self.login_status_lbl, 1, 1, 1, 4)

        account_layout.setColumnStretch(1, 1)
        account_layout.setColumnStretch(3, 1)
        main_layout.addWidget(account_group)

        # 2. 텔레그램 연동 그룹 (TrainMacro와 100% 동일)
        tg_group = QGroupBox("텔레그램 연동 정보")
        tg_layout = QGridLayout(tg_group)
        tg_layout.setVerticalSpacing(8)
        tg_layout.setHorizontalSpacing(8)

        self.tg_token_input = QLineEdit()
        self.tg_token_input.setPlaceholderText("텔레그램 Bot Token 입력 (예: 123456:ABC-DEF...)")
        tg_help_tooltip = (
            "💡 [텔레그램 연동 방법]\n"
            "1. 텔레그램 앱에서 @BotFather 검색 후 대화 시작\n"
            "2. /newbot 명령어로 새 봇을 만들고 HTTP API Token 복사\n"
            "3. 복사한 토큰을 여기에 붙여넣기\n"
            "4. 생성한 내 봇 채팅방에 아무 메시지(예: /start)나 전송\n"
            "5. [연동하기] 버튼을 누르면 Chat ID가 자동 감지(오토 페어링)되어 연동 완료!\n"
            "6. 모바일 텔레그램 채팅창에 대화형 원격 제어 키보드 메뉴가 활성화됩니다."
        )
        self.tg_token_input.setToolTip(tg_help_tooltip)

        self.tg_pair_btn = QPushButton("연동하기")
        self.tg_pair_btn.setMinimumWidth(150)
        self.tg_pair_btn.setToolTip(tg_help_tooltip)
        self.tg_pair_btn.clicked.connect(self.pair_telegram)

        tg_layout.addWidget(QLabel("Bot Token:"), 0, 0)
        tg_layout.addWidget(self.tg_token_input, 0, 1)
        tg_layout.addWidget(self.tg_pair_btn, 0, 2)
        tg_layout.setColumnStretch(1, 1)
        main_layout.addWidget(tg_group)

        # 3. 예매 여정 설정 그룹
        book_group = QGroupBox("예매 설정")
        book_layout = QGridLayout(book_group)
        book_layout.setVerticalSpacing(8)
        book_layout.setHorizontalSpacing(8)

        self.dep_combo = QComboBox()
        self.dep_combo.setEditable(True)
        self.dep_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.dep_combo.currentIndexChanged.connect(self._on_dep_changed)

        self.arr_combo = QComboBox()

        self.swap_btn = QPushButton("⇄")
        self.swap_btn.setFixedWidth(50)
        self.swap_btn.setStyleSheet("font-size: 16px; font-weight: bold; padding: 2px;")
        self.swap_btn.clicked.connect(self._on_swap_click)

        swap_box = QHBoxLayout()
        swap_box.addWidget(QLabel("출발지:"))
        swap_box.addWidget(self.dep_combo, 1)
        swap_box.addWidget(self.swap_btn)
        swap_box.addWidget(QLabel("도착지:"))
        swap_box.addWidget(self.arr_combo, 1)
        book_layout.addLayout(swap_box, 0, 0, 1, 6)

        # 일자 및 시간
        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDate(QDate.currentDate().addDays(1))
        self.date_edit.setMinimumDate(QDate.currentDate())
        if self.date_edit.calendarWidget():
            self.date_edit.calendarWidget().setMinimumWidth(280)

        time_options = [f"{h:02d}:{m:02d}" for h in range(24) for m in (0, 30)]
        self.time_start_combo = QComboBox()
        self.time_start_combo.addItems(time_options)
        self.time_start_combo.setCurrentText("06:00")
        self.time_end_combo = QComboBox()
        self.time_end_combo.addItems(time_options)
        self.time_end_combo.setCurrentText("23:00")

        book_layout.addWidget(QLabel("출발일:"), 1, 0)
        book_layout.addWidget(self.date_edit, 1, 1)
        book_layout.addWidget(QLabel("조회 시간:"), 1, 2)

        time_box = QHBoxLayout()
        time_box.addWidget(self.time_start_combo)
        time_box.addWidget(QLabel("~"))
        time_box.addWidget(self.time_end_combo)
        book_layout.addLayout(time_box, 1, 3, 1, 3)

        # 객실 선호 및 실시간 조회 버튼
        self.room_pref_combo = QComboBox()
        self.room_pref_combo.addItems(["일반객실 우선", "일반객실만", "전체 객실"])

        self.refresh_spin = QSpinBox()
        self.refresh_spin.setRange(1, 30)
        self.refresh_spin.setValue(2)
        self.refresh_spin.setSuffix(" 초")

        self.query_btn = QPushButton("🔍 실시간 여정 조회")
        self.query_btn.clicked.connect(self._on_query_click)

        book_layout.addWidget(QLabel("객실 유형:"), 2, 0)
        book_layout.addWidget(self.room_pref_combo, 2, 1)
        book_layout.addWidget(QLabel("조회 간격:"), 2, 2)
        book_layout.addWidget(self.refresh_spin, 2, 3)
        book_layout.addWidget(self.query_btn, 2, 4, 1, 2)

        book_layout.setColumnStretch(1, 1)
        book_layout.setColumnStretch(3, 1)
        main_layout.addWidget(book_group)

        # 4. 승객 및 카드 결제 설정 그룹 (여객/유아 분리 추가 지원)
        pay_group = QGroupBox("승객 및 결제 설정 (여객 / 유아 분리 등록)")
        pay_layout = QGridLayout(pay_group)
        pay_layout.setVerticalSpacing(8)
        pay_layout.setHorizontalSpacing(10)

        # 승객 테이블
        self.pass_table = QTableWidget(0, 6)
        self.pass_table.setHorizontalHeaderLabels(["구분", "성명", "생년월일(8자리)", "성별", "연락처", "비상연락처"])
        self.pass_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.pass_table.setFixedHeight(95)
        self.pass_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.pass_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.pass_table.setToolTip("💡 등록된 승객 행을 더블클릭하면 정보를 수정할 수 있습니다.")
        self.pass_table.cellDoubleClicked.connect(self._on_edit_passenger)
        pay_layout.addWidget(self.pass_table, 0, 0, 1, 4)

        # 승객 추가/삭제 버튼
        pass_btn_bar = QHBoxLayout()
        pass_btn_bar.setSpacing(12)
        self.add_adult_btn = QPushButton("➕ 여객 추가 (대인/소아/경로)")
        self.add_adult_btn.setToolTip(
            "💡 [여객 추가]\n"
            "대인(만 12세 이상), 소아(만 2세~12세 미만), 경로(만 65세 이상) 승객을 등록합니다.\n"
            "좌석이 정상 배정되며 운임이 부과됩니다. (생년월일 8자리 YYYYMMDD 입력)"
        )
        self.add_adult_btn.clicked.connect(lambda: self._on_add_passenger(is_infant=False))

        self.add_infant_btn = QPushButton("👶 유아 추가 (만 2세 미만 무임)")
        self.add_infant_btn.setToolTip(
            "💡 [유아 추가]\n"
            "만 2세 미만 유아 승객을 등록합니다.\n"
            "좌석을 점유하지 않으며 운임은 0원(무임)입니다. (생년월일 8자리 YYYYMMDD 입력)"
        )
        self.add_infant_btn.clicked.connect(lambda: self._on_add_passenger(is_infant=True))

        self.del_pass_btn = QPushButton("선택 삭제")
        self.del_pass_btn.setToolTip("선택한 승객을 목록에서 삭제합니다.")
        self.del_pass_btn.clicked.connect(self._on_delete_passenger)

        pass_btn_bar.addWidget(self.add_adult_btn)
        pass_btn_bar.addWidget(self.add_infant_btn)
        pass_btn_bar.addWidget(self.del_pass_btn)
        pay_layout.addLayout(pass_btn_bar, 1, 0, 1, 4)

        # 카드 정보 헤더 (자동 결제 체크박스 + 카드 정보 지우기 버튼)
        card_header_layout = QHBoxLayout()
        self.auto_pay_chk = QCheckBox("잔여석 발견 시 즉시 카드 자동 결제 승인")
        self.auto_pay_chk.setToolTip("끄면 가상계좌 예약으로 진행하고, 입금은행·계좌번호·입금기한을 알려 드립니다.")
        self.auto_pay_chk.setChecked(True)
        self.clear_card_btn = QPushButton("🗑️ 카드 정보 지우기")
        self.clear_card_btn.setToolTip("저장된 카드 정보를 화면 및 보안 저장소(Windows 자격 증명)에서 즉시 영구 삭제합니다.")
        self.clear_card_btn.setStyleSheet(
            "QPushButton { background-color: #ffebee; color: #c62828; border: 1px solid #ef9a9a; border-radius: 4px; padding: 3px 8px; font-weight: bold; font-size: 11px; } "
            "QPushButton:hover { background-color: #ffcdd2; border-color: #e57373; }"
        )
        self.clear_card_btn.clicked.connect(self._on_clear_card_info)
        card_header_layout.addWidget(self.auto_pay_chk)
        card_header_layout.addStretch()
        card_header_layout.addWidget(self.clear_card_btn)
        pay_layout.addLayout(card_header_layout, 2, 0, 1, 4)

        digit_rx = QRegularExpression(r"^[0-9]*$")
        self.card_no_input = QLineEdit()
        self.card_no_input.setPlaceholderText("카드번호 14~16자리 (- 제외)")
        self.card_no_input.setMaxLength(16)
        self.card_no_input.setValidator(QRegularExpressionValidator(digit_rx, self))

        self.card_valid_input = QLineEdit()
        self.card_valid_input.setPlaceholderText("MMYY (예: 0928)")
        self.card_valid_input.setMaxLength(4)
        self.card_valid_input.setValidator(QRegularExpressionValidator(digit_rx, self))

        self.card_pw_input = QLineEdit()
        self.card_pw_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.card_pw_input.setPlaceholderText("앞 2자리")
        self.card_pw_input.setMaxLength(2)
        self.card_pw_input.setValidator(QRegularExpressionValidator(digit_rx, self))

        self.card_req_input = QLineEdit()
        self.card_req_input.setPlaceholderText("생년월일 6자리 (YYMMDD)")
        self.card_req_input.setMaxLength(6)
        self.card_req_input.setValidator(QRegularExpressionValidator(digit_rx, self))

        pay_layout.addWidget(QLabel("카드번호:"), 3, 0)
        pay_layout.addWidget(self.card_no_input, 3, 1)
        pay_layout.addWidget(QLabel("유효기간:"), 3, 2)
        pay_layout.addWidget(self.card_valid_input, 3, 3)

        pay_layout.addWidget(QLabel("비밀번호(앞2자리):"), 4, 0)
        pay_layout.addWidget(self.card_pw_input, 4, 1)
        pay_layout.addWidget(QLabel("생년월일(6자리):"), 4, 2)
        pay_layout.addWidget(self.card_req_input, 4, 3)

        pay_layout.setColumnStretch(1, 1)
        pay_layout.setColumnStretch(3, 1)
        main_layout.addWidget(pay_group)

        # 5. 제어 버튼 바 (TrainMacro 스타일)
        ctrl_layout = QHBoxLayout()
        ctrl_layout.setSpacing(10)

        self.start_btn = QPushButton("매크로 시작")
        self.start_btn.setObjectName("startBtn")
        self.start_btn.setMinimumHeight(44)
        self.start_btn.clicked.connect(self._on_start_macro)

        self.stop_btn = QPushButton("매크로 중지")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setMinimumHeight(44)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop_macro)

        self.clear_btn = QPushButton("로그 지우기")
        self.clear_btn.setMinimumHeight(44)
        self.clear_btn.clicked.connect(self.clear_log)

        self.view_res_btn = QPushButton("예매 내역 조회")
        self.view_res_btn.setMinimumHeight(44)
        self.view_res_btn.clicked.connect(self._on_view_reservations)

        ctrl_layout.addWidget(self.start_btn, 2)
        ctrl_layout.addWidget(self.stop_btn, 2)
        ctrl_layout.addWidget(self.clear_btn, 1)
        ctrl_layout.addWidget(self.view_res_btn, 1)
        main_layout.addLayout(ctrl_layout)

        # 6. 작업 로그 영역
        main_layout.addWidget(QLabel("작업 로그:"))
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        main_layout.addWidget(self.log_area, 1)

        self.setCentralWidget(main_widget)

    # ----------------------------------------------------------------------
    # 스타일시트 (TrainMacro 라이트 룩앤필)
    # ----------------------------------------------------------------------
    def _apply_styles(self):
        style = """
        QMainWindow {
            background-color: #fafafa;
        }
        QGroupBox {
            font-weight: bold;
            border: 1px solid #d0d7de;
            border-radius: 6px;
            margin-top: 10px;
            padding: 14px 10px 10px 10px;
            background-color: #fafafa;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            subcontrol-position: top left;
            left: 10px;
            padding: 0 5px;
            color: #1a237e;
            background-color: #fafafa;
        }
        QLabel {
            color: #24292f;
            font-size: 12px;
        }
        QLineEdit, QComboBox, QDateEdit, QSpinBox {
            padding: 5px 8px;
            border: 1px solid #d0d7de;
            border-radius: 4px;
            background-color: #ffffff;
            color: #24292f;
            font-size: 12px;
        }
        QLineEdit {
            placeholder-text-color: #8c959f;
        }
        QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QSpinBox:focus {
            border: 1px solid #1a237e;
        }
        QPushButton {
            background-color: #1a237e;
            color: white;
            border: none;
            padding: 7px 14px;
            border-radius: 4px;
            font-weight: bold;
            font-size: 12px;
        }
        QPushButton:hover {
            background-color: #283593;
        }
        QPushButton:disabled {
            background-color: #b0bec5;
        }
        #startBtn {
            background-color: #2e7d32;
            font-size: 14px;
        }
        #startBtn:hover {
            background-color: #388e3c;
        }
        #startBtn:disabled {
            background-color: #cfd8dc;
            color: #78909c;
        }
        #stopBtn {
            background-color: #c62828;
            font-size: 14px;
        }
        #stopBtn:hover {
            background-color: #d32f2f;
        }
        #stopBtn:disabled {
            background-color: #cfd8dc;
            color: #78909c;
        }
        QTableWidget {
            border: 1px solid #d0d7de;
            border-radius: 4px;
            background-color: #ffffff;
            gridline-color: #e1e4e8;
            font-size: 12px;
        }
        QHeaderView::section {
            background-color: #f6f8fa;
            border: 1px solid #d0d7de;
            padding: 3px 6px;
            font-weight: bold;
            color: #24292f;
        }
        QTextEdit {
            border: 1px solid #d0d7de;
            border-radius: 6px;
            background-color: #ffffff;
            color: #1f2328;
            font-family: Consolas, 'Courier New', monospace;
            font-size: 12px;
            padding: 6px;
        }
        """
        self.setStyleSheet(style)

    # ----------------------------------------------------------------------
    # 로그 출력
    # ----------------------------------------------------------------------
    def log_msg(self, level: str, text: str):
        now_str = datetime.datetime.now().strftime("%H:%M:%S")
        colors = {
            "INFO": "#0277bd",
            "SUCCESS": "#2e7d32",
            "WARNING": "#ef6c00",
            "ERROR": "#c62828"
        }
        color = colors.get(level, "#24292f")
        self.log_area.moveCursor(QTextCursor.MoveOperation.End)
        self.log_area.insertHtml(
            f"<span style='color:#757575;'>[{now_str}]</span> "
            f"<b style='color:{color};'>[{level}]</b> "
            f"<span style='color:#24292f;'>{html.escape(str(text)).replace(chr(10), '<br>')}</span><br>"
        )
        self.log_area.moveCursor(QTextCursor.MoveOperation.End)

        if self.tg_remote and self.tg_remote.isRunning() and level in ["SUCCESS", "ERROR"]:
            self.tg_remote.send_log(level, text)

    def clear_log(self):
        self.log_area.clear()

    # ----------------------------------------------------------------------
    # 텔레그램 원격 제어 (TrainMacro 방식)
    # ----------------------------------------------------------------------
    def pair_telegram(self):
        """TrainMacro와 동일한 텔레그램 연동 및 원격 제어 키보드 활성화."""
        token = self.tg_token_input.text().strip()
        if self.util_worker and self.util_worker.isRunning():
            return  # 연동 확인이 진행 중
        if self.is_tg_paired and self.tg_remote and self.tg_remote.isRunning() and self.tg_remote.token == token:
            QMessageBox.information(self, "안내", "이미 텔레그램에 연동되어 설정이 저장완료된 상태입니다.")
            return

        if not token:
            QMessageBox.warning(self, "입력 오류", "텔레그램 Bot Token을 입력해 주세요.\n(도움말: @BotFather를 통해 생성)")
            return

        self.tg_pair_btn.setEnabled(False)
        self.tg_pair_btn.setText("연동 중...")

        settings = self._gather_settings_from_ui()
        settings["action"] = "test_telegram"
        settings["tg_token"] = token
        settings["tg_chat_id"] = self.tg_chat_id

        self.util_worker = EngineWorker(settings)
        self.util_worker.log_signal.connect(self.log_msg)
        self.util_worker.paired_signal.connect(self.on_pair_finished)
        self.util_worker.start()

    def on_pair_finished(self, success: bool, chat_id: str):
        if success:
            self.is_tg_paired = True
            self.tg_chat_id = chat_id
            self.tg_pair_btn.setText("연동 유지됨")
            self.tg_pair_btn.setEnabled(False)
            self.tg_token_input.setEchoMode(QLineEdit.EchoMode.Password)
            self._auto_save_settings()
            self._init_telegram_remote()
            QMessageBox.information(
                self,
                "연동 성공",
                "텔레그램 연동에 성공했습니다!\n모바일 텔레그램 채팅창에 대화형 원격 제어 키보드 메뉴가 활성화되었습니다."
            )
        else:
            self.tg_pair_btn.setEnabled(True)
            self.tg_pair_btn.setText("연동하기")
            QMessageBox.warning(
                self,
                "연동 실패",
                "텔레그램 연동에 실패했습니다.\n1) Bot Token이 정확한지 확인해 주세요.\n2) 텔레그램 앱에서 봇 채팅방에 아무 메시지(예: /start)나 먼저 전송해 주세요."
            )

    def _init_telegram_remote(self):
        token = self.tg_token_input.text().strip()
        chat_id = self.tg_chat_id
        if not token or not chat_id:
            return

        old = self.tg_remote if self.tg_remote and self.tg_remote.isRunning() else None
        if old:
            # 이전 연결 종료를 기다리며 화면을 멈추지 않고, 끝나면 새 연결을 시작합니다.
            old.stop()
            self._old_tg_remotes.append(old)

        self.tg_remote = TelegramRemoteManager(token, chat_id)
        self.tg_remote.set_current_settings(self._gather_settings_from_ui())
        self.tg_remote.request_start_macro.connect(self._on_start_macro)
        self.tg_remote.request_stop_macro.connect(self._on_stop_macro)
        self.tg_remote.request_status_check.connect(self._sync_telegram_status)
        self.tg_remote.request_journey_check.connect(self._on_telegram_query)
        self.tg_remote.request_update_setting.connect(self._on_telegram_setting_update)
        self.tg_remote.log_signal.connect(self.log_msg)
        self.tg_remote.set_port_options(self._all_ports, self._current_pairs)
        self.tg_remote.set_macro_running(bool(self.worker and self.worker.isRunning()))
        if old:
            new = self.tg_remote
            old.finished.connect(new.start)
            if not old.isRunning():
                new.start()  # connect 전에 이미 끝난 경우 (중복 start는 무시됨)
        else:
            self.tg_remote.start()
        self.is_tg_paired = True
        self.tg_pair_btn.setText("연동 유지됨")
        self.tg_token_input.setEchoMode(QLineEdit.EchoMode.Password)

    def _sync_telegram_status(self):
        if self.tg_remote:
            self.tg_remote.set_current_settings(self._gather_settings_from_ui())
            self.tg_remote.set_port_options(self._all_ports, self._current_pairs)
            if self.worker and self.worker.isRunning():
                self.tg_remote.set_search_count(self.worker.search_count)
            self.tg_remote.send_log("", self.tg_remote.format_settings_text(), html_mode=True)

    def _on_telegram_setting_update(self, key: str, value: object):
        if self.worker and self.worker.isRunning():
            self.tg_remote.send_log("WARNING", "매크로 실행 중에는 예매 설정을 변경할 수 없습니다.")
            return

        try:
            if key == "f_port":
                idx = self.dep_combo.findText(value["port"])
                if idx < 0:
                    raise ValueError("출발항을 찾을 수 없습니다.")
                # 도착항 목록을 받아 온 뒤 보고해야 텔레그램의 도착항 선택지가 최신이 됩니다.
                self._select_dep(idx, self._report_tg_settings)
                return
            elif key == "t_port":
                idx = self.arr_combo.findText(value["t_port"])
                if idx < 0:
                    raise ValueError("현재 출발항에서 선택할 수 없는 도착항입니다.")
                self.arr_combo.setCurrentIndex(idx)
            elif key == "swap_ports":
                old_dep, old_arr = self.dep_combo.currentText(), self.arr_combo.currentText()
                idx = self.dep_combo.findText(old_arr)
                if idx < 0:
                    raise ValueError("출발항과 도착항을 맞바꿀 수 없습니다.")
                self._select_dep(idx, lambda: (self._set_arr_by_name(old_dep), self._report_tg_settings()))
                return
            elif key == "date":
                date = QDate.fromString(str(value), "yyyy-MM-dd")
                if not date.isValid() or date < QDate.currentDate():
                    raise ValueError("오늘 이후의 유효한 날짜를 입력해 주세요.")
                self.date_edit.setDate(date)
            elif key in ("time_start", "time_end"):
                combo = self.time_start_combo if key == "time_start" else self.time_end_combo
                idx = combo.findText(str(value))
                if idx < 0:
                    raise ValueError("시간은 30분 단위로 입력해 주세요.")
                combo.setCurrentIndex(idx)
            elif key == "room_preference":
                self.room_pref_combo.setCurrentText(str(value))
            elif key == "refresh_interval":
                self.refresh_spin.setValue(int(value))
            elif key == "auto_pay":
                self.auto_pay_chk.setChecked(bool(value))
            elif key == "card_info":
                field, text = value
                inputs = {
                    "cardno": self.card_no_input,
                    "validdate": self.card_valid_input,
                    "password": self.card_pw_input,
                    "requestno": self.card_req_input,
                }
                inputs[field].setText(str(text))
            else:
                raise ValueError("지원하지 않는 설정입니다.")

            self._report_tg_settings()
        except (KeyError, TypeError, ValueError) as e:
            self.tg_remote.send_log("WARNING", f"설정 변경 실패: {e}")

    def _report_tg_settings(self):
        if not self.tg_remote:
            return
        self.tg_remote.set_current_settings(self._gather_settings_from_ui())
        self.tg_remote.set_port_options(self._all_ports, self._current_pairs)
        self.tg_remote.send_log("SUCCESS", f"예매 설정이 변경되었습니다.\n{self.tg_remote.format_settings_text()}",
                                html_mode=True, main_keyboard=False)

    # ----------------------------------------------------------------------
    # 설정 로드 & 실시간 자동 저장
    # ----------------------------------------------------------------------
    def _load_settings_to_ui(self):
        cfg = self.config
        self.id_input.setText(cfg.get("ksa_id", ""))
        self.pw_input.setText(cfg.get("ksa_pw", ""))
        self.tg_token_input.setText(cfg.get("tg_token", ""))
        self.tg_chat_id = cfg.get("tg_chat_id", "")
        if self.tg_chat_id and cfg.get("tg_token"):
            self.is_tg_paired = True
            self.tg_pair_btn.setText("연동 유지됨")
            self.tg_pair_btn.setEnabled(False)
            self.tg_token_input.setEchoMode(QLineEdit.EchoMode.Password)
        else:
            self.is_tg_paired = False
            self.tg_pair_btn.setText("연동하기")
            self.tg_pair_btn.setEnabled(True)

        if cfg.get("f_port"):
            idx = self.dep_combo.findText(cfg["f_port"])
            if idx >= 0:
                self.dep_combo.setCurrentIndex(idx)

        if cfg.get("t_port"):
            idx = self.arr_combo.findText(cfg["t_port"])
            if idx >= 0:
                self.arr_combo.setCurrentIndex(idx)

        if cfg.get("date"):
            try:
                d = QDate.fromString(cfg["date"], "yyyy-MM-dd")
                if d.isValid() and d >= QDate.currentDate():
                    self.date_edit.setDate(d)
            except Exception:
                pass

        if cfg.get("time_start"):
            idx = self.time_start_combo.findText(cfg["time_start"])
            if idx >= 0:
                self.time_start_combo.setCurrentIndex(idx)

        if cfg.get("time_end"):
            idx = self.time_end_combo.findText(cfg["time_end"])
            if idx >= 0:
                self.time_end_combo.setCurrentIndex(idx)

        if cfg.get("room_preference"):
            idx = self.room_pref_combo.findText(cfg["room_preference"])
            if idx >= 0:
                self.room_pref_combo.setCurrentIndex(idx)

        self.auto_pay_chk.setChecked(cfg.get("auto_pay", True))

        card = cfg.get("card_info", {})
        self.card_no_input.setText(card.get("cardno", ""))
        self.card_valid_input.setText(card.get("validdate", ""))
        self.card_pw_input.setText(card.get("password", ""))
        self.card_req_input.setText(card.get("requestno", ""))

        self.refresh_spin.setValue(int(cfg.get("refresh_interval", 2)))
        self._refresh_passenger_table()

    def _connect_setting_signals(self):
        self.id_input.editingFinished.connect(self._auto_save_settings)
        self.pw_input.editingFinished.connect(self._auto_save_settings)
        self.tg_token_input.editingFinished.connect(self._auto_save_settings)
        self.id_input.textChanged.connect(self._on_account_input_changed)
        self.pw_input.textChanged.connect(self._on_account_input_changed)
        self.tg_token_input.textChanged.connect(self._on_tg_token_changed)
        self.dep_combo.currentIndexChanged.connect(self._auto_save_settings)
        self.arr_combo.currentIndexChanged.connect(self._auto_save_settings)
        self.date_edit.dateChanged.connect(self._auto_save_settings)
        self.time_start_combo.currentIndexChanged.connect(self._auto_save_settings)
        self.time_end_combo.currentIndexChanged.connect(self._auto_save_settings)
        self.room_pref_combo.currentIndexChanged.connect(self._auto_save_settings)
        self.auto_pay_chk.stateChanged.connect(self._auto_save_settings)
        self.card_no_input.editingFinished.connect(self._auto_save_settings)
        self.card_valid_input.editingFinished.connect(self._auto_save_settings)
        self.card_pw_input.editingFinished.connect(self._auto_save_settings)
        self.card_req_input.editingFinished.connect(self._auto_save_settings)
        self.refresh_spin.valueChanged.connect(self._auto_save_settings)

    def _on_account_input_changed(self):
        self.login_test_btn.setEnabled(True)
        self.login_test_btn.setText("로그인 확인")
        self.login_status_lbl.setText("미로그인 (확인 필요)")
        self.login_status_lbl.setStyleSheet("color: #e65100; font-weight: bold;")

    def _on_tg_token_changed(self):
        self.tg_pair_btn.setEnabled(True)
        self.tg_pair_btn.setText("연동하기")
        self.is_tg_paired = False

    def _auto_save_settings(self):
        if self._is_loading_config or not self.arr_combo.isEnabled():
            return  # 도착항 로딩 중에는 출발/도착 짝이 어긋난 설정을 저장하지 않음 (로드 후 다시 저장됨)
        self._gather_settings_from_ui()

    def _gather_settings_from_ui(self) -> Dict[str, Any]:
        cur_dep = self.dep_combo.currentData() or {}
        cur_arr = self.arr_combo.currentData() or {}

        cfg = {
            "ksa_id": self.id_input.text().strip(),
            "ksa_pw": self.pw_input.text().strip(),
            "tg_token": self.tg_token_input.text().strip(),
            "tg_chat_id": self.tg_chat_id,
            "f_port": cur_dep.get("port", self.dep_combo.currentText().strip() or self.config.get("f_port", "")),
            "f_portid": cur_dep.get("portid", self.config.get("f_portid", "1010")),
            "f_portsubid": cur_dep.get("portsubid", self.config.get("f_portsubid", "0")),
            "t_port": cur_arr.get("t_port", self.arr_combo.currentText().strip() or self.config.get("t_port", "")),
            "t_portid": cur_arr.get("t_portid", self.config.get("t_portid", "1002")),
            "t_portsubid": cur_arr.get("t_portsubid", self.config.get("t_portsubid", "0")),
            "date": self.date_edit.date().toString("yyyy-MM-dd"),
            "time_start": self.time_start_combo.currentText(),
            "time_end": self.time_end_combo.currentText(),
            "room_preference": self.room_pref_combo.currentText(),
            "auto_pay": self.auto_pay_chk.isChecked(),
            "card_info": {
                "cardno": self.card_no_input.text().strip(),
                "validdate": self.card_valid_input.text().strip(),
                "password": self.card_pw_input.text().strip(),
                "requestno": self.card_req_input.text().strip(),
                "installment": "00"
            },
            "passengers": self.config.get("passengers", []),
            "refresh_interval": self.refresh_spin.value()
        }
        self.config = cfg
        save_config(cfg)
        self._report_keyring_warnings()
        return cfg

    def _report_keyring_warnings(self):
        """보안 저장소(keyring) 오류를 같은 내용은 한 번만 로그와 팝업으로 알립니다."""
        new = [w for w in pop_keyring_warnings() if w not in self._keyring_warned]
        if not new:
            return
        self._keyring_warned.update(new)
        text = "\n".join(new)
        self.log_msg("ERROR", f"보안 저장소 오류: {text}")
        QTimer.singleShot(0, lambda: QMessageBox.warning(
            self, "보안 저장소 오류",
            f"{text}\n\nWindows 자격 증명 관리자에 접근하지 못해 이 정보가 저장되지 않았습니다. "
            "다음 실행 때 다시 입력해야 할 수 있습니다."))

    # ----------------------------------------------------------------------
    # 백그라운드 작업 (네트워크 호출이 화면을 멈추지 않도록)
    # ----------------------------------------------------------------------
    def _run_bg(self, fn, on_done=None, on_error=None):
        future = self._bg.submit(fn)
        future.add_done_callback(lambda f: self._bg_finished.emit(f, on_done, on_error))

    def _on_bg_finished(self, future: Future, on_done, on_error):
        if future.cancelled() or getattr(self, "_closing", False):
            return  # 종료 중 취소·완료된 작업은 대화상자를 띄우지 않음
        try:
            result = future.result()
        except Exception as e:
            if on_error:
                on_error(e)
            else:
                self.log_msg("ERROR", f"작업 실패: {e}")
            return
        if on_done:
            try:
                on_done(result)
            except Exception as e:  # 슬롯 예외로 프로그램이 종료되지 않도록
                self.log_msg("ERROR", f"결과 처리 실패: {e}")

    def closeEvent(self, event):
        if getattr(self, "_closing", False):
            event.ignore()  # 진행 중 예매를 기다리는 동안 다시 닫기를 누른 경우
            return
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self, "매크로 실행 중",
                "매크로가 실행 중입니다. 중지하고 종료할까요?\n\n"
                "예매·결제가 진행 중이면 그 단계가 끝날 때까지 기다린 뒤 종료합니다.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            # 결제 도중 프로세스가 끊기면 결과를 알 수 없으므로 진행 중인 예매가 끝날 때까지 기다립니다.
            self._closing = True
            self.worker.stop()
            while not self.worker.wait(200):
                QApplication.processEvents()
        if self.util_worker and self.util_worker.isRunning():
            self.util_worker.stop()
            self.util_worker.wait(20000)
        self._closing = True  # 이후 도착하는 백그라운드 결과·시작 요청은 무시
        self._bg.shutdown(wait=False, cancel_futures=True)
        # 텔레그램 리스너가 종료 메시지와 함께 메뉴 키보드를 걷어낸 뒤 끝나도록 기다립니다.
        for remote in [*self._old_tg_remotes, self.tg_remote]:
            if remote and remote.isRunning():
                remote.stop()
                remote.wait(10000)
        super().closeEvent(event)

    # ----------------------------------------------------------------------
    # 카드 정보 관리
    # ----------------------------------------------------------------------
    def _on_clear_card_info(self):
        reply = QMessageBox.question(
            self,
            "카드 정보 삭제 확인",
            "기억된 신용카드 정보(카드번호, 유효기간, 비밀번호, 생년월일)를\n"
            "보안 저장소(Windows 자격 증명) 및 화면에서 즉시 영구 삭제하시겠습니까?\n\n"
            "삭제 후에는 프로그램을 재실행해도 해당 정보가 복원되지 않습니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._is_loading_config = True
            self.card_no_input.clear()
            self.card_valid_input.clear()
            self.card_pw_input.clear()
            self.card_req_input.clear()
            self._is_loading_config = False

            if "card_info" in self.config:
                self.config["card_info"] = {
                    "cardno": "",
                    "validdate": "",
                    "password": "",
                    "requestno": "",
                    "installment": "00"
                }
            clear_card_info()
            self._auto_save_settings()
            self.log_msg("INFO", "신용카드 정보가 보안 저장소 및 화면에서 완전히 영구 삭제되었습니다.")
            QMessageBox.information(self, "삭제 완료", "저장된 신용카드 정보가 안전하게 영구 삭제되었습니다.")

    # ----------------------------------------------------------------------
    # 승객 및 유아 관리 (여객 추가 / 유아 추가)
    # ----------------------------------------------------------------------
    def _refresh_passenger_table(self):
        passengers = self.config.get("passengers", [])
        self.pass_table.setRowCount(len(passengers))
        for i, p in enumerate(passengers):
            is_inf = (p.get("ticketid") == "5")
            desc = "👶 유아" if is_inf else f"여객({p.get('ticket_desc', '대인')})"
            self.pass_table.setItem(i, 0, QTableWidgetItem(desc))
            self.pass_table.setItem(i, 1, QTableWidgetItem(p.get("name", "")))
            self.pass_table.setItem(i, 2, QTableWidgetItem(p.get("idnumber", "")))
            self.pass_table.setItem(i, 3, QTableWidgetItem("남" if p.get("sex") == "M" else "여"))
            self.pass_table.setItem(i, 4, QTableWidgetItem(p.get("tel", "")))
            self.pass_table.setItem(i, 5, QTableWidgetItem(p.get("emtel", "")))

    def _on_add_passenger(self, is_infant: bool = False):
        default_p = {
            "name": "",
            "idnumber": "",
            "sex": "M",
            "tel": "",
            "ticketid": "5" if is_infant else "1",
            "dcid": "100"
        }
        dlg = PassengerDialog(default_p, is_infant=is_infant, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            passengers = self.config.get("passengers", [])
            passengers.append(dlg.passenger_data)
            self.config["passengers"] = passengers
            self._refresh_passenger_table()
            self._auto_save_settings()
            ptype = "유아" if is_infant else "여객"
            self.log_msg("INFO", f"{ptype} '{dlg.passenger_data['name']}' 등록 완료")

    def _on_edit_passenger(self, row: int, col: int = 0):
        passengers = self.config.get("passengers", [])
        if row < 0 or row >= len(passengers):
            return
        target = passengers[row]
        is_infant = target.get("ticketid") == "5"
        dlg = PassengerDialog(target, is_infant=is_infant, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            passengers[row] = dlg.passenger_data
            self.config["passengers"] = passengers
            self._refresh_passenger_table()
            self._auto_save_settings()
            ptype = "유아" if is_infant else "여객"
            self.log_msg("INFO", f"{ptype} '{dlg.passenger_data['name']}' 수정 완료")

    def _on_delete_passenger(self):
        row = self.pass_table.currentRow()
        passengers = self.config.get("passengers", [])
        if row < 0 or row >= len(passengers):
            QMessageBox.information(self, "선택 안내", "삭제할 승객 행을 먼저 선택해주세요.")
            return

        del_name = passengers[row].get("name", "승객")
        del passengers[row]
        self.config["passengers"] = passengers
        self._refresh_passenger_table()
        self._auto_save_settings()
        self.log_msg("INFO", f"승객 '{del_name}'이(가) 목록에서 삭제되었습니다.")

    # ----------------------------------------------------------------------
    # 자동 로그인 및 항구 초기 로드
    # ----------------------------------------------------------------------
    def _try_auto_login(self):
        user_id = self.id_input.text().strip()
        user_pw = self.pw_input.text().strip()
        if not user_id or not user_pw:
            self.log_msg("INFO", "저장된 로그인 계정 정보가 없습니다. 계정 정보를 입력해 주세요.")
            return

        self.log_msg("INFO", f"기억된 계정 정보로 자동 로그인 시도 중... ({user_id})")
        self.login_status_lbl.setText("로그인 확인 중...")
        self.login_test_btn.setEnabled(False)

        def done(res):
            name = res.get("member", "")
            self.login_status_lbl.setText(f"로그인 완료 ({name} 님)")
            self.login_status_lbl.setStyleSheet("color: #2e7d32; font-weight: bold;")
            self.login_test_btn.setText("로그인 완료")
            self.login_test_btn.setEnabled(False)
            self.log_msg("SUCCESS", f"자동 로그인 성공! {name} 님의 세션이 활성화되었습니다.")

        def failed(e):
            self.login_status_lbl.setText("로그인 확인 필요")
            self.login_status_lbl.setStyleSheet("color: #e65100; font-weight: bold;")
            self.login_test_btn.setText("로그인 확인")
            self.login_test_btn.setEnabled(True)
            self.log_msg("WARNING", f"자동 로그인 확인 필요: {e}")

        self._run_bg(lambda: self.engine.login(user_id, user_pw), done, failed)

    def _load_ports_initial(self):
        self.dep_combo.setEnabled(False)
        self.arr_combo.setEnabled(False)
        self._run_bg(self.engine.get_ports, self._fill_ports,
                     lambda e: self.log_msg("ERROR", f"항구 목록 로드 실패: {e}"))

    def _fill_ports(self, ports):
        self.dep_combo.setEnabled(True)
        try:
            self._all_ports = ports
            self.dep_combo.blockSignals(True)
            self.dep_combo.clear()
            saved_f_port = self.config.get("f_port", "인천")
            match_idx = 0
            for i, p in enumerate(self._all_ports):
                name = p.get("port", "")
                self.dep_combo.addItem(name, p)
                if name == saved_f_port:
                    match_idx = i

            if self._all_ports:
                self.dep_combo.setCurrentIndex(match_idx)
                self.dep_combo.blockSignals(False)
                # match_idx가 0인 경우에도 시그널 불발 없이 도착항 목록이 즉시 채워지도록 명시 호출
                self._on_dep_changed(match_idx)
                self.log_msg("INFO", f"전국 출발항 {len(self._all_ports)}개 목록을 불러왔습니다.")
                if self.tg_remote:
                    self.tg_remote.set_port_options(self._all_ports, self._current_pairs)
            else:
                self.dep_combo.blockSignals(False)
                self.log_msg("ERROR", "항구 목록을 불러오지 못했습니다. 인터넷 연결을 확인한 뒤 프로그램을 다시 실행해 주세요.")
        except Exception as e:
            self.dep_combo.blockSignals(False)
            self.log_msg("ERROR", f"항구 목록 로드 실패: {e}")

    def _on_dep_changed(self, idx: int, then=None):
        """출발항에 맞는 도착항 목록을 백그라운드로 불러옵니다. then은 목록을 채운 뒤 실행됩니다."""
        port_data = self.dep_combo.itemData(idx)
        if not port_data:
            return

        f_id = port_data.get("portid")
        f_sub = port_data.get("portsubid", "0")
        self.arr_combo.setEnabled(False)

        def done(pairs):
            if self.dep_combo.currentIndex() != idx:
                return  # 그사이 출발항이 다시 바뀜 (늦게 도착한 옛 결과 무시)
            self._fill_pairs(pairs)
            if then:
                then()

        def failed(e):
            self.arr_combo.clear()  # 이전 출발항의 도착항이 남아 잘못 짝지어지지 않도록
            self._current_pairs = []
            self.arr_combo.setEnabled(True)
            self.log_msg("ERROR", f"도착항 목록 로드 실패: {e}")

        self._run_bg(lambda: self.engine.get_pair_ports(f_id, f_sub), done, failed)

    def _fill_pairs(self, pairs):
        self.arr_combo.setEnabled(True)
        self._current_pairs = pairs
        self.arr_combo.blockSignals(True)
        self.arr_combo.clear()
        saved_t_port = self.config.get("t_port", "백령")
        match_idx = 0
        for i, p in enumerate(pairs):
            t_name = p.get("t_port", "")
            self.arr_combo.addItem(t_name, p)
            if t_name == saved_t_port:
                match_idx = i

        if pairs:
            self.arr_combo.setCurrentIndex(match_idx)
        self.arr_combo.blockSignals(False)
        if self.tg_remote:
            self.tg_remote.set_port_options(self._all_ports, self._current_pairs)
        self._auto_save_settings()

    def _on_swap_click(self):
        cur_dep_text = self.dep_combo.currentText()
        cur_arr_text = self.arr_combo.currentText()

        idx = self.dep_combo.findText(cur_arr_text)
        if idx >= 0:
            self._select_dep(idx, lambda: self._set_arr_by_name(cur_dep_text))

    def _select_dep(self, idx: int, then=None):
        """출발항을 바꾸고, 새 도착항 목록이 채워진 뒤 then을 실행합니다."""
        self.dep_combo.blockSignals(True)
        self.dep_combo.setCurrentIndex(idx)
        self.dep_combo.blockSignals(False)
        self._on_dep_changed(idx, then)

    def _set_arr_by_name(self, name: str):
        idx = self.arr_combo.findText(name)
        if idx >= 0:
            self.arr_combo.setCurrentIndex(idx)

    # ----------------------------------------------------------------------
    # 버튼 핸들러
    # ----------------------------------------------------------------------
    def _on_login_click(self):
        user_id = self.id_input.text().strip()
        pw = self.pw_input.text().strip()
        if not user_id or not pw:
            QMessageBox.warning(self, "입력 오류", "아이디와 비밀번호를 입력해주세요.")
            return

        self.login_test_btn.setEnabled(False)
        self.login_test_btn.setText("확인 중...")
        self.log_msg("INFO", f"KSA 로그인 시도 중... ({user_id})")

        def done(res):
            name = res.get("member", "")
            self.login_status_lbl.setText(f"로그인 완료 ({name} 님)")
            self.login_status_lbl.setStyleSheet("color: #2e7d32; font-weight: bold;")
            self.login_test_btn.setEnabled(False)
            self.login_test_btn.setText("로그인 완료")
            self.log_msg("SUCCESS", f"로그인 확인 성공! 환영합니다, {name} 님.")
            self._gather_settings_from_ui()
            QMessageBox.information(self, "로그인 성공", f"{name} 님 로그인되었습니다.\n해당 계정 정보는 다음 실행 시에도 기억됩니다.")

        def failed(e):
            self.login_status_lbl.setText("로그인 실패")
            self.login_status_lbl.setStyleSheet("color: #c62828; font-weight: bold;")
            self.login_test_btn.setEnabled(True)
            self.login_test_btn.setText("로그인 확인")
            self.log_msg("ERROR", f"로그인 실패: {e}")
            QMessageBox.critical(self, "로그인 실패", str(e))

        self._run_bg(lambda: self.engine.login(user_id, pw), done, failed)

    def _on_query_click(self, _checked=False, then=None):
        """여정을 백그라운드로 조회해 로그에 표시합니다. then(lines)은 조회 성공 후 실행됩니다."""
        cfg = self._gather_settings_from_ui()
        f_port = {"port": cfg["f_port"], "portid": cfg["f_portid"], "portsubid": cfg["f_portsubid"]}
        t_port = {"t_port": cfg["t_port"], "t_portid": cfg["t_portid"], "t_portsubid": cfg["t_portsubid"]}
        date_str = cfg["date"]
        t_start = cfg["time_start"]
        t_end = cfg["time_end"]

        if not f_port["portid"] or not t_port["t_portid"]:
            QMessageBox.warning(self, "선택 오류", "출발지와 도착지를 선택해주세요.")
            return

        self.log_msg("INFO", f"─── 실시간 여정 조회: {f_port['port']} → {t_port['t_port']} ({date_str}) ───")
        self.query_btn.setEnabled(False)

        def done(lines):
            self.query_btn.setEnabled(True)
            for line in lines:
                self.log_msg("INFO", line)
            if then:
                then(lines)

        def failed(e):
            self.query_btn.setEnabled(True)
            self.log_msg("ERROR", f"여정 조회 실패: {e}")

        self._run_bg(lambda: self.engine.initial_listing(f_port, t_port, date_str, t_start, t_end), done, failed)

    def _on_telegram_query(self):
        def send(lines):
            if self.tg_remote:
                self.tg_remote.send_log("INFO", "\n".join(lines) or "조회된 여정이 없습니다.")
        self._on_query_click(then=send)

    def _warn_start(self, title: str, text: str):
        """매크로 시작이 막힌 이유를 PC와 텔레그램 양쪽에 알립니다 (텔레그램에서 시작한 경우 대비)."""
        if self.tg_remote and self.tg_remote.isRunning():
            self.tg_remote.send_log("WARNING", f"매크로를 시작하지 못했습니다 - {title}\n{text}")
        QMessageBox.warning(self, title, text)

    def _on_start_macro(self):
        # 텔레그램에서 연달아 누르는 등 이미 실행 중이면 두 번째 워커를 만들지 않습니다 (중복 예매 방지).
        if getattr(self, "_closing", False):
            return
        if self.worker and self.worker.isRunning():
            if self.tg_remote and self.tg_remote.isRunning():
                self.tg_remote.send_log("INFO", "이미 매크로가 실행 중입니다.")
            return
        if not self.arr_combo.isEnabled() or not self.arr_combo.currentData():
            # 출발항을 바꾼 직후 도착항 목록을 불러오는 중이면 새 출발항과 옛 도착항이 짝지어질 수 있습니다.
            self._warn_start("도착항 확인 필요", "도착항 목록을 불러오는 중이거나 도착항이 선택되지 않았습니다. 잠시 후 다시 시작해 주세요.")
            return
        cfg = self._gather_settings_from_ui()
        if cfg["time_start"] > cfg["time_end"]:
            self._warn_start("조회 시간 오류", f"시작 시간({cfg['time_start']})이 종료 시간({cfg['time_end']})보다 늦습니다.")
            return

        if not cfg["ksa_id"] or not cfg["ksa_pw"]:
            self._warn_start("설정 필요", "KSA 로그인 아이디와 비밀번호를 입력해주세요.")
            return

        # 1. 승객 등록 여부 및 정보 유효성 전수 검사
        raw_passengers = cfg.get("passengers", [])
        valid_passengers = [
            p for p in raw_passengers
            if isinstance(p, dict) and p.get("name", "").strip() and p.get("idnumber", "").strip()
        ]
        if not valid_passengers:
            self._warn_start(
                "승객 정보 필요",
                "탑승할 승객 정보가 등록되지 않았습니다.\n\n"
                "[➕ 여객 추가] 또는 [👶 유아 추가] 버튼을 눌러 승객 정보를 먼저 등록해주세요."
            )
            return

        today = datetime.date.today()
        for idx, p in enumerate(valid_passengers, start=1):
            p_name = p.get("name", "").strip()
            p_birth = p.get("idnumber", "").strip().replace("-", "")
            p_tel = p.get("tel", "").strip().replace("-", "")
            p_ticketid = str(p.get("ticketid", "1"))

            if not p_name:
                self._warn_start("승객 정보 오류", f"{idx}번째 승객의 성명이 누락되었습니다.")
                return

            if len(p_birth) != 8 or not p_birth.isdigit():
                self._warn_start(
                    "생년월일 규격 오류",
                    f"승객 '{p_name}'님의 생년월일('{p_birth}')이 규격에 맞지 않습니다.\n\n"
                    "생년월일은 반드시 8자리 숫자(YYYYMMDD, 예: 19900101)여야 합니다.\n"
                    "승객 목록에서 해당 승객을 더블클릭하여 수정해주세요."
                )
                return

            try:
                y = int(p_birth[:4])
                m = int(p_birth[4:6])
                d = int(p_birth[6:8])
                b_date = datetime.date(y, m, d)
            except Exception:
                self._warn_start(
                    "생년월일 날짜 오류",
                    f"승객 '{p_name}'님의 생년월일('{p_birth}')은 달력에 존재하지 않는 날짜입니다.\n\n"
                    "승객 목록에서 해당 승객을 더블클릭하여 올바른 날짜로 수정해주세요."
                )
                return

            if b_date > today:
                self._warn_start(
                    "생년월일 오류",
                    f"승객 '{p_name}'님의 생년월일('{p_birth}')이 오늘 날짜보다 미래입니다."
                )
                return

            if y < 1900:
                self._warn_start(
                    "생년월일 오류",
                    f"승객 '{p_name}'님의 출생연도는 1900년 이후여야 합니다."
                )
                return

            age_years = today.year - b_date.year - ((today.month, today.day) < (b_date.month, b_date.day))
            if p_ticketid == "5" and age_years >= 2:
                self._warn_start(
                    "유아 연령 오류",
                    f"유아로 등록된 '{p_name}'님의 나이는 만 {age_years}세로, 만 2세 이상입니다.\n\n"
                    "유아(무임) 대상이 아니므로 [➕ 여객 추가]를 통해 일반 승객으로 다시 등록해주세요."
                )
                return

            if not p_tel or len(p_tel) < 9 or not p_tel.isdigit():
                self._warn_start(
                    "승객 연락처 오류",
                    f"승객 '{p_name}'님의 연락처('{p_tel}')가 올바르지 않습니다.\n"
                    "휴대전화번호를 숫자만 올바르게 입력해주세요."
                )
                return

        telephones = []
        for idx, p in enumerate(valid_passengers, 1):
            p_name = p.get("name", "")
            p_emtel = str(p.get("emtel", "")).strip()
            if not p_emtel or len(p_emtel) < 9 or not p_emtel.isdigit():
                self._warn_start(
                    "비상연락처 필요",
                    f"승객 '{p_name}'님의 비상연락처가 없습니다.\n"
                    "승객을 선택해 [수정]에서 비상연락처를 입력해 주세요. (KSA 예매에 필요합니다)")
                return
            telephones += [str(p.get("tel", "")).strip(), p_emtel]
        duplicates = {t for t in telephones if telephones.count(t) > 1}
        if duplicates:
            self._warn_start(
                "전화번호 중복",
                f"연락처·비상연락처가 중복되었습니다: {', '.join(sorted(duplicates))}\n"
                "KSA는 승객들의 모든 전화번호가 서로 달라야 예매를 받습니다.")
            return

        # 2. 카드 자동 결제 선택 시 카드 정보 엄격 검증
        if cfg.get("auto_pay", True):
            card = cfg.get("card_info", {})
            c_no = card.get("cardno", "").strip().replace("-", "")
            c_val = card.get("validdate", "").strip().replace("/", "")
            c_pw = card.get("password", "").strip()
            c_req = card.get("requestno", "").strip().replace("-", "")

            if not c_no or not c_val or not c_pw or not c_req:
                self._warn_start(
                    "카드 정보 필요",
                    "자동 결제를 사용하려면 카드번호, 유효기간(MMYY), 비밀번호 앞 2자리, 생년월일(YYMMDD 6자리)을 모두 입력해주세요."
                )
                return

            if len(c_no) < 14 or not c_no.isdigit():
                self._warn_start("카드번호 오류", "카드번호를 올바르게 입력해주세요 (숫자 14~16자리).")
                self.card_no_input.setFocus()
                return

            if len(c_val) != 4 or not c_val.isdigit():
                self._warn_start("유효기간 규격 오류", "카드 유효기간은 MMYY 형태의 4자리 숫자(예: 0928 - 9월 28년)여야 합니다.")
                self.card_valid_input.setFocus()
                return

            c_month = int(c_val[:2])
            if c_month < 1 or c_month > 12:
                self._warn_start("유효기간 월 오류", f"카드 유효기간의 월({c_val[:2]})이 올바르지 않습니다. (01~12월)")
                self.card_valid_input.setFocus()
                return

            if len(c_pw) != 2 or not c_pw.isdigit():
                self._warn_start("비밀번호 오류", "카드 비밀번호 앞 2자리를 숫자로 정확히 입력해주세요.")
                self.card_pw_input.setFocus()
                return

            if len(c_req) != 6 or not c_req.isdigit():
                self._warn_start(
                    "생년월일 규격 오류",
                    "카드 결제를 위한 생년월일은 반드시 6자리 숫자(YYMMDD, 예: 850101)여야 합니다."
                )
                self.card_req_input.setFocus()
                return

            req_m = int(c_req[2:4])
            req_d = int(c_req[4:6])
            if req_m < 1 or req_m > 12 or req_d < 1 or req_d > 31:
                self._warn_start(
                    "생년월일 날짜 오류",
                    f"카드 소유자 생년월일('{c_req}')의 월(01~12) 또는 일(01~31)이 올바르지 않습니다."
                )
                self.card_req_input.setFocus()
                return

        # 검증 통과된 승객 리스트 저장
        cfg["passengers"] = valid_passengers

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)

        if self.tg_remote:
            self.tg_remote.set_macro_running(True)

        self.worker = EngineWorker(copy.deepcopy(cfg))
        self.worker.log_signal.connect(self.log_msg)
        self.worker.success_signal.connect(self._on_booking_success)
        self.worker.warning_signal.connect(lambda text: QMessageBox.warning(self, "가상계좌 미지원 선사", text))
        self.worker.finished_signal.connect(self._on_worker_finished)
        self.worker.start()
        if self.tg_remote and self.tg_remote.isRunning():
            self.tg_remote.set_current_settings(cfg)
            self.tg_remote.send_log("SUCCESS", f"🚀 매크로를 시작했습니다.\n{self.tg_remote.format_settings_text()}",
                                    html_mode=True)

    def _on_stop_macro(self):
        if self.worker and self.worker.isRunning():
            self.log_msg("INFO", "매크로 중지 요청 중...")
            self.worker.stop()
            self.stop_btn.setEnabled(False)

    def _on_worker_finished(self):
        if self.worker and self.worker.isRunning():
            return  # 이전 워커의 늦은 종료 신호 (새 워커가 이미 실행 중)
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        if self.tg_remote:
            self.tg_remote.set_macro_running(False)
        self.log_msg("INFO", "매크로가 대기 상태로 전환되었습니다.")
        if self.tg_remote and self.tg_remote.isRunning():
            self.tg_remote.send_log("INFO", "⏹️ 매크로가 중지되어 대기 상태입니다.")

    def _on_booking_success(self, res: Dict[str, Any]):
        if res.get("status") == "SEAT_AVAILABLE":
            # 예약이 아니므로 예매 이력에 남기지 않습니다.
            QMessageBox.information(
                self, "빈자리 발견 (예약 안 됨)",
                f"빈자리를 발견했지만 예약하지 않았습니다.\n\n선박: {res.get('vessel', '')}\n"
                f"출발: {res.get('departure_time', '')}\n객실: {res.get('classes', '')} (잔여 {res.get('remaining', '')}석)\n\n"
                "이 운항편은 가상계좌 예약을 쓸 수 없어 좌석을 잡지 않았습니다.\n"
                "KSA 예매 페이지에서 서둘러 직접 예매하시거나, 자동 결제를 켜고 카드 정보를 입력하세요.")
            return
        va_reserved = res.get("status") == "VA_RESERVED"
        # 로컬 예매 이력에 영구 기록
        rec = {
            "ticketingkey": res.get("ticketingkey", ""),
            "cardgroupid": res.get("cardgroupid", ""),
            "vessel": res.get("vessel", ""),
            "departure_time": res.get("departure_time", ""),
            "route": f"{self.dep_combo.currentText()} → {self.arr_combo.currentText()}",
            "classes": res.get("classes", ""),
            "seats": res.get("seats", ""),
            "fare": res.get("fare", 0),
            "passengers": res.get("passengers", []),
            "booked_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "가상계좌 입금대기" if va_reserved else "예매/발권완료"
        }
        if va_reserved:
            rec.update({k: res.get(k, "") for k in ("rgroupid", "va_bank", "va_account", "va_amount", "va_expire")})
        save_booking_record(rec)

        vessel = res.get("vessel", "")
        seats = res.get("seats", "")
        fare = res.get("fare", 0)
        if va_reserved:
            msg = (f"가상계좌 예약이 완료되었습니다. 입금해야 예매가 확정됩니다.\n\n선박: {vessel}\n좌석: {seats}\n\n"
                   f"입금은행: {res.get('va_bank')}\n계좌번호: {res.get('va_account')}\n"
                   f"입금액: {int(res.get('va_amount') or 0):,}원\n입금기한: {res.get('va_expire')}\n\n"
                   "입금기한까지 미입금 시 예약은 자동 취소됩니다.")
            QMessageBox.information(self, "가상계좌 예약 완료", msg)
            return
        msg = f"축하합니다! 여객선 예매가 성공적으로 완료되었습니다!\n\n선박: {vessel}\n좌석: {seats}\n금액: {fare:,}원\n\n[예매 내역 조회] 버튼을 통해 발권 상세를 확인하실 수 있습니다."
        QMessageBox.information(self, "예매 성공!", msg)

    def _on_view_reservations(self):
        try:
            # 1. 로컬 예매 기록 로드
            history = load_booking_history()

            if not history:
                reply = QMessageBox.question(
                    self,
                    "예매 내역",
                    "이 PC에 저장된 예매 기록이 없습니다.\n\nKSA 공식 웹사이트의 [예매내역 조회] 페이지를 웹 브라우저로 여시겠습니까?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes
                )
                if reply == QMessageBox.StandardButton.Yes:
                    webbrowser.open("https://island.theksa.co.kr/page/payment_confirm")
                return

            dlg = QDialog(self)
            dlg.setWindowTitle("나의 여객선 예매 내역")
            dlg.resize(820, 420)
            layout = QVBoxLayout(dlg)

            info_lbl = QLabel(f"총 {len(history)}건의 예매 완료 내역이 있습니다. (KSA 공식 웹페이지에서도 실시간 확인 가능)")
            info_lbl.setStyleSheet("font-weight: bold; color: #1a237e; margin-bottom: 5px;")
            layout.addWidget(info_lbl)

            table = QTableWidget(len(history), 7)
            table.setHorizontalHeaderLabels(["티켓키/승인번호", "선박명", "출발일시", "구간", "좌석", "금액", "상태"])
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

            for i, r in enumerate(history):
                key_text = r.get("ticketingkey") or str(r.get("cardgroupid", ""))
                table.setItem(i, 0, QTableWidgetItem(str(key_text)))
                table.setItem(i, 1, QTableWidgetItem(str(r.get("vessel", ""))))
                table.setItem(i, 2, QTableWidgetItem(str(r.get("departure_time", ""))))
                table.setItem(i, 3, QTableWidgetItem(str(r.get("route", ""))))
                table.setItem(i, 4, QTableWidgetItem(str(r.get("seats", ""))))
                fare = r.get("fare", 0)
                table.setItem(i, 5, QTableWidgetItem(f"{fare:,}원" if isinstance(fare, (int, float)) else str(fare)))
                status = str(r.get("status", "예매완료"))
                if r.get("va_account"):
                    status += f" ({r.get('va_bank', '')} {r.get('va_account')}, 기한 {r.get('va_expire', '')})"
                table.setItem(i, 6, QTableWidgetItem(status))

            layout.addWidget(table)

            btn_box = QHBoxLayout()
            web_btn = QPushButton("🌐 KSA 공식 웹사이트에서 모바일 티켓 조회")
            web_btn.clicked.connect(lambda: webbrowser.open("https://island.theksa.co.kr/page/payment_confirm"))
            close_btn = QPushButton("닫기")
            close_btn.clicked.connect(dlg.accept)
            btn_box.addWidget(web_btn)
            btn_box.addStretch()
            btn_box.addWidget(close_btn)
            layout.addLayout(btn_box)

            dlg.exec()
        except Exception as e:
            QMessageBox.warning(self, "조회 실패", f"예매 내역 조회 실패: {e}")


def main():
    if sys.platform == "win32":
        import ctypes
        try:
            myappid = "KsaMacro.FerryBooking.Application.v1"
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        except Exception:
            pass

    app = QApplication(sys.argv)
    ico_file = resource_path("ship_icon.ico")
    if os.path.exists(ico_file):
        app.setWindowIcon(QIcon(ico_file))

    window = KsaMainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
