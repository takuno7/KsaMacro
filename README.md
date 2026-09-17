# KsaMacro — KSA 한국해운조합 여객선 예매 매크로

한국해운조합(가보고 싶은 섬, `https://island.theksa.co.kr`)의 전국 여객선 예매 전체 과정을 자동화하는 고성능 데스크톱 매크로 프로그램입니다.

KTX 예매 매크로 `TrainMacro`의 아키텍처와 디자인을 완벽히 계승하였으며, KSA 전용 자체 리버스 엔지니어링 API 엔진을 탑재하여 브라우저 없이 밀리초(ms) 단위의 초고속 예매를 수행합니다.

---

## 주요 기능

1. **자체 리버스 엔지니어링 API 엔진 탑재 (`engines.py`)**
   - 브라우저를 띄우지 않고 가벼운 HTTP 세션으로 KSA 백엔드와 직접 통신
   - 로그인, 전국 300+ 항구 목록, 연결 도착항, 실시간 운항 일정 및 잔여석 조회
   - 잔여석 탐지 시 승객 등록, 좌석 락/배정, 카드 결제 승인, 최종 발권까지 초고속 원클릭 처리
2. **로그인 정보 및 설정 영구 기억 & 자동 복원**
   - 한 번 로그인하면 계정 정보(ID)는 `%APPDATA%/KsaMacro/config.json`에 저장
   - 비밀번호 및 카드번호 등 민감 정보는 Windows Credential Manager (`keyring`)에 안전하게 암호화 보관
   - **앱 실행 시 기억된 정보로 즉시 자동 로그인**되어 매번 로그인할 필요가 없습니다.
   - UI에서 값을 변경할 때마다 실시간으로 자동 저장됩니다.
3. **출발지-도착지 실시간 자동 연동**
   - 출발지를 검색/선택하면 해당 항구에서 실제로 운항 가능한 도착지 목록만 자동으로 필터링되어 도착지 콤보박스에 로드됩니다.
   - 출·도착지 맞바꿈 버튼(`⇄`) 지원
4. **모던 다크 UI 및 실시간 컬러 로그**
   - PyQt6 기반의 세련된 다크 테마 GUI
   - 실시간 여정 조회 버튼으로 현재 시점의 운항 시간표 및 잔여석 상태를 리스트뷰로 확인 가능
   - 상세한 상태 및 오류 내역을 컬러 로그로 즉시 출력
5. **카드 자동 결제 지원**
   - 잔여석 발견 시 카드 정보를 통해 PG 승인 및 최종 발권을 즉시 완료
   - 자동 결제를 원치 않을 경우 체크 해제 시 좌석 선점(락) 상태까지만 진행
6. **텔레그램 원격 알림 연동**
   - 예매 성공 시 선박명, 출발시간, 좌석번호, 금액, 승객 목록을 텔레그램으로 즉시 통지
   - Chat ID 자동 감지 및 연결 테스트 지원

---

## 파일 구성

- [engines.py](file:///c:/KsaMacro/engines.py): KSA 백엔드 API 클라이언트 어댑터
- [macro_worker.py](file:///c:/KsaMacro/macro_worker.py): QThread 기반 비동기 잔여석 감시 및 자동 예매 워커
- [ksa_macro_main.py](file:///c:/KsaMacro/ksa_macro_main.py): PyQt6 메인 데스크톱 GUI 애플리케이션
- [config_manager.py](file:///c:/KsaMacro/config_manager.py): 설정 및 keyring 암호화 보안 관리자
- [telegram_remote.py](file:///c:/KsaMacro/telegram_remote.py): 텔레그램 연동 모듈
- [tests/smoke_test.py](file:///c:/KsaMacro/tests/smoke_test.py): 전체 기능 오프스크린 스모크 테스트

---

## 실행 방법

### 1. 의존 패키지 설치
```bash
python -m pip install -r requirements.txt
```

### 2. 프로그램 실행
```bash
python ksa_macro_main.py
```

### 3. 스모크 테스트 검증
```bash
python tests/smoke_test.py
```
