# -*- coding: utf-8 -*-
"""KsaMacro 설정 및 보안 자격증명 관리자.

설정 파일은 %APPDATA%/KsaMacro/config.json 에 저장되며,
비밀번호 및 카드 정보와 같은 민감한 데이터는 OS의 보안 저장소(Windows Credential Manager / keyring)에 안전하게 보관됩니다.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import keyring

APP_DIR = Path(os.getenv("APPDATA", Path.home())) / "KsaMacro"
APP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = APP_DIR / "config.json"

KEYRING_KSA = "KsaMacro_KSA"
KEYRING_TG = "KsaMacro_Telegram"
KEYRING_CARD = "KsaMacro_Card"


DEFAULT_CONFIG: Dict[str, Any] = {
    "ksa_id": "",
    "tg_token": "",
    "tg_chat_id": "",
    "f_port": "인천",
    "f_portid": "1010",
    "f_portsubid": "0",
    "t_port": "백령",
    "t_portid": "1002",
    "t_portsubid": "0",
    "date": "",
    "time_start": "06:00",
    "time_end": "23:00",
    "room_preference": "일반객실 우선",  # 일반객실 우선 / 일반객실만 / 전체 객실
    "passenger_counts": {
        "adult": 1,
        "infant": 0
    },
    "card_info": {
        "cardno": "",
        "validdate": "",
        "password": "",
        "requestno": "",
        "installment": "00"
    },
    "passengers": [],
    "auto_pay": True,
    "card_installment": "00",
    "refresh_interval": 2.0
}


def load_config() -> Dict[str, Any]:
    """설정 파일과 보안 저장소에서 전체 설정을 불러옵니다."""
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                cfg.update(loaded)
        except Exception:
            pass

    # 테스트 잔여 더미 계정 완벽 필터링 (첫 실행 빈칸 보장)
    if cfg.get("ksa_id") in ["sample_user", "test_user"]:
        cfg["ksa_id"] = ""
        cfg["ksa_pw"] = ""

    # 이름과 생년월일이 모두 비어있는 더미 승객 객체 정리
    cleaned_passengers = [
        p for p in cfg.get("passengers", [])
        if p.get("name", "").strip() or p.get("idnumber", "").strip()
    ]
    cfg["passengers"] = cleaned_passengers

    # Keyring에서 비밀번호 및 카드정보 로드
    ksa_id = cfg.get("ksa_id", "")
    if ksa_id:
        try:
            cfg["ksa_pw"] = keyring.get_password(KEYRING_KSA, ksa_id) or ""
        except Exception:
            cfg["ksa_pw"] = ""
    else:
        cfg["ksa_pw"] = ""

    # 텔레그램 토큰
    try:
        tg_tok = keyring.get_password(KEYRING_TG, "token")
        if tg_tok:
            cfg["tg_token"] = tg_tok
    except Exception:
        pass

    # 카드 정보
    try:
        card_raw = keyring.get_password(KEYRING_CARD, "card_info")
        if card_raw:
            cfg["card_info"] = json.loads(card_raw)
        else:
            cfg["card_info"] = {
                "cardno": "",
                "validdate": "",
                "password": "",
                "requestno": "",
                "installment": "00"
            }
    except Exception:
        cfg["card_info"] = {
            "cardno": "",
            "validdate": "",
            "password": "",
            "requestno": "",
            "installment": "00"
        }

    return cfg


def clear_card_info() -> None:
    """보안 저장소(keyring)에서 저장된 카드 정보를 완전히 영구 삭제합니다."""
    try:
        keyring.delete_password(KEYRING_CARD, "card_info")
    except Exception:
        pass
    try:
        keyring.set_password(KEYRING_CARD, "card_info", "")
    except Exception:
        pass


def save_config(cfg: Dict[str, Any]) -> None:
    """설정을 파일 및 keyring에 안전하게 분리 저장합니다."""
    to_save = cfg.copy()

    # 1. 민감 정보 분리 후 keyring에 저장
    ksa_id = to_save.get("ksa_id", "")
    ksa_pw = to_save.pop("ksa_pw", "")
    if ksa_id and ksa_pw:
        try:
            keyring.set_password(KEYRING_KSA, ksa_id, ksa_pw)
        except Exception:
            pass

    tg_token = to_save.get("tg_token", "")
    if tg_token:
        try:
            keyring.set_password(KEYRING_TG, "token", tg_token)
        except Exception:
            pass

    card_info = to_save.pop("card_info", None)
    if card_info is not None:
        has_any_card_data = any(str(v).strip() for v in card_info.values())
        if has_any_card_data:
            try:
                keyring.set_password(KEYRING_CARD, "card_info", json.dumps(card_info))
            except Exception:
                pass
        else:
            # 빈 카드 정보인 경우 보안 저장소에서 완전 삭제
            clear_card_info()

    # 2. 일반 설정 JSON 저장
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(to_save, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"설정 저장 실패: {e}")


HISTORY_FILE = APP_DIR / "booking_history.json"


def save_booking_record(record: Dict[str, Any]) -> None:
    """성공한 예매 내역을 로컬 히스토리 파일에 영구 기록합니다."""
    records = load_booking_history()
    # 중복 ticketingkey 방지
    t_key = record.get("ticketingkey", "")
    if t_key:
        records = [r for r in records if r.get("ticketingkey") != t_key]
    records.insert(0, record)
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"예매 내역 저장 실패: {e}")


def load_booking_history() -> List[Dict[str, Any]]:
    """로컬에 저장된 예매 성공 이력을 반환합니다."""
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


