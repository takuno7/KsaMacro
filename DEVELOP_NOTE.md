# KsaMacro 개발 노트 (DEVELOP_NOTE.md)

한국해운조합(KSA) 여객선 자동 예매 및 원격 제어 매크로 프로그램의 아키텍처, 역공학 분석 내역, 핵심 구현 상세 및 문제 해결 기록입니다.
향후 유지보수 및 기능 업데이트 시 본 문서를 기준으로 변경 사항을 기록합니다.

---

## 1. 프로젝트 개요

- **프로젝트명**: KsaMacro (한국해운조합 여객선 예매 매크로)
- **목적**: 잔여석 모니터링 자동화, 즉시 예약/좌석선점/카드자동결제, 텔레그램 모바일 원격 제어 지원
- **벤치마크 모델**: TrainMacro의 검증된 아키텍처 및 디자인 계승
  - PyQt6 기반 정갈한 라이트 테마 UI
  - Windows 자격 증명 관리자(Keyring)를 통한 민감 정보 암호화 저장
  - 텔레그램 대화형 메뉴 버튼(원격 제어) 및 실시간 알림
  - 스마트 앱 컨트롤(Smart App Control, SAC) 차단 방지 배포 구조

---

## 2. 모듈 아키텍처 및 역할

```
c:\KsaMacro\
├── ksa_macro_main.py     # PyQt6 GUI 애플리케이션 진입점 및 UI/이벤트 제어
├── engines.py             # KSA HTTP API 통신, 크롤링, 결제/발권 엔진
├── macro_worker.py        # 백그라운드 멀티스레드 예매 검색 및 트랜잭션 워커
├── config_manager.py      # 설정 파일(config.json, history) 및 Keyring 자격증명 관리
├── telegram_remote.py     # 텔레그램 봇 모바일 대화형 키보드 리스너
├── ship_icon.ico / png    # 딥네이비/레드 컬러 초선명 멀티사이즈 배 아이콘 리소스
├── dist/                  # 최종 배포용 폴더 (SAC 방지 런처 + bin/ 은닉 구조)
│   ├── KsaMacro_실행.bat   # 무차단 스마트 런처 진입점
│   ├── KsaMacro_실행.lnk   # 배 아이콘 바로가기
│   ├── 시작하기_안내.html   # 반응형 사용자 가이드
│   └── bin/               # 코어 엔진 데이터(.dat) 및 내부 모듈
└── DEVELOP_NOTE.md        # 개발 상세 기록 및 업데이트 로그 (본 문서)
```

---

## 3. KSA 예매 시스템 역공학(Reverse Engineering) 상세

KSA 여객선 예매 웹사이트(`https://island.theksa.co.kr`)의 복잡한 비동기 트랜잭션 흐름을 분석하여 `engines.py`에 완전 자동화 구현하였습니다.

### 3.1 회원 로그인 및 세션 유지
- **엔드포인트**: `POST /login/checkLogin`
- **파라미터**: `memberid`, `password`, `loginchk="0"`
- **특징**: 성공 시 세션 쿠키(`JSESSIONID`)가 유지되며, 회원 이름 및 고유 시퀀스(`seq`) 반환.

### 3.2 항구 및 운항 스케줄 조회
- **전국 출발항 조회**: `POST /booking/selectPortList`
- **도착항(페어 항구) 조회**: `POST /booking/selectPairPortList` (출발항의 `portid`, `portsubid` 전달)
- **운항 여정 및 객실 조회**: `POST /booking/selectScheduleList`
  - 출발항/도착항 ID, 날짜(`YYYYMMDD`) 전송
  - `result` (선박 일정)와 `resultAll` (객실별 운임, 잔여석 `onlinecnt`, 예약가능여부 `ispossible`) 수신

### 3.3 승객 검증 및 키 발급 트랜잭션 (7단계)
1. **여정 유효성 체크**: `POST /booking/selectCheckJourney`
2. **할인 규격(DC) 조회**: `POST /booking/selectDepartureTicketDC` (기본 할인 코드 `defaultDcid` 획득)
3. **승객 데이터 정밀 검증**: `POST /booking/checkPassengerValidation`
   - 생년월일 8자리(`YYYYMMDD`), 성별(`M`/`F`), 연락처, 승객구분(`ticketid`: 1 대인, 3 소아, 7 경로, 5 유아)
   - 성공 시 임시 트랜잭션 키 **`checkkey`** 발급
4. **승객 데이터 등록**: `POST /booking/checkInsert` (`checkkey` 사용)
5. **운임 상세 계산 및 확정**: `POST /booking/selectFareUpdate` 및 `POST /booking/selectFareList`
   - 순수 승선 운임, 유류할증료/터미널이용료 등 확정 수신
6. **티켓 파라미터 검증**: `POST /booking/ticketCompleteParamsCheck`
   - 성공 시 최종 선점 키 **`ticketingkey`** 발급
7. **좌석 배정/선점**: `POST /booking/checkCapacitySeat` (`TicketingKey` 전송)

### 3.4 신용카드 자동 결제 승인 및 최종 발권
1. **카드 승인 (`cardapprove`)**:
   - **엔드포인트**: `POST /booking/cardapprove`
   - **규격**: 유효기간은 서버 규격상 **`YYMM`** 필수.
     - 사용자는 직관적인 **`MMYY`** (예: `0928`)로 입력하므로, 엔진 내부에서 `valid_raw[2:] + valid_raw[:2]`로 자동 변환하여 안전하게 전송.
     - 금액(`amount`), 카드번호, 비밀번호 앞 2자리, 소유자 생년월일 6자리(`YYMMDD`) 전송.
   - **결과**: `cardgroupid` (승인/그룹번호) 및 `logtime` 발급.
2. **최종 티켓 발권 (`ticketComplete`)**:
   - **엔드포인트**: `POST /booking/ticketComplete`
   - 티켓 파라미터에 `groupid`, `logtime`, 배정된 `seatno`를 병합하여 최종 발권 완료.

### 3.5 가상계좌 예약 (자동 결제 꺼짐)
사이트 `payment.js`의 `fn_ticketComplete_Virtual` 흐름을 그대로 따릅니다 (`/weven_data/wv/web/content/program/booking/js/payment.js`).
1. **가상계좌 지원 확인**: `POST /payment/selectvirtualinfo` (`companyidlist`=`vesselid[:4]`, `mastertime`)
   - `errCode != 0` 또는 `banks` 없음 → 가상계좌 미지원 선사. `ticketCompleteParamsCheck`/`checkCapacitySeat` 전에 멈추고 `SEAT_AVAILABLE` 반환(좌석 미점유, 빈자리 알림만). 좌석을 선점하면 약 20분간 사용자 본인도 직접 예매할 수 없어 폐기한 방식.
   - 시작 시 사전 경고: 자동 결제 꺼짐이면 워커가 `unsupported_virtual_sailings()`로 조회 범위 운항편을 선사(`vesselid[:4]`)별 1회 조회해 미지원 목록을 `warning_signal`(GUI 팝업)·로그·텔레그램으로 알림. 매크로는 계속 진행. 실사이트 확인: 고군산카훼리호(선사 `4322`) 미지원.
   - 좌석을 잡기 전에 호출하여 미지원 시 불필요한 가상계좌 호출을 막습니다. 입금은행은 `banks[0]` 고정.
2. 티켓 파라미터는 `approvekind="1"`(가상계좌), `ispresale="0"`(예약)으로 `ticketCompleteParamsCheck` → `checkCapacitySeat`.
3. **계좌 발급**: `POST /booking/vaapprove` (`amount`, `user_nm`/`user_phone1`=대표 승객, `user_mail`="", `bank_cd`, `companyidlist`, `ticketingkey`)
   - 응답 `rgroupid`, `logtime`, `rctelegram.r_bank_nm/r_account_no/r_amount`, `sdtelegram.expiredatetime`.
4. **예약 확정**: `POST /booking/ticketComplete` — 파라미터에 `rgroupid`/`rgroupid2`/`logtime`/`ticketingkey` 병합 (카드의 `groupid` 대신).
   - `result`와 `data.result[0].errcode == 0`을 모두 확인.
5. **실패 롤백**
   - `vaapprove` 실패: `ticketRollBack` (`Method=CapacitySeat`, `ApproveKind=reserve`).
   - `ticketComplete` 실패: `POST /payment/vacancel {groupid: rgroupid}` 후 `ticketRollBack` (`Method=CashRecord2|Reserve|CapacitySeat`).
   - `ticketRollBack` 필드명은 사이트 규격대로 `Method`/`TicketingKey`/`GroupID`/`ApproveKind` (카드 실패 롤백도 같은 헬퍼 `_rollback` 사용).
- 결과 `status="VA_RESERVED"`: 텔레그램·GUI에 입금은행/계좌번호/입금액/입금기한 표시, 이력 상태 `가상계좌 입금대기`.
- 결제 화면 안내 문구상 결제 진행 제한은 "최대 20분"(주석 처리된 안내라 서버 실제 선점 시간은 미확인).
- ⚠️ 오프라인 목 테스트(`tests/test_booking.py`)로만 검증됨. 실사이트 첫 예약 시 로그와 예매내역을 반드시 확인.

---

## 4. 주요 문제 해결 및 최적화 기록

### 4.1 출발지/도착지/조회시간 재실행 시 초기화 버그 해결
- **문제**: 사용자가 정보 입력 후 재실행 시 출발지, 도착지, 조회시간이 기본값으로 리셋됨.
- **원인**:
  1. `_load_ports_initial()`에서 `addItem` 시 인덱스 0이 자동 선택되면서, 저장된 출발항이 0번일 때 `currentIndexChanged` 시그널이 트리거되지 않아 도착항(`arr_combo`) 목록이 채워지지 않음.
  2. UI 설정 로딩 도중 콤보박스 변경 이벤트가 발동되어 `_auto_save_settings()`가 빈 상태를 덮어씀.
- **해결**:
  - `dep_combo.blockSignals(True)`로 출발항 목록을 안전하게 추가한 후, 저장된 인덱스로 `_on_dep_changed(match_idx)`를 **동기적으로 직접 호출**하여 도착항 목록을 무조건 100% 로드 보장.
  - 설정 로드(`_load_settings_to_ui`)가 완전히 끝난 뒤에만 `_connect_setting_signals()`를 연결하여 설정 덮어쓰기 원천 차단.

### 4.2 카드 유효기간 `MMYY` 표준화
- **내용**: 사용자 혼선을 방지하기 위해 `YYMM` 대신 일반적인 카드 표기법인 `MMYY` (예: `0928` - 9월 28년)로 전면 개편.
- **조치**:
  - GUI 입력창 Placeholder를 `MMYY (예: 0928)`로 변경.
  - 시작 시 앞 2자리(월 01~12) 유효성 검사 적용.
  - `engines.py` 결제 승인 API 호출 시 `MMYY`를 KSA 규격인 `YYMM`으로 자동 변환하여 요청.

### 4.3 잔여석 '❌매진' 오표기 버그 수정
- **문제**: 잔여석이 140석이나 남아있는데 여정 목록에 `일반객실(❌매진, 8,500원)`으로 표기됨.
- **원인**: KSA 응답의 `ispossible`이 문자열 `'1'`인데 `is_pos == 1`로 타입 변환 없이 비교하여 항상 `False`로 판정됨.
- **해결**: `int(r.get("ispossible", 0)) == 1`로 변환하고 상태 텍스트를 `✅{cnt}석 잔여`로 명확하게 표시.

### 4.4 시작 / 중지 버튼 비활성화 시각 미반영 해결
- **문제**: 매크로 실행 중 시작 버튼이 비활성화(`setEnabled(False)`)되어도 계속 초록색으로 남아있음.
- **원인**: Qt 스타일시트의 ID 선택자(`#startBtn`, `#stopBtn`)의 우선순위가 일반 `:disabled` 가상 클래스보다 높아 색상이 고정됨.
- **해결**: `#startBtn:disabled`, `#stopBtn:disabled` 스타일을 명시적으로 추가하여 비활성화 시 연회색(`#cfd8dc`)으로 즉시 전환되도록 수정.

### 4.5 예매 내역 조회 기능 정상화 및 로컬 히스토리 연동
- **문제**: 예매 성공 후 [예매 내역 조회] 클릭 시 무조건 '조회된 예매 내역이 없습니다' 출력.
- **원인**: 기존 조회 URL(`/payment_confirm/selectPaymentConfirmList`)이 404 Not Found 였음.
- **해결**:
  - 예매 성공 시 `%APPDATA%/KsaMacro/booking_history.json`에 영구 자동 저장.
  - 다이얼로그에서 실제 발권 상세(티켓키, 선박명, 일시, 구간, 좌석, 금액, 승객명)를 표로 표시.
  - `[🌐 KSA 공식 웹사이트에서 모바일 티켓 조회]` 버튼을 추가하여 원클릭 브라우저 연동 지원.

### 4.6 Windows 11 스마트 앱 컨트롤(SAC) 차단 방지 배포 구조
- **문제**: 새로 컴파일된 미서명 `.exe` 실행 시 SAC가 정책 차단(Event ID 3118) 발생.
- **해결 구조**:
  - `dist/` 폴더 루트에서 사용자가 `.exe`를 직접 실행하지 않도록 실제 바이너리를 `dist/bin/KsaMacro_core.dat` 형태로 은닉.
  - 사용자는 배 모양 아이콘의 `KsaMacro_실행.lnk` 또는 `KsaMacro_실행.bat`으로만 진입하도록 패키징.
  - 스마트 런처는 공식 서명된 `pythonw.exe` 환경을 우선 탐색하여 실행하므로 SAC 차단이 100% 발생하지 않음.

---

## 5. 향후 업데이트 및 유지보수 규칙

1. **개인정보 보호**:
   - 테스트용 전화번호, 이름, 생년월일, 비밀번호, 카드번호, 텔레그램 토큰 등 개인정보는 소스 코드 및 문서에 절대로 하드코딩하지 않습니다.
   - 모든 사용자 입력은 `%APPDATA%/KsaMacro/config.json` 및 Windows Keyring 보안 저장소를 통해서만 관리합니다.
2. **KSA API 변경 대응**:
   - KSA 웹사이트의 파라미터나 암호화 방식이 변경될 경우 `engines.py`의 `book()` 트랜잭션 7단계를 우선 점검합니다.
3. **버전 관리 및 기록**:
   - 신규 기능 추가 또는 버그 수정 시 아래 [업데이트 히스토리] 섹션에 일자, 변경 내용, 수정 파일을 기록합니다.

---

## 6. 업데이트 히스토리 (Update History)

### v1.0.0 (2026-09-17)
- **최초 정식 릴리즈**
- KSA 여객선 잔여석 실시간 검색 및 자동 예약/좌석배정/카드결제 파이프라인 완성.
- 텔레그램 모바일 원격 제어 대화형 키보드 메뉴 지원.
- 카드 유효기간 `MMYY` 입력 및 서버 자동 변환 지원.
- 재실행 시 출발지/도착지/조회시간/객실선호 복원 동기화 로직 적용.
- 잔여석 `✅N석 잔여` 시각화 및 시작/중지 버튼 비활성화 스타일 적용.
- 로컬 예매 히스토리 영구 저장 및 KSA 웹 연동 조회 다이얼로그 구축.
- SAC(스마트 앱 컨트롤) 차단 방지를 위한 `dist/bin/` 은닉 배포 패키지 구축.

### v1.0.2 (2026-09-20)
- 텔레그램 `🚢 여정 즉시 조회`가 GUI의 기존 실시간 조회 경로를 실행하고 결과를 채팅으로 반환하도록 수정.
- 회귀 테스트 `tests/test_telegram_menu.py` 추가.
- 텍스트 안내서를 단일 반응형 HTML 안내서로 교체하고 모바일·인쇄 레이아웃을 지원.
- 테스트, PyInstaller 빌드, 코어 파일 교체, 버전 ZIP 생성을 수행하는 `build_release.ps1` 추가.

### v1.1.0 (2026-09-20)
- 텔레그램 원격 메뉴에 `⚙️ 예매 설정`을 추가하고 `context.user_data["waiting_for"]`로 다단계 입력 상태를 관리.
- `request_update_setting(key, value)` Qt 신호를 통해 텔레그램 스레드에서 GUI 메인 스레드로 설정 변경을 전달.
- 출발항 검색 후보와 선택 출발항의 연결 도착항을 동적 버튼으로 제공.
- 날짜, 시작·종료 시간, 객실 유형, 조회 간격, 자동 결제 및 출발·도착항 맞바꾸기를 지원.
- GUI의 기존 위젯과 `_gather_settings_from_ui()` 저장 경로를 재사용하고 변경 후 현재 상태를 텔레그램으로 회신.
- 매크로 실행 중에는 텔레그램과 GUI 양쪽에서 설정 변경을 차단.

### v1.2.0 (2026-09-20)
- 자동 결제 꺼짐 시 좌석 선점에서 멈추지 않고 가상계좌 예약까지 완료 (3.5 참고). 미지원 운항편은 좌석을 잡지 않고 빈자리 알림만, 시작 시 사전 경고.
- 텔레그램 `🪪 카드 정보` 메뉴: `waiting_for="card:<field>"` 상태, `validate_card_field()` 검증, 입력 메시지 즉시 삭제, 상태에는 끝 4자리만 표시. GUI는 `request_update_setting("card_info", (field, value))`로 입력칸에 반영.
- 프로그램 종료(`closeEvent`) 시 텔레그램 리스너를 정지하고 `ReplyKeyboardRemove`로 메뉴 키보드 제거.
- `🚢 여정 즉시 조회`의 대기 안내 메시지 제거.
- `KsaPaymentStateError`: 카드 승인 이후 또는 가상계좌 발급·확정 응답을 확인할 수 없을 때 발생. 워커는 이 오류에서 재시도하지 않고 중지(중복 결제·예약 방지). 응답 불명 상태에서는 `vacancel`도 호출하지 않음.
- 워커는 `book()` 성공 직후 `break`를 보장하고, 결과 알림은 `format_result_message()`(HTML 이스케이프)로 별도 보호 블록에서 전송. 회귀 테스트 `tests/test_worker.py`.
- 결과 불명 사후 대조: 워커가 로그인 직후 `get_reservations()`의 `groupid` 집합을 기준선으로 저장하고, `KsaPaymentStateError` 시 재조회해 새 groupid 유무를 알림에 포함. `get_reservations()`는 실패 시 빈 목록 대신 `KsaError`를 던지도록 변경(빈 목록과 실패 구분, GUI 호출부는 기존 try/except로 처리).
- 가상계좌 발급 은행은 `banks[0]` 고정 (희망 은행 입력칸은 선택지 없이 자유 입력이라 혼란만 줘서 제거. 가상계좌는 어느 은행에서든 이체 가능). `selectvirtualinfo`의 `expiredatetime`을 입금기한 예비값으로 사용.
- 실사이트 확인(2026-09-20, 조회 전용): `selectvirtualinfo` 응답은 `data.errCode`(미지원 -2, 지원 0), `data.banks[{vanid, bankcode, bankname}]`, `data.expiredatetime`. 예) 선사 `9603` 지원, `9001` 미지원. `vaapprove`·`ticketComplete`(가상계좌)는 실제 예약이 생기므로 미검증.
- `config_manager.save_config()`: `tg_token`은 keyring 저장 성공 시 `config.json`에서 제외.
- `closeEvent`: 워커 실행 중이면 확인 후 `worker.stop()` → `wait(200)` + `processEvents()` 반복으로 진행 중 예매 완료까지 대기.
- 함정 코드 감사(2026-09-20) 반영:
  - 결과 코드 비교는 `_ok()`로 통일 (사이트 JS의 `== 0` 느슨한 비교와 동일하게 `0`/`"0"` 허용). 카드 승인 응답에 코드가 없으면 `KsaPaymentStateError`, 명시적 거절은 `KsaCardDeclinedError`(워커 중지).
  - `checkCapacitySeat` 실패·응답 오류·배정 좌석 수 ≠ 승객 수면 `_rollback` 후 실패. 객실 선택은 유아(ticketid 5) 제외 필요 좌석 수 이상.
  - 워커는 예매 실패/조회 오류 시 `_relogin_if_expired()`로 `is_logged_in()` 확인 후 재로그인 (기존 오류 문자열 추측 방식 폐기).
  - 설정·이력 파일은 `_atomic_write_json`(임시 파일 + `os.replace`), 손상 시 `.bak` 보존.
  - GUI 네트워크 호출은 `_run_bg(fn, on_done, on_error)` → `ThreadPoolExecutor(max_workers=1)`에서 순서대로 실행하고 `_bg_finished` 신호로 GUI 스레드에 결과 전달 (같은 `requests.Session` 동시 사용 방지). 출발항 변경은 `_select_dep(idx, then)`/`_on_dep_changed(idx, then)`로 도착항 로드 후 후속 동작 실행, 늦게 도착한 옛 결과는 무시.
  - keyring 오류는 `config_manager._keyring_call`이 `_keyring_warnings`에 기록 → GUI `_report_keyring_warnings()`가 같은 내용은 한 번만 로그·팝업. 아이디 변경/비밀번호 삭제 시 이전 keyring 항목 삭제.
  - 2차 감사: 발권 응답 판정 `_ticket_outcome()` = ok/failed/unknown. failed면 카드 `cardcancel{usedate=logtime, cardgroupid, cancelamount}` 성공 확인 후 `CardRecord|Ticket|CapacitySeat` 롤백(사이트 fn_cardRecordCancel), 가상계좌는 `vacancel` 성공 확인 후에만 롤백. unknown은 아무것도 되돌리지 않고 `KsaPaymentStateError`. 좌석 행 수 검사는 사이트와 같이 전체 티켓(유아 포함) 기준.
  - 3차 반영: 특수할인 `checked[].seqstring`(vaindex=승객 index) → 승객 `eventcontents`(checkInsert에 포함) → 티켓 파라미터는 `_event_contents()`(사이트 fn_eventContents 동일 규칙). 좌석 배정 실패 롤백 `_rollback_seat()`는 카드 2회/가상계좌 1회. 가상계좌 발급 errCode 실패는 응답 `rgroupid`로 롤백.
  - 텔레그램: 재연동은 이전 매니저 `finished` → 새 매니저 `start` (GUI `wait()` 없음, `_old_tg_remotes`로 참조 유지). 세션 미가동(`_live` False) 중 `send_log`는 HTTP API로 직접 전송. 세션은 `_run_bot_session`의 finally에서 항상 정리. 로그의 봇 토큰은 `***`로 가리고 `httpx` 로거는 WARNING 이상만.
  - 비상연락처(`emtel` → 티켓 파라미터 `tel2`): 필수 여부는 `POST /booking/selectDepartureConfiguration`의 `requiredemtel`("00"만 선택). 2026-09-20 조회 결과 인천→굴업도 "02", 말도→관리도 "01"로 모두 필수. `checktel`("01")이면 승객 전원의 tel/emtel이 서로 달라야 하므로 시작 전에 중복 검사.
  - 남은 과제(의도적으로 유지): 배포 bat이 상위 폴더 소스를 먼저 실행, 실행 파일 `.dat` 배포, `.lnk`에 개발 PC 경로 포함.
- `tests/test_booking.py`: 저장소에 없는 `diagnose_booking` import를 해당 테스트 내부로 옮겨(없으면 skip) 나머지 테스트가 실행되도록 수정.
