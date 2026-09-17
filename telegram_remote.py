# -*- coding: utf-8 -*-
"""KsaMacro 텔레그램 원격 제어 및 알림 매니저.

python-telegram-bot 라이브러리를 기반으로,
모바일 텔레그램 채팅창에 대화형 키보드 메뉴 버튼을 생성하여
스마트폰에서 매크로 시작/중지, 상태 확인, 설정 조회를 원격으로 제어할 수 있습니다.
"""

import asyncio
import datetime
import logging
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


class TelegramBot:
    """단순 메시지 전송 및 Chat ID 확인용 클라이언트."""

    def __init__(self, token: Optional[str], chat_id: Optional[str]):
        self.token = token or ""
        self.chat_id = chat_id or ""

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
            logger.error(f"텔레그램 전송 오류: {e}")
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
                    last_update = data["result"][-1]
                    if "message" in last_update:
                        return str(last_update["message"]["chat"]["id"])
            return None
        except Exception as e:
            logger.error(f"텔레그램 업데이트 확인 오류: {e}")
            return None


class TelegramRemoteManager(QThread):
    """텔레그램 봇 기반 원격 제어 매니저 (TrainMacro 방식 키보드 메뉴)."""

    request_start_macro = pyqtSignal()
    request_stop_macro = pyqtSignal()
    request_status_check = pyqtSignal()
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
        self.search_count = 0
        self.is_connected = True

    def set_current_settings(self, settings: Dict[str, Any]):
        self.current_settings = settings

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
        s = self.current_settings
        status_str = f"🚀 매크로 동작 중 ({self.search_count}회 조회)" if self.is_macro_running else "⏹️ 매크로 대기 중"
        passengers = s.get("passengers", [])
        p_names = [f"{p.get('name')}({'유아' if p.get('ticketid') == '5' else '여객'})" for p in passengers if p.get('name')]
        p_desc = ", ".join(p_names) if p_names else "미등록"

        return (
            f"<b>[현재 KsaMacro 상태]</b>\n"
            f"상태: {status_str}\n"
            f"구간: 🚢 {s.get('f_port', '')} → {s.get('t_port', '')}\n"
            f"날짜: 📅 {s.get('date', '')} ({s.get('time_start', '')} ~ {s.get('time_end', '')})\n"
            f"객실: 💺 {s.get('room_preference', '')}\n"
            f"승객: 👥 {p_desc}\n"
            f"자동결제: {'켜짐' if s.get('auto_pay') else '꺼짐(좌석선점만)'}\n"
            f"조회주기: ⏱️ {s.get('refresh_interval', 2)}초"
        )

    def send_log(self, level: str, text: str):
        if not self.loop or not self.is_running or not self.app or not self.app.bot:
            return
        asyncio.run_coroutine_threadsafe(self._do_send_message(f"[{level}] {text}"), self.loop)

    async def _do_send_message(self, text: str):
        try:
            if self.app and self.app.bot and self.chat_id:
                reply_keyboard = [
                    ["▶️ 매크로 시작", "⏹️ 매크로 중지"],
                    ["🔍 현재 설정 확인", "🚢 여정 즉시 조회"]
                ]
                await self.app.bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                    reply_markup=ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True)
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
                logger.error(f"Telegram bot session error: {e}")
                for _ in range(backoff):
                    if not self.is_running:
                        break
                    time.sleep(1)
                backoff = min(backoff * 2, 60)

    async def _run_bot_session(self):
        self.app = Application.builder().token(self.token).build()

        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("help", self.cmd_start))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling()

        self.log_signal.emit("SUCCESS", "텔레그램 원격 제어 리스너 가동 완료 (모바일 메뉴 활성화)")
        await self._send_startup_message()

        while self.is_running:
            await asyncio.sleep(1)

        await self.app.updater.stop()
        await self.app.stop()
        await self.app.shutdown()

    async def _send_startup_message(self):
        reply_keyboard = [
            ["▶️ 매크로 시작", "⏹️ 매크로 중지"],
            ["🔍 현재 설정 확인", "🚢 여정 즉시 조회"]
        ]
        status_text = self.format_settings_text()
        try:
            await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=f"⚓ <b>KsaMacro 원격 제어가 활성화되었습니다.</b>\n\n{status_text}",
                reply_markup=ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"시작 메시지 전송 실패: {e}")

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_chat.id) != str(self.chat_id):
            await update.message.reply_text("권한이 없는 사용자입니다.")
            return

        reply_keyboard = [
            ["▶️ 매크로 시작", "⏹️ 매크로 중지"],
            ["🔍 현재 설정 확인", "🚢 여정 즉시 조회"]
        ]
        await update.message.reply_text(
            "⚓ <b>KsaMacro 여객선 원격 제어 봇</b>\n아래 메뉴 버튼을 눌러 원격 제어하세요.",
            reply_markup=ReplyKeyboardMarkup(reply_keyboard, resize_keyboard=True),
            parse_mode="HTML"
        )

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_chat.id) != str(self.chat_id):
            return

        text = update.message.text.strip()

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
            self.request_status_check.emit()
            status_text = self.format_settings_text()
            await update.message.reply_text(status_text, parse_mode="HTML")
        elif text == "🚢 여정 즉시 조회":
            await update.message.reply_text("🔍 PC에서 실시간 여정을 조회합니다. 잠시만 기다려주세요...")
            self.request_status_check.emit()
        else:
            await self.cmd_start(update, context)
