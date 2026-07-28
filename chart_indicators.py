"""
chart_indicators.py — EMS 차트 모듈 지표 계산 (순수 numpy)

에이트파트너스 EMS 신규 차트 기능. 입력은 OHLCV numpy 배열,
출력은 JSON 직렬화 가능 dict (nan -> None).

지시서 §4 스펙을 그대로 구현한다. 표준 지표(SMA/EMA/볼린저/RSI/이격도)와
비표준 지표(LazyBear 스퀴즈 모멘텀, RSI Bear 다이버전스, Hull MA, Trendoscope
축소판 패턴 감지)를 포함한다.

의존성: numpy 만.  스모크 테스트:  python chart_indicators.py
"""

from __future__ import annotations

import numpy as np


# ---------------------------------------------------------------------------
# 직렬화 헬퍼
# ---------------------------------------------------------------------------
def _nan_to_none(arr):
    """numpy 배열 -> list, nan/inf -> None (JSON 직렬화 안전)."""
    out = []
    for x in arr:
        if x is None:
            out.append(None)
        else:
            xf = float(x)
            out.append(None if (np.isnan(xf) or np.isinf(xf)) else xf)
    return out


# ---------------------------------------------------------------------------
# 이동평균류
# ---------------------------------------------------------------------------
def sma(x, n):
    """단순이동평균 (period n).

    NaN 이 없으면 cumsum 고속 경로, 있으면 윈도우 루프(NaN 포함 윈도우는 NaN).
    cumsum 은 NaN 을 만나면 이후 전부 오염되므로 분기가 필요하다.
    """
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    if len(x) < n or n <= 0:
        return out
    if not np.any(np.isnan(x)):
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
        return out
    for i in range(n - 1, len(x)):
        w = x[i - n + 1:i + 1]
        if not np.any(np.isnan(w)):
            out[i] = w.mean()
    return out


def ema(x, n):
    """지수이동평균 (period n). TradingView 관례대로 첫 n개 SMA로 시드.

    선행 NaN(예: lowest/highest 결과)이 있어도 첫 유효 윈도우에서 시드한다.
    시드 이후 중간 NaN은 직전 값을 유지한다.
    """
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    if len(x) < n or n <= 0:
        return out
    start = None
    for i in range(n - 1, len(x)):
        if not np.any(np.isnan(x[i - n + 1:i + 1])):
            start = i
            break
    if start is None:
        return out
    alpha = 2.0 / (n + 1.0)
    out[start] = np.mean(x[start - n + 1:start + 1])
    for i in range(start + 1, len(x)):
        if np.isnan(x[i]):
            out[i] = out[i - 1]
        else:
            out[i] = alpha * x[i] + (1.0 - alpha) * out[i - 1]
    return out


def wma(x, n):
    """가중이동평균 (weights 1..n). nan 포함 윈도우는 nan."""
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    if len(x) < n or n <= 0:
        return out
    w = np.arange(1, n + 1, dtype=float)
    ws = w.sum()
    for i in range(n - 1, len(x)):
        window = x[i - n + 1:i + 1]
        out[i] = np.dot(window, w) / ws  # window에 nan 있으면 자동 nan
    return out


def bollinger(close, n=20, k=2.0):
    """볼린저밴드 (period n, k 표준편차). TV처럼 모표준편차(ddof=0)."""
    close = np.asarray(close, dtype=float)
    mid = sma(close, n)
    std = np.full(len(close), np.nan)
    for i in range(n - 1, len(close)):
        std[i] = np.std(close[i - n + 1:i + 1])  # ddof=0
    upper = mid + k * std
    lower = mid - k * std
    return mid, upper, lower


def rsi_wilder(close, n=14):
    """RSI (Wilder 스무딩)."""
    close = np.asarray(close, dtype=float)
    out = np.full(len(close), np.nan)
    if len(close) <= n:
        return out
    delta = np.diff(close)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = np.mean(gain[:n])
    avg_loss = np.mean(loss[:n])

    def _rsi(ag, al):
        if al == 0:
            return 100.0
        rs = ag / al
        return 100.0 - 100.0 / (1.0 + rs)

    out[n] = _rsi(avg_gain, avg_loss)  # gain[0:n] -> close 1..n -> 첫 RSI는 index n
    for i in range(n + 1, len(close)):
        avg_gain = (avg_gain * (n - 1) + gain[i - 1]) / n
        avg_loss = (avg_loss * (n - 1) + loss[i - 1]) / n
        out[i] = _rsi(avg_gain, avg_loss)
    return out


def disparity(close, n=50):
    """MA 이격도 = (close / SMA(n) - 1) * 100."""
    close = np.asarray(close, dtype=float)
    s = sma(close, n)
    with np.errstate(invalid="ignore", divide="ignore"):
        d = (close / s - 1.0) * 100.0
    return d


def hma(x, p=60):
    """Hull MA: HMA(p) = WMA(2*WMA(p/2) - WMA(p), round(sqrt(p)))."""
    x = np.asarray(x, dtype=float)
    half = int(round(p / 2.0))
    sq = int(round(np.sqrt(p)))
    raw = 2.0 * wma(x, half) - wma(x, p)
    return wma(raw, sq)


# ---------------------------------------------------------------------------
# (a) 스퀴즈 모멘텀 (LazyBear)
# ---------------------------------------------------------------------------
def _linreg_endpoint(y, n):
    """길이 n 윈도우 y에 대해 x=0..n-1 선형회귀, 엔드포인트(x=n-1) 값 반환 (offset 0)."""
    x = np.arange(n, dtype=float)
    xm = x.mean()
    ym = y.mean()
    denom = ((x - xm) ** 2).sum()
    b = ((x - xm) * (y - ym)).sum() / denom
    a = ym - b * xm
    return a + b * (n - 1)


def squeeze_momentum(high, low, close, n=20):
    """LazyBear 스퀴즈 모멘텀 값.

    m[i]   = close[i] - ((max(high,n)+min(low,n))/2 + SMA(close,n)) / 2
    val[i] = linreg(m[i-n+1..i]) 엔드포인트값 (offset 0)
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    L = len(close)
    smac = sma(close, n)
    m = np.full(L, np.nan)
    for i in range(n - 1, L):
        hh = np.max(high[i - n + 1:i + 1])
        ll = np.min(low[i - n + 1:i + 1])
        basis = ((hh + ll) / 2.0 + smac[i]) / 2.0
        m[i] = close[i] - basis
    val = np.full(L, np.nan)
    for i in range(n - 1, L):
        if i - n + 1 < 0:
            continue
        window = m[i - n + 1:i + 1]
        if np.any(np.isnan(window)):
            continue
        val[i] = _linreg_endpoint(window, n)
    return val


# 스퀴즈 색상 (지시서 §4a)
_SQZ_BRIGHT_TEAL = "#2dd4bf"
_SQZ_DARK_TEAL = "#0f766e"
_SQZ_BRIGHT_RED = "#f43f5e"
_SQZ_DARK_RED = "#7f1d1d"


def squeeze_colors(val):
    """val>=0 & 상승=밝은teal / >=0 & 하락=어두운teal / <0 & 하락=밝은red / <0 & 상승=어두운red."""
    colors = []
    for i in range(len(val)):
        v = val[i]
        if v is None or np.isnan(v):
            colors.append(None)
            continue
        prev = val[i - 1] if (i > 0 and not np.isnan(val[i - 1])) else v
        rising = v > prev
        if v >= 0:
            colors.append(_SQZ_BRIGHT_TEAL if rising else _SQZ_DARK_TEAL)
        else:
            colors.append(_SQZ_BRIGHT_RED if not rising else _SQZ_DARK_RED)
    return colors


# ---------------------------------------------------------------------------
# (b) RSI Bear 다이버전스
# ---------------------------------------------------------------------------
def pivot_highs(series, left=4, right=4):
    """피봇하이 인덱스 목록. series[i]가 좌우 각 left/right개보다 엄격히 큰 지점."""
    series = np.asarray(series, dtype=float)
    idx = []
    for i in range(left, len(series) - right):
        c = series[i]
        if all(c > series[i - j] for j in range(1, left + 1)) and \
           all(c > series[i + j] for j in range(1, right + 1)):
            idx.append(i)
    return idx


def rsi_bear_divergence(close, rsi, left=4, right=4):
    """연속 두 종가 피봇하이 a,b 에서 price[b]>price[a] and RSI[b]<RSI[a] -> b 마킹."""
    close = np.asarray(close, dtype=float)
    rsi = np.asarray(rsi, dtype=float)
    phs = pivot_highs(close, left, right)
    marks = []
    for k in range(1, len(phs)):
        a, b = phs[k - 1], phs[k]
        if np.isnan(rsi[a]) or np.isnan(rsi[b]):
            continue
        if close[b] > close[a] and rsi[b] < rsi[a]:
            marks.append(int(b))
    return marks


# ---------------------------------------------------------------------------
# (d) 패턴 감지 (Trendoscope 축소판)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Pine 내장함수 대응 헬퍼
# ---------------------------------------------------------------------------
def true_range(high, low, close):
    """ta.tr(true) — 첫 봉은 high-low."""
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    tr = np.empty(len(close))
    tr[0] = high[0] - low[0]
    for i in range(1, len(close)):
        tr[i] = max(high[i] - low[i],
                    abs(high[i] - close[i - 1]),
                    abs(low[i] - close[i - 1]))
    return tr


def stdev(x, n):
    """ta.stdev (모표준편차)."""
    x = np.asarray(x, dtype=float)
    out = np.full(len(x), np.nan)
    for i in range(n - 1, len(x)):
        w = x[i - n + 1:i + 1]
        if not np.any(np.isnan(w)):
            out[i] = np.std(w)
    return out


def rising(x, n):
    """ta.rising — 최근 n봉 연속 상승이면 True."""
    x = np.asarray(x, dtype=float)
    out = np.zeros(len(x), dtype=bool)
    for i in range(n, len(x)):
        w = x[i - n:i + 1]
        if np.any(np.isnan(w)):
            continue
        out[i] = bool(np.all(np.diff(w) > 0))
    return out


def _crossover(a, b, i):
    """i봉에서 a가 b를 상향 돌파."""
    if i < 1:
        return False
    if np.isnan(a[i]) or np.isnan(b[i]) or np.isnan(a[i - 1]) or np.isnan(b[i - 1]):
        return False
    return a[i] > b[i] and a[i - 1] <= b[i - 1]


def _crossunder(a, b, i):
    if i < 1:
        return False
    if np.isnan(a[i]) or np.isnan(b[i]) or np.isnan(a[i - 1]) or np.isnan(b[i - 1]):
        return False
    return a[i] < b[i] and a[i - 1] >= b[i - 1]


# ---------------------------------------------------------------------------
# Squeeze Momentum Oscillator [AlgoAlpha] 이식
# ---------------------------------------------------------------------------
# 출처: TradingView 공개 오픈소스 (script/WDsx1YV1). Pine 복사 아닌 재작성.
# LazyBear 판(squeeze_momentum)과는 완전히 다른 지표다.
def squeeze_algoalpha(high, low, close, mom_len=10, swing_len=20,
                      sq_period=14, sq_smooth=7, sq_ema=14, hyper_len=5):
    """AlgoAlpha 스퀴즈 모멘텀.

    - 스퀴즈: ATR 대비 EMA(ATR) 차이를 고저폭으로 정규화 -> 변동성 압축도
    - 모멘텀(vf): 추세방향(고/저 EMA 돌파 상태)에 따른 기준선 대비 거리의 HMA
    - zscore: vf 를 20봉 z-score 로 정규화 후 EMA, ×66 스케일
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    n = len(close)

    # --- 스퀴즈 ---
    atr = ema(true_range(high, low, close), sq_period)
    ema_atr = ema(atr, sq_period * 2)
    volatility = ema_atr - atr
    hl = high - low
    ema_hl = ema(hl, sq_period * 2)
    with np.errstate(invalid="ignore", divide="ignore"):
        raw = volatility / ema_hl * 100.0
    sq_val = ema(raw, sq_smooth)
    sq_ma = ema(sq_val, sq_ema)
    hyper = rising(sq_val, hyper_len) & (sq_val > 0)

    # --- 모멘텀 ---
    lowest_l = np.full(n, np.nan)
    highest_h = np.full(n, np.nan)
    for i in range(mom_len - 1, n):
        lowest_l[i] = np.min(low[i - mom_len + 1:i + 1])
        highest_h[i] = np.max(high[i - mom_len + 1:i + 1])
    l_line = ema(lowest_l, mom_len)
    h_line = ema(highest_h, mom_len)

    d = np.zeros(n, dtype=int)
    cur = 0
    for i in range(n):
        if _crossover(close, h_line, i):
            cur = 1
        elif _crossunder(close, l_line, i):
            cur = -1
        d[i] = cur

    val = np.where(d == 1, l_line, h_line)
    val1 = close - val
    val2 = hma(val1, mom_len)
    with np.errstate(invalid="ignore", divide="ignore"):
        vf = (val2 / ema(hl, mom_len * 2) * 100.0) / 8.0

    # --- z-score ---
    basis = sma(vf, swing_len)
    sd = stdev(vf, swing_len)
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (vf - basis) / sd
    z = ema(z, swing_len) * 66.0

    return {
        "vf": _nan_to_none(vf),
        "zscore": _nan_to_none(z),
        "squeeze_val": _nan_to_none(sq_val),
        "squeeze_ma": _nan_to_none(sq_ma),
        "hyper": [bool(x) for x in hyper],
    }


# ---------------------------------------------------------------------------
# Support and Resistance Levels with Breaks [LuxAlgo] 이식
# ---------------------------------------------------------------------------
# 출처: TradingView 공개 오픈소스 (script/JDFoWQbL). Pine 복사 아닌 재작성.
def sr_breaks_luxalgo(open_, high, low, close, volume,
                      left_bars=15, right_bars=15, vol_thresh=20.0):
    """LuxAlgo S/R + 돌파 신호.

    저항/지지 = pivothigh/pivotlow(left,right) 를 fixnan 으로 이어 붙인 선.
    돌파는 거래량 오실레이터(EMA5 vs EMA10)가 임계 초과일 때만 'B',
    캔들 꼬리가 길면 'Bull/Bear Wick' 으로 구분한다.
    """
    open_ = np.asarray(open_, dtype=float)
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    volume = np.asarray(volume, dtype=float)
    n = len(close)

    # 피봇 (확정은 right_bars 뒤, 원본은 추가로 [1] 시프트)
    ph = np.full(n, np.nan)
    pl = np.full(n, np.nan)
    for i in range(left_bars, n - right_bars):
        c = high[i]
        if all(c > high[i - j] for j in range(1, left_bars + 1)) and \
           all(c > high[i + j] for j in range(1, right_bars + 1)):
            ph[i + right_bars] = c
        c2 = low[i]
        if all(c2 < low[i - j] for j in range(1, left_bars + 1)) and \
           all(c2 < low[i + j] for j in range(1, right_bars + 1)):
            pl[i + right_bars] = c2

    def _fixnan_shift1(src):
        out = np.full(n, np.nan)
        last = np.nan
        for i in range(n):
            prev = src[i - 1] if i >= 1 else np.nan
            if not np.isnan(prev):
                last = prev
            out[i] = last
        return out

    res = _fixnan_shift1(ph)      # 저항
    sup = _fixnan_shift1(pl)      # 지지

    short_v = ema(volume, 5)
    long_v = ema(volume, 10)
    with np.errstate(invalid="ignore", divide="ignore"):
        osc = 100.0 * (short_v - long_v) / long_v

    breaks = []
    for i in range(1, n):
        strong = (not np.isnan(osc[i])) and osc[i] > vol_thresh
        up = _crossover(close, res, i)
        dn = _crossunder(close, sup, i)
        if up:
            bull_wick = (open_[i] - low[i]) > (close[i] - open_[i])
            if bull_wick:
                breaks.append({"index": i, "type": "bull_wick", "label": "Bull Wick",
                               "price": float(close[i])})
            elif strong:
                breaks.append({"index": i, "type": "break_up", "label": "B",
                               "price": float(close[i])})
        if dn:
            bear_wick = (open_[i] - close[i]) < (high[i] - open_[i])
            if bear_wick:
                breaks.append({"index": i, "type": "bear_wick", "label": "Bear Wick",
                               "price": float(close[i])})
            elif strong:
                breaks.append({"index": i, "type": "break_down", "label": "B",
                               "price": float(close[i])})

    return {"resistance": _nan_to_none(res), "support": _nan_to_none(sup),
            "vol_osc": _nan_to_none(osc), "breaks": breaks}


# ---------------------------------------------------------------------------
# Order Blocks [Flux Charts] 이식
# ---------------------------------------------------------------------------
# 출처: TradingView 공개 오픈소스 (script/bLdpFVuq). Pine 복사 아닌 재작성.
def order_blocks_flux(high, low, close, volume, swing_length=10,
                      max_atr_mult=3.5, max_blocks=3, invalidation="Wick"):
    """Flux 방식 오더블록 (거래량 정보 포함).

    스윙 고점을 종가가 돌파하면, 스윙~현재 구간에서 저점이 가장 낮은 봉의
    low~high 를 강세 오더블록으로 잡는다(약세는 대칭). 블록 높이가
    ATR(10)×3.5 를 넘으면 버린다. 거래량은 최근 3봉 합.
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    volume = np.asarray(volume, dtype=float)
    n = len(close)
    if n < swing_length + 3:
        return {"bull": [], "bear": []}

    atr = ema(true_range(high, low, close), 10)

    bull, bear = [], []
    swing_type = 0
    top = bottom = None            # (index, price)
    top_crossed = bottom_crossed = True

    for i in range(swing_length, n):
        upper = np.max(high[i - swing_length + 1:i + 1])
        lower = np.min(low[i - swing_length + 1:i + 1])
        j = i - swing_length
        prev_type = swing_type
        if high[j] > upper:
            swing_type = 0
        elif low[j] < lower:
            swing_type = 1
        if swing_type == 0 and prev_type != 0:
            top = (j, high[j])
            top_crossed = False
        if swing_type == 1 and prev_type != 1:
            bottom = (j, low[j])
            bottom_crossed = False

        # --- 강세 오더블록 ---
        if top is not None and not top_crossed and close[i] > top[1]:
            top_crossed = True
            box_btm = high[i - 1]
            box_top = low[i - 1]
            span = i - top[0] - 1
            for k in range(1, max(span, 1)):
                idx = i - k
                if idx < 0:
                    break
                if low[idx] < box_btm:
                    box_btm = low[idx]
                    box_top = high[idx]
            size = abs(box_top - box_btm)
            if not np.isnan(atr[i]) and size <= atr[i] * max_atr_mult and size > 0:
                bull.insert(0, {
                    "index": int(i), "top": float(box_top), "bottom": float(box_btm),
                    "volume": float(volume[i] + volume[i - 1] + volume[i - 2]),
                    "type": "bull",
                })
                bull[:] = bull[:max_blocks]

        # --- 약세 오더블록 ---
        if bottom is not None and not bottom_crossed and close[i] < bottom[1]:
            bottom_crossed = True
            box_top = low[i - 1]
            box_btm = high[i - 1]
            span = i - bottom[0] - 1
            for k in range(1, max(span, 1)):
                idx = i - k
                if idx < 0:
                    break
                if high[idx] > box_top:
                    box_top = high[idx]
                    box_btm = low[idx]
            size = abs(box_top - box_btm)
            if not np.isnan(atr[i]) and size <= atr[i] * max_atr_mult and size > 0:
                bear.insert(0, {
                    "index": int(i), "top": float(box_top), "bottom": float(box_btm),
                    "volume": float(volume[i] + volume[i - 1] + volume[i - 2]),
                    "type": "bear",
                })
                bear[:] = bear[:max_blocks]

    # 무효화 판정 (Wick=꼬리, Close=종가 기준)
    for blk in bull:
        ref = low if invalidation == "Wick" else close
        blk["broken"] = bool(np.any(ref[blk["index"]:] < blk["bottom"]))
    for blk in bear:
        ref = high if invalidation == "Wick" else close
        blk["broken"] = bool(np.any(ref[blk["index"]:] > blk["top"]))

    return {"bull": bull, "bear": bear}


# ---------------------------------------------------------------------------
# Trendoscope 원본 알고리즘 이식 (ACP)
# ---------------------------------------------------------------------------
# 출처: TradingView 공개 오픈소스
#   - Auto Chart Patterns [Trendoscope®]  (script/WZ8B1FIW)
#   - ZigzagLite (library, script/lJXwpXtt)
#   - basechartpatterns (library, script/95AW01ki)
# Pine 코드를 복사한 것이 아니라 알고리즘을 파이썬으로 재작성했다.
# 원저작자: Trendoscope. TradingView 공개 스크립트는 통상 MPL-2.0 이다.
#
# ⚠️ 앞서 쓰던 퍼센트 편차(dev=4.5%) 지그재그와는 알고리즘이 다르다.
#    원본은 ta.highestbars/lowestbars 기반 피봇 방식이다.

ZZ_LENGTH = 8          # 원본 기본값 (zigzagLength1)
ZZ_DEPTH = 55          # 원본 기본값 (depth1) — 보관할 최대 피봇 수
FLAT_RATIO = 0.20      # flatThreshold 20% -> ratio
ERROR_THRESHOLD = 0.20  # errorThresold 20%
BAR_RATIO_LIMIT = 0.382  # barRatioLimit


def zigzag_pivots(high, low, length=ZZ_LENGTH, depth=ZZ_DEPTH):
    """Trendoscope ZigzagLite 방식 지그재그.

    각 봉에서 최근 length 봉 중 최고/최저이면 피봇 후보가 된다.
    같은 방향에서 더 극단적인 값이 나오면 직전 피봇을 교체(removeOld)하고,
    반대 방향이면 새 피봇을 추가한다.

    반환: [(index, price, 'H'|'L'), ...]  (오래된 것 -> 최신 순)
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    n = len(high)
    if n < length + 1:
        return []

    # pivots: 최신이 앞 (Pine 의 unshift 구조와 동일). 각 항목 [index, price, dir]
    # dir: +1 = 고점 피봇, -1 = 저점 피봇
    pivots = []

    for i in range(length - 1, n):
        w = slice(i - length + 1, i + 1)
        hw, lw = high[w], low[w]
        p_high, p_low = float(hw.max()), float(lw.min())
        # ta.highestbars == 0 <=> 현재 봉이 구간 최고
        p_high_bar0 = bool(high[i] >= p_high)
        p_low_bar0 = bool(low[i] <= p_low)

        p_dir = 1
        last = None
        if pivots:
            last = pivots[0]
            p_dir = 1 if last[2] > 0 else -1

        force_double = False
        if len(pivots) > 1:
            llast = pivots[1]
            if p_dir == 1 and p_low_bar0:
                force_double = p_low < llast[1]
            elif p_dir == -1 and p_high_bar0:
                force_double = p_high > llast[1]

        new_pivot = False
        # 1) 같은 방향 연장 -> 직전 피봇 교체
        if pivots and ((p_dir == 1 and p_high_bar0) or (p_dir == -1 and p_low_bar0)):
            value = p_high if p_dir == 1 else p_low
            if value * p_dir >= last[1] * p_dir:
                pivots.pop(0)
                pivots.insert(0, [i, value, p_dir])
                new_pivot = True

        # 2) 반대 방향 -> 새 피봇 추가
        opposite = (p_dir == 1 and p_low_bar0) or (p_dir == -1 and p_high_bar0)
        if opposite and (not new_pivot or force_double):
            value = p_low if p_dir == 1 else p_high
            pivots.insert(0, [i, value, -p_dir])

        if len(pivots) > depth:
            pivots = pivots[:depth]

    out = [(int(p[0]), float(p[1]), "H" if p[2] > 0 else "L") for p in pivots]
    out.reverse()                      # 과거 -> 최신
    return out


_PATTERN_NAMES = {
    1: "상승채널", 2: "하락채널", 3: "횡보채널",
    4: "상승쐐기확장", 5: "하락쐐기확장", 6: "발산삼각형",
    7: "상승삼각형확장", 8: "하락삼각형확장",
    9: "상승쐐기수축", 10: "하락쐐기수축", 11: "수렴삼각형",
    12: "하락삼각형수축", 13: "상승삼각형수축",
}


def resolve_pattern_type(t1p1, t1p2, t2p1, t2p2, i1, i2, flat_ratio=FLAT_RATIO):
    """basechartpatterns.resolvePatternName 이식. 반환 patternType (0=무효).

    t1 = 상단 추세선, t2 = 하단 추세선. p1 = 시작, p2 = 끝.
    """
    # 상/하단선의 방향을 '각도비'로 판정 (원본과 동일한 비율식)
    if t1p1 > t2p1:
        base = min(t2p1, t2p2)
        upper_angle = (t1p2 - base) / (t1p1 - base) if (t1p1 - base) else 1.0
        base2 = max(t1p1, t1p2)
        lower_angle = (t2p2 - base2) / (t2p1 - base2) if (t2p1 - base2) else 1.0
    else:
        base = min(t1p1, t1p2)
        upper_angle = (t2p2 - base) / (t2p1 - base) if (t2p1 - base) else 1.0
        base2 = max(t2p1, t2p2)
        lower_angle = (t1p2 - base2) / (t1p1 - base2) if (t1p1 - base2) else 1.0

    hi = 1 + flat_ratio
    lo = 1 - flat_ratio
    upper_dir = 1 if upper_angle > hi else (-1 if upper_angle < lo else 0)
    # 원본에서 하단선은 부호가 반전되어 있다
    lower_dir = -1 if lower_angle > hi else (1 if lower_angle < lo else 0)

    start_diff = abs(t1p1 - t2p1)
    end_diff = abs(t1p2 - t2p2)
    min_diff = min(start_diff, end_diff)
    bar_diff = i2 - i1
    if bar_diff <= 0:
        return 0, upper_dir, lower_dir
    price_diff = abs(start_diff - end_diff) / bar_diff

    # 두 선이 만나기까지 걸릴 봉 수. 패턴 폭의 2배보다 크면 사실상 평행 -> 채널
    conv_bars = (min_diff / price_diff) if price_diff else float("inf")

    expanding = end_diff > start_diff
    contracting = end_diff < start_diff
    is_channel = (conv_bars > 2 * bar_diff
                  or (not expanding and not contracting)
                  or (upper_dir == 0 and lower_dir == 0))
    invalid = np.sign(t1p1 - t2p1) != np.sign(t1p2 - t2p2)   # 선이 교차 -> 무효

    if invalid:
        t = 0
    elif is_channel:
        if upper_dir > 0 and lower_dir > 0:
            t = 1
        elif upper_dir < 0 and lower_dir < 0:
            t = 2
        else:
            t = 3
    elif expanding:
        if upper_dir > 0 and lower_dir > 0:
            t = 4
        elif upper_dir < 0 and lower_dir < 0:
            t = 5
        elif upper_dir > 0 and lower_dir < 0:
            t = 6
        elif upper_dir > 0 and lower_dir == 0:
            t = 7
        elif upper_dir == 0 and lower_dir < 0:
            t = 8
        else:
            t = 0
    elif contracting:
        if upper_dir > 0 and lower_dir > 0:
            t = 9
        elif upper_dir < 0 and lower_dir < 0:
            t = 10
        elif upper_dir < 0 and lower_dir > 0:
            t = 11
        elif lower_dir == 0:
            t = 12 if upper_dir < 0 else 1
        elif upper_dir == 0:
            t = 13 if lower_dir > 0 else 2
        else:
            t = 0
    else:
        t = 0
    return t, upper_dir, lower_dir


def detect_patterns_trendoscope(pivots, close, num_pivots=5,
                                error_threshold=ERROR_THRESHOLD,
                                flat_ratio=FLAT_RATIO, avoid_overlap=True):
    """Trendoscope ACP 방식 패턴 감지 (5 또는 6 피봇 슬라이딩 윈도우)."""
    close = np.asarray(close, dtype=float)
    patterns = []
    if len(pivots) < num_pivots or len(close) == 0:
        return patterns

    for s in range(0, len(pivots) - num_pivots + 1):
        win = pivots[s:s + num_pivots]
        highs = [(i, p) for (i, p, t) in win if t == "H"]
        lows = [(i, p) for (i, p, t) in win if t == "L"]
        if len(highs) < 2 or len(lows) < 2:
            continue

        hb, ha = _fit_line([x[0] for x in highs], [x[1] for x in highs])
        lb, la = _fit_line([x[0] for x in lows], [x[1] for x in lows])
        i1, i2 = win[0][0], win[-1][0]
        if i2 <= i1:
            continue

        t1p1, t1p2 = ha + hb * i1, ha + hb * i2      # 상단선
        t2p1, t2p2 = la + lb * i1, la + lb * i2      # 하단선

        # 검증: 각 피봇이 자기 추세선에서 errorThreshold 이내인가
        span = max(abs(t1p1 - t2p1), abs(t1p2 - t2p2))
        if span <= 0:
            continue
        tol = span * error_threshold
        if any(abs(p - (ha + hb * i)) > tol for (i, p) in highs):
            continue
        if any(abs(p - (la + lb * i)) > tol for (i, p) in lows):
            continue

        ptype, ud, ld = resolve_pattern_type(t1p1, t1p2, t2p1, t2p2, i1, i2, flat_ratio)
        if ptype <= 0:
            continue

        patterns.append({
            "name": _PATTERN_NAMES.get(ptype, str(ptype)),
            "type_id": ptype,
            "start": int(i1), "end": int(i2),
            "upper": {"x1": int(i1), "y1": float(t1p1), "x2": int(i2), "y2": float(t1p2)},
            "lower": {"x1": int(i1), "y1": float(t2p1), "x2": int(i2), "y2": float(t2p2)},
        })

    if not avoid_overlap:
        return patterns
    kept, last_end = [], -1
    for p in sorted(patterns, key=lambda z: z["start"]):
        if p["start"] > last_end:
            kept.append(p)
            last_end = p["end"]
    return kept


def zigzag(high, low, dev=0.045):
    """퍼센트 지그재그. dev 이상 역행 시 직전 극값을 피봇 확정.

    반환: [(index, price, 'H'|'L'), ...]
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    L = len(high)
    pivots = []
    if L < 2:
        return pivots
    trend = 0  # 1=상승, -1=하락, 0=미정
    hi_idx, hi_val = 0, high[0]
    lo_idx, lo_val = 0, low[0]
    for i in range(1, L):
        if trend == 1:
            if high[i] >= hi_val:
                hi_val, hi_idx = high[i], i
            elif low[i] <= hi_val * (1 - dev):          # 상승추세 중 직전고점 대비 dev 하락 -> 고점확정·하락전환
                pivots.append((hi_idx, hi_val, "H"))
                trend = -1
                lo_val, lo_idx = low[i], i
        elif trend == -1:
            if low[i] <= lo_val:
                lo_val, lo_idx = low[i], i
            elif high[i] >= lo_val * (1 + dev):          # 대칭
                pivots.append((lo_idx, lo_val, "L"))
                trend = 1
                hi_val, hi_idx = high[i], i
        else:  # 초기 방향 확정
            if high[i] >= hi_val:
                hi_val, hi_idx = high[i], i
            if low[i] <= lo_val:
                lo_val, lo_idx = low[i], i
            if low[i] <= hi_val * (1 - dev):
                pivots.append((hi_idx, hi_val, "H"))
                trend = -1
                lo_val, lo_idx = low[i], i
            elif high[i] >= lo_val * (1 + dev):
                pivots.append((lo_idx, lo_val, "L"))
                trend = 1
                hi_val, hi_idx = high[i], i
    return pivots


def _fit_line(xs, ys):
    """최소자승 직선 y = a + b*x. 반환 (slope b, intercept a)."""
    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)
    xm = xs.mean()
    ym = ys.mean()
    denom = ((xs - xm) ** 2).sum()
    if denom == 0:
        return 0.0, ym
    b = ((xs - xm) * (ys - ym)).sum() / denom
    a = ym - b * xm
    return b, a


def _classify(upper_dir, lower_dir, rel):
    """(상단방향, 하단방향, 관계) -> 패턴명 또는 None (지시서 §4d-4 룩업)."""
    if upper_dir == "flat" and lower_dir == "flat":
        return "횡보채널"
    table = {
        ("up", "up", "parallel"): "상승채널",
        ("down", "down", "parallel"): "하락채널",
        ("up", "up", "converging"): "상승쐐기수축",
        ("up", "up", "diverging"): "상승쐐기확장",
        ("down", "down", "converging"): "하락쐐기수축",
        ("down", "down", "diverging"): "하락쐐기확장",
        ("up", "flat", "diverging"): "상승삼각형확장",
        ("flat", "up", "converging"): "상승삼각형수축",
        ("flat", "down", "diverging"): "하락삼각형확장",
        ("down", "flat", "converging"): "하락삼각형수축",
        ("down", "up", "converging"): "수렴삼각형",
        ("up", "down", "diverging"): "발산삼각형",
    }
    return table.get((upper_dir, lower_dir, rel))


# 패턴 검증 허용오차(=평균가*5%)의 "평균가" 기준.
#   "window" : 해당 5피봇 구간의 평균 종가 (기본값)
#   "global" : 전체 기간 평균 종가 (지시서 문구의 축자적 해석)
# ⚠️ 지시서 §4d-3은 "평균가*5%"라고만 해서 범위가 모호하다. 장기 시계열에서
#    가격이 수 배 변하면 global 기준은 초기 구간에 지나치게 관대해지고 후반
#    구간엔 과도하게 엄격해진다(실측: 삼성전자 280봉에서 6만→39만원).
#    스케일 불변인 window 를 기본값으로 두되, 레퍼런스 구현 확인 후 조정 가능하게 둔다.
PATTERN_TOLERANCE_SCOPE = "window"


def detect_patterns(pivots, close):
    """지그재그 피봇 5개 슬라이딩 윈도우로 추세선 패턴 감지 (지시서 §4d)."""
    close = np.asarray(close, dtype=float)
    patterns = []
    if len(pivots) < 5 or len(close) == 0:
        return patterns
    global_mean = float(np.mean(close))

    for s in range(0, len(pivots) - 4):
        window = pivots[s:s + 5]
        highs = [(idx, pr) for (idx, pr, t) in window if t == "H"]
        lows = [(idx, pr) for (idx, pr, t) in window if t == "L"]
        if len(highs) < 2 or len(lows) < 2:
            continue
        hb, ha = _fit_line([p[0] for p in highs], [p[1] for p in highs])
        lb, la = _fit_line([p[0] for p in lows], [p[1] for p in lows])
        x_start = window[0][0]
        x_end = window[-1][0]
        span = x_end - x_start
        if span <= 0:
            continue

        # 허용오차/방향판정 기준가 (PATTERN_TOLERANCE_SCOPE 참고)
        if PATTERN_TOLERANCE_SCOPE == "window":
            seg = close[max(0, x_start):x_end + 1]
            mean_price = float(np.mean(seg)) if len(seg) else global_mean
        else:
            mean_price = global_mean
        tol = mean_price * 0.05

        # 검증 1: 각 피봇의 선편차 < 평균가*5%
        ok = all(abs(pr - (ha + hb * idx)) < tol for (idx, pr) in highs) and \
             all(abs(pr - (la + lb * idx)) < tol for (idx, pr) in lows)
        if not ok:
            continue

        # ⚠️ 알려진 한계: 윈도우 내 동일 타입 피봇이 2개뿐이면 최소자승 적합이
        #    그 2점을 정확히 통과하므로 검증1을 무조건 통과한다. 그 선을 윈도우
        #    시작/끝(반대 타입 피봇일 수 있음)까지 외삽하면 실제 가격대를 크게
        #    벗어난 추세선이 나올 수 있다(실측: 280봉 중 1건, 최대 2.6배).
        #    레퍼런스 구현이 추세선을 피봇 구간으로만 클리핑하는지 확인 필요.
        # 검증 2: 시작/끝 갭 둘 다 > 0
        up_s, lo_s = ha + hb * x_start, la + lb * x_start
        up_e, lo_e = ha + hb * x_end, la + lb * x_end
        start_gap = up_s - lo_s
        end_gap = up_e - lo_e
        if start_gap <= 0 or end_gap <= 0:
            continue

        # 방향 분류 (span 정규화 slope < 3% -> 평행)
        def _dir(slope):
            norm = slope * span / mean_price
            if abs(norm) < 0.03:
                return "flat"
            return "up" if norm > 0 else "down"

        ud, ld = _dir(hb), _dir(lb)

        # 관계 분류
        if end_gap < start_gap * 0.75:
            rel = "converging"
        elif end_gap > start_gap * 1.25:
            rel = "diverging"
        else:
            rel = "parallel"

        name = _classify(ud, ld, rel)
        if name is None:
            continue

        patterns.append({
            "name": name,
            "start": int(x_start),
            "end": int(x_end),
            "upper": {"x1": int(x_start), "y1": float(up_s),
                      "x2": int(x_end), "y2": float(up_e)},
            "lower": {"x1": int(x_start), "y1": float(lo_s),
                      "x2": int(x_end), "y2": float(lo_e)},
        })

    # 겹치는 패턴은 나중 것 무시 (start > 직전 end 인 것만 유지)
    kept = []
    last_end = -1
    for p in sorted(patterns, key=lambda z: z["start"]):
        if p["start"] > last_end:
            kept.append(p)
            last_end = p["end"]
    return kept


# ---------------------------------------------------------------------------
# (e) 이치모쿠 일목균형표 (9, 26, 52, 26)
# ---------------------------------------------------------------------------
def _donchian_mid(high, low, n):
    """(n기간 최고가 + n기간 최저가) / 2."""
    L = len(high)
    out = np.full(L, np.nan)
    for i in range(n - 1, L):
        out[i] = (np.max(high[i - n + 1:i + 1]) + np.min(low[i - n + 1:i + 1])) / 2.0
    return out


def ichimoku(high, low, close, tenkan_p=9, kijun_p=26, senkou_b_p=52, disp=26):
    """일목균형표.

    전환선/기준선은 현재 봉 기준, 선행스팬A/B는 disp 만큼 앞으로 이동해
    그린다(=현재 봉에는 disp 이전 값이 표시된다). 후행스팬은 disp 뒤로 이동.
    선행스팬이 미래로 뻗는 disp 구간은 future_* 로 따로 준다.
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    L = len(close)

    tenkan = _donchian_mid(high, low, tenkan_p)
    kijun = _donchian_mid(high, low, kijun_p)
    span_a_raw = (tenkan + kijun) / 2.0                       # 미이동 원본
    span_b_raw = _donchian_mid(high, low, senkou_b_p)         # 미이동 원본

    # 앞으로 disp 이동: 현재 봉 i 에는 i-disp 의 원본값
    span_a = np.full(L, np.nan)
    span_b = np.full(L, np.nan)
    span_a[disp:] = span_a_raw[:L - disp]
    span_b[disp:] = span_b_raw[:L - disp]

    # 후행스팬: 현재 봉 i 에는 i+disp 의 종가
    chikou = np.full(L, np.nan)
    chikou[:L - disp] = close[disp:]

    # 미래로 뻗는 구름 (마지막 disp개 원본값)
    future_a = span_a_raw[L - disp:] if L > disp else np.array([])
    future_b = span_b_raw[L - disp:] if L > disp else np.array([])

    return {
        "tenkan": tenkan, "kijun": kijun,
        "span_a": span_a, "span_b": span_b, "chikou": chikou,
        "future_span_a": future_a, "future_span_b": future_b,
    }


# ---------------------------------------------------------------------------
# (f) Minervini 추세 템플릿 (52주=260봉 기준)
# ---------------------------------------------------------------------------
def minervini_trend_template(close, lookback=260, benchmark_close=None):
    """Minervini SEPA 추세 템플릿 8조건.

    1) 종가 > MA150 and > MA200
    2) MA150 > MA200
    3) MA200 이 최소 1개월(22봉) 상승 중
    4) MA50 > MA150 and > MA200
    5) 종가 > MA50
    6) 종가가 52주 저가 대비 +30% 이상
    7) 종가가 52주 고가의 -25% 이내
    8) RS(상대강도) 등급 >= 70  — 벤치마크 필요. 없으면 None(판정 제외).

    반환: 봉별 통과여부(list[bool|None]) + 최신봉 조건별 상세.
    """
    close = np.asarray(close, dtype=float)
    L = len(close)
    ma50, ma150, ma200 = sma(close, 50), sma(close, 150), sma(close, 200)

    rs_line = None
    if benchmark_close is not None:
        bm = np.asarray(benchmark_close, dtype=float)
        if len(bm) == L:
            with np.errstate(invalid="ignore", divide="ignore"):
                rs_line = close / bm

    passed = [None] * L
    detail_last = None

    for i in range(L):
        if np.isnan(ma200[i]) or np.isnan(ma150[i]) or np.isnan(ma50[i]):
            continue
        lo_i = max(0, i - lookback + 1)
        w = close[lo_i:i + 1]
        hi52, lo52 = float(np.max(w)), float(np.min(w))

        c1 = bool(close[i] > ma150[i] and close[i] > ma200[i])
        c2 = bool(ma150[i] > ma200[i])
        c3 = bool(i >= 22 and not np.isnan(ma200[i - 22]) and ma200[i] > ma200[i - 22])
        c4 = bool(ma50[i] > ma150[i] and ma50[i] > ma200[i])
        c5 = bool(close[i] > ma50[i])
        c6 = bool(lo52 > 0 and close[i] >= lo52 * 1.30)
        c7 = bool(hi52 > 0 and close[i] >= hi52 * 0.75)

        checks = [c1, c2, c3, c4, c5, c6, c7]

        c8 = None
        if rs_line is not None and i >= lookback - 1:
            # RS 등급 근사: 최근 lookback 구간 RS라인 백분위
            seg = rs_line[lo_i:i + 1]
            seg = seg[~np.isnan(seg)]
            if len(seg) > 10:
                pct = float((seg <= rs_line[i]).sum() / len(seg) * 100.0)
                c8 = bool(pct >= 70.0)
                checks.append(c8)

        passed[i] = all(checks)

        if i == L - 1:
            detail_last = {
                "c1_above_ma150_ma200": c1,
                "c2_ma150_above_ma200": c2,
                "c3_ma200_rising_1m": c3,
                "c4_ma50_above_ma150_ma200": c4,
                "c5_close_above_ma50": c5,
                "c6_above_52w_low_30pct": c6,
                "c7_within_25pct_of_52w_high": c7,
                "c8_rs_rating_70": c8,
                "pass": bool(all(checks)),
                "pct_above_52w_low": float((close[i] / lo52 - 1) * 100) if lo52 else None,
                "pct_below_52w_high": float((close[i] / hi52 - 1) * 100) if hi52 else None,
            }

    return {"passed": passed, "latest": detail_last}


# ---------------------------------------------------------------------------
# (g) 지지/저항 레벨 + 돌파 감지 (pivot 15/15, 거래량 필터)
# ---------------------------------------------------------------------------
def pivot_lows(series, left=4, right=4):
    series = np.asarray(series, dtype=float)
    idx = []
    for i in range(left, len(series) - right):
        c = series[i]
        if all(c < series[i - j] for j in range(1, left + 1)) and \
           all(c < series[i + j] for j in range(1, right + 1)):
            idx.append(i)
    return idx


def support_resistance_breaks(open_, high, low, close, volume,
                              left=15, right=15, vol_thresh=20.0):
    """피봇 기반 S/R 레벨 + 돌파 감지.

    "Support and Resistance Levels with Breaks [LuxAlgo]" (TradingView 공개
    오픈소스 스크립트)의 공개된 로직을 파이썬으로 재구현한 것이다.
    원저작자: LuxAlgo / 원본: tradingview.com/script/JDFoWQbL-
    기본값 left=15, right=15, volumeThresh=20 도 원본과 동일하게 맞춘다.

    원본 로직:
      highUsePivot = fixnan(pivothigh(left, right)[1])   # 최근 확정 피봇하이 유지
      lowUsePivot  = fixnan(pivotlow(left, right)[1])
      osc  = 100 * (ema(vol,5) - ema(vol,10)) / ema(vol,10)
      bull = crossover(close, highUsePivot)
             and not (open - low > close - open)         # 아래꼬리가 몸통보다 크면 제외
             and osc > volumeThresh
      bear = crossunder(close, lowUsePivot)
             and not (open - close < high - open)        # 위꼬리가 몸통보다 크면 제외
             and osc > volumeThresh
    """
    open_ = np.asarray(open_, dtype=float)
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    volume = np.asarray(volume, dtype=float)
    L = len(close)

    ph = pivot_highs(high, left, right)
    pl = pivot_lows(low, left, right)

    levels = [{"index": int(i), "price": float(high[i]), "type": "resistance"} for i in ph]
    levels += [{"index": int(i), "price": float(low[i]), "type": "support"} for i in pl]
    levels.sort(key=lambda x: x["index"])

    # fixnan(pivot[1]): 피봇 p 는 p+right 에 확정, [1] 오프셋으로 p+right+1 부터 사용
    hi_use = np.full(L, np.nan)
    lo_use = np.full(L, np.nan)
    for p in ph:
        avail = p + right + 1
        if avail < L:
            hi_use[avail:] = high[p]
    for p in pl:
        avail = p + right + 1
        if avail < L:
            lo_use[avail:] = low[p]

    e5, e10 = ema(volume, 5), ema(volume, 10)
    with np.errstate(invalid="ignore", divide="ignore"):
        vol_osc = 100.0 * (e5 - e10) / e10

    breaks = []
    for i in range(1, L):
        vo = vol_osc[i]
        if np.isnan(vo) or not (vo > vol_thresh):
            continue

        # 상향돌파: crossover(close, highUsePivot) + 아래꼬리 필터
        if not np.isnan(hi_use[i]) and not np.isnan(hi_use[i - 1]):
            if close[i] > hi_use[i] and close[i - 1] <= hi_use[i - 1]:
                if not (open_[i] - low[i] > close[i] - open_[i]):
                    breaks.append({"index": int(i), "price": float(hi_use[i]),
                                   "direction": "up", "level_type": "resistance",
                                   "vol_osc": float(vo)})

        # 하향돌파: crossunder(close, lowUsePivot) + 위꼬리 필터
        if not np.isnan(lo_use[i]) and not np.isnan(lo_use[i - 1]):
            if close[i] < lo_use[i] and close[i - 1] >= lo_use[i - 1]:
                if not (open_[i] - close[i] < high[i] - open_[i]):
                    breaks.append({"index": int(i), "price": float(lo_use[i]),
                                   "direction": "down", "level_type": "support",
                                   "vol_osc": float(vo)})

    return {"levels": levels, "breaks": breaks,
            "hi_use": hi_use, "lo_use": lo_use}


# ---------------------------------------------------------------------------
# (h) 오더블록 (거래량 표기)
# ---------------------------------------------------------------------------
def order_blocks(open_, high, low, close, volume, swing=5, move_bars=3, min_move=0.02):
    """표준 오더블록.

    상승 OB: 강한 상승 임펄스 직전의 마지막 음봉 (해당 봉의 고저 구간이 존)
    하락 OB: 강한 하락 임펄스 직전의 마지막 양봉
    임펄스 = move_bars 봉 동안 min_move 이상 변동 + 직전 swing 고/저 돌파
    """
    open_ = np.asarray(open_, dtype=float)
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    volume = np.asarray(volume, dtype=float)
    L = len(close)
    blocks = []

    for i in range(swing + 1, L - move_bars):
        fwd = close[i + move_bars]
        if close[i] <= 0:
            continue
        chg = (fwd - close[i]) / close[i]

        if chg >= min_move and high[i + move_bars] > np.max(high[i - swing:i + 1]):
            j = i
            while j > 0 and close[j] >= open_[j]:      # 직전 마지막 음봉
                j -= 1
            if close[j] < open_[j]:
                blocks.append({"index": int(j), "type": "bullish",
                               "top": float(high[j]), "bottom": float(low[j]),
                               "volume": float(volume[j])})
        elif chg <= -min_move and low[i + move_bars] < np.min(low[i - swing:i + 1]):
            j = i
            while j > 0 and close[j] <= open_[j]:      # 직전 마지막 양봉
                j -= 1
            if close[j] > open_[j]:
                blocks.append({"index": int(j), "type": "bearish",
                               "top": float(high[j]), "bottom": float(low[j]),
                               "volume": float(volume[j])})

    # 같은 봉 중복 제거 (최근 것 우선)
    seen, uniq = set(), []
    for b in sorted(blocks, key=lambda x: x["index"], reverse=True):
        if b["index"] in seen:
            continue
        seen.add(b["index"])
        uniq.append(b)
    return list(reversed(uniq))


# ---------------------------------------------------------------------------
# 통합 계산
# ---------------------------------------------------------------------------
def timeframe_signals(high, low, close, volume=None):
    """단일 타임프레임 요약 신호. 멀티타임프레임 패널용.

    반환: dict(close, chg_pct, ma20, above_ma20, rsi, rsi_state,
              squeeze_val, squeeze_dir, verdict, score)
    """
    close = np.asarray(close, dtype=float)
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    n = len(close)
    if n < 2:
        return {"available": False, "reason": "봉 부족"}

    last = float(close[-1])
    prev = float(close[-2])
    chg = (last / prev - 1.0) * 100.0 if prev else None

    ma20 = sma(close, 20)
    ma20_last = float(ma20[-1]) if n >= 20 and not np.isnan(ma20[-1]) else None
    above = bool(last > ma20_last) if ma20_last is not None else None

    r = rsi_wilder(close, 14)
    rsi_last = float(r[-1]) if n > 14 and not np.isnan(r[-1]) else None
    if rsi_last is None:
        rsi_state = None
    elif rsi_last >= 70:
        rsi_state = "과매수"
    elif rsi_last <= 30:
        rsi_state = "과매도"
    else:
        rsi_state = "중립"

    sq_val = sq_dir = None
    if n >= 40:
        sv = squeeze_momentum(high, low, close, 20)
        if not np.isnan(sv[-1]):
            sq_val = float(sv[-1])
            prev_v = sv[-2] if not np.isnan(sv[-2]) else sv[-1]
            sq_dir = "상승" if sv[-1] > prev_v else "하락"

    # 종합 점수: MA / RSI / 스퀴즈 각각 ±1
    score = 0
    if above is True:
        score += 1
    elif above is False:
        score -= 1
    if rsi_last is not None:
        if rsi_last >= 55:
            score += 1
        elif rsi_last <= 45:
            score -= 1
    if sq_val is not None:
        score += 1 if sq_val >= 0 else -1

    verdict = "강세" if score >= 2 else ("약세" if score <= -2 else "중립")

    return {
        "available": True,
        "close": float(last),
        "chg_pct": float(chg) if chg is not None else None,
        "ma20": ma20_last,
        "above_ma20": above,
        "rsi": rsi_last,
        "rsi_state": rsi_state,
        "squeeze_val": sq_val,
        "squeeze_dir": sq_dir,
        "score": int(score),
        "verdict": verdict,
        "bars": int(n),
    }


def compute_all(dates, open_, high, low, close, volume, sr_vol_thresh=20.0,
                benchmark_close=None):
    """OHLCV -> 전체 지표 dict (JSON 직렬화 가능).

    benchmark_close 를 주면 Minervini 8번 조건(RS 등급)까지 판정한다.
    """
    open_ = np.asarray(open_, dtype=float)
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    volume = np.asarray(volume, dtype=float)

    out = {"dates": list(dates)}

    for p in (5, 10, 20, 50, 120, 150, 200):
        out[f"sma{p}"] = _nan_to_none(sma(close, p))
    out["ema50"] = _nan_to_none(ema(close, 50))
    out["ema200"] = _nan_to_none(ema(close, 200))

    mid, up, lo = bollinger(close, 20, 2.0)
    out["bb_mid"] = _nan_to_none(mid)
    out["bb_upper"] = _nan_to_none(up)
    out["bb_lower"] = _nan_to_none(lo)

    rsi = rsi_wilder(close, 14)
    out["rsi"] = _nan_to_none(rsi)
    out["disparity50"] = _nan_to_none(disparity(close, 50))
    out["hma60"] = _nan_to_none(hma(close, 60))

    sqv = squeeze_momentum(high, low, close, 20)
    out["squeeze"] = {"val": _nan_to_none(sqv), "color": squeeze_colors(sqv)}

    out["rsi_bear_div"] = rsi_bear_divergence(close, rsi)

    # 패턴/지그재그는 Trendoscope 원본 알고리즘 사용 (퍼센트 편차 방식은 과검출)
    pivots = zigzag_pivots(high, low, ZZ_LENGTH, ZZ_DEPTH)
    out["zigzag"] = [{"index": int(i), "price": float(pr), "type": t}
                     for (i, pr, t) in pivots]
    out["patterns"] = detect_patterns_trendoscope(pivots, close)

    # --- 이치모쿠 (9,26,52,26) ---
    ich = ichimoku(high, low, close)
    out["ichimoku"] = {
        "tenkan": _nan_to_none(ich["tenkan"]),
        "kijun": _nan_to_none(ich["kijun"]),
        "span_a": _nan_to_none(ich["span_a"]),
        "span_b": _nan_to_none(ich["span_b"]),
        "chikou": _nan_to_none(ich["chikou"]),
        "future_span_a": _nan_to_none(ich["future_span_a"]),
        "future_span_b": _nan_to_none(ich["future_span_b"]),
    }

    # --- Minervini 추세 템플릿 (52주=260봉) ---
    out["minervini"] = minervini_trend_template(close, lookback=260,
                                                benchmark_close=benchmark_close)

    # --- S/R 레벨 + 돌파 (LuxAlgo 원본 이식) ---
    lux = sr_breaks_luxalgo(open_, high, low, close, volume,
                            left_bars=15, right_bars=15, vol_thresh=sr_vol_thresh)
    out["sr"] = {
        "resistance": lux["resistance"],
        "support": lux["support"],
        "breaks": lux["breaks"],
        "vol_osc": lux["vol_osc"],
    }

    # --- 오더블록 (Flux 원본 이식) ---
    out["order_blocks"] = order_blocks_flux(high, low, close, volume)

    # --- AlgoAlpha 스퀴즈 (LazyBear 판과 별개 지표) ---
    out["squeeze_aa"] = squeeze_algoalpha(high, low, close)

    return out


# ---------------------------------------------------------------------------
# 스모크 테스트
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json

    # 재현 가능한 더미 OHLCV (사인파 + 추세 + 노이즈)
    rng = np.random.default_rng(42)
    N = 300
    t = np.arange(N)
    base = 50000 + 8000 * np.sin(t / 25.0) + 30 * t
    noise = rng.normal(0, 400, N).cumsum() * 0.3
    close = base + noise
    high = close + np.abs(rng.normal(0, 300, N))
    low = close - np.abs(rng.normal(0, 300, N))
    open_ = close + rng.normal(0, 200, N)
    volume = rng.integers(1_000_000, 5_000_000, N).astype(float)
    dates = [f"2024-{1 + (i // 21) % 12:02d}-{1 + i % 28:02d}" for i in range(N)]

    res = compute_all(dates, open_, high, low, close, volume)

    # JSON 직렬화 검증
    payload = json.dumps(res)
    print(f"[OK] JSON 직렬화 성공: {len(payload):,} bytes")

    # 키/길이 점검
    print(f"[OK] keys: {sorted(res.keys())}")
    for key in ("sma20", "ema50", "bb_upper", "rsi", "disparity50", "hma60"):
        arr = res[key]
        n_valid = sum(1 for v in arr if v is not None)
        print(f"     {key:12s} len={len(arr)} valid={n_valid} last={arr[-1]}")

    sq = res["squeeze"]
    n_sq = sum(1 for v in sq["val"] if v is not None)
    print(f"[OK] squeeze: valid={n_sq}, last_val={sq['val'][-1]}, last_color={sq['color'][-1]}")
    print(f"[OK] rsi_bear_div marks: {res['rsi_bear_div']}")
    print(f"[OK] zigzag pivots: {len(res['zigzag'])}  (첫 5개: {res['zigzag'][:5]})")
    print(f"[OK] patterns 감지: {len(res['patterns'])}")
    for p in res["patterns"]:
        print(f"     - {p['name']}  bars[{p['start']}..{p['end']}]")

    # 기본 정합성 assert
    assert len(res["sma20"]) == N
    assert res["sma20"][:19] == [None] * 19, "SMA20 앞 19개는 None이어야"
    assert res["rsi"][14] is not None, "RSI는 index14부터 값"
    assert all(v is None or -1e9 < v < 1e9 for v in res["rsi"]), "RSI 범위"
    print("\n[PASS] chart_indicators 스모크 테스트 통과")
