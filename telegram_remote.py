# -*- coding: utf-8 -*-
"""KsaMacro 텔레그램 원격 제어 및 알림 매니저.

python-telegram-bot 라이브러리를 기반으로,
모바일 텔레그램 채팅창에 대화형 키보드 메뉴 버튼을 생성하여
스마트폰에서 매크로 시작/중지, 상태 확인, 설정 조회를 원격으로 제어할 수 있습니다.
"""

import asyncio
import datetime
import html
import logging
import threading
import time
from typing import Any, Dict, Optional

from PyQt6.QtCore import QThread, pyqtSignal
import requests
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)

MAIN_KEYBOARD = [
    ["▶️ 매크로 시작", "⏹️ 매크로 중지"],
    ["🔍 현재 설정 확인", "🚢 여정 즉시 조회"],
    ["⚙️ 예매 설정"],
]

SETTINGS_KEYBOARD = [
    ["🚢 출발항", "🏁 도착항"],
    ["🔄 출·도착항 맞바꾸기"],
    ["📅 날짜", "🕐 시작 시간", "🕙 종료 시간"],
    ["💺 객실 유형", "⏱️ 조회 간격"],
    ["💳 자동 결제", "🪪 카드 정보"],
    ["↩️ 주 메뉴"],
]

# 버튼 문구 -> (card_info 필드, 입력 안내)
CARD_FIELDS = {
    "카드번호": ("cardno", "카드번호 14~16자리를 숫자만 입력해 주세요."),
    "유효기간(MMYY)": ("validdate", "카드 유효기간을 MMYY 4자리로 입력해 주세요. (예: 0928)"),
    "비밀번호 앞 2자리": ("password", "카드 비밀번호 앞 2자리를 입력해 주세요."),
    "생년월일(YYMMDD)": ("requestno", "카드 소유자 생년월일을 YYMMDD 6자리로 입력해 주세요."),
}
CARD_KEYBOARD = [
    ["카드번호", "유효기간(MMYY)"],
    ["비밀번호 앞 2자리", "생년월일(YYMMDD)"],
    ["↩️ 설정 메뉴"],
]


def validate_card_field(field: str, text: str) -> Optional[str]:
    """카드 입력값을 정규화해 반환하고, 형식이 틀리면 None을 반환합니다."""
    value = text.replace("-", "").replace(" ", "")
    if not value.isdigit():
        return None
    if field == "cardno":
        return value if 14 <= len(value) <= 16 else None
    if field == "validdate":
        return value if len(value) == 4 and 1 <= int(value[:2]) <= 12 else None
    if field == "password":
        return value if len(value) == 2 else None
    if field == "requestno":
        try:
            datetime.datetime.strptime(value, "%y%m%d")
        except ValueError:
            return None
        return value if len(value) == 6 else None
    return None


class TelegramBot:
    """단순 메시지 전송 및 Chat ID 확인용 클라이언트."""

    def __init__(self, token: Optional[str], chat_id: Optional[str]):
        self.token = token or ""
        self.chat_id = chat_id or ""
        self.last_error = ""

    def is_configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send_message(self, text: str) -> bool:
        if not self.token or not self.chat_id:
            return False
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML"
        }
        try:
            res = requests.post(url, json=payload, timeout=7)
            return res.status_code == 200
        except Exception as e:
            logger.error(f"텔레그램 전송 오류: {str(e).replace(self.token, '***')}")
            return False

    def get_last_chat_id(self) -> Optional[str]:
        if not self.token:
            return None
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        try:
            res = requests.get(url, timeout=7)
            if res.status_code == 200:
                data = res.json()
                if data.get("ok") and data.get("result"):
                    chats = {str(u["message"]["chat"]["id"]) for u in data["result"] if "message" in u}
                    if len(chats) > 1:
                        # 여러 사람이 봇에 말을 건 상태면 누가 주인인지 알 수 없으므로 연동하지 않습니다.
                        self.last_error = ("봇 대화 기록에 여러 사람의 메시지가 있어 자동 연동을 거부했습니다. "
                                           "새 봇을 만들거나 24시간 뒤 본인만 /start를 보내고 다시 연동하세요.")
                        logger.error(self.last_error)
                        return None
                    if chats:
                        return chats.pop()
            return None
        except Exception as e:
            logger.error(f"텔레그램 업데이트 확인 오류: {str(e).replace(self.token, '***')}")
            return None


class TelegramRemoteManager(QThread):
    """텔레그램 봇 기반 원격 제어 매니저 (TrainMacro 방식 키보드 메뉴)."""

    request_start_macro = pyqtSignal()
    request_stop_macro = pyqtSignal()
    request_status_check = pyqtSignal()
    request_journey_check = pyqtSignal()
    request_update_setting = pyqtSignal(str, object)
    log_signal = pyqtSignal(str, str)

    def __init__(self, token: str, chat_id: str, parent=None):
        super().__init__(parent)
        self.token = token
        self.chat_id = chat_id
        self.app = None
        self.loop = None
        self.is_running = True
        self.is_macro_running = False
        self.current_settings: Dict[str, Any] = {}
        self.port_options = []
        self.pair_options = []
        self.search_count = 0
        self.is_connected = True
        self._live = False  # 폴링 세션이 실제로 동작 중인지 (재연결 대기 중에는 False)

    def set_current_settings(self, settings: Dict[str, Any]):
        self.current_settings = settings

    def set_port_options(self, ports, pairs):
        self.port_options = list(ports)
        self.pair_options = list(pairs)

    def set_macro_running(self, running: bool):
        self.is_macro_running = running
        if not running:
            self.search_count = 0

    def set_search_count(self, count: int):
        self.search_count = count

    def ping(self):
        pass

    def stop(self):
        self.is_running = False

    def format_settings_text(self) -> str:
        s = {k: (html.escape(v) if isinstance(v, str) else v) for k, v in self.current_settings.items()}
        status_str = f"🚀 매크로 동작 중 ({self.search_count}회 조회)" if self.is_macro_running else "⏹️ 매크로 대기 중"
        passengers = s.get("passengers", [])
        p_names = [f"{html.escape(str(p.get('name')))}({'유아' if p.get('ticketid') == '5' else '여객'})"
                   for p in passengers if isinstance(p, dict) and p.get('name')]
        p_desc = ", ".join(p_names) if p_names else "미등록"
        cardno = str((s.get("card_info") or {}).get("cardno", ""))
        card_desc = f"****{cardno[-4:]}" if cardno else "미등록"

        return (
            f"<b>[현재 KsaMacro 상태]</b>\n"
            f"상태: {status_str}\n"
            f"구간: 🚢 {s.get('f_port', '')} → {s.get('t_port', '')}\n"
            f"날짜: 📅 {s.get('date', '')} ({s.get('time_start', '')} ~ {s.get('time_end', '')})\n"
            f"객실: 💺 {s.get('room_preference', '')}\n"
            f"승객: 👥 {p_desc}\n"
            f"자동결제: {'켜짐' if s.get('auto_pay') else '꺼짐(가상계좌 예약)'}\n"
            f"카드: 💳 {card_desc}\n"
            f"조회주기: ⏱️ {s.get('refresh_interval', 2)}초"
        )

    def send_log(self, level: str, text: str, html_mode: bool = False, main_keyboard: bool = True):
        """GUI 스레드에서 호출. html_mode면 text를 HTML로 보냅니다(호출자가 이스케이프 책임).

        main_keyboard=False면 현재 보이는 키보드(예: 설정 메뉴)를 유지합니다.
        """
        if not self.is_running:
            return
        prefix = f"[{level}] " if level else ""
        if not self._live or not self.loop or not self.app:
            # 재연결 대기 중에는 이벤트 루프가 멈춰 있어 메시지가 쌓였다가 늦게 도착합니다.
            # 텔레그램 HTTP API로 바로 보냅니다 (메뉴 키보드는 붙이지 않음).
            body = html.escape(prefix) + text if html_mode else html.escape(prefix + text)
            threading.Thread(target=TelegramBot(self.token, self.chat_id).send_message, args=(body,), daemon=True).start()
            return
        if html_mode:
            prefix = html.escape(prefix)
        asyncio.run_coroutine_threadsafe(
            self._do_send_message(prefix + text, "HTML" if html_mode else None, main_keyboard), self.loop)

    async def _do_send_message(self, text: str, parse_mode: Optional[str] = None, main_keyboard: bool = True):
        try:
            if self.app and self.app.bot and self.chat_id:
                await self.app.bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                    parse_mode=parse_mode,
                    reply_markup=ReplyKeyboardMarkup(MAIN_KEYBOARD, resize_keyboard=True) if main_keyboard else None
                )
        except Exception as e:
            logger.error(f"메시지 전송 실패: {e}")

    def run(self):
        if not self.token:
            return

        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        backoff = 5
        while self.is_running:
            try:
                self.loop.run_until_complete(self._run_bot_session())
                backoff = 5
            except Exception as e:
                err = str(e).replace(self.token, "***") if self.token else str(e)  # 오류 문구에 토큰이 들어감
                logger.error(f"Telegram bot session error: {err}")
                self.log_signal.emit("WARNING", f"텔레그램 연결 오류: {err} ({backoff}초 후 재시도)")
                for _ in range(backoff):
                    if not self.is_running:
                        break
                    time.sleep(1)
                backoff = min(backoff * 2, 60)

    async def _run_bot_session(self):
        app = Application.builder().token(self.token).build()
        try:
            await self._serve(app)
        finally:
            self._live = False
            # 초기화 도중 실패한 세션도 자원을 정리합니다.
            for step in (lambda: app.updater.stop() if app.updater and app.updater.running else None,
                         lambda: app.stop() if app.running else None,
                         app.shutdown):
                try:
                    coro = step()
                    if coro is not None:
                        await coro
                except Exception as e:
                    logger.error(f"텔레그램 세션 정리 실패: {e}")

    async def _serve(self, app):
        self.app = app

        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("help", self.cmd_start))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()
        self._live = True

        self.log_signal.emit("SUCCESS", "텔레그램 원격 제어 리스너 가동 완료 (모바일 메뉴 활성화)")
        await self._send_startup_message()

        while self.is_running:
            await asyncio.sleep(1)

        try:
            await self.app.bot.send_message(
                chat_id=self.chat_id,
                text="⚓ KsaMacro 프로그램이 종료되어 원격 메뉴를 닫습니다.",
                reply_markup=ReplyKeyboardRemove()
            )
        except Exception as e:
            logger.error(f"종료 메시지 전송 실패: {e}")

    async def _send_startup_message(self):
        status_text = self.format_settings_text()
        try:
            await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=f"⚓ <b>KsaMacro 원격 제어가 활성화되었습니다.</b>\n\n{status_text}",
                reply_markup=ReplyKeyboardMarkup(MAIN_KEYBOARD, resize_keyboard=True),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"시작 메시지 전송 실패: {e}")

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_chat.id) != str(self.chat_id):
            await update.message.reply_text("권한이 없는 사용자입니다.")
            return

        await update.message.reply_text(
            "⚓ <b>KsaMacro 여객선 원격 제어 봇</b>\n아래 메뉴 버튼을 눌러 원격 제어하세요.",
            reply_markup=ReplyKeyboardMarkup(MAIN_KEYBOARD, resize_keyboard=True),
            parse_mode="HTML"
        )

    async def _handle_setting_input(self, update: Update, context, text: str) -> bool:
        waiting_for = context.user_data.get("waiting_for") if context else None
        if not waiting_for:
            return False
        if waiting_for.startswith("card:"):
            # 카드 정보가 채팅 기록에 남지 않도록 어떤 경우든 사용자 메시지를 먼저 지웁니다.
            try:
                await update.message.delete()
            except Exception:
                pass
        if self.is_macro_running:
            context.user_data.pop("waiting_for", None)
            await update.message.reply_text("⚠️ 매크로 실행 중에는 예매 설정을 변경할 수 없습니다.")
            return True

        if waiting_for.startswith("card:"):
            field = waiting_for[len("card:"):]
            value = validate_card_field(field, text)
            if value is None:
                prompt = next(p for f, p in CARD_FIELDS.values() if f == field)
                await update.message.reply_text(f"형식이 올바르지 않습니다. {prompt}")
                return True
            context.user_data.pop("waiting_for", None)
            self.request_update_setting.emit("card_info", (field, value))
            await update.message.reply_text("카드 정보 변경을 PC에 전달했습니다. (입력 메시지는 삭제했습니다)", reply_markup=ReplyKeyboardMarkup(CARD_KEYBOARD, resize_keyboard=True))
            return True

        if waiting_for == "f_port_search":
            matches = [p for p in self.port_options if text.casefold() in p.get("port", "").casefold()][:10]
            if not matches:
                await update.message.reply_text("일치하는 출발항이 없습니다. 다른 검색어를 입력해 주세요.")
                return True
            context.user_data["waiting_for"] = "f_port_choice"
            context.user_data["port_candidates"] = matches
            keyboard = [[p.get("port", "")] for p in matches] + [["↩️ 설정 메뉴"]]
            await update.message.reply_text("출발항을 선택해 주세요.", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
            return True

        if waiting_for == "f_port_choice":
            match = next((p for p in context.user_data.get("port_candidates", []) if p.get("port") == text), None)
            if not match:
                await update.message.reply_text("버튼에서 출발항을 선택해 주세요.")
                return True
            value = match
            key = "f_port"
        elif waiting_for == "t_port":
            match = next((p for p in self.pair_options if p.get("t_port") == text), None)
            if not match:
                await update.message.reply_text("버튼에서 도착항을 선택해 주세요.")
                return True
            value = match
            key = "t_port"
        elif waiting_for == "date":
            try:
                value = datetime.date.fromisoformat(text).isoformat()
            except ValueError:
                await update.message.reply_text("날짜를 YYYY-MM-DD 형식으로 입력해 주세요.")
                return True
            key = "date"
        elif waiting_for in ("time_start", "time_end"):
            try:
                parsed = datetime.datetime.strptime(text, "%H:%M")
                if parsed.minute not in (0, 30):
                    raise ValueError
                value = parsed.strftime("%H:%M")
            except ValueError:
                await update.message.reply_text("시간을 30분 단위 HH:MM 형식으로 입력해 주세요. (예: 06:30)")
                return True
            key = waiting_for
        elif waiting_for == "refresh_interval":
            try:
                value = int(text)
                if not 1 <= value <= 30:
                    raise ValueError
            except ValueError:
                await update.message.reply_text("조회 간격을 1~30 사이의 초 단위 숫자로 입력해 주세요.")
                return True
            key = waiting_for
        elif waiting_for == "room_preference" and text in ("일반객실 우선", "일반객실만", "전체 객실"):
            key, value = waiting_for, text
        elif waiting_for == "auto_pay" and text in ("켜기", "끄기"):
            key, value = waiting_for, text == "켜기"
        else:
            await update.message.reply_text("표시된 버튼에서 값을 선택해 주세요.")
            return True

        context.user_data.pop("waiting_for", None)
        context.user_data.pop("port_candidates", None)
        self.request_update_setting.emit(key, value)
        await update.message.reply_text("설정 변경을 PC에 전달했습니다.", reply_markup=ReplyKeyboardMarkup(SETTINGS_KEYBOARD, resize_keyboard=True))
        return True

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.message or not update.effective_chat or str(update.effective_chat.id) != str(self.chat_id):
            return  # 수정된 메시지(edited_message) 등은 무시

        text = update.message.text.strip()

        setting_commands = {
            "⚙️ 예매 설정", "🚢 출발항", "🏁 도착항", "🔄 출·도착항 맞바꾸기",
            "📅 날짜", "🕐 시작 시간", "🕙 종료 시간", "💺 객실 유형",
            "⏱️ 조회 간격", "💳 자동 결제", "🪪 카드 정보", *CARD_FIELDS,
        }
        if self.is_macro_running and text in setting_commands:
            context.user_data.clear()
            await update.message.reply_text("⚠️ 매크로 실행 중에는 예매 설정을 변경할 수 없습니다.")
            return

        if text == "↩️ 주 메뉴":
            context.user_data.clear()
            await self.cmd_start(update, context)
            return
        if text == "↩️ 설정 메뉴":
            context.user_data.clear()
            await update.message.reply_text("변경할 예매 설정을 선택해 주세요.", reply_markup=ReplyKeyboardMarkup(SETTINGS_KEYBOARD, resize_keyboard=True))
            return
        if await self._handle_setting_input(update, context, text):
            return

        if text == "▶️ 매크로 시작":
            if self.is_macro_running:
                await update.message.reply_text("⚠️ 이미 매크로가 실행 중입니다.")
            else:
                self.request_start_macro.emit()
                await update.message.reply_text("🚀 매크로 시작 명령을 전달했습니다.")
        elif text == "⏹️ 매크로 중지":
            if not self.is_macro_running:
                await update.message.reply_text("ℹ️ 현재 매크로가 실행 중이지 않습니다.")
            else:
                self.request_stop_macro.emit()
                await update.message.reply_text("⏹️ 매크로 중지 명령을 전달했습니다.")
        elif text in ["🔍 현재 설정 확인", "상태 확인"]:
            self.request_status_check.emit()  # GUI가 최신 설정·조회 횟수로 동기화한 뒤 응답합니다.
        elif text == "🚢 여정 즉시 조회":
            self.request_journey_check.emit()
        elif text == "⚙️ 예매 설정":
            if self.is_macro_running:
                await update.message.reply_text("⚠️ 매크로 실행 중에는 예매 설정을 변경할 수 없습니다.")
            else:
                await update.message.reply_text("변경할 예매 설정을 선택해 주세요.", reply_markup=ReplyKeyboardMarkup(SETTINGS_KEYBOARD, resize_keyboard=True))
        elif text == "🚢 출발항":
            context.user_data["waiting_for"] = "f_port_search"
            await update.message.reply_text("출발항 검색어를 입력해 주세요.", reply_markup=ReplyKeyboardRemove())
        elif text == "🏁 도착항":
            if not self.pair_options:
                await update.message.reply_text("선택 가능한 도착항이 없습니다. 출발항을 먼저 선택해 주세요.")
            else:
                context.user_data["waiting_for"] = "t_port"
                keyboard = [[p.get("t_port", "")] for p in self.pair_options] + [["↩️ 설정 메뉴"]]
                await update.message.reply_text("도착항을 선택해 주세요.", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        elif text == "🔄 출·도착항 맞바꾸기":
            self.request_update_setting.emit("swap_ports", None)
            await update.message.reply_text("출발항과 도착항 맞바꾸기를 PC에 전달했습니다.")
        elif text in ("📅 날짜", "🕐 시작 시간", "🕙 종료 시간", "⏱️ 조회 간격"):
            key = {"📅 날짜": "date", "🕐 시작 시간": "time_start", "🕙 종료 시간": "time_end", "⏱️ 조회 간격": "refresh_interval"}[text]
            context.user_data["waiting_for"] = key
            prompt = {"date": "날짜를 YYYY-MM-DD 형식으로 입력해 주세요.", "time_start": "시작 시간을 HH:MM 형식으로 입력해 주세요.", "time_end": "종료 시간을 HH:MM 형식으로 입력해 주세요.", "refresh_interval": "조회 간격을 1~30 사이의 초 단위 숫자로 입력해 주세요."}[key]
            await update.message.reply_text(prompt, reply_markup=ReplyKeyboardRemove())
        elif text == "💺 객실 유형":
            context.user_data["waiting_for"] = "room_preference"
            await update.message.reply_text("객실 유형을 선택해 주세요.", reply_markup=ReplyKeyboardMarkup([["일반객실 우선", "일반객실만", "전체 객실"], ["↩️ 설정 메뉴"]], resize_keyboard=True))
        elif text == "💳 자동 결제":
            context.user_data["waiting_for"] = "auto_pay"
            await update.message.reply_text("자동 결제를 선택해 주세요.", reply_markup=ReplyKeyboardMarkup([["켜기", "끄기"], ["↩️ 설정 메뉴"]], resize_keyboard=True))
        elif text == "🪪 카드 정보":
            await update.message.reply_text(
                "변경할 카드 정보를 선택해 주세요.\n\n"
                "⚠️ 입력한 메시지는 봇이 바로 삭제하지만, 전송 순간 텔레그램 서버를 거칩니다. "
                "휴대폰 알림 미리보기나 다른 기기에 잠시 표시될 수 있으니 가능하면 PC 화면에서 입력하세요.",
                reply_markup=ReplyKeyboardMarkup(CARD_KEYBOARD, resize_keyboard=True))
        elif text in CARD_FIELDS:
            field, prompt = CARD_FIELDS[text]
            context.user_data["waiting_for"] = f"card:{field}"
            await update.message.reply_text(prompt, reply_markup=ReplyKeyboardRemove())
        else:
            await self.cmd_start(update, context)
