"""
chart_data_loader.py — EMS 차트 모듈 데이터 로더

데이터 소스:
  - pykrx (검증된 시그니처만 사용, 지시서 §3)
  - KIND(kind.krx.co.kr) 상장법인목록 — 종목 유니버스, 로그인 불필요
  - KRX 데이터마켓플레이스 직접 POST (신용잔고 — bld 미확정, 스텁)

DART는 절대 호출하지 않는다 (지시서 §1).

⚠️ KRX 로그인 이슈 (지시서 §7.1 — 실측 확인됨 2026-07-28):
    data.krx.co.kr/comm/bldAttendant/getJsonData.cmd 가 로그인 없이 호출 시
    HTTP 400 + 본문 "LOGOUT" 을 반환한다. 따라서 KRX 경유 지표
    (수급/공매도/PER·PBR)는 KRX_ID/KRX_PW 환경변수가 있어야 동작한다.
    - OHLCV(adjusted=True)는 네이버 소스라 로그인 없이 정상 동작한다.
    - 유니버스는 KIND 로 우회하여 로그인 없이 확보한다.
    KRX 계정 설정 시:  export KRX_ID=... ; export KRX_PW=...

TTL 캐시(≈300초)로 pykrx 과호출을 방지한다.
종목 유니버스(code->name/market)는 디스크에 일자별로 캐시한다.
"""

from __future__ import annotations

import json
import os
import re
import time
import threading
from datetime import datetime, timedelta

import requests
from pykrx import stock

# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------
_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".chart_cache")
_UNIVERSE_FILE = os.path.join(_CACHE_DIR, "ticker_universe.json")
_TTL_SECONDS = 300
_MARKETS = ("KOSPI", "KOSDAQ")

_KIND_URL = ("https://kind.krx.co.kr/corpgeneral/corpList.do"
             "?method=download&searchType=13")
_KIND_MARKET_MAP = {"유가": "KOSPI", "코스닥": "KOSDAQ", "코넥스": "KONEX"}
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"}

os.makedirs(_CACHE_DIR, exist_ok=True)


def krx_login_configured():
    """KRX_ID/KRX_PW 환경변수 설정 여부 (수급·공매도·펀더멘털 가용성 판단용)."""
    return bool(os.getenv("KRX_ID") and os.getenv("KRX_PW"))


_KRX_LOGIN_HINT = (
    "KRX 로그인 필요: data.krx.co.kr 가 비로그인 요청에 HTTP 400(LOGOUT)을 반환합니다. "
    "서버에 KRX_ID / KRX_PW 환경변수를 설정하세요."
)


class KrxLoginRequired(RuntimeError):
    """KRX 로그인이 없어 데이터를 못 받은 경우. 라우터가 사유를 프론트로 전달한다."""

    def __init__(self, what):
        super().__init__(f"{what} 조회 실패 — {_KRX_LOGIN_HINT}")
        self.what = what
        self.login_configured = krx_login_configured()


# ---------------------------------------------------------------------------
# 간단 TTL 캐시
# ---------------------------------------------------------------------------
_cache_lock = threading.Lock()
_ttl_store: dict = {}


def _ttl_get(key):
    with _cache_lock:
        item = _ttl_store.get(key)
        if item is None:
            return None
        ts, val = item
        if time.time() - ts > _TTL_SECONDS:
            _ttl_store.pop(key, None)
            return None
        return val


def _ttl_set(key, val):
    with _cache_lock:
        _ttl_store[key] = (time.time(), val)


# ---------------------------------------------------------------------------
# 날짜 유틸
# ---------------------------------------------------------------------------
def _today_str():
    return datetime.now().strftime("%Y%m%d")


def _fromdate_for(days):
    """거래일 days개를 확보하기 위해 넉넉히 캘린더일을 역산."""
    calendar_days = int(days * 1.6) + 20
    return (datetime.now() - timedelta(days=calendar_days)).strftime("%Y%m%d")


# ---------------------------------------------------------------------------
# 종목 유니버스 (code -> {name, market})
# ---------------------------------------------------------------------------
_universe_lock = threading.Lock()
_universe_mem = None  # (date_str, dict)


def _build_universe_kind():
    """KIND 상장법인목록에서 유니버스 구축 (1회 호출, 로그인 불필요).

    pykrx의 get_market_ticker_list 는 KRX 로그인이 필요해 실패하고,
    get_market_ticker_name 은 종목당 ~1.9초라 2700종목에 90분이 걸린다.
    KIND는 단일 요청(~0.7초)으로 회사명+시장구분+종목코드를 모두 준다.
    """
    html = requests.get(_KIND_URL, headers=_UA, timeout=30).content.decode("euc-kr", "replace")
    uni = {}
    # 표 구조: 회사명 | 시장구분 | 종목코드 | 업종 | ...
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        tds = [re.sub(r"<[^>]+>", " ", c) for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
        tds = [re.sub(r"\s+", " ", c).strip() for c in tds]
        if len(tds) >= 3 and re.fullmatch(r"\d{6}", tds[2]):
            market = _KIND_MARKET_MAP.get(tds[1], tds[1])
            if market in _MARKETS:  # KONEX 제외
                uni[tds[2]] = {"name": tds[0], "market": market}
    return uni


def _build_universe_pykrx(date_str):
    """폴백: pykrx 경유 (KRX 로그인 설정 시에만 현실적)."""
    uni = {}
    for market in _MARKETS:
        try:
            tickers = stock.get_market_ticker_list(date=date_str, market=market)
        except Exception as e:  # noqa: BLE001
            print(f"[loader] {market} ticker list 실패: {e}")
            continue
        for code in tickers:
            try:
                name = stock.get_market_ticker_name(code)
            except Exception:  # noqa: BLE001
                name = code
            uni[code] = {"name": name, "market": market}
    return uni


def _build_universe(date_str):
    try:
        uni = _build_universe_kind()
        if uni:
            return uni
        print("[loader] KIND 유니버스 비어있음 -> pykrx 폴백")
    except Exception as e:  # noqa: BLE001
        print(f"[loader] KIND 유니버스 실패({e}) -> pykrx 폴백")
    return _build_universe_pykrx(date_str)


def _load_universe():
    """일자별 유니버스. 디스크 캐시 우선, 없으면 빌드 후 저장."""
    global _universe_mem
    today = _today_str()

    with _universe_lock:
        if _universe_mem is not None and _universe_mem[0] == today:
            return _universe_mem[1]

        # 디스크 캐시
        if os.path.exists(_UNIVERSE_FILE):
            try:
                with open(_UNIVERSE_FILE, "r", encoding="utf-8") as f:
                    disk = json.load(f)
                if disk.get("date") == today and disk.get("universe"):
                    _universe_mem = (today, disk["universe"])
                    return _universe_mem[1]
            except Exception:  # noqa: BLE001
                pass

        # 빌드 (오늘 데이터가 아직 없으면 전영업일로 폴백)
        uni = _build_universe(today)
        if not uni:
            prev = (datetime.now() - timedelta(days=4)).strftime("%Y%m%d")
            uni = _build_universe(prev)

        _universe_mem = (today, uni)
        try:
            with open(_UNIVERSE_FILE, "w", encoding="utf-8") as f:
                json.dump({"date": today, "universe": uni}, f, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass
        return uni


def search_ticker(q, limit=20):
    """종목명/코드 부분일치 검색. -> [{code, name, market}]."""
    q = (q or "").strip()
    if not q:
        return []
    uni = _load_universe()
    q_lower = q.lower()
    results = []

    # 코드 완전일치 우선
    if q in uni:
        info = uni[q]
        results.append({"code": q, "name": info["name"], "market": info["market"]})

    for code, info in uni.items():
        if code == q:
            continue
        name = info["name"]
        if q_lower in name.lower() or q in code:
            results.append({"code": code, "name": name, "market": info["market"]})
        if len(results) >= limit:
            break

    # 이름 완전일치/시작일치를 앞으로
    def _rank(r):
        n = r["name"].lower()
        if n == q_lower:
            return 0
        if n.startswith(q_lower):
            return 1
        return 2

    results.sort(key=_rank)
    return results[:limit]


def get_ticker_name(ticker):
    uni = _load_universe()
    if ticker in uni:
        return uni[ticker]["name"]
    try:
        return stock.get_market_ticker_name(ticker)
    except Exception:  # noqa: BLE001
        return ticker


def get_market_of(ticker):
    uni = _load_universe()
    return uni.get(ticker, {}).get("market")


# ---------------------------------------------------------------------------
# OHLCV
# ---------------------------------------------------------------------------
def get_ohlcv(ticker, days=280):
    """OHLCV -> dict(dates, open, high, low, close, volume). 수정주가 기준."""
    key = ("ohlcv", ticker, days)
    cached = _ttl_get(key)
    if cached is not None:
        return cached

    fromdate = _fromdate_for(days)
    todate = _today_str()
    df = stock.get_market_ohlcv_by_date(fromdate, todate, ticker, adjusted=True)
    if df is None or df.empty:
        raise ValueError(f"OHLCV 데이터 없음: {ticker}")

    df = df.tail(days)

    def col(*names):
        for n in names:
            if n in df.columns:
                return df[n]
        raise KeyError(f"컬럼 못찾음: {names} / 실제={list(df.columns)}")

    out = {
        "ticker": ticker,
        "name": get_ticker_name(ticker),
        "market": get_market_of(ticker),
        "dates": [d.strftime("%Y-%m-%d") for d in df.index],
        "open": col("시가").astype(float).tolist(),
        "high": col("고가").astype(float).tolist(),
        "low": col("저가").astype(float).tolist(),
        "close": col("종가").astype(float).tolist(),
        "volume": col("거래량").astype(float).tolist(),
    }
    _ttl_set(key, out)
    return out


# ---------------------------------------------------------------------------
# 수급 (투자자별 순매수 거래대금)
# ---------------------------------------------------------------------------
# 기관 집계에 합산할 컬럼 후보 (detail=True 모드)
_INSTITUTION_COLS = ("금융투자", "보험", "투신", "사모", "은행",
                     "기타금융", "연기금", "연기금등", "기관합계")


def get_trading(ticker, days=280, verbose=False):
    """투자자별 순매수 거래대금.

    반환: dict(dates, 외국인, 기관, 개인, 연기금)
    컬럼명이 pykrx 버전마다 다를 수 있어 유연 매핑한다 (지시서 §7.2).
    """
    key = ("trading", ticker, days)
    cached = _ttl_get(key)
    if cached is not None:
        return cached

    fromdate = _fromdate_for(days)
    todate = _today_str()
    try:
        df = stock.get_market_trading_value_by_date(
            fromdate, todate, ticker, on="순매수", detail=True
        )
    except Exception as e:  # noqa: BLE001  (pykrx 내부에서 비-JSON 응답 시)
        raise KrxLoginRequired("수급") from e
    if df is None or df.empty:
        # KRX 비로그인 시 pykrx는 예외 대신 빈 DF를 반환한다 (실측).
        raise KrxLoginRequired("수급")

    df = df.tail(days)
    cols = list(df.columns)
    if verbose:
        print(f"[loader] 수급 컬럼({ticker}): {cols}")

    def find_col(*cands):
        for c in cands:
            if c in df.columns:
                return df[c]
        return None

    foreign = find_col("외국인", "외국인합계")
    # 기타외국인 있으면 외국인에 합산
    other_foreign = find_col("기타외국인")
    if foreign is not None and other_foreign is not None:
        foreign = foreign + other_foreign

    individual = find_col("개인")
    pension = find_col("연기금등", "연기금")

    inst = find_col("기관합계")
    if inst is None:
        # detail 모드: 기관 계열 컬럼 합산
        inst_members = [c for c in cols
                        if c in ("금융투자", "보험", "투신", "사모", "은행",
                                 "기타금융", "연기금", "연기금등")]
        if inst_members:
            inst = df[inst_members].sum(axis=1)

    def to_list(s):
        return None if s is None else s.astype(float).tolist()

    out = {
        "ticker": ticker,
        "dates": [d.strftime("%Y-%m-%d") for d in df.index],
        "외국인": to_list(foreign),
        "기관": to_list(inst),
        "개인": to_list(individual),
        "연기금": to_list(pension),
        "_columns": cols,
    }
    _ttl_set(key, out)
    return out


# ---------------------------------------------------------------------------
# 공매도 잔고 (옵션)
# ---------------------------------------------------------------------------
def get_shorting_balance(ticker, days=280):
    """공매도 잔고. 반환: dict(dates, balance_qty, balance_amount, ratio)."""
    key = ("short", ticker, days)
    cached = _ttl_get(key)
    if cached is not None:
        return cached

    fromdate = _fromdate_for(days)
    todate = _today_str()
    try:
        df = stock.get_shorting_balance_by_date(fromdate, todate, ticker)
    except Exception as e:  # noqa: BLE001
        raise KrxLoginRequired("공매도잔고") from e
    if df is None or df.empty:
        raise KrxLoginRequired("공매도잔고")
    df = df.tail(days)
    cols = list(df.columns)

    def find_col(*cands):
        for c in cands:
            if c in df.columns:
                return df[c].astype(float).tolist()
        return None

    out = {
        "ticker": ticker,
        "dates": [d.strftime("%Y-%m-%d") for d in df.index],
        "balance_qty": find_col("공매도잔고", "잔고수량"),
        "balance_amount": find_col("공매도금액", "잔고금액"),
        "ratio": find_col("비중"),
        "_columns": cols,
    }
    _ttl_set(key, out)
    return out


# ---------------------------------------------------------------------------
# 펀더멘털 (PER/PBR — 옵션)
# ---------------------------------------------------------------------------
def get_fundamental(ticker, days=280):
    key = ("fund", ticker, days)
    cached = _ttl_get(key)
    if cached is not None:
        return cached

    fromdate = _fromdate_for(days)
    todate = _today_str()
    try:
        df = stock.get_market_fundamental_by_date(fromdate, todate, ticker)
    except Exception as e:  # noqa: BLE001
        raise KrxLoginRequired("펀더멘털") from e
    if df is None or df.empty:
        raise KrxLoginRequired("펀더멘털")
    df = df.tail(days)

    def find_col(c):
        return df[c].astype(float).tolist() if c in df.columns else None

    out = {
        "ticker": ticker,
        "dates": [d.strftime("%Y-%m-%d") for d in df.index],
        "PER": find_col("PER"),
        "PBR": find_col("PBR"),
        "EPS": find_col("EPS"),
        "DIV": find_col("DIV"),
        "_columns": list(df.columns),
    }
    _ttl_set(key, out)
    return out


# ---------------------------------------------------------------------------
# 신용잔고 (신용거래융자) — KRX 데이터마켓플레이스 직접
# ---------------------------------------------------------------------------
# ❗ bld 코드 미확정. 추측 금지 (지시서 §3, §7.3).
# TODO: KRX(data.krx.co.kr) Network 탭에서 개별종목 신용거래융자 실제 bld 확인 후 교체.
#       확인 방법:
#         1) http://data.krx.co.kr -> [통계] -> 개별종목 신용거래 화면 진입
#         2) 개발자도구 Network -> getJsonData.cmd 요청의 payload 중 'bld' 값 확인
#         3) 아래 _CREDIT_BLD 에 넣고, 파라미터(isuCd/strtDd/endDd 등) 매핑
_CREDIT_BLD = None  # 예: "dbms/MDC/STAT/standard/MDCSTAT0XXXX"
_KRX_JSON_URL = "http://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"


def get_credit_balance(ticker, days=280):
    """신용거래융자 잔고. bld 미확정이라 현재는 비활성 (프론트에서 에러 핸들).

    bld 확정 시 아래 requests POST 로직을 활성화한다.
    """
    if _CREDIT_BLD is None:
        return {
            "ticker": ticker,
            "available": False,
            "reason": "KRX 신용거래융자 bld 미확정 (TODO). 프론트에서 신용 패널 비활성.",
            "dates": [], "credit_balance": [],
        }

    # --- bld 확정 후 활성화할 스텁 ---
    # import requests
    # from pykrx.stock import get_market_ticker_name  # isuCd 확보용 별도 처리 필요
    # payload = {
    #     "bld": _CREDIT_BLD,
    #     "isuCd": _to_isu_cd(ticker),   # KRX 표준코드(예: KR7005930003) 매핑 필요
    #     "strtDd": _fromdate_for(days),
    #     "endDd": _today_str(),
    # }
    # headers = {"Referer": "http://data.krx.co.kr/"}
    # r = requests.post(_KRX_JSON_URL, data=payload, headers=headers, timeout=10)
    # rows = r.json().get("output", [])
    # ... 파싱 ...
    raise NotImplementedError("신용잔고 bld 확정 후 구현")


# ---------------------------------------------------------------------------
# 반대매매 (KOFIA) — 이번 범위 제외 (옵션 주석)
# ---------------------------------------------------------------------------
# TODO(옵션): 위탁매매 미수금 대비 반대매매 = KOFIA(freesis.kofia.or.kr) 통계.
#             이번 범위 밖. 필요 시 별도 로더로 추가.


# ---------------------------------------------------------------------------
# CLI 스모크 테스트
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import sys

    tk = sys.argv[1] if len(sys.argv) > 1 else "005930"

    print(f"=== KRX 로그인 설정: {krx_login_configured()} ===")
    print("=== search_ticker('삼성전자') ===")
    t0 = time.time()
    res = search_ticker("삼성전자", limit=5)
    print(f"  ({time.time() - t0:.1f}s) {res}")

    print("=== search_ticker('005930') ===")
    print(f"  {search_ticker('005930', limit=3)}")

    print(f"=== get_ohlcv({tk}, days=30) ===")
    o = get_ohlcv(tk, days=30)
    print(f"  name={o['name']} market={o['market']} rows={len(o['dates'])}")
    print(f"  last date={o['dates'][-1]} close={o['close'][-1]:,.0f} vol={o['volume'][-1]:,.0f}")

    print(f"=== get_trading({tk}, days=30) [컬럼 확인] ===")
    try:
        tr = get_trading(tk, days=30, verbose=True)
        print(f"  외국인 last={tr['외국인'][-1] if tr['외국인'] else None}")
        print(f"  기관   last={tr['기관'][-1] if tr['기관'] else None}")
        print(f"  개인   last={tr['개인'][-1] if tr['개인'] else None}")
        print(f"  연기금 last={tr['연기금'][-1] if tr['연기금'] else None}")
    except Exception as e:  # noqa: BLE001
        print(f"  [수급 실패] {e}")

    print("[DONE]")
