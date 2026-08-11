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
import threading
import time
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import numpy as np

import chart_indicators as ind
import chart_patterns_bridge as cpb
# import 만으로 .env 를 로드해 KRX 로그인이 걸린다(한국 지수 조회에 필요).
import chart_data_loader as loader  # noqa: F401

ROOT = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(ROOT, "docs")
DATA = os.path.join(DOCS, "data")

KRX_BASE = "https://data-dbg.krx.co.kr/svc/apis"
BARS = 750                    # 일봉 수 (약 3년). MA200 계산에도 여유
SUPPLY_TOP = 300              # 수급/공매도를 붙일 한국 시총 상위 N

# --- 장기 시계열 (밸류에이션 밴드 · 주/월봉용) ---
# 일봉을 10년치 다 담으면 종목당 용량이 9배가 되므로, 최근 구간만 일봉으로 두고
# 장기는 주봉/월봉으로 압축해 저장한다. 밴드는 월말 시가총액이면 충분하다.
LONG_YEARS = 15               # KRX 오픈API 는 2010년부터 제공
WEEK_BARS = 780               # 15년 주봉
MONTH_BARS = 180              # 15년 월봉
MIN_MKTCAP = 1e11             # 시총 하한 (1,000억) — 이하 종목은 제외


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
    #
    # 이 한 줄에 예외 처리가 없어서 빌드 전체가 죽은 적이 있다(2026-08-11).
    # pykrx 는 지수 OHLCV 를 받은 뒤 `df.columns.name = get_index_ticker_name(...)`
    # 로 이름을 붙이는데, 그 지수명 표 조회가 KRX 응답 이상으로 실패하면
    # KeyError('지수명') 가 데이터와 무관하게 터진다. 데이터 자체는 멀쩡한데
    # 이름 붙이는 부수 단계에서 죽는 셈이라, 재시도 후 종목 하나의 일봉
    # 날짜 인덱스로 폴백한다(지수명 표를 타지 않는 경로).
    end = datetime.now()
    start = end - timedelta(days=int(BARS * 1.7))
    sd, ed = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")

    days = []
    for attempt in range(3):
        try:
            idx = stock.get_index_ohlcv(sd, ed, "1001")
            days = [d.strftime("%Y%m%d") for d in idx.index][-BARS:]
            if days:
                break
        except Exception as e:  # noqa: BLE001
            log(f"[KR] 거래일(지수) 조회 실패 {attempt + 1}/3: {e}")
            time.sleep(2 * (attempt + 1))
    if not days:
        log("[KR] 지수 경로 포기 → 삼성전자 일봉 날짜로 폴백")
        try:
            df = stock.get_market_ohlcv_by_date(sd, ed, "005930")
            days = [d.strftime("%Y%m%d") for d in df.index][-BARS:]
        except Exception as e:  # noqa: BLE001
            log(f"[KR] 폴백도 실패: {e}")
    if not days:
        log("[KR] 거래일 조회 실패 — KR 수집을 건너뜁니다")
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
            df = yf.download(chunk, period="15y", interval="1d", progress=False,
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
# 장기 시계열 (10년) — 주/월봉 + 월말 시가총액
# ---------------------------------------------------------------------------
def kr_collect_long(key, min_cap=MIN_MKTCAP, years=LONG_YEARS, workers=12):
    """KRX 오픈API 로 10년치 일봉을 모아 주/월봉 + 월말 시총으로 압축한다.

    - 호출 수가 종목 수와 무관하다(날짜 1개 = 전종목). 10년 ≈ 2,450거래일 × 2시장.
    - 메모리를 아끼려고 최근 시총으로 유니버스를 먼저 좁힌 뒤 누적한다.
    - pykrx 웹로그인과 무관한 경로라 계정 잠금 영향을 받지 않는다.
    """
    # 1) 최근 거래일로 유니버스 + 시총 확보
    recent = None
    for ds in kr_candidate_days(10)[::-1]:
        rows = krx_get("sto/stk_bydd_trd", {"basDd": ds}, key)
        if rows:
            recent = ds
            break
    if not recent:
        log("[LONG] 최근 거래일 확보 실패")
        return {}

    universe = {}
    for ep, mkt in (("sto/stk_bydd_trd", "KOSPI"), ("sto/ksq_bydd_trd", "KOSDAQ")):
        for r in krx_get(ep, {"basDd": recent}, key):
            try:
                cap = float(r.get("MKTCAP", "0").replace(",", ""))
            except (ValueError, AttributeError):
                continue
            if cap >= min_cap and r.get("ISU_CD"):
                universe[r["ISU_CD"]] = (r.get("ISU_NM", ""), mkt, cap)
    log(f"[LONG] 시총 {min_cap/1e8:,.0f}억 이상 {len(universe):,}종목 (기준일 {recent})")
    if not universe:
        return {}

    # 2) 10년치 날짜 일괄 수집
    days = []
    d = datetime.strptime(recent, "%Y%m%d")
    end = d - timedelta(days=int(365.25 * years))
    while d >= end:
        if d.weekday() < 5:
            days.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    jobs = [(ds, ep, mkt) for ds in days
            for ep, mkt in (("sto/stk_bydd_trd", "KOSPI"), ("sto/ksq_bydd_trd", "KOSDAQ"))]
    log(f"[LONG] {len(days):,}일 × 2시장 = {len(jobs):,} calls, 병렬 {workers}")

    # 종목별 (날짜 -> ohlcv+cap). 유니버스에 든 종목만 담는다.
    acc = defaultdict(dict)
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(krx_get, ep, {"basDd": ds}, key): ds for ds, ep, mkt in jobs}
        for fut in as_completed(futs):
            ds = futs[fut]
            try:
                rows = fut.result()
            except Exception:  # noqa: BLE001
                rows = []
            for r in rows:
                code = r.get("ISU_CD")
                if code not in universe:
                    continue
                try:
                    acc[code][ds] = (
                        float(r["TDD_OPNPRC"].replace(",", "")),
                        float(r["TDD_HGPRC"].replace(",", "")),
                        float(r["TDD_LWPRC"].replace(",", "")),
                        float(r["TDD_CLSPRC"].replace(",", "")),
                        float(r["ACC_TRDVOL"].replace(",", "")),
                        float(r.get("MKTCAP", "0").replace(",", "")),
                    )
                except (KeyError, ValueError, AttributeError):
                    continue
            done += 1
            if done % 800 == 0:
                log(f"[LONG]   {done:,}/{len(jobs):,} ({time.time()-t0:.0f}s)")
    log(f"[LONG] 수집 완료 {len(acc):,}종목 ({time.time()-t0:.0f}s)")

    # 3) 주봉/월봉/월말시총으로 압축
    out = {}
    for code, bydate in acc.items():
        ds_sorted = sorted(bydate)
        if len(ds_sorted) < 60:
            continue
        name, mkt, cap = universe[code]
        # 일봉도 여기서 만든다. 별도로 kr_collect 를 또 돌리면 같은 날짜를
        # 두 번 받는 셈이라 868콜이 낭비된다.
        recent_ds = ds_sorted[-BARS:]
        daily = {"dates": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
        for ds in recent_ds:
            o, h, l, c, v, _cap = bydate[ds]
            daily["dates"].append(f"{ds[:4]}-{ds[4:6]}-{ds[6:]}")
            daily["open"].append(o); daily["high"].append(h)
            daily["low"].append(l); daily["close"].append(c); daily["volume"].append(v)
        out[code] = {
            "name": name, "market": mkt, "cap": cap,
            "daily": daily,
            "weekly": _agg(bydate, ds_sorted, "W")[-WEEK_BARS:],
            "monthly": _agg(bydate, ds_sorted, "M")[-MONTH_BARS:],
        }
    log(f"[LONG] 압축 완료 {len(out):,}종목")
    return out


def _agg(bydate, ds_sorted, mode):
    """일봉 dict -> 주/월봉 리스트. 각 원소 [date, o, h, l, c, v, mktcap(말일)]"""
    buckets = {}
    order = []
    for ds in ds_sorted:
        o, h, l, c, v, cap = bydate[ds]
        if mode == "W":
            dt = datetime.strptime(ds, "%Y%m%d")
            y, w, _ = dt.isocalendar()
            k = f"{y}-W{w:02d}"
        else:
            k = ds[:6]
        b = buckets.get(k)
        if b is None:
            buckets[k] = [f"{ds[:4]}-{ds[4:6]}-{ds[6:]}", o, h, l, c, v, cap]
            order.append(k)
        else:
            b[0] = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}"
            b[2] = max(b[2], h)
            b[3] = min(b[3], l)
            b[4] = c
            b[5] += v
            b[6] = cap
    return [buckets[k] for k in order]


# ---------------------------------------------------------------------------
# DART 재무 (밸류에이션 밴드용)
# ---------------------------------------------------------------------------
# PBR = 시총/자본총계, PER = 시총/당기순이익, POR = 시총/영업이익,
# ROE = 당기순이익/자본총계.  연간(사업보고서) 기준으로 10년치를 모은다.
DART_BASE = "https://opendart.fss.or.kr/api"
DART_BATCH = 10               # fnlttMultiAcnt 는 corp_code 를 콤마로 여러 개 받는다
_DART_WANT = {
    "매출액": "rev",
    "영업이익": "op",
    "당기순이익(손실)": "ni",
    "자본총계": "eq",
}


#: DART 키를 담는 환경변수/`.env` 항목 이름들. 프로젝트마다 다른 이름을 써서
#: (spac_tracker.py 는 셋 다 지원한다) 하나만 보면 이미 있는 키를 놓친다.
_DART_KEY_NAMES = ("OPENDART_API_KEY", "DART_API_KEY", "OPENDART_KEY")


def _dart_key():
    """OpenDART 인증키. 환경변수 -> 이 레포 .env -> 이웃 프로젝트 .env 순서.

    키가 없으면 PER/PBR 밸류에이션 밴드와 DART 발행주식수 폴백이 조용히 죽는다
    (`/bands` 가 "DART 재무 없음"으로 응답). 실측 2026-07-30: 폴더명이
    `stock_bot`(밑줄)인데 `stock-bot`(하이픈)만 찾아서 못 잡고 있었다.
    """
    import re

    def valid(v):
        """OpenDART 인증키는 40자 hex 다. 형식을 검증하지 않으면 이웃 .env 의
        엉뚱한 값(실측: 88자짜리)을 집어와서 DART 가 zip 대신 에러 JSON 을
        돌려주고 'File is not a zip file' 이라는 엉뚱한 예외로 터진다."""
        if not v:
            return None
        v = v.split("#", 1)[0].strip().strip('"').strip("'")
        return v if re.fullmatch(r"[0-9a-fA-F]{40}", v) else None

    for name in _DART_KEY_NAMES:
        key = valid(os.getenv(name))
        if key:
            return key

    pat = re.compile(r"\s*(?:%s)\s*=\s*(.+?)\s*$" % "|".join(_DART_KEY_NAMES))
    candidates = [os.path.join(ROOT, ".env")]
    for sib in ("stock_bot", "stock-bot", "telegram_bot", "spac-tracker"):
        candidates.append(os.path.join(ROOT, os.pardir, sib, ".env"))
    candidates.append(os.path.expanduser("~/hermes-trade/.env"))

    for path in candidates:
        try:
            with open(os.path.normpath(path), encoding="utf-8") as f:
                for line in f:
                    m = pat.match(line)
                    if m:
                        val = valid(m.group(1))
                        if val:
                            return val
        except OSError:
            continue
    return None


def dart_corp_map():
    """종목코드 -> corp_code 매핑 (3.6MB 1회 다운로드)."""
    key = _dart_key()
    if not key:
        return {}
    import io
    import zipfile
    import xml.etree.ElementTree as ET

    # 3.6MB 한 방 다운로드라 연결이 한 번 끊기면 그걸로 끝이었다. 이 표가 비면
    # 종목→corp_code 매핑이 없어 **전 종목 재무가 통째로 스킵**되고, 그 결과
    # 밸류에이션 밴드(PER/PBR/POR/ROE)가 사이트에서 사라진다. 실제로 2026-08-11
    # 빌드가 "Remote end closed connection" 한 줄로 1,351종목 밴드를 날렸다.
    root = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(
                    f"{DART_BASE}/corpCode.xml?crtfc_key={key}", timeout=90) as r:
                raw = r.read()
            z = zipfile.ZipFile(io.BytesIO(raw))
            root = ET.fromstring(z.read(z.namelist()[0]).decode("utf-8"))
            break
        except Exception as e:  # noqa: BLE001
            log(f"[DART] corp_code 실패 {attempt + 1}/3: {e}")
            time.sleep(3 * (attempt + 1))
    if root is None:
        log("[DART] ⚠ corp_code 확보 실패 — 밸류에이션 밴드가 전부 빠집니다")
        return {}
    m = {}
    for c in root.iter("list"):
        sc = (c.findtext("stock_code") or "").strip()
        if sc:
            m[sc] = c.findtext("corp_code")
    log(f"[DART] corp_code 매핑 {len(m):,}종목")
    return m


def dart_financials(tickers, years=LONG_YEARS, workers=6):
    """연도별 재무. 반환: {ticker: {year: {rev, op, ni, eq}}}"""
    key = _dart_key()
    if not key:
        log("[DART] OPENDART_API_KEY 없음 -> 밴드 생략")
        return {}
    cmap = dart_corp_map()
    pairs = [(t, cmap[t]) for t in tickers if t in cmap]
    if not pairs:
        return {}

    this_year = datetime.now().year
    yrs = list(range(this_year - years, this_year + 1))
    batches = [pairs[i:i + DART_BATCH] for i in range(0, len(pairs), DART_BATCH)]
    rev_map = {c: t for t, c in pairs}
    out = defaultdict(dict)

    # DART 는 짧은 시간에 몰아치면 연결을 그냥 끊는다(RemoteDisconnected).
    # 2026-08-11 에 2,112콜을 41초에 6병렬로 던졌다가 그때부터 전 호출이 거부됐고,
    # 그 전에 받아둔 것도 연도가 군데군데 비어 쓸 수 없었다. 초당 호출 수를
    # 묶어두고, 끊기면 물러섰다가 다시 시도한다.
    stats = defaultdict(int)             # throttled / err / dropped 집계
    _gate = threading.Semaphore(1)
    _last = [0.0]
    MIN_GAP = 0.12                       # 초당 8콜 상한

    def _throttle():
        with _gate:
            gap = time.time() - _last[0]
            if gap < MIN_GAP:
                time.sleep(MIN_GAP - gap)
            _last[0] = time.time()

    def fetch(args):
        yr, batch = args
        codes = ",".join(c for _, c in batch)
        url = (f"{DART_BASE}/fnlttMultiAcnt.json?crtfc_key={key}"
               f"&corp_code={codes}&bsns_year={yr}&reprt_code=11011")
        for attempt in range(3):
            _throttle()
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    d = json.loads(r.read().decode("utf-8", "replace"))
                st = d.get("status")
                # 020=사용한도 초과, 021=조회 제한. 물러서지 않으면 계속 막힌다.
                if st in ("020", "021"):
                    stats["throttled"] += 1
                    time.sleep(2 * (attempt + 1))
                    continue
                if st not in ("000", "013"):        # 013 = 데이터 없음(정상)
                    stats["err"] += 1
                return yr, d
            except Exception:  # noqa: BLE001
                stats["dropped"] += 1
                time.sleep(1.5 * (attempt + 1))
        return yr, {}

    jobs = [(yr, b) for yr in yrs for b in batches]
    log(f"[DART] {len(pairs):,}종목 × {len(yrs)}년 = {len(jobs):,} calls, 병렬 {workers}")
    t0, done = time.time(), 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for yr, d in ex.map(fetch, jobs):
            done += 1
            for it in (d.get("list") or []):
                # 연결(CFS) 우선, 없으면 개별(OFS)
                fld = _DART_WANT.get(it.get("account_nm"))
                if not fld:
                    continue
                tk = rev_map.get(it.get("corp_code"))
                if not tk:
                    continue
                try:
                    val = float(str(it.get("thstrm_amount", "")).replace(",", ""))
                except (TypeError, ValueError):
                    continue
                slot = out[tk].setdefault(yr, {})
                if it.get("fs_div") == "CFS" or fld not in slot:
                    slot[fld] = val
            if done % 300 == 0:
                log(f"[DART]   {done:,}/{len(jobs):,} ({time.time()-t0:.0f}s)")
    # 조용히 반쪽짜리 재무를 넘기면 밴드가 최근 구간만 텅 빈 채로 그려진다.
    # 실패 집계를 남겨서 '데이터가 원래 없는 것'과 '못 받은 것'을 구분한다.
    if stats:
        log(f"[DART] 실패 집계 — 한도 {stats['throttled']} / 오류 {stats['err']}"
            f" / 연결끊김 {stats['dropped']}")
    covered = sum(1 for v in out.values() if v)
    log(f"[DART] 완료 {covered:,}/{len(pairs):,}종목 ({time.time()-t0:.0f}s)")
    if covered < len(pairs) * 0.8:
        log("[DART] ⚠ 커버리지 80% 미만 — 밸류에이션 밴드가 반쪽이 됩니다."
            " 잠시 후 다시 시도하세요.")
    return out


def dart_quarterly(ticker, years=LONG_YEARS, workers=6):
    """분기 재무 -> LTM(최근 4분기 합) 산출용 타임라인.

    ⚠️ 필드 의미 (실측 확인):
        분기/반기 보고서 thstrm_amount     = 해당 3개월 (이미 분기값, 차분 불필요)
        분기/반기 보고서 thstrm_add_amount = 누적
        사업보고서      thstrm_amount     = 연간 (add 없음)
      -> Q1~Q3 는 그대로 쓰고, Q4 만 '연간 − 3분기누적' 으로 구한다.
      검산(삼성전자 2025): 79.1+74.6=153.7(반기누적), +86.1=239.8(3Q누적),
                          333.6−239.8=93.8(Q4)

    손익(rev/op/ni)은 분기 차분 후 4개를 더해 LTM 을 만들고,
    자본총계(eq)는 저량(stock)이므로 해당 시점 값을 그대로 쓴다.

    반환: [{"avail": "YYYY-MM-DD", "y":연, "q":분기, "rev","op","ni","eq"}, ...]
          avail = 그 보고서가 공시되어 '알 수 있었던' 시점 (선행편향 제거용)
    """
    key = _dart_key()
    if not key:
        return []
    cmap = dart_corp_map()
    corp = cmap.get(ticker)
    if not corp:
        return []

    this_year = datetime.now().year
    yrs = list(range(this_year - years, this_year + 1))
    reports = [("11013", 1), ("11012", 2), ("11014", 3), ("11011", 4)]

    def fetch(args):
        yr, (rc, q) = args
        url = (f"{DART_BASE}/fnlttSinglAcnt.json?crtfc_key={key}"
               f"&corp_code={corp}&bsns_year={yr}&reprt_code={rc}")
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                return yr, q, json.loads(r.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            return yr, q, {}

    jobs = [(yr, rq) for yr in yrs for rq in reports]
    raw = {}                     # (year, q) -> {"amt":{...}, "cum":{...}}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for yr, q, d in ex.map(fetch, jobs):
            for it in (d.get("list") or []):
                fld = _DART_WANT.get(it.get("account_nm"))
                if not fld:
                    continue
                slot = raw.setdefault((yr, q), {"amt": {}, "cum": {}})
                prefer = it.get("fs_div") == "CFS"
                for src, bucket in (("thstrm_amount", "amt"),
                                    ("thstrm_add_amount", "cum")):
                    try:
                        val = float(str(it.get(src, "")).replace(",", ""))
                    except (TypeError, ValueError):
                        continue
                    if prefer or fld not in slot[bucket]:
                        slot[bucket][fld] = val

    quarters = []
    for (yr, q) in sorted(raw):
        cur = raw[(yr, q)]
        row = {"y": yr, "q": q, "eq": cur["amt"].get("eq")}
        for f in ("rev", "op", "ni"):
            if q < 4:
                row[f] = cur["amt"].get(f)              # 이미 3개월치
            else:
                annual = cur["amt"].get(f)
                q3 = raw.get((yr, 3), {}).get("cum", {}).get(f)
                row[f] = (annual - q3) if (annual is not None and q3 is not None) else None
        # 공시 시점 (분기·반기는 분기말+45일, 사업보고서는 이듬해 3월말)
        row["avail"] = {1: f"{yr}-05-16", 2: f"{yr}-08-15",
                        3: f"{yr}-11-15", 4: f"{yr+1}-04-01"}[q]
        quarters.append(row)
    return quarters


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
        df = yf.download(syms, period="15y", interval="1d", progress=False,
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
def market_regime(series_map, limit=800):
    """유니버스 중앙값의 200일선 위/아래 → bull / bear.

    책 통계가 강세/약세로 갈리므로 Bulkowski 점수에 필요하다. 종목마다 지수를
    다시 받을 수는 없고 국면은 종목별로 다르지 않으니, 스크리너와 같은 방식으로
    시장당 한 번만 구해서 build_one 에 넘긴다.
    """
    vals = []
    for s in list(series_map.values())[:limit]:
        c = s.get("close") or []
        if len(c) < 210:
            continue
        ma = float(np.mean(c[-200:]))
        if ma > 0:
            vals.append(c[-1] / ma - 1.0)
    if not vals:
        return "bull"
    return "bull" if float(np.median(vals)) > 0 else "bear"


def _mnv_light(closes, lookback, mf, mm, ms, rb):
    """Minervini 추세템플릿 통과율 → 신호등 한 칸. 봉이 모자라면 None."""
    if len(closes) < max(ms, 20) + 2:
        return None
    try:
        mv = ind.minervini_trend_template(
            closes, lookback=min(lookback, max(len(closes) - 1, 2)),
            ma_fast=mf, ma_mid=mm, ma_slow=ms, rise_bars=rb)
    except Exception:  # noqa: BLE001
        return None
    latest = (mv or {}).get("latest")
    if not latest:
        return None
    crit = [v for k, v in latest.items() if k.startswith("c") and v is not None]
    if not crit:
        return None
    passed = sum(1 for v in crit if v)
    ratio = passed / len(crit)
    return {"passed": passed, "total": len(crit),
            "light": "green" if ratio >= 0.75 else
                     ("yellow" if ratio >= 0.5 else "red")}


def build_one(code, name, market, s, with_supply=False, long_data=None, fin=None,
              regime=None):
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

    # --- Bulkowski 패턴 ---
    # 추가 네트워크 호출이 없다. 위에서 이미 받은 일봉만으로 계산되므로 빌드
    # 시간에 사실상 영향이 없다(종목당 수십 ms). 책 데이터가 없는 환경에서는
    # available() 이 False 라 조용히 빠진다.
    # notes 는 화면에 안 쓰면서 용량만 차지해서 뺀다.
    if cpb.available():
        try:
            bk = cpb.detect(dates, o, h, lo, c, v, tf="D",
                            recent=cpb.DEFAULT_RECENT, regime=regime,
                            with_notes=False)
            # 히트가 0건이어도 키는 넣는다. 프론트가 '탐지했는데 없음' 과
            # '이 필드가 아예 없는 구버전 빌드' 를 구분해야 하기 때문이다.
            if bk.get("available"):
                out["bulkowski"] = {"regime": bk["regime"], "recent": bk["recent"],
                                    "hits": bk["hits"]}
        except Exception as e:  # noqa: BLE001
            log(f"  ! 패턴 실패 {code}: {e}")

    # --- 장기 시계열 (주/월봉) + 월말 시가총액 ---
    # 시총 하한 미달 종목은 long_data 가 없다. 그래도 일봉 신호등/MTF 는 나와야
    # 하므로 아래 계산을 이 블록 안에 두지 않는다 (주/월 칸만 비게 된다).
    if long_data:
        out["w"] = [[b[0]] + [r2(x, px_nd) for x in b[1:5]] + [int(b[5])]
                    for b in long_data.get("weekly", [])]
        out["m"] = [[b[0]] + [r2(x, px_nd) for x in b[1:5]] + [int(b[5]), int(b[6])]
                    for b in long_data.get("monthly", [])]

    # --- 신호등 + MTF (일 / 주 / 월) ---
    # Minervini 템플릿은 일봉 지표라 주/월봉에 50/150/200 을 그대로 쓰면 MA200 이
    # 200주(4년)·200개월(16년)이 되어 뜻이 달라진다. 라이브 /signals 와 같은
    # 환산을 쓴다: 일 50/150/200,22 → 주 10/30/40,4 → 월 3/7/10,1
    wb = (long_data or {}).get("weekly") or []
    mb = (long_data or {}).get("monthly") or []
    sig = {"D": _mnv_light(c, 260, 50, 150, 200, 22)}
    if wb:
        sig["W"] = _mnv_light([b[4] for b in wb], 52, 10, 30, 40, 4)
    if mb:
        sig["M"] = _mnv_light([b[4] for b in mb], 12, 3, 7, 10, 1)
    sig = {k: x for k, x in sig.items() if x}
    if sig:
        out["signals"] = sig

    mtf = {}
    for key, bars in (("D", None), ("W", wb), ("M", mb)):
        try:
            if key == "D":
                t = ind.timeframe_signals(h, lo, c, v)
            else:
                if len(bars) < 30:
                    continue
                t = ind.timeframe_signals([b[2] for b in bars], [b[3] for b in bars],
                                          [b[4] for b in bars], [b[5] for b in bars])
            if t.get("available"):
                mtf[key] = t
        except Exception:  # noqa: BLE001
            continue
    if mtf:
        out["mtf"] = mtf

    # --- DART 연간 재무 (밸류에이션 밴드용) ---
    if fin:
        out["fin"] = {str(y): {k: int(v) for k, v in d.items()}
                      for y, d in sorted(fin.items())}
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
    ap.add_argument("--min-cap", type=float, default=MIN_MKTCAP,
                    help="시총 하한(원). 기본 1e11=1,000억. 0이면 전종목")
    ap.add_argument("--skip-long", action="store_true", help="장기 주/월봉 생략")
    ap.add_argument("--skip-dart", action="store_true", help="DART 재무 생략")
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
        long_map = {}
        if key and not args.skip_long:
            # 장기 수집이 일봉까지 만들어 주므로 별도 수집을 생략한다.
            long_map = kr_collect_long(key, min_cap=args.min_cap or MIN_MKTCAP)
            items = sorted(((c, v["name"], v["market"], v["cap"])
                            for c, v in long_map.items()), key=lambda x: -x[3])
            series = {c: v["daily"] for c, v in long_map.items()}
            if args.kr_limit:
                items = items[:args.kr_limit]
        elif key:
            items, series = kr_collect(key, args.kr_limit)
        else:
            log("[KR] KRX_API_KEY 없음 -> pykrx 폴백(KRX 웹 로그인) 사용")
            items, series = kr_collect_pykrx(args.kr_limit)
        # 시총 하한 필터 (기본 1,000억) — 소형주는 데이터도 지표도 신뢰도가 낮다
        if args.min_cap > 0:
            before = len(items)
            items = [it for it in items if it[3] >= args.min_cap]
            log(f"[KR] 시총 {args.min_cap/1e8:,.0f}억 필터: {before} -> {len(items)}종목")

        # DART 재무 — 밸류에이션 밴드용 (장기 시계열은 위에서 이미 확보)
        fin_map = {}
        if items and not args.skip_dart:
            fin_map = dart_financials([it[0] for it in items])

        if items:
            ok = 0
            kr_regime = market_regime(series)
            log(f"[KR] 시장 국면 {kr_regime} (Bulkowski 책 통계 선택 기준)")
            for i, (code, name, mkt, cap) in enumerate(items, 1):
                doc = build_one(code, name, mkt, series[code],
                                long_data=long_map.get(code), fin=fin_map.get(code),
                                regime=kr_regime)
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
            us_regime = market_regime(series)
            log(f"[US] 시장 국면 {us_regime} (Bulkowski 책 통계 선택 기준)")
            for i, (sym, sdata) in enumerate(series.items(), 1):
                doc = build_one(sym, namemap.get(sym, sym), "US", sdata,
                                regime=us_regime)
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
