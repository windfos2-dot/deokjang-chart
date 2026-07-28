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


def us_collect(symbols, batch=200):
    """yfinance 배치 다운로드 -> 종목별 시계열."""
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
        log(f"[US] {min(i + batch, len(syms))}/{len(syms)} (누적 {len(series)})")
    return series


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
        "ma": {k: rlist(r[k], px_nd) for k in
               ("sma5", "sma10", "sma20", "sma50", "sma120", "sma150", "sma200",
                "ema50", "ema200", "hma60")},
        "bb": {k: rlist(r["bb_" + k], px_nd) for k in ("mid", "upper", "lower")},
        "rsi": rlist(r["rsi"], 1),
        "disparity50": rlist(r["disparity50"], 2),
        "squeeze": {"val": rlist(r["squeeze"]["val"], 1),
                    "color": r["squeeze"]["color"]},
        "squeeze_aa": {"vf": rlist(r["squeeze_aa"]["vf"], 2),
                       "zscore": rlist(r["squeeze_aa"]["zscore"], 1),
                       "squeeze_val": rlist(r["squeeze_aa"]["squeeze_val"], 2),
                       "squeeze_ma": rlist(r["squeeze_aa"]["squeeze_ma"], 2),
                       "hyper": r["squeeze_aa"]["hyper"]},
        "ichimoku": {k: rlist(r["ichimoku"][k], px_nd) for k in r.get("ichimoku", {})} if r.get("ichimoku") else None,
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

    # ---- 한국 ----
    if not args.skip_kr:
        key = _krx_key()
        if not key:
            log("[KR] KRX_API_KEY 없음 -> 한국 건너뜀")
        else:
            items, series = kr_collect(key, args.kr_limit)
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
        uni = us_universe(args.us_limit)
        if uni:
            series = us_collect(uni)
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
