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
from datetime import date
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

    # --- 주봉 / 월봉 (일봉 재집계) ---
    try:
        o = loader.get_ohlcv(ticker, days=1200)      # 월봉 판정에 충분한 기간
        def _iso_week(d):
            y, w, _ = date(int(d[:4]), int(d[5:7]), int(d[8:10])).isocalendar()
            return f"{y}-W{w:02d}"

        for key_fn, label in ((_iso_week, "주봉"), (lambda d: d[:7], "월봉")):
            buckets = {}
            for i, dt in enumerate(o["dates"]):
                k = key_fn(dt)
                b = buckets.get(k)
                if b is None:
                    buckets[k] = {"h": o["high"][i], "l": o["low"][i], "c": o["close"][i],
                                  "v": o["volume"][i]}
                else:
                    b["h"] = max(b["h"], o["high"][i])
                    b["l"] = min(b["l"], o["low"][i])
                    b["c"] = o["close"][i]
                    b["v"] += o["volume"][i]
            ks = sorted(buckets)
            sig = ind.timeframe_signals([buckets[k]["h"] for k in ks],
                                        [buckets[k]["l"] for k in ks],
                                        [buckets[k]["c"] for k in ks],
                                        [buckets[k]["v"] for k in ks])
            sig["tf"] = label
            frames.append(sig)
    except Exception as e:  # noqa: BLE001
        for label in ("주봉", "월봉"):
            frames.append({"tf": label, "available": False, "reason": str(e)})

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

    order = {"5분": 0, "60분": 1, "일봉": 2, "주봉": 3, "월봉": 4}
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


@router.get("/bands")
def bands(ticker: str = Query(..., min_length=6, max_length=6),
          years: int = Query(15, ge=1, le=20),
          basis: str = Query("FY", pattern="^(FY|LTM)$")):
    """역사적 밸류에이션 밴드 (PER/PBR/POR/ROE).

    월말 시가총액 시계열 × DART 연간 재무로 산출한다.
    """
    try:
        return loader.get_valuation_bands(ticker, years=years, basis=basis)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"밴드 조회 실패: {e}") from e


@router.get("/signals")
def signals(ticker: str = Query(..., min_length=6, max_length=6),
            basis: str = Query("LTM", pattern="^(FY|LTM)$")):
    """신호등 — 일/주/월봉 각각의 추세(Minervini) + 밸류에이션 위치.

    추세등: Minervini 추세템플릿 8조건 통과율
    밸류등: 역사적 PER 밴드에서 현재 위치(z-score). 낮을수록 초록.
    """
    out = {"ticker": ticker, "frames": [], "valuation": None}

    # --- 타임프레임별 추세 ---
    try:
        o = loader.get_ohlcv(ticker, days=2000)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"OHLCV 실패: {e}") from e

    def _bucket(key_fn):
        b, order = {}, []
        for i, dt in enumerate(o["dates"]):
            k = key_fn(dt)
            if k not in b:
                b[k] = {"h": o["high"][i], "l": o["low"][i], "c": o["close"][i]}
                order.append(k)
            else:
                b[k]["h"] = max(b[k]["h"], o["high"][i])
                b[k]["l"] = min(b[k]["l"], o["low"][i])
                b[k]["c"] = o["close"][i]
        return ([b[k]["h"] for k in order], [b[k]["l"] for k in order],
                [b[k]["c"] for k in order])

    def _iso_week(d):
        y, w, _ = date(int(d[:4]), int(d[5:7]), int(d[8:10])).isocalendar()
        return f"{y}-W{w:02d}"

    # Minervini 템플릿은 일봉 기준 지표다. 주/월봉에 50/150/200 을 그대로 쓰면
    # MA200 이 200주(4년)·200개월(16년)이 되어 의미가 달라지므로 기간을 환산한다.
    #   일봉 50/150/200, 1개월=22봉  ->  주봉 10/30/40, 4봉  ->  월봉 3/7/10, 1봉
    frames = [
        ("일봉", (o["high"], o["low"], o["close"]), 260, (50, 150, 200, 22)),
        ("주봉", _bucket(_iso_week), 52, (10, 30, 40, 4)),
        ("월봉", _bucket(lambda d: d[:7]), 12, (3, 7, 10, 1)),
    ]

    for label, (h, l, c), lookback, (mf, mm, ms, rb) in frames:
        try:
            mv = ind.minervini_trend_template(
                c, lookback=min(lookback, max(len(c) - 1, 2)),
                ma_fast=mf, ma_mid=mm, ma_slow=ms, rise_bars=rb)
            latest = mv.get("latest")
            if not latest:
                out["frames"].append({"tf": label, "available": False,
                                      "reason": f"봉 부족({len(c)}개)"})
                continue
            crit = [v for k, v in latest.items() if k.startswith("c") and v is not None]
            passed = sum(1 for v in crit if v)
            total = len(crit)
            ratio = passed / total if total else 0
            light = "green" if ratio >= 0.75 else ("yellow" if ratio >= 0.5 else "red")
            out["frames"].append({
                "tf": label, "available": True, "bars": len(c),
                "passed": passed, "total": total, "pass_all": bool(latest.get("pass")),
                "light": light, "criteria": {k: v for k, v in latest.items()
                                             if k.startswith("c")},
            })
        except Exception as e:  # noqa: BLE001
            out["frames"].append({"tf": label, "available": False, "reason": str(e)})

    # --- 밸류에이션 위치 ---
    try:
        b = loader.get_valuation_bands(ticker, basis=basis)
        if b.get("available"):
            vs = {}
            for m, arr in b["series"].items():
                if len(arr) < 24:
                    continue
                vals = [x[1] for x in arr]
                mean = sum(vals) / len(vals)
                sd = (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5
                cur = vals[-1]
                z = (cur - mean) / sd if sd else 0.0
                pct = sum(1 for v in vals if v <= cur) / len(vals) * 100
                # 밸류는 낮을수록 좋다(ROE 는 반대)
                good_low = m != "ROE"
                light = ("green" if (z <= -0.5) else "yellow" if z < 1.0 else "red") \
                    if good_low else \
                    ("green" if z >= 0.5 else "yellow" if z > -1.0 else "red")
                vs[m] = {"current": round(cur, 2), "mean": round(mean, 2),
                         "z": round(z, 2), "pct": round(pct, 1), "light": light}
            out["valuation"] = {"basis": b.get("basis"), "metrics": vs,
                                "forward": b.get("forward")}
    except Exception as e:  # noqa: BLE001
        out["valuation"] = {"error": str(e)}

    return out


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
         days: int = Query(280, ge=30, le=6000),
         tf: str = Query("D", pattern="^[DWM]$"),
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

    # --- 봉 재집계 (주/월) ---
    # 클라이언트에서 재집계하면 서버가 계산한 지표(이동평균·패턴·오더블록…)와
    # 봉이 어긋난다. 여기서 먼저 집계한 뒤 지표를 계산해야 전부 그 봉 기준이 된다.
    if tf != "D":
        def _key(dstr):
            if tf == "W":
                y, w, _ = date(int(dstr[:4]), int(dstr[5:7]), int(dstr[8:10])).isocalendar()
                return f"{y}-W{w:02d}"
            return dstr[:7]

        agg, order = {}, []
        for i, dstr in enumerate(o["dates"]):
            k = _key(dstr)
            if k not in agg:
                agg[k] = {"d": dstr, "o": o["open"][i], "h": o["high"][i],
                          "l": o["low"][i], "c": o["close"][i], "v": o["volume"][i]}
                order.append(k)
            else:
                b = agg[k]
                b["h"] = max(b["h"], o["high"][i])
                b["l"] = min(b["l"], o["low"][i])
                b["c"] = o["close"][i]
                b["v"] += o["volume"][i]
                b["d"] = dstr
        o = {**o,
             "dates": [agg[k]["d"] for k in order],
             "open": [agg[k]["o"] for k in order],
             "high": [agg[k]["h"] for k in order],
             "low": [agg[k]["l"] for k in order],
             "close": [agg[k]["c"] for k in order],
             "volume": [agg[k]["v"] for k in order],
             "tf": tf}

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
