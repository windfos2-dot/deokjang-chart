# -*- coding: utf-8 -*-
"""
국장/미장 일봉 OHLCV 수집·캐시 (chart_patterns/ohlcv.db).

  국장: 금융위 주식시세정보 API(data.go.kr) — 일자별 전종목 스냅샷.
        한 번 호출로 그날 시장 전체(코스피 ~950 / 코스닥 ~1820)를 받는다. T+1 반영.
  미장: yfinance 벌크 다운로드 (기본 유니버스는 us_market.db 의 us_universe).

  python -m chart_patterns.ohlcv --market KR --days 400     # 백필
  python -m chart_patterns.ohlcv --market US --days 400
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np
import requests

from . import utf8_stdout

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DB = HERE / "ohlcv.db"

DATAGO_URL = ("https://apis.data.go.kr/1160100/service"
              "/GetStockSecuritiesInfoService/getStockPriceInfo")
KR_MARKETS = ("KOSPI", "KOSDAQ")


# ------------------------------------------------------------------ schema
def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB, timeout=120)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=120000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS bars (
            market TEXT NOT NULL,          -- KR / US
            code   TEXT NOT NULL,
            dt     TEXT NOT NULL,          -- YYYY-MM-DD
            open   REAL, high REAL, low REAL, close REAL,
            volume REAL, value REAL,
            PRIMARY KEY (market, code, dt)
        );
        CREATE INDEX IF NOT EXISTS ix_bars_code ON bars(market, code, dt);
        CREATE TABLE IF NOT EXISTS universe (
            market TEXT NOT NULL, code TEXT NOT NULL,
            name TEXT, board TEXT, mktcap REAL, updated TEXT,
            PRIMARY KEY (market, code)
        );
        CREATE TABLE IF NOT EXISTS universe_index (
            market TEXT NOT NULL, code TEXT NOT NULL, idx TEXT NOT NULL,
            rank INTEGER, updated TEXT,
            PRIMARY KEY (market, code, idx)
        );
        CREATE TABLE IF NOT EXISTS fetch_log (
            market TEXT NOT NULL, key TEXT NOT NULL, rows INTEGER,
            PRIMARY KEY (market, key)
        );
    """)
    return conn


# ---------------------------------------------------------------- KR (국장)
def _datago_key() -> str:
    key = os.environ.get("DATAGO_KEY") or os.environ.get("DART_API_KEY")
    if not key:
        try:
            from dotenv import load_dotenv
            load_dotenv(ROOT / ".env")
        except ImportError:
            pass
        key = os.environ.get("DATAGO_KEY") or os.environ.get("DART_API_KEY")
    if not key:
        raise RuntimeError(".env 에 DART_API_KEY(data.go.kr 서비스키)가 필요합니다")
    return key


def fetch_kr_day(bas_dt: date, board: str, key: str) -> List[tuple]:
    """금융위 API → 해당 일자 board 전종목 [(code, name, o,h,l,c,vol,val,cap)]"""
    params = {
        "serviceKey": key, "numOfRows": 6000, "pageNo": 1,
        "resultType": "json", "basDt": bas_dt.strftime("%Y%m%d"),
        "mrktCls": board,
    }
    for attempt in range(3):
        try:
            res = requests.get(DATAGO_URL, params=params, timeout=40)
            body = res.json()["response"]["body"]
            break
        except Exception:
            if attempt == 2:
                return []
            time.sleep(1.5 * (attempt + 1))
    if int(body.get("totalCount", 0) or 0) == 0:
        return []
    items = body.get("items", {})
    items = items.get("item", []) if isinstance(items, dict) else []
    if isinstance(items, dict):
        items = [items]

    out = []
    for it in items:
        try:
            out.append((
                it["srtnCd"], it.get("itmsNm", ""),
                float(it["mkp"]), float(it["hipr"]), float(it["lopr"]),
                float(it["clpr"]), float(it.get("trqu") or 0),
                float(it.get("trPrc") or 0), float(it.get("mrktTotAmt") or 0),
            ))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _upsert_universe(conn: sqlite3.Connection, market: str, board: str,
                     rows: List[tuple], as_of: str) -> None:
    """universe 갱신 — 시가총액은 '가장 최근 일자'의 값만 남긴다.

    백필은 과거로 거슬러 올라가므로 무조건 REPLACE 하면 제일 오래된 시총이
    남아버린다(주가가 오르면 실제와 크게 어긋남). updated 비교로 막는다.
    """
    conn.executemany(
        "INSERT INTO universe(market, code, name, board, mktcap, updated) "
        "VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(market, code) DO UPDATE SET "
        "  name=excluded.name, board=excluded.board, "
        "  mktcap=excluded.mktcap, updated=excluded.updated "
        "WHERE excluded.updated > universe.updated",
        [(market, c, n, board, cap, as_of) for c, n, cap in rows])


def refresh_kr_universe(verbose: bool = True) -> int:
    """가장 최근 영업일 스냅샷으로 종목명·시가총액만 다시 맞춘다 (2회 호출)."""
    key = _datago_key()
    conn = connect()
    d, updated = date.today(), 0
    for _ in range(10):                      # 휴일이면 하루씩 뒤로
        got = False
        for board in KR_MARKETS:
            rows = fetch_kr_day(d, board, key)
            if not rows:
                continue
            got = True
            _upsert_universe(conn, "KR", board,
                             [(r[0], r[1], r[8]) for r in rows], d.isoformat())
            updated += len(rows)
        if got:
            break
        d -= timedelta(days=1)
    conn.commit()
    conn.close()
    if verbose:
        print(f"   KR 유니버스(종목명·시총) 기준일 {d} · {updated:,}종목 갱신")
    return updated


def sync_kr(days: int = 400, verbose: bool = True) -> int:
    key = _datago_key()
    conn = connect()
    done = {r[0] for r in conn.execute(
        "SELECT key FROM fetch_log WHERE market='KR'")}

    today = date.today()
    wanted: List[Tuple[date, str]] = []
    d = today
    span = int(days * 1.45)                       # 주말·휴일 감안
    for _ in range(span):
        if d.weekday() < 5:
            for b in KR_MARKETS:
                if f"{d.isoformat()}|{b}" not in done:
                    wanted.append((d, b))
        d -= timedelta(days=1)

    total = 0
    for i, (dt, board) in enumerate(wanted, 1):
        rows = fetch_kr_day(dt, board, key)
        conn.execute("INSERT OR REPLACE INTO fetch_log VALUES('KR',?,?)",
                     (f"{dt.isoformat()}|{board}", len(rows)))
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO bars VALUES('KR',?,?,?,?,?,?,?,?)",
                [(r[0], dt.isoformat(), r[2], r[3], r[4], r[5], r[6], r[7])
                 for r in rows])
            _upsert_universe(conn, "KR", board,
                             [(r[0], r[1], r[8]) for r in rows], dt.isoformat())
            total += len(rows)
        if i % 20 == 0:
            conn.commit()
            if verbose:
                print(f"   KR {i}/{len(wanted)} … 누적 {total:,}행", flush=True)
    conn.commit()
    conn.close()
    if verbose:
        print(f"   KR 동기화 완료: {total:,}행 신규")
    return total


# ---------------------------------------------------------------- US (미장)
US_UNIVERSE_CSV = HERE / "us_universe.csv"


def _us_universe_from_csv(conn: sqlite3.Connection) -> List[str]:
    """us_market.db 가 없는 PC용 폴백 (레포에 같이 다니는 CSV)."""
    import csv
    if not US_UNIVERSE_CSV.exists():
        return []
    today = date.today().isoformat()
    tk = []
    with US_UNIVERSE_CSV.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            tk.append(row["ticker"])
            conn.execute("INSERT OR REPLACE INTO universe VALUES('US',?,?,?,?,?)",
                         (row["ticker"], row.get("name") or row["ticker"],
                          row.get("sector") or "US", None, today))
    conn.commit()
    return tk


def us_universe(conn: sqlite3.Connection,
                which: str = "sp500") -> List[str]:
    """미장 유니버스 선택.

    which = 'sp500' | 'russell3000' | 'all'(DB에 있는 전부)
    """
    if which in ("russell3000", "r3000"):
        return _load_index_csv(conn, "russell3000")
    if which == "all":
        return [r[0] for r in conn.execute(
            "SELECT code FROM universe WHERE market='US' ORDER BY code")]
    src = ROOT / "us_market.db"
    if src.exists():
        c = sqlite3.connect(src)
        tk = [r[0] for r in c.execute(
            "SELECT ticker FROM us_universe ORDER BY ticker")]
        c.close()
        if tk:
            for t in tk:
                conn.execute(
                    "INSERT OR IGNORE INTO universe VALUES('US',?,?,?,?,?)",
                    (t, t, "US", None, date.today().isoformat()))
            names = sqlite3.connect(src)
            for t, n, s in names.execute(
                    "SELECT ticker,name,sector FROM us_universe"):
                conn.execute("UPDATE universe SET name=?, board=? "
                             "WHERE market='US' AND code=?", (n, s or "US", t))
            names.close()
            conn.commit()
            return tk
    return _us_universe_from_csv(conn) or [r[0] for r in conn.execute(
        "SELECT code FROM universe WHERE market='US' ORDER BY code")]


def sync_us_caps(tickers: Optional[List[str]] = None,
                 verbose: bool = True, which: str = "all") -> int:
    """미장 시가총액 갱신 — NASDAQ 스크리너 1회 호출.

    yfinance 벌크 다운로드는 시총을 안 준다. 그렇다고 종목별 fast_info 로
    수천 건을 때리면 야후가 대부분 막는다(실측 3,000건 중 1,177건만 성공).
    스크리너는 한 번에 5,800여 종목을 주므로 이쪽을 쓴다.
    """
    from .us_universe import fetch_caps, fetch_caps_bulk

    conn = connect()
    tk = tickers or us_universe(conn, which)
    today = date.today().isoformat()

    try:
        caps = fetch_caps_bulk(verbose=False)
    except Exception as e:                                       # noqa: BLE001
        print(f"   ⚠ 스크리너 실패({e}) → yfinance 폴백", file=sys.stderr)
        caps = fetch_caps(tk, verbose=False)

    want = set(tk)
    done = 0
    for t, cap in caps.items():
        if t not in want:
            continue
        conn.execute("UPDATE universe SET mktcap=?, updated=? "
                     "WHERE market='US' AND code=?", (cap, today, t))
        done += 1
    conn.commit()
    conn.close()
    if verbose:
        print(f"   US 시가총액 {done:,}/{len(tk):,}종목 갱신")
    return done


def _load_index_csv(conn: sqlite3.Connection, idx: str) -> List[str]:
    """러셀3000 등 지수 구성 CSV → universe + universe_index 반영."""
    from .us_universe import R3000_CSV, load_csv
    rows = load_csv(R3000_CSV if idx == "russell3000" else HERE / f"{idx}.csv")
    if not rows:
        print(f"   {idx} 목록이 없습니다 — "
              f"python -m chart_patterns.us_universe --build 먼저 실행",
              file=sys.stderr)
        return []
    today = date.today().isoformat()
    for r in rows:
        conn.execute(
            "INSERT INTO universe(market, code, name, board, mktcap, updated) "
            "VALUES('US',?,?,?,?,?) "
            "ON CONFLICT(market, code) DO UPDATE SET "
            "  name=excluded.name, mktcap=excluded.mktcap, "
            "  updated=excluded.updated",
            (r["ticker"], r.get("name") or r["ticker"], r.get("sector") or "US",
             float(r.get("mktcap") or 0) or None, r.get("as_of") or today))
        conn.execute(
            "INSERT OR REPLACE INTO universe_index VALUES('US',?,?,?,?)",
            (r["ticker"], idx, int(r.get("rank") or 0), r.get("as_of") or today))
    conn.commit()
    return [r["ticker"] for r in rows]


def sync_us(days: int = 400, tickers: Optional[List[str]] = None,
            verbose: bool = True, which: str = "sp500") -> int:
    import yfinance as yf

    conn = connect()
    tk = tickers or us_universe(conn, which)
    if not tk:
        print("   US 유니버스가 비어 있습니다", file=sys.stderr)
        return 0

    period = f"{max(1, round(days / 250) + 1)}y"
    total, chunk = 0, 120
    for i in range(0, len(tk), chunk):
        part = tk[i:i + chunk]
        try:
            df = yf.download(part, period=period, interval="1d",
                             auto_adjust=True, progress=False,
                             group_by="ticker", threads=True)
        except Exception as e:                                   # noqa: BLE001
            print(f"   ⚠ yfinance 실패 {part[0]}~: {e}", file=sys.stderr)
            continue
        rows = []
        for t in part:
            try:
                sub = df[t] if len(part) > 1 else df
                sub = sub.dropna(subset=["Close"])
            except Exception:                                    # noqa: BLE001
                continue
            for ts, r in sub.iterrows():
                vol = float(r.get("Volume") or 0)
                rows.append((t, ts.date().isoformat(), float(r["Open"]),
                             float(r["High"]), float(r["Low"]),
                             float(r["Close"]), vol, vol * float(r["Close"])))
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO bars VALUES('US',?,?,?,?,?,?,?,?)", rows)
            conn.commit()
            total += len(rows)
        if verbose:
            print(f"   US {min(i + chunk, len(tk))}/{len(tk)} … 누적 {total:,}행",
                  flush=True)
    conn.close()
    if verbose:
        print(f"   US 동기화 완료: {total:,}행")
    return total


# ------------------------------------------------------------------ 조회
@dataclass
class Series:
    market: str
    code: str
    name: str
    dates: List[str]
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    mktcap: Optional[float] = None

    def __len__(self) -> int:
        return len(self.c)

    def weekly(self) -> "Series":
        """일봉 → 주봉 (파이프/혼 등 주봉 패턴용). 주 단위는 ISO 주차."""
        import datetime as _dt
        keys, groups = [], []
        for i, ds in enumerate(self.dates):
            y, w, _ = _dt.date.fromisoformat(ds).isocalendar()
            k = (y, w)
            if not keys or keys[-1] != k:
                keys.append(k)
                groups.append([i])
            else:
                groups[-1].append(i)
        o = np.array([self.o[g[0]] for g in groups])
        h = np.array([self.h[g].max() for g in groups])
        lo = np.array([self.l[g].min() for g in groups])
        c = np.array([self.c[g[-1]] for g in groups])
        v = np.array([self.v[g].sum() for g in groups])
        d = [self.dates[g[-1]] for g in groups]
        return Series(self.market, self.code, self.name, d, o, h, lo, c, v,
                      self.mktcap)


def load_series(market: str, min_bars: int = 150,
                codes: Optional[Iterable[str]] = None,
                min_price: float = 0.0,
                min_value: float = 0.0,
                min_cap: float = 0.0,
                max_cap: float = 0.0,
                index: str = "") -> List[Series]:
    """캐시에서 종목별 시계열 로드.

    min_value : 최근 20일 평균 거래대금 하한 (국장 원, 미장 달러). 잡주 제거용.
    min_cap/max_cap : 시가총액 하한·상한 (국장 원, 미장 달러). 0 이면 미적용.
                      시총 정보가 없는 종목은 필터를 걸면 제외된다.
    index     : 'russell3000' 등 지수 구성종목으로 한정 (미장). 빈값이면 전체.
    """
    conn = connect()
    if index:
        members = {r[0] for r in conn.execute(
            "SELECT code FROM universe_index WHERE market=? AND idx=?",
            (market, index))}
        codes = (set(codes) & members) if codes else members
        if not codes:
            conn.close()
            return []
    meta = {r[0]: (r[1] or r[0], r[2]) for r in conn.execute(
        "SELECT code, name, mktcap FROM universe WHERE market=?", (market,))}
    want = set(codes) if codes else None

    cur = conn.execute(
        "SELECT code, dt, open, high, low, close, volume, value "
        "FROM bars WHERE market=? ORDER BY code, dt", (market,))
    out: List[Series] = []
    cur_code, buf = None, []

    def flush():
        if cur_code is None or len(buf) < min_bars:
            return
        name, cap = meta.get(cur_code, (cur_code, None))
        if min_cap or max_cap:
            if not cap:                       # 시총 미상은 필터 적용 시 제외
                return
            if min_cap and cap < min_cap:
                return
            if max_cap and cap > max_cap:
                return
        arr = np.array([[b[1], b[2], b[3], b[4], b[5], b[6]] for b in buf],
                       dtype=float)
        if min_price and arr[-1, 3] < min_price:
            return
        if min_value:
            tail = arr[-20:, 5]
            tail = tail[tail > 0]
            if tail.size == 0 or float(tail.mean()) < min_value:
                return
        s = Series(market, cur_code, name, [b[0] for b in buf], arr[:, 0],
                   arr[:, 1], arr[:, 2], arr[:, 3], arr[:, 4])
        s.mktcap = float(cap) if cap else None
        out.append(s)

    for code, dt, o, h, lo, c, v, val in cur:
        if want is not None and code not in want:
            continue
        if code != cur_code:
            flush()
            cur_code, buf = code, []
        if c is None or c <= 0:
            continue
        buf.append((dt, o or c, h or c, lo or c, c, v or 0, val or 0))
    flush()
    conn.close()
    return out


def coverage() -> None:
    conn = connect()
    for m in ("KR", "US"):
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT code), MIN(dt), MAX(dt) "
            "FROM bars WHERE market=?", (m,)).fetchone()
        print(f"  {m}: {row[0]:,}행 / {row[1]:,}종목 / {row[2]} ~ {row[3]}")
    conn.close()


def main() -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["KR", "US", "ALL"], default="ALL")
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--universe", choices=["sp500", "russell3000", "all"],
                    default="sp500", help="미장 유니버스 (기본 S&P500)")
    ap.add_argument("--caps", action="store_true",
                    help="시가총액만 갱신 (국장: 최근 영업일 / 미장: yfinance)")
    a = ap.parse_args()

    if a.status:
        coverage()
        return 0
    if a.caps:                                   # 시총만 갱신하고 종료
        if a.market in ("KR", "ALL"):
            print("▶ 국장 시가총액 갱신")
            refresh_kr_universe()
        if a.market in ("US", "ALL"):
            print("▶ 미장 시가총액 갱신")
            sync_us_caps(which=a.universe)
        return 0
    if a.market in ("KR", "ALL"):
        print("▶ 국장 수집 (data.go.kr)")
        sync_kr(a.days)
        refresh_kr_universe()                    # 시총은 최신 영업일 기준으로
    if a.market in ("US", "ALL"):
        print(f"▶ 미장 수집 (yfinance, {a.universe})")
        sync_us(a.days, which=a.universe)
        sync_us_caps(which=a.universe)
    coverage()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
