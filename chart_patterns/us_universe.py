# -*- coding: utf-8 -*-
"""
미장 유니버스 구성 — S&P500 / 러셀 3000(근사).

러셀 3000 = "미국 상장 보통주 중 시가총액 상위 3,000개"가 정의다.
FTSE Russell 이 구성종목 파일을 무료로 주지 않고 iShares(IWV) 보유종목 CSV 는
봇 차단이 걸려 있어서, **NASDAQ Trader 전체 상장 심볼 디렉터리에서
보통주만 걸러내고 시가총액으로 정렬해 상위 N개**를 취하는 방식으로 만든다.

  python -m chart_patterns.us_universe --build --top 3000

주의: 실제 러셀 3000 과 완전히 같지는 않다.
  - 러셀은 유동주식(float) 조정 시총을 쓰고 매년 6월에만 정기변경한다.
  - 러셀은 외국 소재 기업·특정 주식종류를 배제하는 별도 규칙이 있다.
  이 목록은 '현재 시총 상위 3,000 미국 보통주'로, 커버리지 목적에는 충분하다.
"""
from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from . import utf8_stdout

HERE = Path(__file__).resolve().parent
SP500_CSV = HERE / "us_universe.csv"
R3000_CSV = HERE / "us_universe_russell3000.csv"

NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
UA = {"User-Agent": "Mozilla/5.0"}

# 보통주가 아닌 것들 — 이름으로 걸러낸다
EXCLUDE_RE = re.compile(
    r"(warrant|right|unit|preferred|depositary|debenture|note[s]? due|"
    r"%\s*(notes|bond)|subordinated|trust preferred|when.issued|"
    r"contingent value|escrow|liquidating)", re.I)
KEEP_RE = re.compile(r"(common stock|ordinary share|class [a-z] (common|ordinary))",
                     re.I)


def _rows(url: str) -> List[List[str]]:
    r = requests.get(url, headers=UA, timeout=90)
    r.raise_for_status()
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    if lines and lines[-1].lower().startswith("file creation time"):
        lines.pop()
    return [ln.split("|") for ln in lines]


def fetch_listed_symbols() -> List[Tuple[str, str, str]]:
    """NASDAQ Trader 디렉터리 → [(yfinance티커, 종목명, 거래소)] 보통주만."""
    out: Dict[str, Tuple[str, str]] = {}

    tbl = _rows(NASDAQ_LISTED)
    hdr = {h: i for i, h in enumerate(tbl[0])}
    for row in tbl[1:]:
        if len(row) <= max(hdr.values()):
            continue
        sym, name = row[hdr["Symbol"]], row[hdr["Security Name"]]
        if row[hdr["Test Issue"]] == "Y" or row[hdr["ETF"]] == "Y":
            continue
        if EXCLUDE_RE.search(name) or not KEEP_RE.search(name):
            continue
        out[sym] = (name, "NASDAQ")

    tbl = _rows(OTHER_LISTED)
    hdr = {h: i for i, h in enumerate(tbl[0])}
    for row in tbl[1:]:
        if len(row) <= max(hdr.values()):
            continue
        sym = row[hdr["NASDAQ Symbol"]] or row[hdr["ACT Symbol"]]
        name = row[hdr["Security Name"]]
        if row[hdr["Test Issue"]] == "Y" or row[hdr["ETF"]] == "Y":
            continue
        if EXCLUDE_RE.search(name) or not KEEP_RE.search(name):
            continue
        out[sym] = (name, row[hdr["Exchange"]])

    res = []
    for sym, (name, exch) in out.items():
        if "$" in sym or "." in sym.replace(".", "", 1) and sym.count(".") > 1:
            continue
        yf_sym = sym.replace(".", "-").strip()      # BRK.B → BRK-B
        if not yf_sym or not re.fullmatch(r"[A-Z0-9-]{1,8}", yf_sym):
            continue
        res.append((yf_sym, _clean_name(name), exch))
    res.sort()
    return res


def _clean_name(name: str) -> str:
    n = re.sub(r"\s*[-,]?\s*(Class [A-Z]\s*)?(Common Stock|Common Shares|"
               r"Ordinary Shares?|Ordinary Share)\b.*$", "", name, flags=re.I)
    n = re.sub(r"\s*[-,]\s*$", "", n)
    return re.sub(r"\s+", " ", n).strip().rstrip(",")


NASDAQ_SCREENER = "https://api.nasdaq.com/api/screener/stocks"


def fetch_caps_bulk(verbose: bool = True) -> Dict[str, float]:
    """NASDAQ 스크리너 API — 한 번 호출로 전 종목 시가총액.

    yfinance 개별 조회(fast_info)는 수천 건을 때리면 야후가 대부분 막아버린다
    (실측: 4,938건 중 1,503건만 성공). 이쪽은 1회 호출로 7,000+ 종목을 준다.
    """
    r = requests.get(NASDAQ_SCREENER,
                     params={"tableonly": "true", "limit": "12000", "offset": "0"},
                     headers={**UA, "Accept": "application/json"}, timeout=90)
    r.raise_for_status()
    rows = r.json()["data"]["table"]["rows"]
    caps: Dict[str, float] = {}
    for x in rows:
        sym = (x.get("symbol") or "").strip().replace("/", "-").replace(".", "-")
        raw = (x.get("marketCap") or "").replace(",", "").replace("$", "").strip()
        try:
            cap = float(raw)
        except ValueError:
            continue
        if sym and cap > 0:
            caps[sym] = cap
    if verbose:
        print(f"   시총 확보 {len(caps):,}종목 (전체 응답 {len(rows):,})")
    return caps


def fetch_caps(tickers: List[str], workers: int = 16,
               verbose: bool = True) -> Dict[str, float]:
    """폴백: yfinance fast_info 개별 조회 (느리고 차단당하기 쉬움)."""
    from concurrent.futures import ThreadPoolExecutor
    import yfinance as yf

    def one(t: str) -> Optional[Tuple[str, float]]:
        try:
            fi = yf.Ticker(t).fast_info
            cap = fi.get("market_cap") or fi.get("marketCap")
            return (t, float(cap)) if cap and cap > 0 else None
        except Exception:                                        # noqa: BLE001
            return None

    caps: Dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for got in ex.map(one, tickers):
            if got:
                caps[got[0]] = got[1]
    if verbose:
        print(f"   시총 확보 {len(caps):,}/{len(tickers):,} (yfinance 폴백)")
    return caps


def build_russell3000(top: int = 3000, min_cap: float = 0.0,
                      verbose: bool = True) -> Path:
    utf8_stdout()
    if verbose:
        print("▶ 미국 상장 보통주 목록 수집 (NASDAQ Trader)")
    syms = fetch_listed_symbols()
    if verbose:
        print(f"   보통주 후보 {len(syms):,}개")

    named = {s[0]: (s[1], s[2]) for s in syms}
    try:
        caps = fetch_caps_bulk(verbose=verbose)
    except Exception as e:                                       # noqa: BLE001
        print(f"   ⚠ 스크리너 실패({e}) → yfinance 폴백", file=sys.stderr)
        caps = fetch_caps([s[0] for s in syms], verbose=verbose)

    # 보통주 목록에 있는 것만 채택 (스크리너에는 ETF·우선주 등도 섞여 있다)
    caps = {t: c for t, c in caps.items() if t in named}
    if verbose:
        print(f"   보통주 교집합 {len(caps):,}종목")

    ranked = sorted(caps.items(), key=lambda kv: -kv[1])
    if min_cap:
        ranked = [r for r in ranked if r[1] >= min_cap]
    ranked = ranked[:top]

    with R3000_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "name", "sector", "mktcap", "rank", "as_of"])
        today = date.today().isoformat()
        for i, (tk, cap) in enumerate(ranked, 1):
            name, exch = named.get(tk, (tk, ""))
            w.writerow([tk, name, exch, f"{cap:.0f}", i, today])

    if verbose:
        print(f"   → {R3000_CSV}  ({len(ranked):,}종목)")
        if ranked:
            print(f"   시총 1위 {ranked[0][0]} ${ranked[0][1]/1e12:.2f}T / "
                  f"{len(ranked)}위 {ranked[-1][0]} ${ranked[-1][1]/1e9:.2f}B")
    return R3000_CSV


def load_csv(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def main() -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true", help="러셀3000 목록 생성")
    ap.add_argument("--top", type=int, default=3000)
    ap.add_argument("--min-cap", type=float, default=0.0,
                    help="시총 하한(달러). 예: 3e8")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()

    if a.build:
        build_russell3000(a.top, a.min_cap)
        return 0
    if a.show:
        rows = load_csv(R3000_CSV)
        print(f"{R3000_CSV.name}: {len(rows):,}종목")
        for r in rows[:10] + rows[-5:]:
            print(f"  {r['rank']:>5} {r['ticker']:<7} "
                  f"${float(r['mktcap'])/1e9:>9,.1f}B  {r['name'][:40]}")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
