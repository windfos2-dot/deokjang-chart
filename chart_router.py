"""
chart_router.py — EMS 차트 모듈 FastAPI 라우터 (/api/chart/*)

지시서 §5 스펙:
    GET /api/chart/search?q=삼성전자
    GET /api/chart/ohlcv?ticker=005930&days=280
    GET /api/chart/full?ticker=005930&days=280

헌법 준수 (§1):
    - 신규 파일만 생성. 기존 EMS 파일(server.py 등)은 건드리지 않는다.
    - 네임스페이스는 /api/chart/* 로 IPO(/api/ipo-report/*)와 분리.
    - DART 호출 없음.

EMS 편입 방법 (server.py 는 보호 파일이므로 사용자 승인 후 적용):
    from chart_router import router as chart_router
    app.include_router(chart_router)

단독 실행 (검증용):
    uvicorn chart_router:app --port 8010
"""

from __future__ import annotations

import os
import traceback

from fastapi import APIRouter, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

import chart_data_loader as loader
import chart_indicators as ind

router = APIRouter(prefix="/api/chart", tags=["chart"])

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_HTML_PATH = os.path.join(_BASE_DIR, "static", "chart_live.html")


# ---------------------------------------------------------------------------
# 상태
# ---------------------------------------------------------------------------
@router.get("/mtf")
def mtf(ticker: str = Query(..., min_length=6, max_length=6)):
    """멀티타임프레임 신호 (5분 / 60분 / 일봉).

    분봉은 KIS 전용이라 KIS 미설정이면 일봉만 반환한다.
    """
    frames = []

    # --- 일봉 (pykrx) ---
    try:
        o = loader.get_ohlcv(ticker, days=280)
        sig = ind.timeframe_signals(o["high"], o["low"], o["close"], o["volume"])
        sig["tf"] = "일봉"
        frames.append(sig)
    except Exception as e:  # noqa: BLE001
        frames.append({"tf": "일봉", "available": False, "reason": str(e)})

    # --- 분봉 (KIS) ---
    if loader.kis_configured():
        for minutes, label in ((5, "5분"), (60, "60분")):
            try:
                bars = loader.get_intraday(ticker, tf_minutes=minutes, bars=80)
                sig = ind.timeframe_signals([b["h"] for b in bars],
                                            [b["l"] for b in bars],
                                            [b["c"] for b in bars],
                                            [b["v"] for b in bars])
                sig["tf"] = label
                sig["last_dt"] = bars[-1]["dt"] if bars else None
                frames.append(sig)
            except Exception as e:  # noqa: BLE001
                frames.append({"tf": label, "available": False, "reason": str(e)})
    else:
        for label in ("5분", "60분"):
            frames.append({"tf": label, "available": False,
                           "reason": "KIS 미설정 (분봉은 KIS 전용)"})

    order = {"5분": 0, "60분": 1, "일봉": 2}
    frames.sort(key=lambda f: order.get(f.get("tf"), 9))
    return {"ticker": ticker, "frames": frames}


@router.get("/intraday")
def intraday(ticker: str = Query(..., min_length=6, max_length=6),
             tf: int = Query(5, ge=1, le=240),
             bars: int = Query(120, ge=10, le=400)):
    """N분봉 OHLCV (KIS 전용)."""
    try:
        data = loader.get_intraday(ticker, tf_minutes=tf, bars=bars)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"분봉 조회 실패: {e}") from e
    return {"ticker": ticker, "tf_minutes": tf, "bars": data}


@router.get("/health")
def health():
    """모듈 상태 + KRX 로그인 게이트 상태."""
    krx = loader.krx_login_configured()
    kis = loader.kis_configured()
    if krx:
        supply = "KRX(pykrx) — 전 기간"
    elif kis:
        supply = f"KIS 폴백 — 최근 {loader.KIS_MAX_DAYS}영업일만"
    else:
        supply = "불가 — KRX_ID/KRX_PW 또는 KIS_APP_KEY/SECRET 필요"
    return {
        "ok": True,
        "krx_login_configured": krx,
        "kis_configured": kis,
        "supply_source": supply,
        "note": None if krx else loader._KRX_LOGIN_HINT,
    }


# ---------------------------------------------------------------------------
# 검색
# ---------------------------------------------------------------------------
@router.get("/search")
def search(q: str = Query(..., min_length=1), limit: int = Query(20, ge=1, le=100)):
    """종목명/코드 검색 -> {results: [{code, name, market}]}"""
    try:
        return {"results": loader.search_ticker(q, limit=limit)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"검색 실패: {e}") from e


# ---------------------------------------------------------------------------
# OHLCV
# ---------------------------------------------------------------------------
@router.get("/ohlcv")
def ohlcv(ticker: str = Query(..., min_length=6, max_length=6),
          days: int = Query(280, ge=30, le=2000)):
    try:
        return loader.get_ohlcv(ticker, days=days)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"OHLCV 조회 실패({ticker}): {e}") from e


# ---------------------------------------------------------------------------
# 전체 (프론트 메인)
# ---------------------------------------------------------------------------
@router.get("/full")
def full(ticker: str = Query(..., min_length=6, max_length=6),
         days: int = Query(280, ge=30, le=2000),
         sr_vol_thresh: float = Query(20.0, ge=-100.0, le=500.0)):
    """OHLCV + 전체 지표 + 수급.

    OHLCV 실패만 치명적. 수급/공매도/신용은 실패해도 차트는 뜨게
    각각 try/except 로 분리한다 (지시서 §5).
    """
    # --- 필수: OHLCV ---
    try:
        o = loader.get_ohlcv(ticker, days=days)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"OHLCV 조회 실패({ticker}): {e}") from e

    # --- 옵션: 벤치마크 지수 (Minervini RS용). 실패해도 차트는 떠야 한다 ---
    bench = None
    bench_note = None
    try:
        bench = loader.get_benchmark_close(o["dates"], market=o.get("market") or "KOSPI")
    except Exception as e:  # noqa: BLE001
        bench_note = f"벤치마크 미적용(RS 조건 제외): {e}"

    # --- 필수: 지표 ---
    try:
        indicators = ind.compute_all(
            o["dates"], o["open"], o["high"], o["low"], o["close"], o["volume"],
            sr_vol_thresh=sr_vol_thresh, benchmark_close=bench,
        )
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"지표 계산 실패: {e}") from e

    payload = {
        "ticker": ticker,
        "name": o["name"],
        "market": o["market"],
        "days": days,
        "ohlcv": {
            "dates": o["dates"], "open": o["open"], "high": o["high"],
            "low": o["low"], "close": o["close"], "volume": o["volume"],
        },
        "indicators": indicators,
        "krx_login_configured": loader.krx_login_configured(),
        "benchmark_note": bench_note,
    }

    # --- 옵션: 수급 ---
    try:
        payload["trading"] = {"available": True, **loader.get_trading(ticker, days=days)}
    except loader.KrxLoginRequired as e:
        payload["trading"] = {"available": False, "reason": str(e)}
    except Exception as e:  # noqa: BLE001
        payload["trading"] = {"available": False, "reason": f"수급 조회 실패: {e}"}

    # --- 옵션: 공매도 잔고 ---
    try:
        payload["shorting"] = {"available": True, **loader.get_shorting_balance(ticker, days=days)}
    except loader.KrxLoginRequired as e:
        payload["shorting"] = {"available": False, "reason": str(e)}
    except Exception as e:  # noqa: BLE001
        payload["shorting"] = {"available": False, "reason": f"공매도 조회 실패: {e}"}

    # --- 옵션: 포워드 컨센서스 + 포워드 PER/PBR ---
    try:
        payload["estimates"] = loader.get_forward_estimates(ticker)
    except Exception as e:  # noqa: BLE001
        payload["estimates"] = {"available": False, "reason": f"추정실적 조회 실패: {e}"}

    # --- 옵션: 신용잔고 (bld 미확정 -> 항상 비활성) ---
    try:
        payload["credit"] = loader.get_credit_balance(ticker, days=days)
    except Exception as e:  # noqa: BLE001
        payload["credit"] = {"available": False, "reason": f"신용 조회 실패: {e}"}

    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# 프론트 서빙 (EMS static 관례와 별개로 라우터 자체 제공)
# ---------------------------------------------------------------------------
@router.get("/ui")
def ui():
    if not os.path.exists(_HTML_PATH):
        raise HTTPException(status_code=404, detail="chart_live.html 없음")
    return FileResponse(_HTML_PATH, media_type="text/html")


# ---------------------------------------------------------------------------
# 단독 실행용 앱 (검증 전용). EMS 편입 시에는 router 만 사용한다.
# ---------------------------------------------------------------------------
app = FastAPI(title="EMS Chart Module (standalone)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)
app.include_router(router)


@app.get("/")
def root():
    return ui()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8010)
