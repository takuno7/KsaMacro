# KsaMacro exe 빌드 가이드

이 문서는 `KsaMacro`를 단일 실행 파일(`.exe`)로 패키징하는 방법을 안내합니다.

## 1. 사전 준비

PyInstaller가 설치되어 있지 않다면 설치합니다:

```bash
python -m pip install pyinstaller
```

## 2. 릴리스 빌드

프로젝트 루트 디렉토리에서 다음 명령을 실행합니다. 소스의 `VERSION`을 읽어 코어 파일과 버전별 ZIP을 함께 생성합니다.

```powershell
.\build_release.ps1
```

## 3. 빌드 결과물 확인

빌드가 완료되면 `dist/bin/KsaMacro_core.dat`와 `KsaMacro-v<버전>-windows.zip`이 생성됩니다.
