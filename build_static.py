"""
build_static.py — 정적 사이트용 데이터 빌더 (GitHub Pages)

GitHub Pages 는 파이썬을 못 돌리므로, Actions 에서 이 스크립트로 종목별 JSON 을
미리 만들어 두고 Pages 는 그것만 서빙한다. 방문자는 정적 파일만 받으므로
KRX/KIS 자격증명이 노출되지 않는다.

수집 전략 (호출 수를 종목 수와 분리하는 것이 핵심):
  - 한국: KRX Open API 는 '날짜 1개 = 전종목' 이므로 280일 × 2시장 = 560 calls
  - 미국: yfinance 배치 다운로드 (수백 종목씩)
  - 수급/공매도: 종목당 개별 호출이라 비싸다 → 상위 N개만 (SUPPLY_TOP)

출력:
  docs/data/<market>/<ticker>.json   종목별 OHLCV + 지표
  docs/index.json                    검색용 경량 인덱스
  docs/meta.json                     빌드 시각 등

사용:
  python build_static.py --kr-limit 50 --us-limit 50      (테스트)
  python build_static.py                                   (전종목)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import numpy as np

import chart_indicators as ind
# import 만으로 .env 를 로드해 KRX 로그인이 걸린다(한국 지수 조회에 필요).
import chart_data_loader as loader  # noqa: F401

ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(ROOT, "docs")
DATA = os.path.join(DOCS, "data")

KRX_BASE = "https://data-dbg.krx.co.kr/svc/apis"
BARS = 280                    # 차트 봉 수 (MA200 계산에 여유)
SUPPLY_TOP = 300              # 수급/공매도를 붙일 한국 시총 상위 N


# ---------------------------------------------------------------------------
# 공통
# ---------------------------------------------------------------------------
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def r2(v, nd=2):
    """JSON 크기를 줄이기 위한 반올림. None/NaN 안전."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if np.isnan(f) or np.isinf(f):
        return None
    return round(f, nd)


def rlist(arr, nd=2):
    return [r2(v, nd) for v in (arr or [])]


def _krx_key():
    key = os.getenv("KRX_API_KEY")
    if key:
        return key
    # 로컬 개발 편의: 기존 프로젝트 .env 에서 찾기
    import re
    for path in (os.path.join(ROOT, ".env"),
                 os.path.expanduser("~/hermes-trade/.env")):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    m = re.match(r"\s*KRX_API_KEY\s*=\s*(.+?)\s*$", line)
                    if m:
                        return m.group(1).strip().strip('"').strip("'")
        except OSError:
            continue
    return None


def krx_get(path, params, key, timeout=30):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(f"{KRX_BASE}/{path}?{qs}", headers={"AUTH_KEY": key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    for v in d.values():
        if isinstance(v, list):
            return v
    return []


# ---------------------------------------------------------------------------
# 한국 — KRX Open API 일괄 수집
# ---------------------------------------------------------------------------
def kr_candidate_days(need=BARS):
    """거래일 후보(주말 제외). 휴장일은 빈 응답으로 자연 제거되므로
    별도 탐색 호출을 하지 않는다 (탐색만으로 280콜을 쓰던 것을 제거)."""
    days, d = [], datetime.now()
    while len(days) < int(need * 1.55):
        if d.weekday() < 5:
            days.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    days.reverse()
    return days


def kr_collect(key, limit=None, workers=12):
    """한국 전종목 OHLCV 를 날짜별 일괄 호출로 조립 (병렬)."""
    cand = kr_candidate_days()
    log(f"[KR] 후보 {len(cand)}일 × 2시장 = {len(cand)*2} calls, 병렬 {workers}")

    jobs = [(ds, ep, mkt) for ds in cand
            for ep, mkt in (("sto/stk_bydd_trd", "KOSPI"), ("sto/ksq_bydd_trd", "KOSDAQ"))]

    fetched = {}
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(krx_get, ep, {"basDd": ds}, key): (ds, mkt)
                for ds, ep, mkt in jobs}
        done = 0
        for fut in as_completed(futs):
            ds, mkt = futs[fut]
            try:
                rows = fut.result()
            except Exception:  # noqa: BLE001
                rows = []
            if rows:
                fetched[(ds, mkt)] = rows
            done += 1
            if done % 200 == 0:
                log(f"[KR]   {done}/{len(jobs)} calls ({time.time()-t0:.0f}s)")

    days = sorted({ds for (ds, _) in fetched})[-BARS:]
    if not days:
        log("[KR] 유효 거래일 없음")
        return [], {}
    log(f"[KR] 거래일 {len(days)}일 확보: {days[0]} ~ {days[-1]} ({time.time()-t0:.0f}s)")

    series = defaultdict(lambda: {"dates": [], "open": [], "high": [],
                                  "low": [], "close": [], "volume": []})
    names, markets, caps = {}, {}, {}

    for ds in days:
        for mkt in ("KOSPI", "KOSDAQ"):
            for row in fetched.get((ds, mkt), []):
                code = row.get("ISU_CD", "")
                if not code:
                    continue
                try:
                    o = float(row["TDD_OPNPRC"].replace(",", ""))
                    h = float(row["TDD_HGPRC"].replace(",", ""))
                    lo = float(row["TDD_LWPRC"].replace(",", ""))
                    c = float(row["TDD_CLSPRC"].replace(",", ""))
                    v = float(row["ACC_TRDVOL"].replace(",", ""))
                except (KeyError, ValueError, AttributeError):
                    continue
                if c <= 0:
                    continue
                s = series[code]
                s["dates"].append(f"{ds[:4]}-{ds[4:6]}-{ds[6:]}")
                s["open"].append(o); s["high"].append(h)
                s["low"].append(lo); s["close"].append(c); s["volume"].append(v)
                names[code] = row.get("ISU_NM", code)
                markets[code] = mkt
                try:
                    caps[code] = float(row.get("MKTCAP", "0").replace(",", ""))
                except (ValueError, AttributeError):
                    pass
    log(f"[KR] 조립 완료: {len(series)}종목")

    items = [(code, names.get(code, code), markets.get(code, ""), caps.get(code, 0))
             for code in series]
    items.sort(key=lambda x: -x[3])              # 시총 내림차순
    if limit:
        items = items[:limit]
    log(f"[KR] 수집 완료: {len(items)}종목")
    return items, series


def kr_collect_pykrx(limit=None, workers=6):
    """KRX_API_KEY 가 없을 때의 폴백 — pykrx(KRX 웹 로그인)로 동일 결과를 만든다.

    오픈API 인증키는 별도 발급 절차가 필요한데, 이미 갖고 있는 KRX_ID/PW 만으로도
    같은 '날짜 1개 = 전종목' 호출이 가능하다:
        get_market_ohlcv_by_ticker(date, market="ALL")  -> 2,800여 종목 + 시가총액
    호출 수가 종목 수와 무관하므로 오픈API 경로와 비용 구조가 같다(280 calls).
    """
    from pykrx import stock
    import chart_data_loader as _loader

    uni = _loader._load_universe()          # KIND 1콜: code -> {name, market}
    log(f"[KR] 유니버스 {len(uni)}종목 (KIND)")

    # 실제 거래일은 코스피 지수 시계열에서 얻는다(휴장일이 자동으로 빠진다).
    end = datetime.now()
    start = end - timedelta(days=int(BARS * 1.7))
    idx = stock.get_index_ohlcv(start.strftime("%Y%m%d"),
                                end.strftime("%Y%m%d"), "1001")
    days = [d.strftime("%Y%m%d") for d in idx.index][-BARS:]
    if not days:
        log("[KR] 거래일 조회 실패")
        return [], {}
    log(f"[KR] 거래일 {len(days)}일: {days[0]} ~ {days[-1]}, 병렬 {workers}")

    def fetch(ds):
        try:
            return ds, stock.get_market_ohlcv_by_ticker(ds, market="ALL")
        except Exception:  # noqa: BLE001
            return ds, None

    fetched, t0, done = {}, time.time(), 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for ds, df in ex.map(fetch, days):
            done += 1
            if df is not None and not df.empty:
                fetched[ds] = df
            if done % 50 == 0:
                log(f"[KR]   {done}/{len(days)} calls ({time.time()-t0:.0f}s)")
    log(f"[KR] 수집 {len(fetched)}일 ({time.time()-t0:.0f}s)")

    series = defaultdict(lambda: {"dates": [], "open": [], "high": [],
                                  "low": [], "close": [], "volume": []})
    caps = {}
    for ds in sorted(fetched):
        df = fetched[ds]
        date_str = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}"
        # iterrows 는 2,800행×280일에서 지나치게 느려 컬럼 단위로 훑는다.
        cols = zip(df.index.tolist(), df["시가"].tolist(), df["고가"].tolist(),
                   df["저가"].tolist(), df["종가"].tolist(),
                   df["거래량"].tolist(), df["시가총액"].tolist())
        for code, o, h, lo, c, v, cap in cols:
            if code not in uni or not c or c <= 0:
                continue
            s = series[code]
            s["dates"].append(date_str)
            s["open"].append(float(o)); s["high"].append(float(h))
            s["low"].append(float(lo)); s["close"].append(float(c))
            s["volume"].append(float(v))
            caps[code] = float(cap or 0)     # 마지막 날 값이 남는다

    items = [(code, uni[code]["name"], uni[code]["market"], caps.get(code, 0))
             for code in series]
    items.sort(key=lambda x: -x[3])          # 시총 내림차순
    if limit:
        items = items[:limit]
    log(f"[KR] 조립 완료: {len(series)}종목 -> 대상 {len(items)}종목")
    return items, series


# ---------------------------------------------------------------------------
# 미국 — yfinance 배치
# ---------------------------------------------------------------------------
def us_universe(limit=None):
    """미국 종목 목록. NASDAQ Trader 공개 파일 사용(무료, 키 불필요)."""
    urls = [
        "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
        "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
    ]
    out = []
    for u in urls:
        try:
            with urllib.request.urlopen(u, timeout=40) as r:
                text = r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            log(f"[US] 목록 실패 {u}: {e}")
            continue
        lines = text.splitlines()
        if not lines:
            continue
        header = lines[0].split("|")
        try:
            sym_i = header.index("Symbol") if "Symbol" in header else header.index("ACT Symbol")
            name_i = header.index("Security Name")
        except ValueError:
            continue
        etf_i = header.index("ETF") if "ETF" in header else None
        test_i = header.index("Test Issue") if "Test Issue" in header else None
        for line in lines[1:]:
            p = line.split("|")
            if len(p) <= max(sym_i, name_i):
                continue
            sym = p[sym_i].strip()
            if not sym or "File Creation" in line:
                continue
            if etf_i is not None and len(p) > etf_i and p[etf_i].strip() == "Y":
                continue
            if test_i is not None and len(p) > test_i and p[test_i].strip() == "Y":
                continue
            if not sym.isalpha():                 # 우선주/워런트 등 제외
                continue
            out.append((sym, p[name_i].strip()[:60]))
    # 중복 제거
    seen, uniq = set(), []
    for s, nm in out:
        if s not in seen:
            seen.add(s)
            uniq.append((s, nm))
    if limit:
        uniq = uniq[:limit]
    log(f"[US] 유니버스 {len(uniq)}종목")
    return uniq


def us_turnover(s):
    """최근 60일 '종가×거래량' 중앙값 — 유동성 대용치.

    yfinance 는 시총을 배치로 주지 않으므로, 이미 받은 시계열만으로 계산할 수 있는
    달러 거래대금을 대신 쓴다. 중앙값이라 하루짜리 이상거래에 흔들리지 않는다.
    """
    c, v = s["close"][-60:], s["volume"][-60:]
    if not c:
        return 0.0
    vals = sorted(ci * vi for ci, vi in zip(c, v))
    return vals[len(vals) // 2]


def us_collect(symbols, batch=200, keep=None):
    """yfinance 배치 다운로드 -> 종목별 시계열.

    keep 을 주면 배치마다 거래대금 상위 keep 개만 남긴다. 전체 유니버스가
    6,000종목 가까이라 전부 메모리에 들고 있으면 수백 MB 를 먹기 때문이다.
    """
    import yfinance as yf
    series = {}
    syms = [s for s, _ in symbols]
    for i in range(0, len(syms), batch):
        chunk = syms[i:i + batch]
        try:
            df = yf.download(chunk, period="18mo", interval="1d", progress=False,
                             auto_adjust=True, threads=True, group_by="column")
        except Exception as e:  # noqa: BLE001
            log(f"[US] 배치 실패 {i}: {e}")
            continue
        if df is None or df.empty:
            continue
        for s in chunk:
            try:
                if len(chunk) == 1:
                    sub = df
                else:
                    sub = df.xs(s, axis=1, level=1)
                sub = sub.dropna()
                if len(sub) < 60:
                    continue
                sub = sub.tail(BARS)
                series[s] = {
                    "dates": [d.strftime("%Y-%m-%d") for d in sub.index],
                    "open": sub["Open"].astype(float).tolist(),
                    "high": sub["High"].astype(float).tolist(),
                    "low": sub["Low"].astype(float).tolist(),
                    "close": sub["Close"].astype(float).tolist(),
                    "volume": sub["Volume"].astype(float).tolist(),
                }
            except Exception:  # noqa: BLE001
                continue
        if keep and len(series) > keep * 2:
            series = dict(sorted(series.items(),
                                 key=lambda kv: -us_turnover(kv[1]))[:keep])
        log(f"[US] {min(i + batch, len(syms))}/{len(syms)} (누적 {len(series)})")
    if keep and len(series) > keep:
        series = dict(sorted(series.items(),
                             key=lambda kv: -us_turnover(kv[1]))[:keep])
        log(f"[US] 거래대금 상위 {len(series)}종목 선별")
    return series


# ---------------------------------------------------------------------------
# 지수
# ---------------------------------------------------------------------------
# 한국 지수는 pykrx(KRX 로그인 필요), 해외는 yfinance.
KR_INDICES = [
    ("1001", "코스피"),
    ("1028", "코스피200"),
    ("2001", "코스닥"),
    ("2203", "코스닥150"),
]
WORLD_INDICES = [
    ("^N225", "닛케이225"),
    ("^GSPC", "S&P 500"),
    ("^IXIC", "나스닥 종합"),
    ("^RUI", "러셀1000"),
    ("^RUT", "러셀2000"),
    ("^RUA", "러셀3000"),
    ("000300.SS", "CSI 300"),
    ("^HSI", "항셍"),
    ("^TWII", "대만 가권"),
]


def collect_indices():
    """지수 시계열 수집. 반환: {code: (name, series)}"""
    out = {}

    # --- 한국 (pykrx) ---
    try:
        from pykrx import stock
        frm = (datetime.now() - timedelta(days=int(BARS * 1.7))).strftime("%Y%m%d")
        to = datetime.now().strftime("%Y%m%d")
        for code, name in KR_INDICES:
            try:
                df = stock.get_index_ohlcv_by_date(frm, to, code)
                if df is None or df.empty:
                    continue
                df = df.tail(BARS)
                out["IDX" + code] = (name, {
                    "dates": [d.strftime("%Y-%m-%d") for d in df.index],
                    "open": df["시가"].astype(float).tolist(),
                    "high": df["고가"].astype(float).tolist(),
                    "low": df["저가"].astype(float).tolist(),
                    "close": df["종가"].astype(float).tolist(),
                    "volume": df["거래량"].astype(float).tolist()
                    if "거래량" in df.columns else [0.0] * len(df),
                })
                log(f"[IDX] {name} {len(df)}일")
            except Exception as e:  # noqa: BLE001
                log(f"[IDX] {name} 실패: {e}")
    except Exception as e:  # noqa: BLE001
        log(f"[IDX] 한국 지수 건너뜀(KRX 로그인 필요): {e}")

    # --- 해외 (yfinance) ---
    try:
        import yfinance as yf
        syms = [s for s, _ in WORLD_INDICES]
        df = yf.download(syms, period="18mo", interval="1d", progress=False,
                         auto_adjust=False, threads=True)
        for sym, name in WORLD_INDICES:
            try:
                sub = df.xs(sym, axis=1, level=1).dropna().tail(BARS)
                if len(sub) < 60:
                    continue
                out[sym] = (name, {
                    "dates": [d.strftime("%Y-%m-%d") for d in sub.index],
                    "open": sub["Open"].astype(float).tolist(),
                    "high": sub["High"].astype(float).tolist(),
                    "low": sub["Low"].astype(float).tolist(),
                    "close": sub["Close"].astype(float).tolist(),
                    "volume": sub["Volume"].astype(float).tolist(),
                })
                log(f"[IDX] {name} {len(sub)}일")
            except Exception as e:  # noqa: BLE001
                log(f"[IDX] {name} 실패: {e}")
    except Exception as e:  # noqa: BLE001
        log(f"[IDX] 해외 지수 실패: {e}")

    return out


# ---------------------------------------------------------------------------
# 종목 1개 -> JSON
# ---------------------------------------------------------------------------
def build_one(code, name, market, s, with_supply=False):
    dates = s["dates"][-BARS:]
    o = s["open"][-BARS:]; h = s["high"][-BARS:]
    lo = s["low"][-BARS:]; c = s["close"][-BARS:]; v = s["volume"][-BARS:]
    if len(c) < 60:
        return None

    bench = None
    try:
        r = ind.compute_all(dates, o, h, lo, c, v, benchmark_close=bench)
    except Exception as e:  # noqa: BLE001
        log(f"  ! 지표 실패 {code}: {e}")
        return None

    # 배열형 지표는 브라우저에서도 만들 수 있지만, 계산 일관성을 위해 서버 값을 쓰되
    # 소수점을 줄여 용량을 낮춘다.
    px_nd = 2
    out = {
        "ticker": code, "name": name, "market": market,
        "dates": dates,
        "o": rlist(o, px_nd), "h": rlist(h, px_nd),
        "l": rlist(lo, px_nd), "c": rlist(c, px_nd),
        "v": [int(x) if x is not None else None for x in v],
        # ma / bb / rsi / disparity / ichimoku 는 내려보내지 않는다.
        # OHLCV 만으로 만들 수 있어 docs/indicators.js 가 브라우저에서 계산한다
        # (파이썬과 값 일치 검증 완료). 종목당 용량 약 61% 절감.
        "squeeze": {"val": rlist(r["squeeze"]["val"], 1),
                    "color": r["squeeze"]["color"]},
        "squeeze_aa": {"vf": rlist(r["squeeze_aa"]["vf"], 2),
                       "zscore": rlist(r["squeeze_aa"]["zscore"], 1),
                       "squeeze_val": rlist(r["squeeze_aa"]["squeeze_val"], 2),
                       "squeeze_ma": rlist(r["squeeze_aa"]["squeeze_ma"], 2),
                       "hyper": r["squeeze_aa"]["hyper"]},
        "rsi_bear_div": r["rsi_bear_div"],
        "patterns": r["patterns"],
        "zigzag": r["zigzag"],
        "sr": {"resistance": rlist(r["sr"]["resistance"], px_nd),
               "support": rlist(r["sr"]["support"], px_nd),
               "breaks": r["sr"]["breaks"]},
        "order_blocks": r["order_blocks"],
        "minervini": r["minervini"]["latest"],
    }
    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kr-limit", type=int, default=None)
    ap.add_argument("--us-limit", type=int, default=None)
    ap.add_argument("--skip-us", action="store_true")
    ap.add_argument("--skip-kr", action="store_true")
    args = ap.parse_args()

    os.makedirs(os.path.join(DATA, "KR"), exist_ok=True)
    os.makedirs(os.path.join(DATA, "US"), exist_ok=True)

    index = []
    t_start = time.time()

    # ---- 지수 (검색 상단에 오도록 먼저) ----
    os.makedirs(os.path.join(DATA, "IDX"), exist_ok=True)
    for code, (name, s) in collect_indices().items():
        doc = build_one(code, name, "지수", s)
        if not doc:
            continue
        safe = code.replace("^", "_").replace(".", "_")
        with open(os.path.join(DATA, "IDX", f"{safe}.json"), "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
        index.append({"t": safe, "n": name, "m": "지수", "r": "IDX"})
    log(f"[IDX] 완료 {len(index)}개")

    # ---- 한국 ----
    if not args.skip_kr:
        key = _krx_key()
        if key:
            items, series = kr_collect(key, args.kr_limit)
        else:
            log("[KR] KRX_API_KEY 없음 -> pykrx 폴백(KRX 웹 로그인) 사용")
            items, series = kr_collect_pykrx(args.kr_limit)
        if items:
            ok = 0
            for i, (code, name, mkt, cap) in enumerate(items, 1):
                doc = build_one(code, name, mkt, series[code])
                if not doc:
                    continue
                with open(os.path.join(DATA, "KR", f"{code}.json"), "w",
                          encoding="utf-8") as f:
                    json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
                index.append({"t": code, "n": name, "m": mkt, "r": "KR"})
                ok += 1
                if i % 300 == 0:
                    log(f"[KR] 생성 {i}/{len(items)}")
            log(f"[KR] 완료 {ok}종목")

    # ---- 미국 ----
    if not args.skip_us:
        # 유니버스는 심볼 알파벳순이라 앞에서 자르면 AA* 만 남는다.
        # 전체를 받아 us_collect 안에서 거래대금 상위로 추린다.
        uni = us_universe(None)
        if uni:
            series = us_collect(uni, keep=args.us_limit)
            namemap = dict(uni)
            ok = 0
            for i, (sym, sdata) in enumerate(series.items(), 1):
                doc = build_one(sym, namemap.get(sym, sym), "US", sdata)
                if not doc:
                    continue
                with open(os.path.join(DATA, "US", f"{sym}.json"), "w",
                          encoding="utf-8") as f:
                    json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
                index.append({"t": sym, "n": namemap.get(sym, sym), "m": "US", "r": "US"})
                ok += 1
                if i % 300 == 0:
                    log(f"[US] 생성 {i}/{len(series)}")
            log(f"[US] 완료 {ok}종목")

    # ---- 인덱스 ----
    with open(os.path.join(DOCS, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(DOCS, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"built_at": datetime.now().isoformat(timespec="seconds"),
                   "count": len(index), "bars": BARS}, f, ensure_ascii=False)

    total = sum(os.path.getsize(os.path.join(dp, fn))
                for dp, _, fns in os.walk(DATA) for fn in fns)
    log(f"완료: {len(index)}종목 / {total/1e6:.1f}MB / {time.time()-t_start:.0f}초")


if __name__ == "__main__":
    main()
