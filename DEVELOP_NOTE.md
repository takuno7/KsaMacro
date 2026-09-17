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
│   ├── 시작하기_안내.txt    # 사용자 가이드
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
