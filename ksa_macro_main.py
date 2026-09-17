# -*- coding: utf-8 -*-
"""KsaMacro — 한국해운조합(KSA) 여객선 예매 매크로 메인 GUI 애플리케이션.

TrainMacro의 검증된 아키텍처와 디자인을 계승하여:
1. 텔레그램 대화형 키보드 메뉴 버튼(원격 제어) 완벽 지원
2. 승객 등록 시 생년월일 8자리(YYYYMMDD) 정밀 지원
3. '여객 추가'와 '유아 추가'를 분리 지원하는 직관적인 승객 관리 시스템
4. 로그인 정보 영구 기억 및 시작 시 자동 로그인 복원
5. 깔끔하고 정갈한 표준 라이트 테마
"""

import datetime
import logging
import os
import sys
import webbrowser
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QDate, QRegularExpression, QSize, Qt, QTimer, pyqtSlot
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

from config_manager import clear_card_info, load_config, save_config, save_booking_record, load_booking_history
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
logger = logging.getLogger(__name__)

VERSION = "1.0.0"


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
        self.resize(380, 270)
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

        self.passenger_data["name"] = name
        self.passenger_data["idnumber"] = birth  # KSA에서는 idnumber에 8자리 생년월일 전송
        self.passenger_data["sex"] = "M" if self.sex_combo.currentIndex() == 0 else "F"
        self.passenger_data["tel"] = tel

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
        self.pass_table = QTableWidget(0, 5)
        self.pass_table.setHorizontalHeaderLabels(["구분", "성명", "생년월일(8자리)", "성별", "연락처"])
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
        self.card_no_input.setPlaceholderText("카드번호 15~16자리 (- 제외)")
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
            f"<span style='color:#24292f;'>{text}</span><br>"
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
        if self.is_tg_paired and self.tg_remote and self.tg_remote.isRunning():
            QMessageBox.information(self, "안내", "이미 텔레그램에 연동되어 설정이 저장완료된 상태입니다.")
            return

        token = self.tg_token_input.text().strip()
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

        if self.tg_remote and self.tg_remote.isRunning():
            self.tg_remote.stop()
            self.tg_remote.wait()

        self.tg_remote = TelegramRemoteManager(token, chat_id)
        self.tg_remote.set_current_settings(self._gather_settings_from_ui())
        self.tg_remote.request_start_macro.connect(self._on_start_macro)
        self.tg_remote.request_stop_macro.connect(self._on_stop_macro)
        self.tg_remote.request_status_check.connect(self._sync_telegram_status)
        self.tg_remote.log_signal.connect(self.log_msg)
        self.tg_remote.start()
        self.is_tg_paired = True
        self.tg_pair_btn.setText("연동 유지됨")
        self.tg_token_input.setEchoMode(QLineEdit.EchoMode.Password)

    def _sync_telegram_status(self):
        if self.tg_remote:
            self.tg_remote.set_current_settings(self._gather_settings_from_ui())
            if self.worker and self.worker.isRunning():
                self.tg_remote.set_search_count(self.worker.search_count)

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
        if self._is_loading_config:
            return
        self._gather_settings_from_ui()

    def _gather_settings_from_ui(self) -> Dict[str, Any]:
        cur_dep = self.dep_combo.currentData() or {}
        cur_arr = self.arr_combo.currentData() or {}

        cfg = {
            "ksa_id": self.id_input.text().strip(),
            "ksa_pw": self.pw_input.text().strip(),
            "tg_token": self.tg_token_input.text().strip(),
            "tg_chat_id": self.tg_chat_id,
            "f_port": cur_dep.get("port", self.dep_combo.currentText().strip()),
            "f_portid": cur_dep.get("portid", self.config.get("f_portid", "1010")),
            "f_portsubid": cur_dep.get("portsubid", self.config.get("f_portsubid", "0")),
            "t_port": cur_arr.get("t_port", self.arr_combo.currentText().strip()),
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
        return cfg

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
            self.log_msg("SUCCESS", f"{ptype} '{dlg.passenger_data['name']}'(생년월일: {dlg.passenger_data['idnumber']}) 등록 완료")

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
            self.log_msg("SUCCESS", f"{ptype} '{dlg.passenger_data['name']}'(생년월일: {dlg.passenger_data['idnumber']}) 수정 완료")

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
        try:
            res = self.engine.login(user_id, user_pw)
            name = res.get("member", "")
            self.login_status_lbl.setText(f"로그인 완료 ({name} 님)")
            self.login_status_lbl.setStyleSheet("color: #2e7d32; font-weight: bold;")
            self.login_test_btn.setText("로그인 완료")
            self.login_test_btn.setEnabled(False)
            self.log_msg("SUCCESS", f"자동 로그인 성공! {name} 님의 세션이 활성화되었습니다.")
        except Exception as e:
            self.login_status_lbl.setText("로그인 확인 필요")
            self.login_status_lbl.setStyleSheet("color: #e65100; font-weight: bold;")
            self.login_test_btn.setText("로그인 확인")
            self.login_test_btn.setEnabled(True)
            self.log_msg("WARNING", f"자동 로그인 확인 필요: {e}")

    def _load_ports_initial(self):
        try:
            self._all_ports = self.engine.get_ports()
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
            else:
                self.dep_combo.blockSignals(False)
        except Exception as e:
            self.dep_combo.blockSignals(False)
            self.log_msg("ERROR", f"항구 목록 로드 실패: {e}")

    def _on_dep_changed(self, idx: int):
        port_data = self.dep_combo.itemData(idx)
        if not port_data:
            return

        f_id = port_data.get("portid")
        f_sub = port_data.get("portsubid", "0")

        pairs = self.engine.get_pair_ports(f_id, f_sub)
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

    def _on_swap_click(self):
        cur_dep_text = self.dep_combo.currentText()
        cur_arr_text = self.arr_combo.currentText()

        idx = self.dep_combo.findText(cur_arr_text)
        if idx >= 0:
            self.dep_combo.setCurrentIndex(idx)
            QTimer.singleShot(200, lambda: self._set_arr_by_name(cur_dep_text))

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

        login_success = False
        try:
            res = self.engine.login(user_id, pw)
            name = res.get("member", "")
            self.login_status_lbl.setText(f"로그인 완료 ({name} 님)")
            self.login_status_lbl.setStyleSheet("color: #2e7d32; font-weight: bold;")
            self.log_msg("SUCCESS", f"로그인 확인 성공! 환영합니다, {name} 님.")
            QMessageBox.information(self, "로그인 성공", f"{name} 님 로그인되었습니다.\n해당 계정 정보는 다음 실행 시에도 기억됩니다.")
            self._gather_settings_from_ui()
            login_success = True
        except Exception as e:
            self.login_status_lbl.setText("로그인 실패")
            self.login_status_lbl.setStyleSheet("color: #c62828; font-weight: bold;")
            self.log_msg("ERROR", f"로그인 실패: {e}")
            QMessageBox.critical(self, "로그인 실패", str(e))
        finally:
            if login_success:
                self.login_test_btn.setEnabled(False)
                self.login_test_btn.setText("로그인 완료")
            else:
                self.login_test_btn.setEnabled(True)
                self.login_test_btn.setText("로그인 확인")

    def _on_query_click(self):
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
        try:
            lines = self.engine.initial_listing(f_port, t_port, date_str, t_start, t_end)
            for line in lines:
                self.log_msg("INFO", line)
        except Exception as e:
            self.log_msg("ERROR", f"여정 조회 실패: {e}")

    def _on_start_macro(self):
        cfg = self._gather_settings_from_ui()

        if not cfg["ksa_id"] or not cfg["ksa_pw"]:
            QMessageBox.warning(self, "설정 필요", "KSA 로그인 아이디와 비밀번호를 입력해주세요.")
            return

        # 1. 승객 등록 여부 및 정보 유효성 전수 검사
        raw_passengers = cfg.get("passengers", [])
        valid_passengers = [
            p for p in raw_passengers
            if isinstance(p, dict) and p.get("name", "").strip() and p.get("idnumber", "").strip()
        ]
        if not valid_passengers:
            QMessageBox.warning(
                self,
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
                QMessageBox.warning(self, "승객 정보 오류", f"{idx}번째 승객의 성명이 누락되었습니다.")
                return

            if len(p_birth) != 8 or not p_birth.isdigit():
                QMessageBox.warning(
                    self,
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
                QMessageBox.warning(
                    self,
                    "생년월일 날짜 오류",
                    f"승객 '{p_name}'님의 생년월일('{p_birth}')은 달력에 존재하지 않는 날짜입니다.\n\n"
                    "승객 목록에서 해당 승객을 더블클릭하여 올바른 날짜로 수정해주세요."
                )
                return

            if b_date > today:
                QMessageBox.warning(
                    self,
                    "생년월일 오류",
                    f"승객 '{p_name}'님의 생년월일('{p_birth}')이 오늘 날짜보다 미래입니다."
                )
                return

            if y < 1900:
                QMessageBox.warning(
                    self,
                    "생년월일 오류",
                    f"승객 '{p_name}'님의 출생연도는 1900년 이후여야 합니다."
                )
                return

            age_years = today.year - b_date.year - ((today.month, today.day) < (b_date.month, b_date.day))
            if p_ticketid == "5" and age_years >= 2:
                QMessageBox.warning(
                    self,
                    "유아 연령 오류",
                    f"유아로 등록된 '{p_name}'님의 나이는 만 {age_years}세로, 만 2세 이상입니다.\n\n"
                    "유아(무임) 대상이 아니므로 [➕ 여객 추가]를 통해 일반 승객으로 다시 등록해주세요."
                )
                return

            if not p_tel or len(p_tel) < 9 or not p_tel.isdigit():
                QMessageBox.warning(
                    self,
                    "승객 연락처 오류",
                    f"승객 '{p_name}'님의 연락처('{p_tel}')가 올바르지 않습니다.\n"
                    "휴대전화번호를 숫자만 올바르게 입력해주세요."
                )
                return

        # 2. 카드 자동 결제 선택 시 카드 정보 엄격 검증
        if cfg.get("auto_pay", True):
            card = cfg.get("card_info", {})
            c_no = card.get("cardno", "").strip().replace("-", "")
            c_val = card.get("validdate", "").strip().replace("/", "")
            c_pw = card.get("password", "").strip()
            c_req = card.get("requestno", "").strip().replace("-", "")

            if not c_no or not c_val or not c_pw or not c_req:
                QMessageBox.warning(
                    self,
                    "카드 정보 필요",
                    "자동 결제를 사용하려면 카드번호, 유효기간(MMYY), 비밀번호 앞 2자리, 생년월일(YYMMDD 6자리)을 모두 입력해주세요."
                )
                return

            if len(c_no) < 14 or not c_no.isdigit():
                QMessageBox.warning(self, "카드번호 오류", "카드번호를 올바르게 입력해주세요 (숫자 14~16자리).")
                self.card_no_input.setFocus()
                return

            if len(c_val) != 4 or not c_val.isdigit():
                QMessageBox.warning(self, "유효기간 규격 오류", "카드 유효기간은 MMYY 형태의 4자리 숫자(예: 0928 - 9월 28년)여야 합니다.")
                self.card_valid_input.setFocus()
                return

            c_month = int(c_val[:2])
            if c_month < 1 or c_month > 12:
                QMessageBox.warning(self, "유효기간 월 오류", f"카드 유효기간의 월({c_val[:2]})이 올바르지 않습니다. (01~12월)")
                self.card_valid_input.setFocus()
                return

            if len(c_pw) != 2 or not c_pw.isdigit():
                QMessageBox.warning(self, "비밀번호 오류", "카드 비밀번호 앞 2자리를 숫자로 정확히 입력해주세요.")
                self.card_pw_input.setFocus()
                return

            if len(c_req) != 6 or not c_req.isdigit():
                QMessageBox.warning(
                    self,
                    "생년월일 규격 오류",
                    "카드 결제를 위한 생년월일은 반드시 6자리 숫자(YYMMDD, 예: 850101)여야 합니다."
                )
                self.card_req_input.setFocus()
                return

            req_m = int(c_req[2:4])
            req_d = int(c_req[4:6])
            if req_m < 1 or req_m > 12 or req_d < 1 or req_d > 31:
                QMessageBox.warning(
                    self,
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

        self.worker = EngineWorker(cfg)
        self.worker.log_signal.connect(self.log_msg)
        self.worker.success_signal.connect(self._on_booking_success)
        self.worker.finished_signal.connect(self._on_worker_finished)
        self.worker.start()

    def _on_stop_macro(self):
        if self.worker and self.worker.isRunning():
            self.log_msg("INFO", "매크로 중지 요청 중...")
            self.worker.stop()
            self.stop_btn.setEnabled(False)

    def _on_worker_finished(self):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        if self.tg_remote:
            self.tg_remote.set_macro_running(False)
        self.log_msg("INFO", "매크로가 대기 상태로 전환되었습니다.")

    def _on_booking_success(self, res: Dict[str, Any]):
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
            "status": "예매/발권완료"
        }
        save_booking_record(rec)

        vessel = res.get("vessel", "")
        seats = res.get("seats", "")
        fare = res.get("fare", 0)
        msg = f"축하합니다! 여객선 예매가 성공적으로 완료되었습니다!\n\n선박: {vessel}\n좌석: {seats}\n금액: {fare:,}원\n\n[예매 내역 조회] 버튼을 통해 발권 상세를 확인하실 수 있습니다."
        QMessageBox.information(self, "예매 성공!", msg)

    def _on_view_reservations(self):
        try:
            # 1. 로컬 예매 기록 로드
            history = load_booking_history()

            # 2. KSA 서버 예매 조회 시도
            server_res = []
            try:
                server_res = self.engine.get_reservations()
            except Exception:
                pass

            if not history and not server_res:
                reply = QMessageBox.question(
                    self,
                    "예매 내역",
                    "조회된 예매 내역이 없습니다.\n\nKSA 공식 웹사이트의 [예매내역 조회] 페이지를 웹 브라우저로 여시겠습니까?",
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
                table.setItem(i, 6, QTableWidgetItem(str(r.get("status", "예매완료"))))

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
