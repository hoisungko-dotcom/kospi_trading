# KOSPI 자동매매 시스템

코스피 6종목 + 코스닥 4종목에 집중하는 국내 주식 자동매매 봇입니다.  
한국투자증권(KIS) Open API를 사용하며, **처음에는 반드시 모의투자 모드**로 시작하세요.

## 주요 기능

- 매일 08:30 코스피 전체 + 코스닥 상위 300 종목 스캔 → 매수 후보 선정
- 09:00~15:30 5분마다 신호 확인 및 자동 매수/매도
- 텔레그램 알림 (선택)
- Claude AI 매도 판단 (선택)
- 모의투자 / 실전투자 전환 지원

---

## 설치 방법

### 방법 1. Windows EXE 간편 설치 (Python 불필요)

1. [Releases](https://github.com/hoisungko-dotcom/kospi_trading/releases/latest) 페이지에서 두 파일 다운로드
   - 
   - 
2. 두 파일을 **같은 폴더**에 저장
3.  파일을  로 이름 변경 후 KIS API 키 입력
4.  실행

> 실행 후 같은 폴더에 ,  폴더가 자동 생성됩니다.

---

### 방법 2. Python 소스코드 설치

### 1. Python 설치

Python 3.11 이상이 필요합니다.  
[python.org](https://www.python.org/downloads/) 에서 다운로드 후 설치하세요.  
설치 시 **"Add Python to PATH"** 체크박스를 반드시 체크하세요.

### 2. 소스코드 다운로드

이 저장소를 ZIP으로 다운로드하거나 git clone 하세요.

```
git clone https://github.com/<username>/kospi_trading_system.git
cd kospi_trading_system
```

### 3. 패키지 설치

터미널(명령 프롬프트)에서 프로젝트 폴더로 이동 후:

```
pip install -r requirements.txt
```

### 4. 환경 변수 설정

`.env.example` 파일을 복사해 `.env` 파일을 만드세요.

**Windows:**
```
copy .env.example .env
```

**Mac/Linux:**
```
cp .env.example .env
```

`.env` 파일을 메모장(또는 텍스트 편집기)으로 열어 KIS API 키를 입력하세요.

---

## KIS API 키 발급 방법

1. [한국투자증권 홈페이지](https://securities.koreainvestment.com) 로그인
2. 상단 메뉴 → **트레이딩** → **Open API**
3. **API 신청하기** 클릭
4. 모의투자 신청 후 앱키(APP KEY)와 앱시크릿(APP SECRET) 발급
5. `.env` 파일에 입력

> **모의 계좌번호**는 KIS 홈페이지 → 계좌번호 조회에서 확인할 수 있습니다.

---

## 실행 방법

```
python main.py
```

처음에는 `.env` 파일에서 `MOCK_TRADING=true` 로 설정된 상태로 시작됩니다.  
2~4주 모의 운용 후 전략이 맞는다고 판단되면 실전으로 전환하세요.

---

## 실전투자 전환

`.env` 파일에서 아래 두 항목을 수정하세요.

```
MOCK_TRADING=false
LIVE_TRADING_CONFIRMED=true
```

> 실전 전환 후 발생하는 손실에 대한 책임은 사용자 본인에게 있습니다.

---

## Claude AI 매도 판단 설정 (선택)

AI가 수익 구간에서 매도 여부를 판단합니다. 없어도 기본 지표 기반으로 동작합니다.

**비용:** Claude Haiku 기준 1회 판단 약 $0.001 미만 → 월 $0.5~1 수준  
**신규 가입 시 $5 무료 크레딧 제공**

1. [console.anthropic.com](https://console.anthropic.com) 접속 → 회원가입
2. 좌측 메뉴 **API Keys** → **Create Key**
3. 생성된 키(`sk-ant-...`)를 복사
4. `.env` 파일에 입력:
   ```
   ANTHROPIC_API_KEY=sk-ant-여기에_키_입력
   ```

> 키를 입력하지 않으면 AI 판단 없이 지표만으로 매도합니다.

---

## 텔레그램 알림 설정 (선택)

1. 텔레그램에서 [@BotFather](https://t.me/BotFather) 대화
2. `/newbot` 입력 → 봇 이름 설정 → 토큰 발급
3. [@userinfobot](https://t.me/userinfobot) 에서 본인 채팅 ID 확인
4. `.env` 파일에 입력:
   ```
   TELEGRAM_TOKEN=발급받은_토큰
   TELEGRAM_CHAT_ID=본인_채팅_ID
   ```

---

## 주의사항

- 이 소프트웨어는 **투자 조언이 아닙니다**. 투자 결과에 대한 책임은 사용자 본인에게 있습니다.
- KIS API 키는 절대 타인과 공유하지 마세요.
- `.env` 파일은 절대 GitHub 등에 업로드하지 마세요.
- 자동매매 특성상 예상치 못한 시장 상황에서 손실이 발생할 수 있습니다.

---

## 라이선스

MIT License — 자유롭게 사용, 수정, 배포 가능합니다.
