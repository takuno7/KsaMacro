# KsaMacro exe 빌드 가이드

이 문서는 `KsaMacro`를 단일 실행 파일(`.exe`)로 패키징하는 방법을 안내합니다.

## 1. 사전 준비

PyInstaller가 설치되어 있지 않다면 설치합니다:

```bash
python -m pip install pyinstaller
```

## 2. exe 빌드 명령

프로젝트 루트 디렉토리(`c:\KsaMacro`)에서 다음 명령을 실행합니다:

```bash
python -m PyInstaller --noconfirm --onedir --windowed --name "KsaMacro" --clean ksa_macro_main.py
```

또는 단일 파일(`.exe`)로 만들 경우:

```bash
python -m PyInstaller --noconfirm --onefile --windowed --name "KsaMacro" --clean ksa_macro_main.py
```

## 3. 빌드 결과물 확인

빌드가 완료되면 `dist/KsaMacro/` (또는 `dist/KsaMacro.exe`) 위치에 실행 파일이 생성됩니다.
더블 클릭하여 실행할 수 있습니다.
