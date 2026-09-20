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


# 보안 저장소(keyring) 오류 메시지. GUI가 pop_keyring_warnings()로 꺼내 사용자에게 알립니다.
_keyring_warnings: List[str] = []
# 마지막으로 keyring에 비밀번호를 저장한 KSA 아이디 (아이디가 바뀌면 이전 비밀번호를 지우기 위함)
_stored_ksa_id: str = ""
# 설정 파일을 읽지 못한 실행에서는 파일을 덮어쓰지 않음
_config_unreadable: bool = False


def pop_keyring_warnings() -> List[str]:
    warnings = list(dict.fromkeys(_keyring_warnings))
    _keyring_warnings.clear()
    return warnings


def _keyring_call(what: str, fn, *args):
    """keyring 호출이 실패하면 조용히 넘기지 않고 경고로 남깁니다."""
    try:
        return fn(*args)
    except Exception as e:
        _keyring_warnings.append(f"{what} 실패: {e}")
        return None


def _atomic_write_json(path: Path, data: Any) -> None:
    """임시 파일에 쓴 뒤 교체해, 쓰는 도중 종료돼도 기존 파일이 잘리지 않게 합니다."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp, path)


def _backup_broken(path: Path) -> None:
    """읽을 수 없는 파일을 .bak으로 보존해 다음 저장이 내용을 덮어쓰지 않게 합니다."""
    import datetime
    stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    try:
        os.replace(path, path.with_name(f"{path.name}.{stamp}.bak"))  # 여러 번 손상돼도 이전 백업을 덮지 않음
    except OSError:
        pass


def load_config() -> Dict[str, Any]:
    """설정 파일과 보안 저장소에서 전체 설정을 불러옵니다."""
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        global _config_unreadable
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                raise ValueError("설정 형식 오류")
            cfg.update(loaded)
        except ValueError:  # JSON 손상/형식 오류 (JSONDecodeError 포함)
            _backup_broken(CONFIG_FILE)
            _keyring_warnings.append("설정 파일이 손상되어 기본값으로 시작합니다. 이전 파일은 .bak으로 보관했습니다.")
        except OSError as e:
            # 잠겨 있는 등 일시적으로 못 읽는 정상 파일을 기본값으로 덮어쓰지 않도록 이번 실행에서는 저장하지 않습니다.
            _config_unreadable = True
            _keyring_warnings.append(f"설정 파일을 읽지 못했습니다({e}). 이번 실행에서는 설정을 저장하지 않습니다.")

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
    global _stored_ksa_id
    ksa_id = cfg.get("ksa_id", "")
    _stored_ksa_id = ksa_id
    cfg["ksa_pw"] = (_keyring_call("KSA 비밀번호 불러오기", keyring.get_password, KEYRING_KSA, ksa_id) or "") if ksa_id else ""

    # 텔레그램 토큰
    tg_tok = _keyring_call("텔레그램 토큰 불러오기", keyring.get_password, KEYRING_TG, "token")
    if tg_tok:
        cfg["tg_token"] = tg_tok

    # 카드 정보
    cfg["card_info"] = {"cardno": "", "validdate": "", "password": "", "requestno": "", "installment": "00"}
    card_raw = _keyring_call("카드 정보 불러오기", keyring.get_password, KEYRING_CARD, "card_info")
    if card_raw:
        try:
            cfg["card_info"] = json.loads(card_raw)
        except ValueError:
            _keyring_warnings.append("저장된 카드 정보가 손상되어 불러오지 못했습니다.")

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
    global _stored_ksa_id
    ksa_id = to_save.get("ksa_id", "")
    ksa_pw = to_save.pop("ksa_pw", "")
    if _stored_ksa_id and _stored_ksa_id != ksa_id:
        # 아이디를 바꾸면 이전 계정 비밀번호를 보안 저장소에 남기지 않습니다.
        try:
            keyring.delete_password(KEYRING_KSA, _stored_ksa_id)
        except Exception:
            pass  # 저장된 적 없으면 삭제 실패가 정상
    if ksa_id and ksa_pw:
        _keyring_call("KSA 비밀번호 저장", keyring.set_password, KEYRING_KSA, ksa_id, ksa_pw)
    _stored_ksa_id = ksa_id

    tg_token = to_save.get("tg_token", "")
    if tg_token:
        _keyring_call("텔레그램 토큰 저장", keyring.set_password, KEYRING_TG, "token", tg_token)
    # 토큰이 있으면 봇이 받은 메시지(카드 정보 입력 포함)를 읽을 수 있으므로 평문 파일에는 절대 남기지 않습니다.
    to_save.pop("tg_token", None)

    card_info = to_save.pop("card_info", None)
    if card_info is not None:
        has_any_card_data = any(str(v).strip() for v in card_info.values())
        if has_any_card_data:
            _keyring_call("카드 정보 저장", keyring.set_password, KEYRING_CARD, "card_info", json.dumps(card_info))
        else:
            # 빈 카드 정보인 경우 보안 저장소에서 완전 삭제
            clear_card_info()

    # 2. 일반 설정 JSON 저장
    if _config_unreadable:
        return
    try:
        _atomic_write_json(CONFIG_FILE, to_save)
    except Exception as e:
        print(f"설정 저장 실패: {e}")


HISTORY_FILE = APP_DIR / "booking_history.json"


def save_booking_record(record: Dict[str, Any]) -> None:
    """성공한 예매 내역을 로컬 히스토리 파일에 영구 기록합니다."""
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                f.read(1)
        except OSError:
            # 기존 이력을 읽을 수 없으면 덮어쓰지 않고 새 기록만 별도 파일로 남깁니다.
            import datetime
            stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            try:
                _atomic_write_json(HISTORY_FILE.with_name(f"booking_history.{stamp}.json"), [record])
            except Exception as e:
                print(f"예매 내역 저장 실패: {e}")
            return
    records = load_booking_history()
    # 중복 ticketingkey 방지
    t_key = record.get("ticketingkey", "")
    if t_key:
        records = [r for r in records if r.get("ticketingkey") != t_key]
    records.insert(0, record)
    try:
        _atomic_write_json(HISTORY_FILE, records)
    except Exception as e:
        print(f"예매 내역 저장 실패: {e}")


def load_booking_history() -> List[Dict[str, Any]]:
    """로컬에 저장된 예매 성공 이력을 반환합니다."""
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                records = json.load(f)
            if isinstance(records, list):
                return records
        except OSError:
            return []  # 잠김 등 일시적 오류: 파일은 그대로 둠
        except ValueError:
            pass
        _backup_broken(HISTORY_FILE)  # 손상된 이력을 새 기록으로 덮어쓰지 않도록 보존
    return []


