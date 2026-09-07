# 하나투어 경쟁상품 가격 모니터 (GitHub Actions)

부산출발 부관훼리 **후쿠오카/벳부/유후인 4일** 상품의 출발일별 가격을 하루 2회(09:00·18:00) 자동 조회하고,
가격이 바뀌면 즉시, 변동이 없으면 2일에 한 번 "이상 없음" 메일을 보냅니다.
PC를 켜둘 필요 없이 GitHub 무료 서버에서 돌아갑니다.

## 동작 원리

하나투어 상품 페이지가 내부적으로 호출하는 상품목록 API(JSON)를 그대로 사용합니다.
브라우저 없이 조회되므로 가볍고 안정적입니다. 대상 상품은 상품코드 규칙
`JKP4432` + 출발일(YYMMDD) + `P0D` 로 골라냅니다 (config.json 에서 변경 가능).

수집 항목: 성인/아동/유아 가격, 잔여석/총좌석, 출발확정 여부, 상품코드(→ 상품 페이지 링크)

## 설치 (약 10분)

### 1. GitHub 저장소 만들기
1. https://github.com/new 접속
2. Repository name: `hanatour-price-monitor`, **Public** 선택 (카카오톡 알림용 Claude 예약작업이 결과 파일을 읽으려면 Public 이어야 합니다. 가격 정보만 저장되므로 민감정보 없음)
3. 다른 옵션은 건드리지 않고 **Create repository**

### 2. 파일 업로드
1. 새 저장소 화면에서 **uploading an existing file** 링크 클릭 (또는 Add file → Upload files)
2. 이 폴더 안의 파일을 **폴더째** 드래그 (`.github` 폴더가 포함되어야 합니다 — 탐색기에서 숨김 파일 표시가 꺼져 있으면 `.github` 폴더가 안 보일 수 있으니 확인)
   - 필요한 파일: `.github/workflows/monitor.yml`, `monitor.py`, `config.json`, `requirements.txt`, `README.md`, `data/.gitkeep`
3. **Commit changes**

> 드래그가 안 되면 GitHub Desktop 을 쓰거나, 저장소에서 Add file → Create new file 로 경로 `.github/workflows/monitor.yml` 을 직접 입력해 내용을 붙여넣어도 됩니다.

### 3. Gmail 앱 비밀번호 만들기 (발신 계정)
1. 발신에 쓸 Google 계정에서 https://myaccount.google.com/security → **2단계 인증**이 켜져 있어야 합니다
2. https://myaccount.google.com/apppasswords → 앱 이름 `hanatour-monitor` 입력 → 만들기
3. 표시되는 16자리 비밀번호를 복사 (공백 제거)

### 4. GitHub Secrets 등록
저장소 → **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `SMTP_USER` | 발신 Gmail 주소 (예: yangsiri75@gmail.com) |
| `SMTP_PASS` | 3번에서 만든 16자리 앱 비밀번호 |
| `MAIL_TO` | 수신자 이메일. 여러 명은 쉼표로 구분 (예: `yangsiri75@gmail.com, staff@ejemtour.co.kr`) |

### 5. 첫 실행 (테스트)
1. 저장소 → **Actions** 탭 → 좌측 `hanatour-price-monitor` → **Run workflow** → `force_notify` 에 `1` 입력 → Run
2. 1분 내 초록색 체크 표시가 뜨고, 메일이 도착하면 성공
3. 이후 매일 09:00 / 18:00 자동 실행 (GitHub 사정으로 몇 분~수십 분 지연될 수 있음)

## 결과 파일 (data/ 폴더, 자동 커밋)

| 파일 | 내용 |
|---|---|
| `data/report.md` | 최신 리포트 (변동 내역 + 전체 가격표). GitHub 에서 바로 표로 보임 |
| `data/changes.json` | 최신 실행 요약 (카카오톡 알림용 Claude 예약작업이 읽는 파일) |
| `data/latest.json` | 직전 스냅샷 (비교 기준) |
| `data/history.csv` | 전체 이력 누적 — 엑셀로 열어 날짜별 가격 추이 분석 가능 |
| `data/state.json` | 마지막 알림 시각 |

## 설정 변경 (config.json)

- `days_ahead`: 오늘부터 며칠 앞까지 조회할지 (기본 150일)
- `notify.heartbeat_days`: 변동 없을 때 "이상 없음" 메일 주기 (기본 2일)
- `products`: 감시 상품 추가. 예) 3일 상품을 추가하려면
  ```json
  {"id": "fukuoka-shimonoseki-3d", "label": "후쿠오카/시모노세키 3일", "code_prefix": "JKP4332", "code_suffix": "P0J"}
  ```
  상품코드는 하나투어 상품 페이지 URL 의 `pkgCd=` 값에서 앞 7자리(접두)와 뒤 3자리(접미)를 씁니다.
- 실행 시각 변경: `.github/workflows/monitor.yml` 의 cron (UTC 기준, 한국시간 −9시간)

## 문제가 생기면

- Actions 실행이 빨간색 ✗: 실행 클릭 → 로그 확인. `대상 상품이 0건` 이면 하나투어가 상품코드 규칙을 바꾼 것 → config.json 의 접두/접미 수정
- 메일이 안 옴: Secrets 이름 오타, 앱 비밀번호 공백 포함 여부, 스팸함 확인
- 하나투어가 GitHub 서버 IP 를 차단하는 경우(로그에 403/timeout): 알려주시면 다른 실행 방식으로 전환
