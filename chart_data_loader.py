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
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
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


def _read_env_file(path):
    """.env 한 개를 환경변수로 로드 (python-dotenv 의존 없이).

    이미 설정된 키는 덮어쓰지 않는다 → 우선순위: 실제 환경변수 > 앞쪽 파일 > 뒤쪽 파일.
    """
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and v and k not in os.environ:
                    os.environ[k] = v
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[loader] .env 로드 실패(무시) {path}: {e}")
        return False


def _load_dotenv():
    """.env 를 여러 후보 경로에서 순서대로 로드한다.

    KIS 키처럼 이미 다른 프로젝트에 있는 자격증명을 복사하지 않고 재사용하기 위함.
    운영(EMS 서버)에서는 실제 환경변수만 있으면 되고, 후보 파일이 없어도 무해하다.

    우선순위:
      1) 실제 환경변수 (운영 우선)
      2) 이 모듈 폴더의 .env
      3) CHART_ENV_FILES 에 지정한 경로들 (콜론 구분)
      4) 로컬 개발 편의: ../stock-bot/.env  (기존 KIS 키 재사용)
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [os.path.join(here, ".env")]
    extra = os.getenv("CHART_ENV_FILES", "")
    candidates += [p for p in extra.split(":") if p]
    candidates.append(os.path.join(here, os.pardir, "stock-bot", ".env"))
    # KRX 오픈API 키(KRX_API_KEY)는 hermes-trade 에 있다. 복사하지 않고 재사용한다.
    candidates.append(os.path.expanduser("~/hermes-trade/.env"))

    for path in candidates:
        _read_env_file(os.path.normpath(path))


_load_dotenv()


_krx_login_disabled = False


def disable_krx_login(reason=""):
    """KRX 웹로그인 경로를 이 프로세스에서 끈다.

    계정이 잠기면(CD007) pykrx 는 매 호출마다 재로그인을 시도하다 예외를 던진다.
    반복 시도는 잠금을 연장시키므로, 한 번 실패하면 환경변수를 비워
    pykrx 가 스스로 '미설정' 경로(예외 없이 None 반환)를 타게 한다.
    오픈API(KRX_API_KEY)·KIS 는 별개 인증이라 영향받지 않는다.
    """
    global _krx_login_disabled
    if _krx_login_disabled:
        return
    _krx_login_disabled = True
    os.environ.pop("KRX_ID", None)
    os.environ.pop("KRX_PW", None)
    print(f"[loader] KRX 웹로그인 비활성화 (재시도 중단): {reason}")


def krx_login_broken():
    return _krx_login_disabled


def _pykrx(fn, *a, **kw):
    """pykrx 호출 래퍼. 로그인 관련 실패면 차단기를 올리고 None 을 준다."""
    if _krx_login_disabled:
        return None
    try:
        return fn(*a, **kw)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "Expecting value" in msg or "JSONDecode" in msg or "로그인" in msg:
            disable_krx_login(f"{type(e).__name__}: {msg[:60]}")
            return None
        raise


def krx_login_configured():
    """KRX_ID/KRX_PW 환경변수 설정 여부 (수급·공매도·펀더멘털 가용성 판단용)."""
    return bool(os.getenv("KRX_ID") and os.getenv("KRX_PW"))


_KRX_LOGIN_HINT = (
    "KRX 로그인 필요: data.krx.co.kr 가 비로그인 요청에 HTTP 400(LOGOUT)을 반환합니다. "
    "서버에 KRX_ID / KRX_PW 환경변수를 설정하세요."
)


# ---------------------------------------------------------------------------
# KIS(한국투자증권) 폴백 — KRX 로그인이 없을 때 수급을 최근 30일이라도 채운다
# ---------------------------------------------------------------------------
# 읽기전용 시세 조회만 사용한다. 주문 관련 API는 절대 호출하지 않는다.
_KIS_BASE = "https://openapi.koreainvestment.com:9443"
_KIS_TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".cache", "kis_token.json")
_kis_token_mem = {"token": None, "expires": 0.0}
_kis_lock = threading.Lock()

# KIS inquire-investor 는 요청 기간과 무관하게 최근 약 30영업일만 반환한다(실측).
KIS_MAX_DAYS = 30


def kis_configured():
    return bool(os.getenv("KIS_APP_KEY") and os.getenv("KIS_APP_SECRET"))


def _kis_token():
    """OAuth 토큰: 메모리 -> 파일 -> 신규발급. 1분 발급한도 때문에 캐시 필수."""
    now = time.time()
    with _kis_lock:
        if _kis_token_mem["token"] and _kis_token_mem["expires"] > now + 60:
            return _kis_token_mem["token"]
        try:
            with open(_KIS_TOKEN_FILE, encoding="utf-8") as f:
                cached = json.load(f)
            if cached.get("token") and cached.get("expires", 0) > now + 60:
                _kis_token_mem.update(cached)
                return cached["token"]
        except Exception:  # noqa: BLE001
            pass

        r = requests.post(
            f"{_KIS_BASE}/oauth2/tokenP",
            json={"grant_type": "client_credentials",
                  "appkey": os.getenv("KIS_APP_KEY"),
                  "appsecret": os.getenv("KIS_APP_SECRET")},
            timeout=15,
        )
        r.raise_for_status()
        d = r.json()
        token = d["access_token"]
        expires = now + int(d.get("expires_in", 86400))
        _kis_token_mem.update({"token": token, "expires": expires})
        try:
            os.makedirs(os.path.dirname(_KIS_TOKEN_FILE), exist_ok=True)
            with open(_KIS_TOKEN_FILE, "w", encoding="utf-8") as f:
                json.dump({"token": token, "expires": expires}, f)
            os.chmod(_KIS_TOKEN_FILE, 0o600)
        except Exception:  # noqa: BLE001
            pass
        return token


def get_trading_kis(ticker, days=KIS_MAX_DAYS):
    """KIS 종목별 투자자매매동향 (최근 ~30영업일, 순매수 '수량').

    ⚠️ pykrx 경로는 거래대금(원) 기준인데 KIS는 수량(주) 기준이라 단위가 다르다.
       반환 dict 의 unit 필드로 구분한다.
    """
    if not kis_configured():
        raise RuntimeError("KIS_APP_KEY / KIS_APP_SECRET 미설정")

    end = _today_str()
    start = (datetime.now() - timedelta(days=days * 2 + 10)).strftime("%Y%m%d")
    r = requests.get(
        f"{_KIS_BASE}/uapi/domestic-stock/v1/quotations/inquire-investor",
        headers={
            "authorization": f"Bearer {_kis_token()}",
            "appkey": os.getenv("KIS_APP_KEY"),
            "appsecret": os.getenv("KIS_APP_SECRET"),
            "tr_id": "FHKST01010900",
        },
        params={
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": ticker,
            "FID_INPUT_DATE_1": start,
            "FID_INPUT_DATE_2": end,
            "FID_PERIOD_DIV_CODE": "D",
        },
        timeout=20,
    )
    r.raise_for_status()
    rows = r.json().get("output") or []
    if not rows:
        raise RuntimeError("KIS 수급 응답 없음")

    rows = list(reversed(rows))          # KIS는 최신순 -> 과거순으로 뒤집기
    dates, foreign, inst, indiv = [], [], [], []
    for it in rows:
        d = it.get("stck_bsop_date", "")
        if len(d) != 8:
            continue
        dates.append(f"{d[:4]}-{d[4:6]}-{d[6:]}")
        foreign.append(float(it.get("frgn_ntby_qty", 0) or 0))
        inst.append(float(it.get("orgn_ntby_qty", 0) or 0))
        indiv.append(float(it.get("prsn_ntby_qty", 0) or 0))

    return {
        "ticker": ticker, "dates": dates,
        "외국인": foreign, "기관": inst, "개인": indiv, "연기금": None,
        "source": "KIS", "unit": "주(수량)",
        "note": f"KRX 로그인 미설정으로 KIS 폴백. 최근 {len(dates)}영업일만 제공.",
    }


def _kis_get(path, tr_id, params, timeout=20):
    r = requests.get(
        f"{_KIS_BASE}{path}",
        headers={"authorization": f"Bearer {_kis_token()}",
                 "appkey": os.getenv("KIS_APP_KEY"),
                 "appsecret": os.getenv("KIS_APP_SECRET"),
                 "tr_id": tr_id},
        params=params, timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def _f(v):
    """KIS 문자열 숫자 -> float. 결측(99.99 등 자리표시자 포함) 처리."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x


def get_forward_estimates(ticker):
    """컨센서스 추정실적 + 현재 시가총액 기준 포워드 PER/PBR.

    소스: KIS 종목추정실적(HHKST668300C0) + 대차대조표(FHKST66430100).
    단위는 KIS 관례대로 억원.

    ⚠️ 응답 구조 주의(실측):
      - output4: 기간 라벨 5개. 'E' 접미사가 붙은 것이 추정치.
      - output2: 항상 6행 = [매출액, 매출증감률, 영업이익, 영업이익증감률,
                            순이익, 순이익증감률]. 증감률은 ×10 스케일.
      - output3: 행 수가 종목마다 다르다(3행/8행 실측). PER 등을 인덱스로
        집으면 종목에 따라 엉뚱한 값을 읽으므로 사용하지 않고 직접 계산한다.
      - 커버리지 없는 종목은 output 자체가 비어온다(예: 086520).
    """
    key = ("fwd", ticker)
    cached = _ttl_get(key)
    if cached is not None:
        return cached

    if not kis_configured():
        return {"available": False, "reason": "KIS_APP_KEY / KIS_APP_SECRET 미설정"}

    try:
        d = _kis_get("/uapi/domestic-stock/v1/quotations/estimate-perform",
                     "HHKST668300C0", {"SHT_CD": ticker})
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"추정실적 조회 실패: {e}"}

    o1 = d.get("output1") or {}
    o2 = d.get("output2") or []
    o4 = d.get("output4") or []
    if not o4 or len(o2) < 5:
        return {"available": False,
                "reason": "증권사 컨센서스 커버리지 없음(추정치 미제공 종목)"}

    periods = [x.get("dt", "") for x in o4]
    cols = [f"data{i}" for i in range(1, len(periods) + 1)]

    def row(idx):
        if idx >= len(o2):
            return [None] * len(periods)
        return [_f(o2[idx].get(c)) for c in cols]

    revenue = row(0)          # 매출액 (억원)
    op_profit = row(2)        # 영업이익 (억원)
    net_income = row(4)       # 순이익 (억원)

    # --- 현재 시가총액 (억원) ---
    mktcap_eok = None
    cap_src = None
    try:
        df = _pykrx(stock.get_market_cap_by_ticker, _today_str(), market="ALL")
        if ticker in df.index:
            mktcap_eok = float(df.loc[ticker, "시가총액"]) / 1e8
            cap_src = "KRX"
    except Exception:  # noqa: BLE001
        pass
    if mktcap_eok is None:
        # 폴백: 최근 종가 × DART 발행주식수.
        # (이전엔 KRX 월말시총 캐시의 마지막 값을 썼는데, 부분 수집으로 캐시가
        #  오염되면 몇 년 전 시총을 현재값으로 쓰는 사고가 났다 — 삼양식품 실측)
        try:
            shares = get_shares_dart(ticker, years=3)
            o = get_ohlcv(ticker, days=10)
            if shares and o["close"]:
                n = _shares_at(shares, o["dates"][-1])
                if n:
                    mktcap_eok = o["close"][-1] * n / 1e8
                    cap_src = "종가×DART주식수"
        except Exception:  # noqa: BLE001
            pass
    if mktcap_eok is None:
        return {"available": False, "reason": "시가총액 조회 실패"}

    # --- 최근 자본총계 (억원) ---
    equity = None
    try:
        bs = _kis_get("/uapi/domestic-stock/v1/finance/balance-sheet", "FHKST66430100",
                      {"FID_DIV_CLS_CODE": "0", "fid_cond_mrkt_div_code": "J",
                       "fid_input_iscd": ticker}).get("output") or []
        if bs:
            equity = _f(bs[0].get("total_cptl"))
    except Exception:  # noqa: BLE001
        pass

    # --- 포워드 배수 계산 ---
    rows = []
    cum_ni = 0.0
    for i, p in enumerate(periods):
        ni = net_income[i]
        is_est = p.endswith("E")
        if ni is not None:
            cum_ni += ni
        per = (mktcap_eok / ni) if (ni and ni > 0) else None
        # 추정 자본 = 최근 자본총계 + 추정 순이익 누계 (배당 미반영 → 근사)
        pbr = None
        if equity:
            eq_i = equity + (cum_ni if is_est else 0.0)
            pbr = mktcap_eok / eq_i if eq_i > 0 else None
        opm = (op_profit[i] / revenue[i] * 100) if (revenue[i] and op_profit[i] is not None) else None
        rows.append({
            "period": p, "is_estimate": is_est,
            "revenue": revenue[i], "op_profit": op_profit[i], "net_income": ni,
            "op_margin": opm, "per": per, "pbr": pbr,
        })

    out = {
        "available": True,
        "ticker": ticker,
        "name": o1.get("item_kor_nm"),
        "opinion": o1.get("rcmd_name") or None,
        "est_date": o1.get("estdate") or None,
        "analyst": o1.get("name1") or None,
        "mktcap_eok": mktcap_eok,
        "mktcap_src": cap_src,
        "equity_eok": equity,
        "unit": "억원",
        "rows": rows,
        "pbr_note": "포워드 PBR은 (최근 자본총계 + 추정 순이익 누계) 기준 근사치(배당 미반영)",
    }
    _ttl_set(key, out)
    return out


# ---------------------------------------------------------------------------
# 분봉 (멀티타임프레임) — KIS 전용. pykrx 는 일봉만 제공한다.
# ---------------------------------------------------------------------------
# KIS 일별분봉(FHKST03010230)은 호출당 1분봉 120건을 주며, 지정 시각에서
# 과거로 내려가다 날짜 경계를 자동으로 넘어간다(실측 확인).
_INTRADAY_URL = "/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice"
_INTRADAY_TR = "FHKST03010230"
_INTRADAY_PER_CALL = 120
_INTRADAY_MAX_CALLS = 40          # 폭주 방지 상한 (≈4800 분봉)


def _minus_one_minute(date_str, hhmmss):
    dt = datetime.strptime(date_str + hhmmss[:4], "%Y%m%d%H%M") - timedelta(minutes=1)
    return dt.strftime("%Y%m%d"), dt.strftime("%H%M") + "00"


def get_intraday_1m(ticker, need_bars=600):
    """1분봉을 과거로 페이지네이션하며 수집. 오래된 것 -> 최신 순으로 반환.

    반환: [{"dt": "YYYY-MM-DD HH:MM", "o","h","l","c","v"}, ...]
    """
    if not kis_configured():
        raise RuntimeError("KIS_APP_KEY / KIS_APP_SECRET 미설정 (분봉은 KIS 전용)")

    key = ("i1m", ticker, need_bars)
    cached = _ttl_get(key)
    if cached is not None:
        return cached

    seen = {}
    cur_date, cur_hour = _today_str(), "153000"
    calls = 0
    while len(seen) < need_bars and calls < _INTRADAY_MAX_CALLS:
        calls += 1
        try:
            d = _kis_get(_INTRADAY_URL, _INTRADAY_TR, {
                "FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": ticker,
                "FID_INPUT_DATE_1": cur_date, "FID_INPUT_HOUR_1": cur_hour,
                "FID_PW_DATA_INCU_YN": "Y", "FID_FAKE_TICK_INCU_YN": "",
            })
        except Exception as e:  # noqa: BLE001
            if not seen:
                raise RuntimeError(f"분봉 조회 실패: {e}") from e
            break

        rows = d.get("output2") or []
        if not rows:
            break

        for it in rows:
            ds, hs = it.get("stck_bsop_date", ""), it.get("stck_cntg_hour", "")
            if len(ds) != 8 or len(hs) < 4:
                continue
            stamp = f"{ds[:4]}-{ds[4:6]}-{ds[6:]} {hs[:2]}:{hs[2:4]}"
            if stamp in seen:
                continue
            try:
                seen[stamp] = {
                    "dt": stamp,
                    "o": float(it.get("stck_oprc", 0) or 0),
                    "h": float(it.get("stck_hgpr", 0) or 0),
                    "l": float(it.get("stck_lwpr", 0) or 0),
                    "c": float(it.get("stck_prpr", 0) or 0),
                    "v": float(it.get("cntg_vol", 0) or 0),
                }
            except (TypeError, ValueError):
                continue

        last = rows[-1]
        nd, nh = _minus_one_minute(last.get("stck_bsop_date"), last.get("stck_cntg_hour"))
        if (nd, nh) == (cur_date, cur_hour):     # 진행 없음 -> 무한루프 방지
            break
        cur_date, cur_hour = nd, nh

    bars = [seen[k] for k in sorted(seen)]
    bars = [b for b in bars if b["c"] > 0]
    _ttl_set(key, bars)
    return bars


def resample_bars(bars, minutes):
    """1분봉 -> N분봉 집계. 한국장 09:00 기준 정시 경계로 버킷팅."""
    out = {}
    for b in bars:
        date_part, time_part = b["dt"].split(" ")
        hh, mm = int(time_part[:2]), int(time_part[3:5])
        total = hh * 60 + mm
        bucket = (total // minutes) * minutes
        label = f"{date_part} {bucket // 60:02d}:{bucket % 60:02d}"
        cur = out.get(label)
        if cur is None:
            out[label] = {"dt": label, "o": b["o"], "h": b["h"],
                          "l": b["l"], "c": b["c"], "v": b["v"]}
        else:
            cur["h"] = max(cur["h"], b["h"])
            cur["l"] = min(cur["l"], b["l"])
            cur["c"] = b["c"]           # bars 가 시간순이므로 마지막이 종가
            cur["v"] += b["v"]
    return [out[k] for k in sorted(out)]


def get_intraday(ticker, tf_minutes=5, bars=120):
    """N분봉. tf_minutes 에 맞춰 필요한 1분봉을 계산해 수집 후 집계."""
    need_1m = min(bars * tf_minutes + tf_minutes * 5,
                  _INTRADAY_PER_CALL * _INTRADAY_MAX_CALLS)
    raw = get_intraday_1m(ticker, need_bars=need_1m)
    agg = resample_bars(raw, tf_minutes) if tf_minutes > 1 else raw
    return agg[-bars:]


# ---------------------------------------------------------------------------
# 역사적 밸류에이션 밴드 (PER / PBR / POR / ROE)
# ---------------------------------------------------------------------------
# 월말 시가총액 × DART 연간 재무.
#   PER = 시총/당기순이익   PBR = 시총/자본총계
#   POR = 시총/영업이익     ROE = 당기순이익/자본총계
#
# KRX 오픈API 는 '날짜 1개 = 전종목' 이라, 월말 180개 날짜를 한 번 받아두면
# 그 안에 모든 종목의 시총이 들어있다. 그래서 첫 조회만 느리고 이후는 즉시다.
_KRX_OPENAPI = "https://data-dbg.krx.co.kr/svc/apis"
_mktcap_cache = {"data": None, "built": 0.0}
_mktcap_lock = threading.Lock()
_MKTCAP_TTL = 6 * 3600
_MKTCAP_FILE = os.path.join(_CACHE_DIR, "monthly_mktcap.json")


def _krx_openapi_key():
    return os.getenv("KRX_API_KEY")


def _month_end_dates(years):
    """최근 N년의 월말(마지막 평일) 날짜 목록."""
    out = []
    today = datetime.now()
    y, m = today.year, today.month
    for _ in range(years * 12 + 1):
        # 해당 월의 마지막 날 -> 주말이면 앞으로 당김
        nm_y, nm_m = (y + 1, 1) if m == 12 else (y, m + 1)
        d = datetime(nm_y, nm_m, 1) - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        if d <= today:
            out.append(d.strftime("%Y%m%d"))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    out.reverse()
    return out


def get_monthly_mktcap(years=15, workers=10):
    """{ticker: [(YYYY-MM-DD, mktcap), ...]} — 전종목 월말 시가총액.

    첫 호출만 네트워크를 타고(약 180콜) 이후엔 메모리/디스크 캐시를 쓴다.
    """
    with _mktcap_lock:
        now = time.time()
        if _mktcap_cache["data"] and now - _mktcap_cache["built"] < _MKTCAP_TTL:
            return _mktcap_cache["data"]
        try:
            st = os.path.getmtime(_MKTCAP_FILE)
            if now - st < _MKTCAP_TTL:
                with open(_MKTCAP_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                _mktcap_cache.update({"data": data, "built": now})
                return data
        except (OSError, ValueError):
            pass

        key = _krx_openapi_key()
        if not key:
            raise RuntimeError("KRX_API_KEY 미설정 (밴드는 KRX 오픈API 필요)")

        dates = _month_end_dates(years)

        def fetch(ds):
            got = []
            for ep in ("sto/stk_bydd_trd", "sto/ksq_bydd_trd"):
                try:
                    r = requests.get(f"{_KRX_OPENAPI}/{ep}", params={"basDd": ds},
                                     headers={"AUTH_KEY": key}, timeout=30)
                    d = r.json()
                    rows = next((v for v in d.values() if isinstance(v, list)), [])
                    got.extend(rows)
                except Exception:  # noqa: BLE001
                    continue
            return ds, got

        out = defaultdict(list)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for ds, rows in ex.map(fetch, dates):
                if not rows:
                    continue
                label = f"{ds[:4]}-{ds[4:6]}-{ds[6:]}"
                for r in rows:
                    code = r.get("ISU_CD")
                    if not code:
                        continue
                    try:
                        cap = float(str(r.get("MKTCAP", "0")).replace(",", ""))
                    except (TypeError, ValueError):
                        continue
                    if cap > 0:
                        out[code].append((label, cap))
        data = {k: sorted(v) for k, v in out.items()}
        _mktcap_cache.update({"data": data, "built": now})
        try:
            with open(_MKTCAP_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except OSError:
            pass
        return data


def get_shares_dart(ticker, years=15):
    """DART 주식총수현황 -> {연도: 발행주식총수}.

    시가총액을 KRX 에서 날짜별로 긁는 대신 '종가 × 주식수' 로 만들기 위한 것.
    KRX 오픈API 는 날짜당 1콜이라 15년치면 수천 콜이고, 실제로 IP 차단까지 갔다.
    DART 는 종목당 연 1콜이면 되고 별도 서버라 KRX 상태와 무관하다.
    유상증자·액면분할로 주식수가 변하므로 연도별로 받아 구간 적용한다.
    """
    key = ("shares", ticker, years)
    cached = _ttl_get(key)
    if cached is not None:
        return cached

    import build_static as bs
    dkey = bs._dart_key()
    corp = bs.dart_corp_map().get(ticker)
    if not dkey or not corp:
        return {}

    this_year = datetime.now().year
    yrs = list(range(this_year - years, this_year + 1))

    def fetch(y):
        try:
            r = requests.get(f"{bs.DART_BASE}/stockTotqySttus.json",
                             params={"crtfc_key": dkey, "corp_code": corp,
                                     "bsns_year": y, "reprt_code": "11011"},
                             timeout=25)
            rows = r.json().get("list") or []
        except Exception:  # noqa: BLE001
            return y, None
        for it in rows:
            if (it.get("se") or "").strip() in ("합계", "보통주"):
                try:
                    n = float(str(it.get("istc_totqy", "")).replace(",", ""))
                except (TypeError, ValueError):
                    continue
                if n > 0:
                    return y, n
        return y, None

    out = {}
    with ThreadPoolExecutor(max_workers=5) as ex:
        for y, n in ex.map(fetch, yrs):
            if n:
                out[y] = n
    _ttl_set(key, out)
    return out


def _shares_at(shares_by_year, date_str):
    """해당 시점에 유효한 주식수 (그 연도 값, 없으면 가장 가까운 과거 연도)."""
    if not shares_by_year:
        return None
    y = int(date_str[:4])
    for yy in range(y, min(shares_by_year) - 1, -1):
        if yy in shares_by_year:
            return shares_by_year[yy]
    return shares_by_year[min(shares_by_year)]


def _reported_fy(date_str):
    """그 시점에 공시되어 있었을 회계연도.
    FY Y 사업보고서는 이듬해 3월경 제출되므로 4월 이후라야 FY(Y-1)을 쓴다.
    (안 그러면 미래 실적으로 과거 PER 을 그리는 선행편향이 생긴다)"""
    y, m = int(date_str[:4]), int(date_str[5:7])
    return y - 1 if m >= 4 else y - 2


def _ltm_timeline(quarters):
    """분기 리스트 -> [(공시일, {ni, op, rev, eq}), ...] LTM 누적.

    각 시점에서 '그때까지 공시된' 최근 4개 분기를 합한다(선행편향 제거).
    자본총계는 저량이라 합산하지 않고 최신값을 쓴다.
    """
    rows = sorted((q for q in quarters if q.get("avail")), key=lambda q: q["avail"])
    out = []
    for i, q in enumerate(rows):
        win = rows[max(0, i - 3):i + 1]
        if len(win) < 4:
            continue
        agg = {}
        for f in ("rev", "op", "ni"):
            vals = [w[f] for w in win if w.get(f) is not None]
            agg[f] = sum(vals) if len(vals) == 4 else None
        agg["eq"] = next((w["eq"] for w in reversed(win) if w.get("eq") is not None), None)
        out.append((q["avail"], agg))
    return out


def get_valuation_bands(ticker, years=15, daily=True, basis="FY"):
    """PER/PBR/POR/ROE 시계열 + 포워드 배수.

    일별로 계산한다. 시가총액 = 종가 × 상장주식수인데 주식수는 시간에 따라
    변하므로(유상증자·분할 등), 월말 시총에서 역산한 주식수를 구간별로 적용한다:
        shares(월) = 월말시총 / 월말종가
    """
    key = ("bands", ticker, years, daily, basis)
    cached = _ttl_get(key)
    if cached is not None:
        return cached

    import build_static as bs                     # DART 수집 재사용
    fin = bs.dart_financials([ticker], years=years).get(ticker)
    if not fin:
        return {"available": False, "reason": "DART 재무 없음(비상장/신규상장 등)"}

    ltm = None
    if basis == "LTM":
        ltm = _ltm_timeline(bs.dart_quarterly(ticker, years=years))
        if not ltm:
            return {"available": False, "reason": "LTM 산출 불가(분기 데이터 부족)"}

    # --- 일별 시가총액 = 종가 × 발행주식수(DART, 연도별) ---
    # KRX 를 날짜별로 긁지 않는다(호출 1회 = OHLCV, 주식수는 DART).
    shares_by_year = get_shares_dart(ticker, years=years)
    if not shares_by_year:
        return {"available": False, "reason": "발행주식수 없음(DART)"}
    try:
        o = get_ohlcv(ticker, days=int(years * 252))
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"OHLCV 실패: {e}"}

    points = []                                    # [(date, mktcap)]
    for d, c in zip(o["dates"], o["close"]):
        n = _shares_at(shares_by_year, d)
        if n and c:
            points.append((d, c * n))
    if not points:
        return {"available": False, "reason": "시가총액 산출 불가"}

    series = {"PER": [], "PBR": [], "POR": [], "ROE": []}
    li = 0
    for date, cap in points:
        if basis == "LTM":
            while li + 1 < len(ltm) and ltm[li + 1][0] <= date:
                li += 1
            if ltm[li][0] > date:               # 아직 4개 분기가 안 쌓임
                continue
            fy = ltm[li][1]
        else:
            fy = fin.get(_reported_fy(date))
        if not fy:
            continue
        ni, eq, op = fy.get("ni"), fy.get("eq"), fy.get("op")
        if ni and ni > 0:
            series["PER"].append([date, round(cap / ni, 2)])
        if eq and eq > 0:
            series["PBR"].append([date, round(cap / eq, 3)])
            if ni:
                series["ROE"].append([date, round(ni / eq * 100, 2)])
        if op and op > 0:
            series["POR"].append([date, round(cap / op, 2)])

    # --- 포워드 배수 (증권사 추정치가 있으면) ---
    forward = None
    cur_cap = points[-1][1] if points else None
    try:
        est = get_forward_estimates(ticker)
        if est.get("available") and cur_cap:
            rows = []
            for r in est["rows"]:
                if not r.get("is_estimate"):
                    continue
                ni, op, = r.get("net_income"), r.get("op_profit")
                # DART 는 원, KIS 추정은 억원 -> 억원으로 통일
                cap_eok = cur_cap / 1e8
                rows.append({
                    "period": r["period"],
                    "PER": round(cap_eok / ni, 2) if ni and ni > 0 else None,
                    "POR": round(cap_eok / op, 2) if op and op > 0 else None,
                    "PBR": r.get("pbr"),
                    "ROE": round(ni / (op or 1) * 0, 2) if False else None,
                })
            if rows:
                forward = {"rows": rows, "opinion": est.get("opinion"),
                           "est_date": est.get("est_date")}
    except Exception:  # noqa: BLE001
        forward = None

    out = {"available": True, "ticker": ticker,
           "basis": basis,
           "resolution": "daily",
           "mktcap_source": "종가 × DART 발행주식수",
           "series": series, "forward": forward,
           "fin": {str(y): v for y, v in sorted(fin.items())},
           "note": "FY 실적은 이듬해 3월경 공시 -> 4월부터 반영(선행편향 제거)"}
    _ttl_set(key, out)
    return out


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
    # ⚠️ OHLCV(adjusted=True)는 네이버 소스라 KRX 로그인이 필요 없다.
    #    다만 pykrx 는 KRX_ID/PW 가 있으면 호출 전에 로그인을 먼저 시도하고,
    #    계정이 잠겨 있으면 거기서 예외를 던져 멀쩡한 시세까지 못 받는다.
    #    -> 첫 실패에 차단기를 올린 뒤(환경변수 제거) 바로 재시도한다.
    try:
        df = stock.get_market_ohlcv_by_date(fromdate, todate, ticker, adjusted=True)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "Expecting value" in msg or "JSONDecode" in msg:
            disable_krx_login(f"OHLCV 중 로그인 실패: {msg[:40]}")
            df = stock.get_market_ohlcv_by_date(fromdate, todate, ticker, adjusted=True)
        else:
            raise
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
    """투자자별 수급. pykrx(KRX 로그인) 우선, 실패 시 KIS 폴백(최근 30일).

    반환: dict(dates, 외국인, 기관, 개인, 연기금, source, unit)
    """
    try:
        return _get_trading_pykrx(ticker, days=days, verbose=verbose)
    except KrxLoginRequired:
        if kis_configured():
            try:
                out = get_trading_kis(ticker)
                _ttl_set(("trading", ticker, days), out)
                return out
            except Exception as e:  # noqa: BLE001
                raise KrxLoginRequired(f"수급(KIS 폴백도 실패: {e})") from e
        raise


def _get_trading_pykrx(ticker, days=280, verbose=False):
    """pykrx 경유 투자자별 순매수 '거래대금'. KRX 로그인 필요.

    컬럼명이 pykrx 버전마다 다를 수 있어 유연 매핑한다 (지시서 §7.2).
    """
    key = ("trading", ticker, days)
    cached = _ttl_get(key)
    if cached is not None:
        return cached

    fromdate = _fromdate_for(days)
    todate = _today_str()
    try:
        df = _pykrx(stock.get_market_trading_value_by_date,
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
        "source": "KRX(pykrx)", "unit": "원(거래대금)",
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
        df = _pykrx(stock.get_shorting_balance_by_date, fromdate, todate, ticker)
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
# 벤치마크 지수 (Minervini RS 계산용) — KRX 로그인 필요
# ---------------------------------------------------------------------------
_INDEX_CODE = {"KOSPI": "1001", "KOSDAQ": "2001"}


_IDX_OPENAPI = {
    "KOSPI": ("idx/kospi_dd_trd", "코스피"),
    "KOSPI200": ("idx/kospi_dd_trd", "코스피 200"),
    "KOSDAQ": ("idx/kosdaq_dd_trd", "코스닥"),
    "KOSDAQ150": ("idx/kosdaq_dd_trd", "코스닥 150"),
}


_idx_bulk_cache = {"data": None, "built": 0.0, "days": 0}
_idx_lock = threading.Lock()


def _fetch_indices_bulk(days=280, workers=4):
    """날짜별로 지수 응답을 '한 번만' 받아 모든 지수를 함께 담는다.

    지수 하나당 434일씩 따로 돌면 같은 응답을 4번 받는 셈이라 레이트리밋에 걸린다.
    (실측: 4종 개별 호출 시 KOSPI 실패, KOSDAQ 1일만 수신)
    한 응답에 40~51개 지수가 들어있으므로 한 번 받아 나눠 쓴다.

    반환: {(ep, IDX_NM): {날짜: row}}
    """
    with _idx_lock:
        now = time.time()
        c = _idx_bulk_cache
        if c["data"] and now - c["built"] < _MKTCAP_TTL and c["days"] >= days:
            return c["data"]

        key = _krx_openapi_key()
        if not key:
            raise RuntimeError("KRX_API_KEY 미설정")

        cand, d = [], datetime.now()
        while len(cand) < int(days * 1.55):
            if d.weekday() < 5:
                cand.append(d.strftime("%Y%m%d"))
            d -= timedelta(days=1)

        eps = sorted({ep for ep, _ in _IDX_OPENAPI.values()})

        def fetch(ds):
            got = {}
            for ep in eps:
                for attempt in range(3):
                    try:
                        r = requests.get(f"{_KRX_OPENAPI}/{ep}", params={"basDd": ds},
                                         headers={"AUTH_KEY": key}, timeout=25)
                        rows = next((v for v in r.json().values()
                                     if isinstance(v, list)), [])
                        for x in rows:
                            got[(ep, (x.get("IDX_NM") or "").strip())] = x
                        break
                    except Exception:  # noqa: BLE001
                        time.sleep(0.5 * (attempt + 1))     # 레이트리밋 백오프
            return ds, got

        out = defaultdict(dict)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for ds, got in ex.map(fetch, cand):
                for k, row in got.items():
                    out[k][ds] = row
        data = dict(out)
        _idx_bulk_cache.update({"data": data, "built": now, "days": days})
        return data


def get_index_ohlcv_openapi(name="KOSPI", days=280, workers=6):
    """KRX 오픈API 로 지수 시계열을 만든다.

    pykrx 의 get_index_ohlcv_by_date 는 KRX 웹로그인이 필요해 계정이 잠기면
    막힌다. 오픈API 는 별개 인증(AUTH_KEY)이라 영향을 받지 않는다.
    '날짜 1개 = 전 지수' 구조라 날짜만 병렬로 돌면 된다.
    """
    key = _krx_openapi_key()
    if not key:
        raise RuntimeError("KRX_API_KEY 미설정")
    ep, idx_nm = _IDX_OPENAPI.get(name, _IDX_OPENAPI["KOSPI"])

    cache_key = ("idxopen", name, days)
    cached = _ttl_get(cache_key)
    if cached is not None:
        return cached

    got = _fetch_indices_bulk(days).get((ep, idx_nm), {})

    def _num(v):
        try:
            return float(str(v).replace(",", ""))
        except (TypeError, ValueError):
            return None

    out = {"dates": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
    for ds in sorted(got)[-days:]:
        x = got[ds]
        c = _num(x.get("CLSPRC_IDX"))
        if not c:
            continue
        out["dates"].append(f"{ds[:4]}-{ds[4:6]}-{ds[6:]}")
        out["open"].append(_num(x.get("OPNPRC_IDX")) or c)
        out["high"].append(_num(x.get("HGPRC_IDX")) or c)
        out["low"].append(_num(x.get("LWPRC_IDX")) or c)
        out["close"].append(c)
        out["volume"].append(_num(x.get("ACC_TRDVOL")) or 0.0)
    out["name"] = idx_nm
    out["source"] = "KRX오픈API"
    _ttl_set(cache_key, out)
    return out


def get_benchmark_close(dates, market="KOSPI"):
    """종목 날짜열에 정렬된 벤치마크 지수 종가 리스트.

    dates 와 길이가 같은 list 를 반환하며, 지수 데이터가 없는 날짜는 직전 값으로
    채운다(휴장 등). 조회 실패 시 KrxLoginRequired.
    """
    code = _INDEX_CODE.get(market, "1001")
    key = ("bench", code, dates[0] if dates else "", dates[-1] if dates else "")
    cached = _ttl_get(key)
    if cached is not None:
        return cached

    fromdate = dates[0].replace("-", "") if dates else _fromdate_for(280)
    todate = dates[-1].replace("-", "") if dates else _today_str()
    by_date = None
    try:
        df = _pykrx(stock.get_index_ohlcv_by_date, fromdate, todate, code)
        if df is not None and not df.empty:
            by_date = {d.strftime("%Y-%m-%d"): float(v)
                       for d, v in zip(df.index, df["종가"].astype(float))}
    except Exception:  # noqa: BLE001
        by_date = None

    if not by_date:
        # pykrx 는 KRX 웹로그인이 필요하다. 계정이 잠겨도 오픈API 로는 받을 수 있다.
        # ⚠️ 다만 오픈API 지수는 '날짜당 1콜' 이라 수백 콜이 나간다.
        #    /full 같은 일반 조회에서 이걸 매번 트리거하면 KRX 에 과부하를 주고
        #    (실측: IP 차단까지 갔다) 응답도 몇 분씩 걸린다.
        #    -> 이미 받아둔 캐시가 있을 때만 쓰고, 없으면 즉시 포기한다.
        if _idx_bulk_cache.get("data"):
            try:
                idx = get_index_ohlcv_openapi(market, days=max(len(dates), 280))
                by_date = dict(zip(idx["dates"], idx["close"]))
            except Exception:  # noqa: BLE001
                by_date = None
        if not by_date:
            raise KrxLoginRequired("벤치마크지수(캐시 없음 — RS 조건 제외)")
    if not by_date:
        raise KrxLoginRequired("벤치마크지수")
    out, last = [], None
    for d in dates:
        last = by_date.get(d, last)
        out.append(last)
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
        df = _pykrx(stock.get_market_fundamental_by_date, fromdate, todate, ticker)
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
