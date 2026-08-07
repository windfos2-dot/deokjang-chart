# 덕장 차트 (deokjang-chart)

**종목 검색 + 실데이터 차트** 모듈. 국장 데이터(수급·공매도)와 판단 자동화가 목표.
pykrx / KRX / KIS 실데이터 기반.

## 실행

```bash
./start.sh          # 기본 8010 포트, 브라우저 자동 열림
./start.sh 9000     # 포트 지정
```

처음 받은 경우엔 venv부터:

```bash
python3.12 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
```

자격증명은 `.env` 에 넣는다 (`.env.example` 참고). KRX 로그인이 없으면
수급은 KIS 폴백(최근 30영업일)으로 자동 전환된다.

## 구성

| 파일 | 역할 |
|---|---|
| `chart_indicators.py` | 지표 계산 (순수 numpy, 외부 의존 없음) |
| `chart_data_loader.py` | 데이터 로더 (pykrx + KIND + KRX) · TTL 300초 캐시 |
| `chart_router.py` | FastAPI 라우터 `/api/chart/*` |
| `static/chart_live.html` | 프론트 (lightweight-charts 4.2.3) |
| `chart_patterns/` | Bulkowski 차트패턴 스크리너 (73/75 패턴) — 원래 별도 프로젝트였다 |
| `chart_patterns_bridge.py` | 위 스크리너를 단일 종목 차트 오버레이로 변환하는 어댑터 |

## 엔드포인트

```
GET /api/chart/health                              모듈 상태 + KRX 로그인 게이트
GET /api/chart/search?q=삼성전자                     종목 검색
GET /api/chart/ohlcv?ticker=005930&days=280        OHLCV
GET /api/chart/full?ticker=005930&days=280         OHLCV+전지표+수급+패턴 (프론트 메인)
GET /api/chart/ui                                  차트 페이지

GET /api/chart/patterns?ticker=005930&recent=60    Bulkowski 패턴 (탐색 구간 조절용)
GET /api/chart/patterns/catalog                    구현된 73패턴 목록
GET /api/chart/pattern/{pid}                       책 식별규칙·매매전술 원문
```

`/full` 은 `sr_vol_thresh` (기본 20) 로 S/R 돌파 거래량 필터를 조절할 수 있다.

## 지표

**표준** — SMA(5·10·20·50·120·150·200), EMA(50·200), 볼린저(20,2),
RSI(14, Wilder), MA이격도(50), Hull MA(60)

**비표준**
- **스퀴즈 모멘텀 (LazyBear)** — period 20, 선형회귀 엔드포인트, 4색 구분
- **RSI Bear 다이버전스** — 종가 피봇하이(L=R=4) 연속 비교
- **ACP 패턴 (Trendoscope 축소판)** — 지그재그(dev 4.5%) → 피봇 5개 슬라이딩 →
  추세선 최소자승 적합 → 13종 분류(채널/쐐기/삼각형)
- **이치모쿠** (9,26,52,26) — 전환·기준·선행A/B·후행 + 미래 구름 26봉
- **Minervini 추세 템플릿** (52주=260봉) — 8조건 개별 판정
- **S/R + 돌파** — [LuxAlgo 공개 오픈소스 로직](https://www.tradingview.com/script/JDFoWQbL-Support-and-Resistance-Levels-with-Breaks-LuxAlgo/)
  재구현 (pivot 15/15, 거래량 오실 20%, 꼬리 필터 포함)
- **오더블록** — 임펄스 직전 반대색 캔들, 거래량 표기

## Bulkowski 패턴 오버레이

Thomas Bulkowski, *Encyclopedia of Chart Patterns* (3rd ed.) 의 식별규칙을 옮긴
`chart_patterns` 를 단일 종목 차트에 얹은 것이다. 원래 전종목 스크리너라
방향이 반대인데, `chart_patterns_bridge.py` 가 그 간극을 메운다.

- 감지된 패턴을 **외곽선**(방향색)으로 그리고, 선택한 패턴은 **구름대**(캔들 뒤
  음영)로 구간을 강조한다. 트리거·목표는 가격선, 돌파 시점은 캔들 마커.
- 태그에 종합점수(책 통계 45% + 형태적합 35% + 확인도 20%)를, 상세줄에 책의
  평균 수익률·손익분기 실패율·순위를 띄운다. 국면(강세/약세)은 벤치마크 지수의
  200일선 위/아래로 판정한다.
- 탐색 구간 버튼(10/20/60/150봉)은 **패턴의 돌파(또는 완성) 시점**을 최근 몇 봉
  까지 인정할지다. 기본 60봉. 바꾸면 OHLCV 재조회 없이 다시 탐지한다.
- 시세 DB(`chart_patterns/ohlcv.db`, 415MB)는 **스캔 CLI 전용**이다. 차트
  오버레이는 덕장차트가 이미 받은 OHLCV 를 그대로 쓰므로 DB 가 없어도 된다.
  (DB 는 커밋하지 않는다 — `python -m chart_patterns.ohlcv --market ALL --days 400`)

### ⚠ 책 데이터는 이 레포에 없다

패턴 이름·식별규칙·성과통계가 담긴 `chart_patterns/book_patterns.json` 은
Bulkowski 책을 파싱한 결과물이라 **커밋하지 않는다**(`.gitignore`). 이 레포는
공개라서 커밋하면 저작물을 재배포하는 셈이 된다. 책을 가진 사람이 각자 만든다:

```bash
python chart_patterns/extract_book.py     # 본인 PDF 필요
```

파일이 없으면 **패턴 오버레이만 조용히 꺼진다**. 차트·지표·수급·밸류에이션과
공개 사이트(`docs/`)는 이 파일과 무관하게 그대로 동작한다. 켜졌는지는
`/api/chart/health` 의 `patterns_available` 로 확인한다.

**알려진 한계**

- 같은 패턴이 구간 안에서 여러 번 나와도 **패턴 종류당 1건**만 남는다
  (`detectors.run` 이 완성도 기준으로 추린다). 과거 전 구간 라벨링용이 아니다.
- 측정룰 목표가는 `트리거 ± 패턴높이` 라서 키 큰 패턴에서 비현실적인 값이 나온다.
  0 이하로 내려가면 '목표 없음'으로 처리하고, 차트 범위를 벗어나면 숫자만 띄우고
  선은 생략한다. **같은 값이 스크리너 표의 목표여력 칼럼에는 그대로 나온다.**
- 책 통계는 1990~2019 미국 표본이다. 국장 절대 기대치가 아니라 패턴 간 상대
  우선순위로만 쓸 것.

## 데이터 소스

| 데이터 | 소스 | KRX 로그인 |
|---|---|---|
| OHLCV(수정주가) | pykrx → 네이버 | 불필요 |
| 종목 유니버스 2,704개 | KIND 상장법인목록 | 불필요 |
| 수급/공매도/PER | pykrx → KRX | **필수** |
| 신용거래융자 | KRX 직접 | bld 미확정(TODO) |

> pykrx의 `get_market_ticker_list` 는 KRX 로그인이 필요하고
> `get_market_ticker_name` 은 종목당 ~1.9초(2,700종목 ≈ 90분)라
> 유니버스는 KIND 단일 호출(~0.7초)로 우회했다.

## 검증 상태

- `chart_indicators.py` 스모크 테스트 통과, 패턴 분류 13종 전부 발화 확인
- 지그재그 스윙 72개 전부 dev(4.5%) 이상, H/L 교대 정상
- `/full` 실데이터 200 OK (삼성전자 280봉)
- 프론트 실렌더 확인 (5패널 + 8토글)

## 알려진 한계

- **패턴 추세선 외삽** — 윈도우 내 동일 타입 피봇이 2개면 적합선이 두 점을 정확히
  지나 검증을 무조건 통과한다. 이를 윈도우 시작/끝까지 외삽하면 실제 가격대를
  벗어날 수 있다(280봉 중 1건, 최대 2.6배). 레퍼런스 구현의 클리핑 여부 확인 필요.
- **`평균가*5%` 해석** — 지시서 문구가 전체/구간 평균 중 어느 쪽인지 모호해
  스케일 불변인 **구간 평균**을 기본으로 두고 `PATTERN_TOLERANCE_SCOPE` 로 전환 가능하게 했다.
- **S/R 돌파 0건** — 버그 아님. LuxAlgo 거래량 필터(osc>20)가 엄격해서
  삼성전자 280봉에서 osc>20인 봉이 1개뿐이었다. `sr_vol_thresh` 로 조절.
- **5분/1시간봉 불가** — pykrx는 일봉만. 멀티타임프레임 패널은 KIS API 필요.

## 공개 사이트 (GitHub Pages)

스팩 트래커와 같은 방식이다. **Actions 는 쓰지 않는다** — 로컬에서 데이터를 만들어
`docs/` 째로 커밋하면 Pages 가 그대로 서빙한다.

```bash
python publish_chart.py                # 전종목 재빌드 → 커밋 → 푸시
python publish_chart.py --no-build     # 현재 docs/ 만 배포
python publish_chart.py --kr-limit 300 # 상위 300종목만 (빠른 테스트)
```

공개 URL: https://windfos2-dot.github.io/deokjang-chart/ (Pages 소스: `master` 브랜치 `/docs`)

`build_static.py` 는 KRX 오픈API 인증키(`KRX_API_KEY`)가 있으면 그걸 쓰고, 없으면
**`KRX_ID`/`KRX_PW` 로 pykrx 폴백**을 탄다(`kr_collect_pykrx`). 폴백도 오픈API 와
마찬가지로 '날짜 1개 = 전종목' 호출이라 280콜이면 끝난다(약 60초).

> 종목당 JSON 이 ~35KB 라 전종목이면 ~92MB 다. 갱신마다 커밋을 쌓으면 저장소가
> 하루 92MB 씩 불어나므로, `publish_chart.py` 는 기본적으로 직전 **데이터 커밋만**
> amend 로 덮어쓴다(코드 커밋은 건드리지 않는다). 히스토리를 남기려면 `--keep-history`.

로컬 실시간 서버 배포는 [DEPLOY.md](DEPLOY.md) 참조.
