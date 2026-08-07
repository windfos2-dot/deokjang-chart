# 차트패턴 스크리너 (Bulkowski 기반)

Thomas Bulkowski, *Encyclopedia of Chart Patterns* (3rd ed., Wiley) 의
**75개 패턴 식별규칙·성과통계를 기계화**해서 국장·미장 전종목에서
"지금 잡히는 패턴"을 찾아 점수순으로 뽑는다.

```
extract_book.py  PDF → book_patterns.json   (식별규칙 · 성과통계 · 매매전술)
ohlcv.py         국장/미장 일봉 수집 → ohlcv.db
pivots.py        minor high/low, 추세선, ATR, Adam/Eve 판별 등 기하 프리미티브
detectors.py     식별규칙 → 탐지 코드 (73/75 패턴)
registry.py      pid ↔ 책 통계 매핑 + 스코어링
scan.py          유니버스 스캔 CLI
report.py        HTML 리포트 (미니차트 + 필터)
api.py           HTTP API (다른 PC에서 설치 없이 호출)
notify.py        텔레그램 전송 (스캔 결과 / 모듈 zip)
doctor.py        새 PC 설치 점검
```

## 사용법

```bash
# 0) 책 파싱 (최초 1회)
python chart_patterns/extract_book.py

# 1) 시세 수집 (최초 국장 ~10분 / 미장 ~1분, 이후 증분)
python -m chart_patterns.ohlcv --market ALL --days 400
python -m chart_patterns.ohlcv --status

# 2) 스캔
python -m chart_patterns.scan --market KR --recent 10 --top 60
python -m chart_patterns.scan --market US --pattern cup_handle,db_ee,hs_bottom
python -m chart_patterns.scan --market ALL --recent 10 --min-score 62 \
       --html output/chart_patterns.html --json output/chart_patterns.json
```

주요 옵션

| 옵션 | 뜻 |
|---|---|
| `--recent N` | 돌파(또는 패턴 완성)가 최근 N봉 이내인 것만. 스크리너 성격상 필수 |
| `--pattern` | 쉼표구분 pid 필터 (`triangle_asc,db_ee,…`) |
| `--direction` | `up` / `down` |
| `--min-value` | 최근 20일 평균 거래대금 하한 (국장 기본 5억원, 미장 500만달러) |
| `--min-cap` / `--max-cap` | 시가총액 밴드. **국장 억원 / 미장 백만달러** 단위 |
| `--index` | 지수 구성종목만 (미장 `russell3000`) |
| `--min-score` | 종합점수 하한. 60~65가 실전 체감상 적당 |

시가총액 예시 — 국장 `--min-cap 3000`(3천억 이상) · `--min-cap 50000`(5조 이상),
미장 `--min-cap 50000`(500억달러 이상). 시총 정보가 없는 종목은 필터를 걸면 제외된다.

> 시가총액은 **가장 최근 영업일 기준**으로 저장된다. 백필은 과거로 거슬러
> 올라가므로 그냥 덮어쓰면 제일 오래된 시총이 남는다 — `_upsert_universe` 가
> `updated` 를 비교해 최신 값만 남긴다. 시총만 새로 맞추려면:
> `python -m chart_patterns.ohlcv --market ALL --caps`

## API 로 쓰기 (모듈 설치 없이)

시세 DB(172MB)와 스캔 연산을 **서버 한 곳에만** 두고, 다른 PC는 HTTP 로 호출한다.
클라이언트에 필요한 건 `curl` 뿐이다.

```bash
# 서버 (로컬 전용)
python -m chart_patterns.api                       # 127.0.0.1:8020

# 서버 (외부 공개 — 키 필수)
CHART_API_KEY=<임의의긴문자열> \
  uvicorn chart_patterns.api:app --host 0.0.0.0 --port 8020
```

띄운 뒤 브라우저로 **<http://127.0.0.1:8020>** 을 열면 첫 화면이 나온다.
시장·방향·최근 N봉·최소 점수·패턴(복수 선택)을 골라 `리포트 보기` 를 누르면
미니차트가 붙은 HTML 리포트로 넘어간다. CLI 를 몰라도 쓸 수 있는 경로.

| 엔드포인트 | 용도 |
|---|---|
| `GET /` | **홈 — 조건 선택 폼 (브라우저용)** |
| `GET /healthz` | 시세 커버리지·구현 패턴 수 (무인증) |
| `GET /api/patterns` | 패턴 목록 + 한글명 + 책 통계 |
| `GET /api/patterns/{pid}` | 그 패턴의 **책 식별규칙·measure rule 원문** |
| `GET /api/scan` | 스캔 결과 JSON (`min_cap`/`max_cap` 포함) |
| `GET /api/report` | 스캔 결과 HTML (브라우저로 바로) |
| `GET /docs` | 자동 생성 API 문서 (Swagger) |

```bash
curl "http://<서버>:8020/api/scan?market=KR&recent=10&min_score=62&top=20"
curl "http://<서버>:8020/api/scan?market=US&pattern=cup_handle,hs_bottom&direction=up"
curl "http://<서버>:8020/api/patterns/cup_handle"          # 책 원문
# 브라우저:  http://<서버>:8020/api/report?market=KR&top=60
```

`--pattern`, `--direction`, `--min-score`, `--min-value`, `--min-price` 등
CLI 옵션이 그대로 쿼리 파라미터다. `charts=true` 를 주면 미니차트용 종가 배열도 온다.

**캐시**: 시세가 T+1 이라 동일 조건 재호출은 30분 캐시로 응답한다
(국장 첫 호출 30초 → 재호출 0.2초). 시세 DB의 최신 일자가 바뀌면 자동 무효화된다.

**인증**: `CHART_API_KEY` 를 설정하면 모든 `/api/*` 가 `X-API-Key` 헤더 또는
`?key=` 를 요구한다. 미설정이면 인증 없음 — **외부에 열 거면 반드시 설정할 것.**

## 다른 PC / 서버에서 돌리기

옮겨야 하는 건 **이 폴더의 코드 + `book_patterns.json` + `us_universe.csv`** 뿐이다.
시세 DB(`ohlcv.db`, 172MB)와 PDF는 **안 옮겨도 된다** — API로 재생성된다.

```bash
# 1) 파일 이동 — 셋 중 하나

#  (a) 텔레그램으로 모듈 zip 전송 (본인 채팅). 160KB, 받아서 압축만 풀면 됨
python -m chart_patterns.notify --module
python -m chart_patterns.notify --module --zip-only   # 전송 없이 zip 만

#  (b) git
git add chart_patterns && git commit -m "add chart pattern screener" && git push
#      → 새 PC:  git clone … && cd telegram_bot

#  (c) 폴더 복사 (ohlcv.db 는 빼고)

# 2) 의존성
pip install -r chart_patterns/requirements.txt

# 3) 국장 쓰려면 data.go.kr 서비스키
#    .env 파일에  DART_API_KEY=<키>      (또는 환경변수로)

# 4) 점검 — 부족한 것과 실행할 명령을 찍어준다
python -m chart_patterns.doctor

# 5) 시세 수집 (국장 ~10분 / 미장 ~1분)
python -m chart_patterns.ohlcv --market ALL --days 400

# 6) 스캔
python -m chart_patterns.scan --market ALL --recent 10 --min-score 62
```

알아둘 것

- `book_patterns.json` 이 있으면 **PDF도 PyMuPDF도 필요 없다.** 책을 다시 파싱할
  때만 필요하고, 그때는 `python -m chart_patterns.extract_book <PDF경로>` 또는
  환경변수 `BULKOWSKI_PDF` 로 경로를 준다 (하드코딩된 경로 없음).
- `us_market.db` 가 없는 PC에서는 동봉된 `us_universe.csv`(S&P500)로 자동 폴백한다.
- 윈도우 cp949 콘솔에서도 안 깨지게 CLI가 stdout을 UTF-8로 강제한다
  (`PYTHONIOENCODING` 안 걸어도 됨).
- 리눅스 서버(systemd)도 동일하다. 경로는 전부 `pathlib` 상대경로라
  `/home/ubuntu/telegram_bot` 에 그대로 올라간다.
- **`.env` 는 git에 안 올라간다** (`.gitignore`). 새 PC에는 수동으로 넣어야 한다.
- ⚠ `book_patterns.json` 은 저작권 있는 책에서 추출한 식별규칙·통계 원문을
  담고 있다. **공개 저장소에는 올리지 말 것.**

검증: 코드+JSON+CSV만 있는 빈 폴더에서 위 순서대로 실행해 미장 25만행 수집 →
스캔 704건까지 재현 확인함.

## 데이터 소스

- **국장**: 금융위 주식시세정보 API(data.go.kr). 일자별 전종목 스냅샷을
  한 번에 받으므로 1년치 전체 시장을 ~10분에 백필. `.env` 의 `DART_API_KEY`
  를 서비스키로 사용한다. **T+1 반영**(오늘 장 마감분은 다음 영업일에 들어옴).
- **미장 시세**: yfinance 벌크 다운로드.
- **미장 유니버스**: 두 가지.
  - `sp500` (기본) — 기존 `us_market.db` / 동봉 `us_universe.csv`, 503종목
  - `russell3000` — **미국 상장 보통주 시총 상위 3,000**, 3,000종목

  러셀 3000 목록은 직접 만든다. FTSE Russell 이 구성종목을 무료 배포하지 않고
  iShares(IWV) 보유종목 CSV 는 봇 차단이 걸려 있어서:
  1. NASDAQ Trader 심볼 디렉터리에서 **보통주만** 추출 (ETF·워런트·우선주·
     테스트이슈 제외 → 4,938종목)
  2. NASDAQ 스크리너 API 로 **전 종목 시총을 1회 호출**로 수집 (5,829종목)
  3. 교집합을 시총순 정렬 → 상위 3,000

  ```bash
  python -m chart_patterns.us_universe --build --top 3000
  python -m chart_patterns.us_universe --show
  python -m chart_patterns.ohlcv --market US --days 500 --universe russell3000
  ```

  > 실제 러셀 3000 과 완전히 같지는 않다. 러셀은 **유동주식 조정 시총**을 쓰고
  > **매년 6월에만 정기변경**하며, 외국 소재 기업·일부 주식종류를 배제하는
  > 별도 규칙이 있다. 이 목록은 '현재 시총 상위 3,000 미국 보통주'다.
  >
  > 시총 조회에 yfinance `fast_info` 를 쓰면 안 된다 — 수천 건을 때리면
  > 야후가 막는다(실측 4,938건 중 1,503건만 성공). NASDAQ 스크리너를 쓴다.

> pykrx 의 전종목 엔드포인트는 현재 빈 값을 반환해 쓰지 않는다
> (개별종목 기간조회는 정상 동작).

## 점수 산식

```
점수 = 100 × (0.45 × 책통계 + 0.35 × 형태적합도 + 0.20 × 돌파확인)
```

- **책통계**: 현재 시장국면(강세/약세) × 돌파방향에 해당하는
  평균 수익률과 손익분기 실패율. 예) 이중바닥 Eve&Eve 상승돌파 =
  평균 +49.7%, 실패율 11.7%, 성과순위 5위.
- **형태적합도**: 각 탐지기가 계산하는 0~1 값 (추세선 터치 수, 바닥 일치도,
  곡선 적합도 R², 피보나치 비율 오차 등).
- **돌파확인**: 확정 돌파 1.0 / 형성중 0.5. 책이 명시한 거래량 추세와
  실제가 일치하면 +0.15.
- 이미 measure rule 목표가를 지나친 셋업은 ×0.72 감점.

시장국면은 유니버스 중앙값의 200일선 위/아래로 판정한다.

## 결과 필드에서 꼭 볼 것

| 필드 | 뜻 |
|---|---|
| `status` | `breakout` = 이미 돌파 / `forming` = 아직 경계 안쪽 |
| `trigger` | 돌파 판정 기준가(넥라인·보드 상단·컵 테두리 등) |
| `to_trigger_pct` | (형성중) 현재가에서 트리거까지 남은 거리 |
| `since_breakout_pct` | (돌파) 돌파 종가 대비 현재가 |
| `back_inside` | 돌파 후 되돌림으로 경계 안쪽에 다시 들어옴 → **진입 재검토 신호** |
| `upside_pct` | measure rule 목표가까지의 여력. 목표를 이미 지났으면 음수 |

`to_trigger_pct` 가 크면(예: +500%) 패턴 구조는 성립하지만 아직 트리거가
한참 멀다는 뜻이다. 다이빙 보드처럼 급락 후 회복을 기다리는 패턴에서 흔하다.

## 구현 범위

**73/75 구현.** 미구현 2개와 이유:

| 패턴 | 이유 |
|---|---|
| Cloudbank | 월봉 기준 *수년* 저항대 + 40% 급락이 조건. 현재 캐시(국장 1.5년/미장 3년)로는 판정 불가 |
| Three Peaks and Domed House | 28개 전환점을 1.5년에 걸쳐 세는 패턴. 규칙 자체가 서술적이라 오탐 대비 실익 낮음 |

일봉이 아닌 **주봉** 기준 패턴(책의 지시대로): 파이프 바닥/천정,
혼 바닥/천정, 다이빙 보드.

## 튜닝 포인트

임계값은 전부 `detectors.py` 상단·각 섹션 첫머리 상수로 노출돼 있다.
탐지가 너무 많으면 조이고, 너무 적으면 푼다.

| 상수 | 영향 |
|---|---|
| `TOL_ATR` | 추세선 터치 허용오차. 작을수록 엄격 |
| `FLAT` / `TILT` | 수평/경사 판정 경계 → 삼각형·쐐기·확대형 분류 |
| `CONVERGE` | 수렴·확산 배율 |
| `HARM_TOL` | 하모닉 피보나치 비율 허용오차 (0.038 = ±3.8%) |
| `ROUND_R2` | 원형바닥·컵의 2차곡선 적합도 하한 |
| `DB_MAX_VAR` / `DB_MIN_RISE` / `DB_MAX_RISE` | 이중바닥·천정의 바닥 일치도와 높이 밴드 |

## 한계 (읽고 쓸 것)

1. **책의 성과통계는 1990~2019년 미국 시장 표본**이다. 국장에 그대로
   적용되지 않는다. 점수의 책통계 항목은 '패턴 간 상대 우선순위' 용도로만
   보고, 절대 수익률 기대치로 읽지 말 것.
2. **탐지 = 검증이 아니다.** 규칙 기반 탐지는 사람 눈으로 보는 패턴과
   경계 사례에서 갈린다. 상위 후보를 실제 차트로 확인하는 절차가 필요하다.
3. **국장 데이터는 T+1**. 당일 장중 스캔은 불가.
4. 이 도구는 패턴 탐색기이지 매매 신호가 아니다.
