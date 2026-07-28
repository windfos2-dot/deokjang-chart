# 배포 가이드 — EMS 차트 모듈

## ⚠️ 보호 파일 관련 (지시서 §1)

이 모듈은 **신규 파일만** 생성했다. 아래 보호 파일은 **열람조차 하지 않았다**
(이 맥에 EMS 코드베이스가 없어서 물리적으로 접근 불가):

`server.py`, `ipo_report_router.py`, `ipo_report_storage.py`, `dart_loader.py`,
`dart_mezzanine_loader.py`, `dart_xbrl_loader.py`, `ipo_loader.py`,
`ipo_lockup_loader.py`, `dart_corp_codes.json`, `dart_cache.json`

**DART 호출 없음.** 이 모듈은 DART를 전혀 사용하지 않는다.

---

## 1. 파일 배치

```
~/web/chart_indicators.py
~/web/chart_data_loader.py
~/web/chart_router.py
~/web/static/chart_live.html
```

`static/` 경로가 EMS 관례와 다르면 `chart_router.py` 의 `_HTML_PATH` 만 수정하면 된다.

## 2. 의존성

기존 venv 에서:

```bash
pip install pykrx numpy requests
```

`fastapi` / `uvicorn` 은 기존 EMS 것을 그대로 쓴다.

## 3. ⛔ server.py 한 줄 — 사용자 승인 필요

`server.py` 는 보호 파일이라 **내가 직접 수정하지 않았다.** 아래 두 줄을
승인 후 직접 추가해야 라우터가 붙는다:

```python
from chart_router import router as chart_router
app.include_router(chart_router)
```

> 라우터 자동등록 메커니즘이 EMS에 이미 있으면 그것을 쓰는 편이 낫다.
> (이 맥에 EMS가 없어 자동등록 여부를 조사하지 못했다 — 서버에서 확인 필요)

## 4. KRX 계정 설정 (수급 기능에 필수)

**실측 확인(2026-07-28):** `data.krx.co.kr` 는 비로그인 요청에 `HTTP 400` +
본문 `LOGOUT` 을 반환한다. 따라서 아래 기능은 KRX 계정 없이는 동작하지 않는다:

| 기능 | KRX 로그인 | 비고 |
|---|---|---|
| OHLCV | 불필요 | 네이버 소스 (`adjusted=True`) |
| 종목검색 유니버스 | 불필요 | KIND 우회 |
| 전 지표 계산 | 불필요 | OHLCV 기반 |
| **수급(투자자별)** | **필수** | 이 모듈의 차별점 |
| **공매도 잔고** | **필수** | |
| PER/PBR | 필수 | |

서버에 환경변수 설정:

```bash
export KRX_ID='발급받은ID'
export KRX_PW='비밀번호'
```

systemd/launchd 로 EMS를 띄운다면 서비스 파일의 `Environment=` 에 추가해야 한다.
설정 후 `GET /api/chart/health` 의 `krx_login_configured` 가 `true` 인지 확인.

## 5. 프론트 API_BASE

`static/chart_live.html` 상단:

```js
const API_BASE = window.location.origin;
```

EMS와 다른 도메인에서 서빙하면 EMS 주소로 바꾼다. CORS 미들웨어는
단독 실행 앱에만 붙어 있으므로, EMS 본체에 CORS가 없고 크로스도메인으로
쓸 거라면 EMS 쪽 설정이 별도로 필요하다.

## 6. 검증

```bash
python chart_indicators.py                    # 지표 스모크 테스트
python chart_data_loader.py 005930            # 로더 실호출
uvicorn chart_router:app --port 8010          # 단독 기동 후 /api/chart/ui
```

## 7. 미해결 TODO

- [ ] **신용거래융자 bld 미확정** — `chart_data_loader._CREDIT_BLD = None`.
      KRX Network 탭에서 실제 bld 확인 후 교체해야 신용 패널이 켜진다.
      (추측 금지 지시에 따라 placeholder 유지)
- [ ] 반대매매(KOFIA) — 이번 범위 제외
- [ ] 패턴 추세선 외삽 한계 (아래 "알려진 한계" 참조)
- [ ] Minervini RS(c8) — 벤치마크 지수 필요, KRX 로그인 시 가능
