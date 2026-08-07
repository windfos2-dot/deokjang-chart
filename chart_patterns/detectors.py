# -*- coding: utf-8 -*-
"""
Bulkowski 식별규칙(Table x.1)을 코드로 옮긴 패턴 탐지기.

설계 원칙
  1) 스크리너용이므로 '지금 진행 중이거나 최근 N봉 안에 돌파한' 패턴만 보고한다.
     (과거 차트 전체를 라벨링하는 용도가 아님)
  2) 탐지 판정은 책의 규칙을, 임계값은 책이 명시한 수치를 우선 사용하고
     명시가 없으면 보수적으로 잡는다. 임계값은 전부 상단 상수로 노출.
  3) 각 히트는 measure rule 목표가·패턴 높이·거래량 추세를 함께 계산한다.

용어: H=minor high, L=minor low, ctx.piv 는 H/L 교대 정렬된 피벗열.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .pivots import (Line, Pivot, atr, breakout_down, breakout_up, curvature,
                     drift, fib_near, find_pivots, fitline, rounded_r2,
                     shape_class, slope_pct, touches, trend_before, zigzag)

# ── 공통 임계값 ──────────────────────────────────────────────────────────
FLAT = 0.030          # 추세선이 '수평'으로 간주되는 구간 총 변화율
TILT = 0.040          # '기울어짐'으로 간주되는 최소 구간 변화율
CONVERGE = 1.30       # 수렴/확산 판정: 시작폭/끝폭 배율
MAX_DUR = 189         # 대부분 패턴의 최대 지속(≈9개월)
TOL_ATR = 0.35        # 추세선 터치 허용오차(ATR 배수) — 작을수록 엄격

WINDOWS = (25, 35, 45, 60, 80, 105, 135)   # 추세선 패턴 탐색 윈도우


@dataclass
class Hit:
    pid: str
    direction: str                    # 'up' | 'down'
    start: int
    end: int                          # 패턴 종료(마지막 피벗)
    status: str                       # 'forming' | 'breakout'
    breakout_i: Optional[int] = None
    breakout_px: Optional[float] = None
    height_pct: float = 0.0
    target: Optional[float] = None
    quality: float = 0.5              # 0~1 형태 적합도
    points: List[Tuple[int, float]] = field(default_factory=list)
    notes: Dict[str, object] = field(default_factory=dict)


@dataclass
class Ctx:
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    v: np.ndarray
    recent: int = 12                  # '최근'으로 인정할 봉 수
    scale: str = "D"                  # D=일봉, W=주봉

    def __post_init__(self):
        self.n = len(self.c)
        self.atr = atr(self.h, self.l, self.c)
        self.piv = zigzag(self.h, self.l, sep=5 if self.scale == "D" else 3)
        self.raw_piv = find_pivots(self.h, self.l, 5 if self.scale == "D" else 3)

    def tol(self, i: int) -> float:
        return max(TOL_ATR * float(self.atr[i]), 0.004 * float(self.c[i]))

    def is_recent(self, i: Optional[int]) -> bool:
        return i is not None and (self.n - 1 - i) <= self.recent

    def vol_trend(self, a: int, b: int) -> float:
        return slope_pct(self.v[a:b + 1]) if b > a + 2 else 0.0


Detector = Callable[[Ctx], Iterable[Hit]]
REGISTRY: Dict[str, Detector] = {}
SCALE: Dict[str, str] = {}            # pid → 'D' | 'W'


def register(*pids: str, scale: str = "D"):
    def deco(fn: Detector):
        for p in pids:
            REGISTRY[p] = fn
            SCALE[p] = scale
        return fn
    return deco


# ── 공통 헬퍼 ────────────────────────────────────────────────────────────
def _height_pct(hi: float, lo: float, ref: float) -> float:
    return (hi - lo) / ref * 100.0 if ref else 0.0


def _finish(ctx: Ctx, hit: Hit) -> Optional[Hit]:
    """돌파 시점/목표가/상태를 확정. 최근성 필터도 여기서."""
    anchor = hit.breakout_i if hit.breakout_i is not None else hit.end
    if not ctx.is_recent(anchor):
        return None
    if hit.breakout_i is not None:
        hit.status = "breakout"
        hit.breakout_px = float(ctx.c[hit.breakout_i])
    else:
        hit.status = "forming"
    hit.notes.setdefault("vol_trend", round(ctx.vol_trend(hit.start, hit.end), 3))
    hit.notes.setdefault("bars", hit.end - hit.start + 1)
    return hit


def _measure(direction: str, level: float, height: float) -> float:
    return level + height if direction == "up" else level - height


def _window_pivots(ctx: Ctx, a: int, b: int) -> List[Pivot]:
    return [p for p in ctx.piv if a <= p.i <= b]


def _lines(ctx: Ctx, pv: List[Pivot]) -> Optional[Tuple[Line, Line, int, int]]:
    """피벗 고점/저점에 각각 추세선 적합 + 터치 수 반환."""
    hs = [p for p in pv if p.kind == "H"]
    ls = [p for p in pv if p.kind == "L"]
    if len(hs) < 2 or len(ls) < 2:
        return None
    up = fitline([p.i for p in hs], [p.p for p in hs])
    dn = fitline([p.i for p in ls], [p.p for p in ls])
    if up is None or dn is None:
        return None
    tol = ctx.tol(pv[-1].i)
    return up, dn, touches(up, hs, tol), touches(dn, ls, tol)


# ═══════════════════════════════════════════════════════════ 1. 이중바닥/천정
DB_MAX_VAR = 0.05      # 두 바닥 가격 편차 상한
DB_MIN_RISE = 0.08     # 바닥 사이 반등폭 하한 (책 권고 10%, 유연하게)
DB_MIN_SEP = 10        # 최소 간격(봉)
DB_MAX_RISE = 0.60     # 반등폭 상한 — 이보다 크면 이중바닥이 아니라 별개 파동


@register("db_aa", "db_ae", "db_ea", "db_ee")
def detect_double_bottom(ctx: Ctx) -> List[Hit]:
    out = []
    pv = ctx.piv
    for k in range(len(pv) - 2):
        a, m, b = pv[k], pv[k + 1], pv[k + 2]
        if (a.kind, m.kind, b.kind) != ("L", "H", "L"):
            continue
        if not (DB_MIN_SEP <= b.i - a.i <= MAX_DUR):
            continue
        lo = min(a.p, b.p)
        if abs(a.p - b.p) / lo > DB_MAX_VAR:
            continue
        rise = (m.p - lo) / lo
        if not (DB_MIN_RISE <= rise <= DB_MAX_RISE):
            continue
        if trend_before(ctx.c, a.i) > -3:              # 하락 추세 뒤에 나와야 함
            continue
        if float(ctx.l[a.i:b.i + 1].min()) < lo * 0.985:
            continue
        bo = breakout_up(ctx.c, m.p, b.i + 1)
        sa = shape_class(ctx.h, ctx.l, ctx.atr, a.i, "L")
        sb = shape_class(ctx.h, ctx.l, ctx.atr, b.i, "L")
        pid = {"adamadam": "db_aa", "adameve": "db_ae",
               "eveadam": "db_ea", "eveeve": "db_ee"}[sa + sb]
        height = m.p - lo
        hit = Hit(pid, "up", a.i, b.i,
                  status="forming", breakout_i=bo,
                  height_pct=_height_pct(m.p, lo, lo),
                  target=_measure("up", m.p, height),
                  quality=0.5 * (1 - abs(a.p - b.p) / lo / DB_MAX_VAR)
                          + 0.5 * min(1.0, rise / 0.30),
                  points=[(a.i, a.p), (m.i, m.p), (b.i, b.p)],
                  notes={"neckline": round(m.p, 4), "shape": f"{sa}/{sb}"})
        got = _finish(ctx, hit)
        if got:
            out.append(got)
    return out


@register("dt_aa", "dt_ae", "dt_ea", "dt_ee")
def detect_double_top(ctx: Ctx) -> List[Hit]:
    out = []
    pv = ctx.piv
    for k in range(len(pv) - 2):
        a, m, b = pv[k], pv[k + 1], pv[k + 2]
        if (a.kind, m.kind, b.kind) != ("H", "L", "H"):
            continue
        if not (DB_MIN_SEP <= b.i - a.i <= MAX_DUR):
            continue
        hi = max(a.p, b.p)
        if abs(a.p - b.p) / hi > DB_MAX_VAR:
            continue
        drop = (hi - m.p) / hi
        if not (DB_MIN_RISE <= drop <= DB_MAX_RISE):
            continue
        if trend_before(ctx.c, a.i) < 3:               # 상승 추세 뒤에 나와야 함
            continue
        if float(ctx.h[a.i:b.i + 1].max()) > hi * 1.015:
            continue
        bo = breakout_down(ctx.c, m.p, b.i + 1)
        sa = shape_class(ctx.h, ctx.l, ctx.atr, a.i, "H")
        sb = shape_class(ctx.h, ctx.l, ctx.atr, b.i, "H")
        pid = {"adamadam": "dt_aa", "adameve": "dt_ae",
               "eveadam": "dt_ea", "eveeve": "dt_ee"}[sa + sb]
        height = hi - m.p
        hit = Hit(pid, "down", a.i, b.i, status="forming", breakout_i=bo,
                  height_pct=_height_pct(hi, m.p, hi),
                  target=_measure("down", m.p, height),
                  quality=0.5 * (1 - abs(a.p - b.p) / hi / DB_MAX_VAR)
                          + 0.5 * min(1.0, drop / 0.30),
                  points=[(a.i, a.p), (m.i, m.p), (b.i, b.p)],
                  notes={"neckline": round(m.p, 4), "shape": f"{sa}/{sb}"})
        got = _finish(ctx, hit)
        if got:
            out.append(got)
    return out


# ═════════════════════════════════════════════════════ 2. 삼중바닥/천정·3봉
TB_MAX_VAR = 0.04


@register("triple_bottom")
def detect_triple_bottom(ctx: Ctx) -> List[Hit]:
    out = []
    pv = ctx.piv
    for k in range(len(pv) - 4):
        seq = pv[k:k + 5]
        if [p.kind for p in seq] != ["L", "H", "L", "H", "L"]:
            continue
        lows = [seq[0].p, seq[2].p, seq[4].p]
        peaks = [seq[1].p, seq[3].p]
        lo, hi = min(lows), max(peaks)
        if (max(lows) - lo) / lo > TB_MAX_VAR:
            continue
        if not (30 <= seq[4].i - seq[0].i <= MAX_DUR):
            continue
        if min(seq[2].i - seq[0].i, seq[4].i - seq[2].i) < 10:
            continue
        if trend_before(ctx.c, seq[0].i) > -3:
            continue
        bo = breakout_up(ctx.c, hi, seq[4].i + 1)
        hit = Hit("triple_bottom", "up", seq[0].i, seq[4].i, status="forming",
                  breakout_i=bo, height_pct=_height_pct(hi, lo, lo),
                  target=_measure("up", hi, hi - lo),
                  quality=1.0 - (max(lows) - lo) / lo / TB_MAX_VAR * 0.4,
                  points=[(p.i, p.p) for p in seq],
                  notes={"neckline": round(hi, 4)})
        got = _finish(ctx, hit)
        if got:
            out.append(got)
    return out


@register("triple_top")
def detect_triple_top(ctx: Ctx) -> List[Hit]:
    out = []
    pv = ctx.piv
    for k in range(len(pv) - 4):
        seq = pv[k:k + 5]
        if [p.kind for p in seq] != ["H", "L", "H", "L", "H"]:
            continue
        highs = [seq[0].p, seq[2].p, seq[4].p]
        valleys = [seq[1].p, seq[3].p]
        hi, lo = max(highs), min(valleys)
        if (hi - min(highs)) / hi > TB_MAX_VAR:
            continue
        if not (30 <= seq[4].i - seq[0].i <= MAX_DUR):
            continue
        if min(seq[2].i - seq[0].i, seq[4].i - seq[2].i) < 10:
            continue
        if trend_before(ctx.c, seq[0].i) < 3:
            continue
        bo = breakout_down(ctx.c, lo, seq[4].i + 1)
        hit = Hit("triple_top", "down", seq[0].i, seq[4].i, status="forming",
                  breakout_i=bo, height_pct=_height_pct(hi, lo, hi),
                  target=_measure("down", lo, hi - lo),
                  quality=1.0 - (hi - min(highs)) / hi / TB_MAX_VAR * 0.4,
                  points=[(p.i, p.p) for p in seq],
                  notes={"neckline": round(lo, 4)})
        got = _finish(ctx, hit)
        if got:
            out.append(got)
    return out


@register("three_rising_valleys")
def detect_three_rising_valleys(ctx: Ctx) -> List[Hit]:
    out = []
    pv = ctx.piv
    for k in range(len(pv) - 4):
        seq = pv[k:k + 5]
        if [p.kind for p in seq] != ["L", "H", "L", "H", "L"]:
            continue
        v1, v2, v3 = seq[0].p, seq[2].p, seq[4].p
        if not (v1 < v2 < v3):
            continue
        if (v3 - v1) / v1 < 0.08 or (v3 - v1) / v1 > 0.60:
            continue
        if min(seq[2].i - seq[0].i, seq[4].i - seq[2].i) < 12:
            continue
        if seq[3].p <= seq[1].p:                    # 사이 봉우리도 계단식
            continue
        if trend_before(ctx.c, seq[0].i) > 5:
            continue
        if not (40 <= seq[4].i - seq[0].i <= MAX_DUR):
            continue
        hi = max(seq[1].p, seq[3].p)
        bo = breakout_up(ctx.c, hi, seq[4].i + 1)
        hit = Hit("three_rising_valleys", "up", seq[0].i, seq[4].i,
                  status="forming", breakout_i=bo,
                  height_pct=_height_pct(hi, v1, v1),
                  target=_measure("up", hi, hi - v1),
                  quality=0.55 + min(0.45, (v3 - v1) / v1 * 1.5),
                  points=[(p.i, p.p) for p in seq])
        got = _finish(ctx, hit)
        if got:
            out.append(got)
    return out


@register("three_falling_peaks")
def detect_three_falling_peaks(ctx: Ctx) -> List[Hit]:
    out = []
    pv = ctx.piv
    for k in range(len(pv) - 4):
        seq = pv[k:k + 5]
        if [p.kind for p in seq] != ["H", "L", "H", "L", "H"]:
            continue
        p1, p2, p3 = seq[0].p, seq[2].p, seq[4].p
        if not (p1 > p2 > p3):
            continue
        if (p1 - p3) / p1 < 0.08 or (p1 - p3) / p1 > 0.60:
            continue
        if min(seq[2].i - seq[0].i, seq[4].i - seq[2].i) < 12:
            continue
        if seq[3].p >= seq[1].p:
            continue
        if trend_before(ctx.c, seq[0].i) < -5:
            continue
        if not (40 <= seq[4].i - seq[0].i <= MAX_DUR):
            continue
        lo = min(seq[1].p, seq[3].p)
        bo = breakout_down(ctx.c, lo, seq[4].i + 1)
        hit = Hit("three_falling_peaks", "down", seq[0].i, seq[4].i,
                  status="forming", breakout_i=bo,
                  height_pct=_height_pct(p1, lo, p1),
                  target=_measure("down", lo, p1 - lo),
                  quality=0.55 + min(0.45, (p1 - p3) / p1 * 1.5),
                  points=[(p.i, p.p) for p in seq])
        got = _finish(ctx, hit)
        if got:
            out.append(got)
    return out


# ══════════════════════════════════════════════════════ 3. 헤드앤숄더 (+복합)
HS_SHOULDER_VAR = 0.15     # 양 어깨 높이 편차 허용
HS_HEAD_MARGIN = 0.02      # 머리는 어깨보다 최소 2% 높아야


@register("hs_top", "hs_top_complex")
def detect_hs_top(ctx: Ctx) -> List[Hit]:
    return _hs(ctx, top=True)


@register("hs_bottom", "hs_bottom_complex")
def detect_hs_bottom(ctx: Ctx) -> List[Hit]:
    return _hs(ctx, top=False)


def _hs(ctx: Ctx, top: bool) -> List[Hit]:
    out, pv = [], ctx.piv
    want = ["H", "L", "H", "L", "H"] if top else ["L", "H", "L", "H", "L"]
    for k in range(len(pv) - 4):
        seq = pv[k:k + 5]
        if [p.kind for p in seq] != want:
            continue
        ls, t1, head, t2, rs = seq
        if not (25 <= rs.i - ls.i <= MAX_DUR):
            continue
        if top:
            if not (head.p > ls.p * (1 + HS_HEAD_MARGIN)
                    and head.p > rs.p * (1 + HS_HEAD_MARGIN)):
                continue
        else:
            if not (head.p < ls.p * (1 - HS_HEAD_MARGIN)
                    and head.p < rs.p * (1 - HS_HEAD_MARGIN)):
                continue
        if abs(ls.p - rs.p) / max(ls.p, rs.p) > HS_SHOULDER_VAR:
            continue
        # 어깨는 머리에서 비슷한 거리에 위치해야 (책: 대칭성)
        d1, d2 = head.i - ls.i, rs.i - head.i
        if min(d1, d2) == 0 or max(d1, d2) / min(d1, d2) > 3.0:
            continue

        neck = fitline([t1.i, t2.i], [t1.p, t2.p])
        lvl = float(neck.at(rs.i))
        height = abs(head.p - float(neck.at(head.i)))
        if top:
            bo = None
            for j in range(rs.i + 1, ctx.n):
                if ctx.c[j] < neck.at(j):
                    bo = j
                    break
            direction, tgt_lvl = "down", lvl
        else:
            bo = None
            for j in range(rs.i + 1, ctx.n):
                if ctx.c[j] > neck.at(j):
                    bo = j
                    break
            direction, tgt_lvl = "up", lvl

        # 복합형: 좌·우 양쪽에 비슷한 높이의 추가 어깨가 모두 있어야 한다
        left_extra = [p for p in pv[max(0, k - 3):k]
                      if p.kind == want[0] and abs(p.p - ls.p) / ls.p < 0.07]
        right_extra = [p for p in pv[k + 5:k + 8]
                       if p.kind == want[0] and abs(p.p - rs.p) / rs.p < 0.07]
        complex_ = bool(left_extra) and bool(right_extra) and (rs.i - ls.i) >= 45
        pid = ("hs_top_complex" if top else "hs_bottom_complex") if complex_ \
            else ("hs_top" if top else "hs_bottom")

        ref = head.p
        hit = Hit(pid, direction, ls.i, rs.i, status="forming", breakout_i=bo,
                  height_pct=height / ref * 100.0,
                  target=_measure(direction, tgt_lvl, height),
                  quality=1.0 - abs(ls.p - rs.p) / max(ls.p, rs.p) / HS_SHOULDER_VAR * 0.5,
                  points=[(p.i, p.p) for p in seq],
                  notes={"neckline": round(lvl, 4),
                         "neck_slope": round(float(neck.slope), 5)})
        got = _finish(ctx, hit)
        if got:
            out.append(got)
    return out


# ═══════════════════════════════════════════════════════ 4. 빅 M / 빅 W
@register("big_w")
def detect_big_w(ctx: Ctx) -> List[Hit]:
    out = []
    for hit in detect_double_bottom(ctx):
        pre = trend_before(ctx.c, hit.start, lookback=90)
        if pre <= -20 and hit.end - hit.start >= 30:
            h2 = Hit("big_w", "up", hit.start, hit.end, hit.status,
                     hit.breakout_i, hit.breakout_px, hit.height_pct,
                     hit.target, min(1.0, hit.quality + 0.1), hit.points,
                     dict(hit.notes, prior_decline=round(pre, 1)))
            out.append(h2)
    return out


@register("big_m")
def detect_big_m(ctx: Ctx) -> List[Hit]:
    out = []
    for hit in detect_double_top(ctx):
        pre = trend_before(ctx.c, hit.start, lookback=90)
        if pre >= 20 and hit.end - hit.start >= 30:
            h2 = Hit("big_m", "down", hit.start, hit.end, hit.status,
                     hit.breakout_i, hit.breakout_px, hit.height_pct,
                     hit.target, min(1.0, hit.quality + 0.1), hit.points,
                     dict(hit.notes, prior_rise=round(pre, 1)))
            out.append(h2)
    return out


# ════════════════════════════════════════ 5. 추세선 패턴 (삼각/쐐기/확대/박스)
TRENDLINE_PIDS = (
    "triangle_asc", "triangle_desc", "triangle_sym",
    "wedge_rising", "wedge_falling",
    "broadening_top", "broadening_bottom",
    "broadening_ra_ascending", "broadening_ra_descending",
    "broadening_wedge_asc", "broadening_wedge_desc",
    "rectangle_top", "rectangle_bottom",
)


@register(*TRENDLINE_PIDS)
def detect_trendline_patterns(ctx: Ctx) -> List[Hit]:
    """수렴/확산/수평 추세선 조합으로 13개 패턴을 한 번에 판별."""
    best: Dict[str, Hit] = {}
    for w in WINDOWS:
        a = ctx.n - w
        if a < 20:
            continue
        pv = _window_pivots(ctx, a, ctx.n - 1)
        if len(pv) < 5:
            continue
        got = _lines(ctx, pv)
        if got is None:
            continue
        up, dn, t_up, t_dn = got
        if t_up + t_dn < 5 or t_up < 2 or t_dn < 2 or max(t_up, t_dn) < 3:
            continue                                    # 책의 3+2 터치 규칙

        s, e = pv[0].i, pv[-1].i
        span = e - s
        if span < 20 or span > MAX_DUR:
            continue
        xs = np.arange(s, e + 1)
        mid = (up.at(xs) + dn.at(xs)) / 2.0
        side = np.sign(ctx.c[s:e + 1] - mid)
        side = side[side != 0]
        crossings = int((np.diff(side) != 0).sum()) if side.size > 1 else 0
        if crossings < 3:              # 책의 crossing pattern 규칙
            continue
        w0, w1 = float(up.at(s) - dn.at(s)), float(up.at(e) - dn.at(e))
        if w0 <= 0 or w1 <= 0:
            continue
        ref = float(ctx.c[s])
        du, dd = drift(up, span, ref), drift(dn, span, ref)
        pre = trend_before(ctx.c, s)
        conv, div = w0 / w1, w1 / w0

        pid: Optional[str] = None
        if conv >= CONVERGE:                                       # 수렴형
            if abs(du) < FLAT and dd > TILT:
                pid = "triangle_asc"
            elif du < -TILT and abs(dd) < FLAT:
                pid = "triangle_desc"
            elif du < -TILT * 0.6 and dd > TILT * 0.6:
                pid = "triangle_sym"
            elif du > TILT and dd > TILT:
                pid = "wedge_rising"
            elif du < -TILT and dd < -TILT:
                pid = "wedge_falling"
        elif div >= CONVERGE:                                      # 확산형
            if du > TILT and dd > TILT:
                pid = "broadening_wedge_asc"
            elif du < -TILT and dd < -TILT:
                pid = "broadening_wedge_desc"
            elif du > TILT and abs(dd) < FLAT:
                pid = "broadening_ra_ascending"
            elif abs(du) < FLAT and dd < -TILT:
                pid = "broadening_ra_descending"
            elif du > 0 and dd < 0:
                pid = "broadening_top" if pre > 3 else "broadening_bottom"
        else:                                                       # 평행형
            if abs(du) < FLAT and abs(dd) < FLAT and w1 / ref > 0.03:
                pid = "rectangle_top" if pre > 3 else "rectangle_bottom"
        if pid is None:
            continue

        # 돌파 판정: 마지막 피벗 이후 종가가 추세선 밖으로
        bo, direction = None, _default_dir(pid)
        for j in range(e + 1, ctx.n):
            if ctx.c[j] > up.at(j) + 0.15 * ctx.tol(j):
                bo, direction = j, "up"
                break
            if ctx.c[j] < dn.at(j) - 0.15 * ctx.tol(j):
                bo, direction = j, "down"
                break
        if pid in ("triangle_asc", "rectangle_top", "rectangle_bottom",
                   "broadening_top", "broadening_bottom") and bo is None:
            direction = _default_dir(pid)

        height = max(w0, w1)
        lvl = float(up.at(bo if bo is not None else e)) if direction == "up" \
            else float(dn.at(bo if bo is not None else e))
        hit = Hit(pid, direction, s, e, status="forming", breakout_i=bo,
                  height_pct=height / ref * 100.0,
                  target=_measure(direction, lvl, height),
                  quality=min(1.0, 0.35 + 0.09 * (t_up + t_dn)),
                  points=[(p.i, p.p) for p in pv],
                  notes={"touches": t_up + t_dn, "crossings": crossings,
                         "slope_top": round(du, 4), "slope_bot": round(dd, 4),
                         "prior_trend": round(pre, 1),
                         "upper": [round(up.slope, 6), round(up.intercept, 4)],
                         "lower": [round(dn.slope, 6), round(dn.intercept, 4)]})
        fin = _finish(ctx, hit)
        if fin and (pid not in best or fin.quality > best[pid].quality):
            best[pid] = fin
    return list(best.values())


def _default_dir(pid: str) -> str:
    if pid in ("triangle_asc", "wedge_falling", "rectangle_bottom",
               "broadening_bottom", "broadening_ra_descending",
               "broadening_wedge_desc"):
        return "up"
    if pid in ("triangle_desc", "wedge_rising", "rectangle_top",
               "broadening_top", "broadening_ra_ascending",
               "broadening_wedge_asc"):
        return "down"
    return "up"


# ═══════════════════════════════════════════════════ 6. 하모닉 (XABCD 피보나치)
HARMONIC = {
    # pid: (AB/AX, CB/AB, CD/CB, AD/AX)
    "gartley_bull": ((.618,), (.382, .5, .618, .707, .786, .886),
                     (1.13, 1.27, 1.41, 1.618), (.786,)),
    "bat_bull": ((.382, .5), (.382, .5, .618, .707, .786, .886),
                 (1.618, 2.0, 2.24, 2.618), (.886,)),
    "butterfly_bull": ((.786,), (.382, .5, .618, .707, .786, .886),
                       (1.618, 2.0, 2.24), (1.27,)),
    "crab_bull": ((.382, .5, .618), (.382, .5, .618, .707, .786, .886),
                  (2.618, 3.14, 3.618), (1.618,)),
}
for _k in list(HARMONIC):
    HARMONIC[_k.replace("_bull", "_bear")] = HARMONIC[_k]

FIB_RETRACE = (.382, .5, .618, .707, .786, .886)
FIB_EXTEND = (1.13, 1.27, 1.41, 1.618, 2.0, 2.24, 2.618, 3.14)
HARM_TOL = 0.038          # 비율 허용오차 (엄격할수록 오탐 급감)
HARM_MAX = 130            # 책: 6개월 제한


@register(*HARMONIC.keys())
def detect_harmonics(ctx: Ctx) -> List[Hit]:
    out, pv = [], ctx.piv
    for k in range(max(0, len(pv) - 14), len(pv) - 4):
        X, A, B, C, D = pv[k:k + 5]
        if D.i - X.i > HARM_MAX:
            continue
        bull = (X.kind == "L")
        if [p.kind for p in (X, A, B, C, D)] != (
                ["L", "H", "L", "H", "L"] if bull else ["H", "L", "H", "L", "H"]):
            continue
        xa, ab = abs(A.p - X.p), abs(B.p - A.p)
        bc, cd = abs(C.p - B.p), abs(D.p - C.p)
        ad = abs(D.p - A.p)
        if min(xa, ab, bc, cd) <= 0:
            continue
        r_ab, r_cb, r_cd, r_ad = ab / xa, bc / ab, cd / bc, ad / xa

        for pid, (t_ab, t_cb, t_cd, t_ad) in HARMONIC.items():
            if pid.endswith("_bull") != bull:
                continue
            if (fib_near(r_ab, t_ab, HARM_TOL) is None
                    or fib_near(r_cb, t_cb, HARM_TOL) is None
                    or fib_near(r_cd, t_cd, HARM_TOL) is None
                    or fib_near(r_ad, t_ad, HARM_TOL) is None):
                continue
            direction = "up" if bull else "down"
            # 하모닉의 목표는 D 에서의 반전 → 1차 목표 C, 2차 목표 A
            hit = Hit(pid, direction, X.i, D.i, status="forming",
                      breakout_i=None,
                      height_pct=abs(A.p - D.p) / D.p * 100.0,
                      target=float(C.p),
                      quality=1.0 - abs(r_ad - t_ad[0]) / HARM_TOL * 0.3,
                      points=[(p.i, p.p) for p in (X, A, B, C, D)],
                      notes={"AB/AX": round(r_ab, 3), "CB/AB": round(r_cb, 3),
                             "CD/CB": round(r_cd, 3), "AD/AX": round(r_ad, 3),
                             "D_price": round(D.p, 4), "target2": round(A.p, 4)})
            got = _finish(ctx, hit)
            if got:
                out.append(got)
    return out


@register("abcd_bull", "abcd_bear")
def detect_abcd(ctx: Ctx) -> List[Hit]:
    out, pv = [], ctx.piv
    for k in range(max(0, len(pv) - 12), len(pv) - 3):
        A, B, C, D = pv[k:k + 4]
        if D.i - A.i > HARM_MAX:
            continue
        bull = (A.kind == "H")          # 하락 지그재그 → D 에서 매수
        want = ["H", "L", "H", "L"] if bull else ["L", "H", "L", "H"]
        if [p.kind for p in (A, B, C, D)] != want:
            continue
        ab, bc, cd = abs(B.p - A.p), abs(C.p - B.p), abs(D.p - C.p)
        if min(ab, bc, cd) <= 0:
            continue
        r_cb, r_cd = bc / ab, cd / bc
        if fib_near(r_cb, FIB_RETRACE, HARM_TOL) is None:
            continue
        if fib_near(r_cd, FIB_EXTEND, HARM_TOL) is None:
            continue
        # 책: A~B 사이에 A보다 높은 고점/B보다 낮은 저점이 없어야 함
        seg = slice(A.i, B.i + 1)
        if bull and (ctx.h[seg].max() > A.p * 1.005 or ctx.l[seg].min() < B.p * 0.995):
            continue
        if (not bull) and (ctx.l[seg].min() < A.p * 0.995 or ctx.h[seg].max() > B.p * 1.005):
            continue
        seg2 = slice(A.i, D.i + 1)                # D 가 패턴 내 극단이어야
        if bull and float(ctx.l[seg2].min()) < D.p * 0.999:
            continue
        if (not bull) and float(ctx.h[seg2].max()) > D.p * 1.001:
            continue
        pid = "abcd_bull" if bull else "abcd_bear"
        hit = Hit(pid, "up" if bull else "down", A.i, D.i, status="forming",
                  height_pct=abs(A.p - D.p) / D.p * 100.0, target=float(C.p),
                  quality=0.7, points=[(p.i, p.p) for p in (A, B, C, D)],
                  notes={"CB/AB": round(r_cb, 3), "CD/CB": round(r_cd, 3),
                         "D_price": round(D.p, 4)})
        got = _finish(ctx, hit)
        if got:
            out.append(got)
    return out


# ══════════════════════════════════════════════════ 7. 갭 / 섬꼴반전 / V 반전
@register("gap")
def detect_gap(ctx: Ctx) -> List[Hit]:
    out = []
    lo = max(1, ctx.n - ctx.recent)
    avgv = float(np.mean(ctx.v[max(0, ctx.n - 60):])) or 1.0
    for i in range(lo, ctx.n):
        up_gap = ctx.l[i] > ctx.h[i - 1]
        dn_gap = ctx.h[i] < ctx.l[i - 1]
        if not (up_gap or dn_gap):
            continue
        size = (ctx.l[i] - ctx.h[i - 1]) if up_gap else (ctx.l[i - 1] - ctx.h[i])
        if size < 1.0 * float(ctx.atr[i]):
            continue
        if float(ctx.v[i]) < 1.3 * avgv:
            continue
        direction = "up" if up_gap else "down"
        pre = trend_before(ctx.c, i, lookback=40)
        kind = ("breakaway" if abs(pre) < 8 else
                "exhaustion" if (pre > 25 and up_gap) or (pre < -25 and dn_gap)
                else "continuation")
        hit = Hit("gap", direction, i - 1, i, status="breakout", breakout_i=i,
                  height_pct=size / float(ctx.c[i]) * 100.0,
                  target=None, quality=min(1.0, size / float(ctx.atr[i]) / 3),
                  points=[(i - 1, float(ctx.c[i - 1])), (i, float(ctx.c[i]))],
                  notes={"gap_type": kind,
                         "gap_pct": round(size / float(ctx.c[i]) * 100, 2),
                         "vol_x": round(float(ctx.v[i]) / avgv, 2)})
        got = _finish(ctx, hit)
        if got:
            out.append(got)
    return out


@register("island_reversal")
def detect_island(ctx: Ctx) -> List[Hit]:
    out = []
    lo = max(1, ctx.n - ctx.recent - 15)
    for i in range(lo, ctx.n - 1):
        for j in range(i, min(i + 12, ctx.n - 1)):
            isl_hi = float(ctx.h[i:j + 1].max())
            isl_lo = float(ctx.l[i:j + 1].min())
            # 바닥섬: 하락갭 진입 → 상승갭 이탈, 양쪽 갭이 가격대에서 겹침
            atr_i = float(ctx.atr[i])
            pre = trend_before(ctx.c, i, lookback=40)
            if (ctx.l[i - 1] - ctx.h[i] > 0.5 * atr_i
                    and ctx.l[j + 1] - isl_hi > 0.5 * atr_i
                    and min(ctx.l[i - 1], ctx.l[j + 1]) > isl_hi and pre < -8):
                d, pid = "up", "island_reversal"
            elif (ctx.l[i] - ctx.h[i - 1] > 0.5 * atr_i
                    and isl_lo - ctx.h[j + 1] > 0.5 * atr_i
                    and max(ctx.h[i - 1], ctx.h[j + 1]) < isl_lo and pre > 8):
                d, pid = "down", "island_reversal"
            else:
                continue
            hit = Hit(pid, d, i, j + 1, status="breakout", breakout_i=j + 1,
                      height_pct=(isl_hi - isl_lo) / float(ctx.c[j]) * 100.0,
                      target=None, quality=0.75,
                      points=[(i, isl_lo), (j, isl_hi)],
                      notes={"island_bars": j - i + 1,
                             "type": "bottom" if d == "up" else "top"})
            got = _finish(ctx, hit)
            if got:
                out.append(got)
            break
    return out


V_MOVE = 0.20          # V 양쪽 다리 최소 변동
V_MAX_BARS = 30


@register("v_bottom", "v_bottom_ext")
def detect_v_bottom(ctx: Ctx) -> List[Hit]:
    return _v(ctx, low=True)


@register("v_top", "v_top_ext")
def detect_v_top(ctx: Ctx) -> List[Hit]:
    return _v(ctx, low=False)


def _v(ctx: Ctx, low: bool) -> List[Hit]:
    out, pv = [], ctx.piv
    for k in range(1, len(pv) - 1):
        a, m, b = pv[k - 1], pv[k], pv[k + 1]
        if (m.kind == "L") != low:
            continue
        d1, d2 = m.i - a.i, b.i - m.i
        if not (3 <= d1 <= V_MAX_BARS and 3 <= d2 <= V_MAX_BARS):
            continue
        if max(d1, d2) / min(d1, d2) > 2.5:
            continue
        leg1 = abs(a.p - m.p) / a.p
        leg2 = abs(b.p - m.p) / m.p
        if leg1 < V_MOVE or leg2 < V_MOVE:
            continue
        lb = max(0, m.i - 60)                     # 반전점은 최근 구간의 극단이어야
        if low and float(ctx.l[lb:m.i + 1].min()) < m.p * 0.999:
            continue
        if (not low) and float(ctx.h[lb:m.i + 1].max()) > m.p * 1.001:
            continue
        # 확장형: 반전 후 되돌림이 있고 다시 원방향으로 진행
        ext = False
        if b.i + 5 < ctx.n:
            after = ctx.c[b.i:]
            ext = bool((after.min() < b.p * 0.96).any() and after[-1] > b.p * 0.99) \
                if low else bool((after.max() > b.p * 1.04).any() and after[-1] < b.p * 1.01)
        pid = ("v_bottom_ext" if ext else "v_bottom") if low else \
              ("v_top_ext" if ext else "v_top")
        direction = "up" if low else "down"
        hit = Hit(pid, direction, a.i, b.i, status="breakout", breakout_i=b.i,
                  height_pct=max(leg1, leg2) * 100.0,
                  target=float(a.p), quality=min(1.0, (leg1 + leg2) / 1.4),
                  points=[(a.i, a.p), (m.i, m.p), (b.i, b.p)],
                  notes={"leg1_pct": round(leg1 * 100, 1),
                         "leg2_pct": round(leg2 * 100, 1)})
        got = _finish(ctx, hit)
        if got:
            out.append(got)
    return out


# ═════════════════════════════════════════════════ 8. 깃발 / 페넌트 / HTF
POLE_MIN = 0.15
POLE_MAX_BARS = 25
FLAG_BARS = (4, 22)


@register("flag", "pennant")
def detect_flag_pennant(ctx: Ctx) -> List[Hit]:
    out = []
    for fl in range(FLAG_BARS[0], FLAG_BARS[1] + 1):
        e = ctx.n - 1
        s = e - fl + 1
        if s - POLE_MAX_BARS < 5:
            continue
        pole_a = max(0, s - POLE_MAX_BARS)
        seg = ctx.c[pole_a:s]
        if len(seg) < 6:
            continue
        rise = (ctx.c[s - 1] - seg.min()) / seg.min()
        fall = (seg.max() - ctx.c[s - 1]) / seg.max()
        if rise >= POLE_MIN:
            direction, pole = "up", rise
        elif fall >= POLE_MIN:
            direction, pole = "down", fall
        else:
            continue
        hi, lo = ctx.h[s:e + 1], ctx.l[s:e + 1]
        rng = float(hi.max() - lo.min())
        if rng / float(ctx.c[e]) > 0.35 * pole:           # 조정폭은 깃대의 1/3 이하
            continue
        up = fitline(range(fl), hi)
        dn = fitline(range(fl), lo)
        if up is None or dn is None:
            continue
        w0 = float(up.at(0) - dn.at(0))
        w1 = float(up.at(fl - 1) - dn.at(fl - 1))
        if w0 <= 0 or w1 <= 0:
            continue
        mid_slope = (up.slope + dn.slope) / 2.0
        counter = (mid_slope < 0) if direction == "up" else (mid_slope > 0)
        if not counter:                                   # 깃발은 추세 반대로 기운다
            continue
        parallel = abs(up.slope - dn.slope) * fl < 0.30 * rng
        converge = w0 / w1 >= 1.4
        if converge:
            pid = "pennant"
        elif parallel:
            pid = "flag"
        else:
            continue
        if ctx.vol_trend(s, e) > 0.35:                    # 깃발 중 거래량은 감소
            continue
        lvl = float(hi.max()) if direction == "up" else float(lo.min())
        pole_h = abs(float(ctx.c[s - 1]) - (float(seg.min()) if direction == "up"
                                            else float(seg.max())))
        hit = Hit(pid, direction, s, e, status="forming",
                  breakout_i=(e if (direction == "up" and ctx.c[e] > float(hi[:-1].max()))
                              or (direction == "down" and ctx.c[e] < float(lo[:-1].min()))
                              else None),
                  height_pct=pole * 100.0,
                  target=_measure(direction, lvl, pole_h),
                  quality=min(1.0, 0.45 + pole),
                  points=[(s, float(ctx.c[s])), (e, float(ctx.c[e]))],
                  notes={"pole_pct": round(pole * 100, 1), "flag_bars": fl})
        got = _finish(ctx, hit)
        if got:
            out.append(got)
            break
    return out


@register("flag_high_tight")
def detect_htf(ctx: Ctx) -> List[Hit]:
    """책: 2개월(≈40봉) 이내 90% 이상 상승 후 짧은 조정."""
    e = ctx.n - 1
    win = 45
    a = max(0, e - win)
    seg = ctx.c[a:e + 1]
    if len(seg) < 25:
        return []
    lo_i = int(np.argmin(seg))
    hi_i = int(np.argmax(seg[lo_i:])) + lo_i
    lo, hi = float(seg[lo_i]), float(seg[hi_i])
    if lo <= 0 or (hi - lo) / lo < 0.90:
        return []
    tail = seg[hi_i:]
    if len(tail) < 3 or len(tail) > 20:
        return []
    retrace = (hi - float(tail.min())) / (hi - lo)
    if retrace > 0.40:
        return []
    hit = Hit("flag_high_tight", "up", a + lo_i, e, status="forming",
              breakout_i=(e if ctx.c[e] > hi else None),
              height_pct=(hi - lo) / lo * 100.0,
              target=hi + (hi - lo), quality=min(1.0, (hi - lo) / lo / 1.5),
              points=[(a + lo_i, lo), (a + hi_i, hi)],
              notes={"rise_pct": round((hi - lo) / lo * 100, 1),
                     "retrace_pct": round(retrace * 100, 1),
                     "trigger": round(hi, 4)})
    got = _finish(ctx, hit)
    return [got] if got else []


# ═════════════════════════════════════ 9. 주봉 패턴: 파이프 / 혼 / 다이빙보드
@register("pipe_bottom", "pipe_top", scale="W")
def detect_pipe(ctx: Ctx) -> List[Hit]:
    """책: 주봉 인접 2봉의 긴 꼬리 쌍. 두 저(고)점이 근접."""
    out = []
    n = ctx.n
    rng = ctx.h - ctx.l
    avg = float(np.mean(rng[max(0, n - 52):])) or 1.0
    for i in range(max(2, n - ctx.recent - 2), n - 1):
        j = i + 1
        long_pair = rng[i] > 1.4 * avg and rng[j] > 1.4 * avg
        if not long_pair:
            continue
        # 파이프 바닥
        if abs(ctx.l[i] - ctx.l[j]) / min(ctx.l[i], ctx.l[j]) < 0.06 \
                and trend_before(ctx.c, i, lookback=20) < -8 \
                and min(ctx.l[i], ctx.l[j]) < ctx.l[max(0, i - 6):i].min():
            top = float(max(ctx.h[i], ctx.h[j]))
            bot = float(min(ctx.l[i], ctx.l[j]))
            bo = breakout_up(ctx.c, top, j + 1)
            hit = Hit("pipe_bottom", "up", i, j, status="forming", breakout_i=bo,
                      height_pct=(top - bot) / bot * 100.0,
                      target=top + (top - bot), quality=0.75,
                      points=[(i, bot), (j, bot)],
                      notes={"scale": "weekly", "trigger": round(top, 4)})
            got = _finish(ctx, hit)
            if got:
                out.append(got)
        # 파이프 천정
        if abs(ctx.h[i] - ctx.h[j]) / max(ctx.h[i], ctx.h[j]) < 0.06 \
                and trend_before(ctx.c, i, lookback=20) > 8 \
                and max(ctx.h[i], ctx.h[j]) > ctx.h[max(0, i - 6):i].max():
            top = float(max(ctx.h[i], ctx.h[j]))
            bot = float(min(ctx.l[i], ctx.l[j]))
            bo = breakout_down(ctx.c, bot, j + 1)
            hit = Hit("pipe_top", "down", i, j, status="forming", breakout_i=bo,
                      height_pct=(top - bot) / top * 100.0,
                      target=bot - (top - bot), quality=0.75,
                      points=[(i, top), (j, top)],
                      notes={"scale": "weekly"})
            got = _finish(ctx, hit)
            if got:
                out.append(got)
    return out


@register("horn_bottom", "horn_top", scale="W")
def detect_horn(ctx: Ctx) -> List[Hit]:
    """책: 주봉에서 한 주를 사이에 둔 두 개의 긴 꼬리."""
    out = []
    n = ctx.n
    rng = ctx.h - ctx.l
    avg = float(np.mean(rng[max(0, n - 52):])) or 1.0
    for i in range(max(2, n - ctx.recent - 3), n - 2):
        j = i + 2
        if not (rng[i] > 1.4 * avg and rng[j] > 1.4 * avg):
            continue
        if rng[i + 1] > 0.9 * min(rng[i], rng[j]):
            continue
        if abs(ctx.l[i] - ctx.l[j]) / min(ctx.l[i], ctx.l[j]) < 0.07 \
                and trend_before(ctx.c, i, lookback=20) < -8:
            top = float(max(ctx.h[i], ctx.h[i + 1], ctx.h[j]))
            bot = float(min(ctx.l[i], ctx.l[j]))
            bo = breakout_up(ctx.c, top, j + 1)
            hit = Hit("horn_bottom", "up", i, j, status="forming", breakout_i=bo,
                      height_pct=(top - bot) / bot * 100.0,
                      target=top + (top - bot), quality=0.7,
                      points=[(i, bot), (j, bot)], notes={"scale": "weekly"})
            got = _finish(ctx, hit)
            if got:
                out.append(got)
        if abs(ctx.h[i] - ctx.h[j]) / max(ctx.h[i], ctx.h[j]) < 0.07 \
                and trend_before(ctx.c, i, lookback=20) > 8:
            top = float(max(ctx.h[i], ctx.h[j]))
            bot = float(min(ctx.l[i], ctx.l[i + 1], ctx.l[j]))
            bo = breakout_down(ctx.c, bot, j + 1)
            hit = Hit("horn_top", "down", i, j, status="forming", breakout_i=bo,
                      height_pct=(top - bot) / top * 100.0,
                      target=bot - (top - bot), quality=0.7,
                      points=[(i, top), (j, top)], notes={"scale": "weekly"})
            got = _finish(ctx, hit)
            if got:
                out.append(got)
    return out


# ═══════════════════════════════════ 10. 곡선형: 원형·컵·스캘럽·다이아몬드
ROUND_MIN_BARS = 30
ROUND_R2 = 0.82        # 2차곡선 적합도 하한 (원형/컵)


@register("rounding_bottom", "cup_handle")
def detect_rounding_bottom(ctx: Ctx) -> List[Hit]:
    out = []
    for w in (40, 55, 75, 100, 130):
        e = ctx.n - 1
        s = e - w
        if s < 10:
            continue
        seg = ctx.l[s:e + 1]
        r2 = rounded_r2(seg)
        if r2 < ROUND_R2 or curvature(seg) <= 0:
            continue
        lo = float(seg.min())
        pos = int(np.argmin(seg)) / max(1, len(seg) - 1)
        if not (0.25 <= pos <= 0.75):                      # 바닥은 가운데 부근
            continue
        left, right = float(ctx.h[s]), float(ctx.h[e])
        rim = max(left, right)
        if lo <= 0 or (rim - lo) / lo < 0.18:
            continue
        if abs(left - right) / rim > 0.12:                 # 컵 양쪽 테두리 대칭
            continue
        # 손잡이: 우측 테두리 이후 5~25봉의 얕은 조정
        pid, handle = "rounding_bottom", None
        tail_a = s + int(np.argmax(ctx.h[s:e + 1]))
        if e - tail_a >= 4:
            t = ctx.c[tail_a:e + 1]
            dip = (float(t.max()) - float(t.min())) / float(t.max())
            if 0.02 <= dip <= 0.35 and (e - tail_a) <= 30:
                pid, handle = "cup_handle", round(dip * 100, 1)
        bo = breakout_up(ctx.c, rim, e - 3)
        hit = Hit(pid, "up", s, e, status="forming", breakout_i=bo,
                  height_pct=(rim - lo) / lo * 100.0,
                  target=rim + (rim - lo), quality=min(1.0, r2),
                  points=[(s, left), (s + int(np.argmin(seg)), lo), (e, right)],
                  notes={"r2": round(r2, 3), "handle_pct": handle,
                         "trigger": round(rim, 4)})
        got = _finish(ctx, hit)
        if got:
            out.append(got)
            break
    return out


@register("rounding_top", "cup_handle_inv")
def detect_rounding_top(ctx: Ctx) -> List[Hit]:
    out = []
    for w in (40, 55, 75, 100, 130):
        e = ctx.n - 1
        s = e - w
        if s < 10:
            continue
        seg = ctx.h[s:e + 1]
        r2 = rounded_r2(seg)
        if r2 < ROUND_R2 or curvature(seg) >= 0:
            continue
        hi = float(seg.max())
        pos = int(np.argmax(seg)) / max(1, len(seg) - 1)
        if not (0.25 <= pos <= 0.75):
            continue
        left, right = float(ctx.l[s]), float(ctx.l[e])
        rim = min(left, right)
        if rim <= 0 or (hi - rim) / rim < 0.18:
            continue
        if abs(left - right) / hi > 0.12:
            continue
        pid = "rounding_top"
        tail_a = s + int(np.argmin(ctx.l[s:e + 1]))
        if e - tail_a >= 4:
            t = ctx.c[tail_a:e + 1]
            bump = (float(t.max()) - float(t.min())) / float(t.max())
            if 0.02 <= bump <= 0.35 and (e - tail_a) <= 30:
                pid = "cup_handle_inv"
        bo = breakout_down(ctx.c, rim, e - 3)
        hit = Hit(pid, "down", s, e, status="forming", breakout_i=bo,
                  height_pct=(hi - rim) / hi * 100.0,
                  target=rim - (hi - rim), quality=min(1.0, r2),
                  points=[(s, left), (s + int(np.argmax(seg)), hi), (e, right)],
                  notes={"r2": round(r2, 3), "trigger": round(rim, 4)})
        got = _finish(ctx, hit)
        if got:
            out.append(got)
            break
    return out


@register("scallop_asc", "scallop_desc", "scallop_asc_inv", "scallop_desc_inv")
def detect_scallop(ctx: Ctx) -> List[Hit]:
    """J자(상승 스캘럽) / 역J자 계열. 둥근 저점 + 좌우 높이 차."""
    out = []
    for w in (30, 45, 60, 85):
        e = ctx.n - 1
        s = e - w
        if s < 10:
            continue
        segl, segh = ctx.l[s:e + 1], ctx.h[s:e + 1]
        r2l, r2h = rounded_r2(segl), rounded_r2(segh)
        left, right = float(ctx.c[s]), float(ctx.c[e])
        gap = (right - left) / left
        depth = (float(segh.max()) - float(segl.min())) / float(segl.min())
        if depth < 0.15:
            continue
        if r2l >= 0.86 and curvature(segl) > 0:            # U자 (정방향)
            if gap > 0.10:
                pid, direction = "scallop_asc", "up"
            elif gap < -0.10:
                pid, direction = "scallop_desc", "down"
            else:
                continue
            q = r2l
        elif r2h >= 0.86 and curvature(segh) < 0:          # ∩자 (역방향)
            if gap > 0.10:
                pid, direction = "scallop_asc_inv", "up"
            elif gap < -0.10:
                pid, direction = "scallop_desc_inv", "down"
            else:
                continue
            q = r2h
        else:
            continue
        hi, lo = float(segh.max()), float(segl.min())
        lvl = hi if direction == "up" else lo
        bo = (breakout_up(ctx.c, hi, e - 3) if direction == "up"
              else breakout_down(ctx.c, lo, e - 3))
        hit = Hit(pid, direction, s, e, status="forming", breakout_i=bo,
                  height_pct=(hi - lo) / lo * 100.0,
                  target=_measure(direction, lvl, (hi - lo) * 0.5),
                  quality=min(1.0, q), points=[(s, left), (e, right)],
                  notes={"r2": round(q, 3), "lr_gap_pct": round(gap * 100, 1)})
        got = _finish(ctx, hit)
        if got:
            out.append(got)
            break
    return out


@register("diamond_top", "diamond_bottom")
def detect_diamond(ctx: Ctx) -> List[Hit]:
    """전반부 확산 + 후반부 수렴."""
    out = []
    for w in (30, 45, 65, 90):
        e = ctx.n - 1
        s = e - w
        if s < 15:
            continue
        pv = _window_pivots(ctx, s, e)
        if len(pv) < 6:
            continue
        mid = s + w // 2
        a1 = _lines(ctx, [p for p in pv if p.i <= mid])
        a2 = _lines(ctx, [p for p in pv if p.i >= mid])
        if a1 is None or a2 is None:
            continue
        u1, d1, _, _ = a1
        u2, d2, _, _ = a2
        w1a, w1b = float(u1.at(s) - d1.at(s)), float(u1.at(mid) - d1.at(mid))
        w2a, w2b = float(u2.at(mid) - d2.at(mid)), float(u2.at(e) - d2.at(e))
        if min(w1a, w1b, w2a, w2b) <= 0:
            continue
        if not (w1b / w1a >= 1.25 and w2a / w2b >= 1.25):
            continue
        pre = trend_before(ctx.c, s)
        pid = "diamond_top" if pre > 3 else "diamond_bottom"
        direction = "down" if pid == "diamond_top" else "up"
        hi = float(ctx.h[s:e + 1].max())
        lo = float(ctx.l[s:e + 1].min())
        lvl = float(ctx.c[e])
        bo = (breakout_down(ctx.c, float(d2.at(e)), e - 3) if direction == "down"
              else breakout_up(ctx.c, float(u2.at(e)), e - 3))
        hit = Hit(pid, direction, s, e, status="forming", breakout_i=bo,
                  height_pct=(hi - lo) / lo * 100.0,
                  target=_measure(direction, lvl, hi - lo),
                  quality=0.6, points=[(p.i, p.p) for p in pv],
                  notes={"expand": round(w1b / w1a, 2),
                         "contract": round(w2a / w2b, 2)})
        got = _finish(ctx, hit)
        if got:
            out.append(got)
            break
    return out


# ═══════════════════════════════════════════════════════ 11. 측정된 상승/하락
@register("mm_up", "mm_down")
def detect_measured_move(ctx: Ctx) -> List[Hit]:
    out, pv = [], ctx.piv
    for k in range(len(pv) - 3):
        a, b, c_, d = pv[k:k + 4]
        if d.i - a.i > MAX_DUR:
            continue
        up = [p.kind for p in (a, b, c_, d)] == ["L", "H", "L", "H"]
        dn = [p.kind for p in (a, b, c_, d)] == ["H", "L", "H", "L"]
        if not (up or dn):
            continue
        leg1 = abs(b.p - a.p)
        corr = abs(c_.p - b.p)
        leg2 = abs(d.p - c_.p)
        if leg1 <= 0 or leg2 <= 0:
            continue
        retr = corr / leg1
        if not (0.25 <= retr <= 0.70):
            continue
        if not (0.65 <= leg2 / leg1 <= 1.55):
            continue
        if abs(a.p) <= 0 or leg1 / a.p < 0.15:
            continue
        pid = "mm_up" if up else "mm_down"
        hit = Hit(pid, "up" if up else "down", a.i, d.i, status="forming",
                  breakout_i=d.i, height_pct=leg1 / a.p * 100.0,
                  target=float(c_.p),
                  quality=1.0 - abs(1 - leg2 / leg1),
                  points=[(p.i, p.p) for p in (a, b, c_, d)],
                  notes={"retrace": round(retr, 2),
                         "leg_ratio": round(leg2 / leg1, 2)})
        got = _finish(ctx, hit)
        if got:
            out.append(got)
    return out


# ══════════════════════════════════════ 12. 범프앤런 반전 (BARR) — 책 성과 1위
BARR_LEADIN = (18, 60)     # 리드인(프라이팬 손잡이) 길이
BARR_BUMP_X = 2.0          # 범프 깊이 ≥ 리드인 높이 × 배수


@register("burr_bottom", "burr_top")
def detect_burr(ctx: Ctx) -> List[Hit]:
    """하향 리드인 추세선 → 급격히 깊어지는 범프 → 추세선 재돌파."""
    out = []
    e = ctx.n - 1
    for total in (60, 85, 115, 150):
        s = e - total
        if s < 10:
            continue
        li_end = s + max(BARR_LEADIN[0], total // 3)
        pv_li = _window_pivots(ctx, s, li_end)
        hi_li = [p for p in pv_li if p.kind == "H"]
        lo_li = [p for p in pv_li if p.kind == "L"]
        if len(hi_li) < 2 or len(lo_li) < 2:
            continue
        ref = float(ctx.c[s])

        # --- 바닥형: 하향 리드인 추세선(고점) + 아래로 깊어지는 범프
        top = fitline([p.i for p in hi_li], [p.p for p in hi_li])
        if top is not None and top.slope < 0:
            lead_h = float(np.mean([top.at(p.i) - p.p for p in lo_li]))
            if lead_h > 0.03 * ref:
                bump_i = li_end + int(np.argmin(ctx.l[li_end:e + 1]))
                depth = float(top.at(bump_i) - ctx.l[bump_i])
                if depth >= BARR_BUMP_X * lead_h and bump_i < e - 3:
                    bo = None
                    for j in range(bump_i + 1, ctx.n):
                        if ctx.c[j] > top.at(j):
                            bo = j
                            break
                    hit = Hit("burr_bottom", "up", s, e, status="forming",
                              breakout_i=bo, height_pct=depth / ref * 100.0,
                              target=float(top.at(bo if bo else e)) + depth,
                              quality=min(1.0, 0.45 + depth / lead_h / 8),
                              points=[(s, float(ctx.h[s])),
                                      (bump_i, float(ctx.l[bump_i])),
                                      (e, float(ctx.c[e]))],
                              notes={"lead_in_height": round(lead_h, 4),
                                     "bump_x": round(depth / lead_h, 2)})
                    got = _finish(ctx, hit)
                    if got:
                        out.append(got)
                        break

        # --- 천정형: 상향 리드인 추세선(저점) + 위로 치솟는 범프
        bot = fitline([p.i for p in lo_li], [p.p for p in lo_li])
        if bot is not None and bot.slope > 0:
            lead_h = float(np.mean([p.p - bot.at(p.i) for p in hi_li]))
            if lead_h > 0.03 * ref:
                bump_i = li_end + int(np.argmax(ctx.h[li_end:e + 1]))
                depth = float(ctx.h[bump_i] - bot.at(bump_i))
                if depth >= BARR_BUMP_X * lead_h and bump_i < e - 3:
                    bo = None
                    for j in range(bump_i + 1, ctx.n):
                        if ctx.c[j] < bot.at(j):
                            bo = j
                            break
                    hit = Hit("burr_top", "down", s, e, status="forming",
                              breakout_i=bo, height_pct=depth / ref * 100.0,
                              target=float(bot.at(bo if bo else e)) - depth,
                              quality=min(1.0, 0.45 + depth / lead_h / 8),
                              points=[(s, float(ctx.l[s])),
                                      (bump_i, float(ctx.h[bump_i])),
                                      (e, float(ctx.c[e]))],
                              notes={"lead_in_height": round(lead_h, 4),
                                     "bump_x": round(depth / lead_h, 2)})
                    got = _finish(ctx, hit)
                    if got:
                        out.append(got)
                        break
    return out


# ═══════════════════════════════════════════════════════ 13. 루프 / 역루프
@register("roof", "roof_inv")
def detect_roof(ctx: Ctx) -> List[Hit]:
    """가운데 꼭짓점 + 양쪽으로 기운 변 + 평평한 반대편 경계(최소 3터치)."""
    out = []
    e = ctx.n - 1
    for w in (30, 42, 56, 75):
        s = e - w
        if s < 10:
            continue
        pv = _window_pivots(ctx, s, e)
        hs = [p for p in pv if p.kind == "H"]
        ls = [p for p in pv if p.kind == "L"]
        if len(hs) < 3 or len(ls) < 3:
            continue
        ref = float(ctx.c[s])
        tol = ctx.tol(e)
        found = False

        # --- 루프: 중앙 고점 + 하향 좌우 변 + 수평 바닥
        apex = max(hs, key=lambda p: p.p)
        left = [p for p in hs if p.i < apex.i]
        right = [p for p in hs if p.i > apex.i]
        base = fitline([p.i for p in ls], [p.p for p in ls])
        if left and right and base is not None \
                and abs(drift(base, w, ref)) < FLAT \
                and touches(base, ls, tol) >= 3:
            if (apex.p - min(p.p for p in left)) / ref > 0.03 \
                    and (apex.p - min(p.p for p in right)) / ref > 0.03:
                lvl = float(base.at(e))
                height = apex.p - lvl
                bo = breakout_down(ctx.c, lvl, apex.i + 1)
                hit = Hit("roof", "down", s, e, status="forming", breakout_i=bo,
                          height_pct=height / ref * 100.0,
                          target=lvl - height, quality=0.6,
                          points=[(left[0].i, left[0].p), (apex.i, apex.p),
                                  (right[-1].i, right[-1].p)],
                          notes={"base": round(lvl, 4),
                                 "base_touches": touches(base, ls, tol)})
                got = _finish(ctx, hit)
                if got:
                    out.append(got)
                    found = True

        # --- 역루프: 중앙 저점 + 상향 좌우 변 + 수평 천정
        nadir = min(ls, key=lambda p: p.p)
        left = [p for p in ls if p.i < nadir.i]
        right = [p for p in ls if p.i > nadir.i]
        cap = fitline([p.i for p in hs], [p.p for p in hs])
        if left and right and cap is not None \
                and abs(drift(cap, w, ref)) < FLAT \
                and touches(cap, hs, tol) >= 3:
            if (max(p.p for p in left) - nadir.p) / ref > 0.03 \
                    and (max(p.p for p in right) - nadir.p) / ref > 0.03:
                lvl = float(cap.at(e))
                height = lvl - nadir.p
                bo = breakout_up(ctx.c, lvl, nadir.i + 1)
                hit = Hit("roof_inv", "up", s, e, status="forming", breakout_i=bo,
                          height_pct=height / ref * 100.0,
                          target=lvl + height, quality=0.6,
                          points=[(left[0].i, left[0].p), (nadir.i, nadir.p),
                                  (right[-1].i, right[-1].p)],
                          notes={"cap": round(lvl, 4),
                                 "cap_touches": touches(cap, hs, tol)})
                got = _finish(ctx, hit)
                if got:
                    out.append(got)
                    found = True
        if found:
            break
    return out


# ═════════════════════════════════════════════════ 14. 다이빙 보드 (주봉)
@register("diving_board", scale="W")
def detect_diving_board(ctx: Ctx) -> List[Hit]:
    """긴 평평한 베이스(보드) → 급락 → 보드 상단 재돌파."""
    e = ctx.n - 1
    for board_w in (14, 20, 28, 36):
        for plunge_w in (6, 10, 16, 24):
            b1 = e - plunge_w
            b0 = b1 - board_w
            if b0 < 3:
                continue
            top = float(ctx.h[b0:b1].max())
            bot = float(ctx.l[b0:b1].min())
            mid = (top + bot) / 2.0
            if mid <= 0 or (top - bot) / mid > 0.28:      # 보드는 평평해야
                continue
            low = float(ctx.l[b1:e + 1].min())
            plunge = (bot - low) / bot
            if plunge < 0.20:                             # 급락 요건
                continue
            bo = breakout_up(ctx.c, top, b1)
            hit = Hit("diving_board", "up", b0, e, status="forming",
                      breakout_i=bo, height_pct=plunge * 100.0,
                      target=top + (top - low),
                      quality=0.35 + 0.30 * min(1.0, plunge / 0.40)
                              + 0.35 * min(1.0, board_w / 26),
                      points=[(b0, top),
                              (b1 + int(np.argmin(ctx.l[b1:e + 1])), low)],
                      notes={"board_top": round(top, 4),
                             "trigger": round(top, 4),
                             "half_target": round(top + (top - low) / 2, 4),
                             "plunge_pct": round(plunge * 100, 1),
                             "board_weeks": board_w, "scale": "weekly"})
            got = _finish(ctx, hit)
            if got:
                return [got]
    return []


# ══════════════════════════════════════════════════════════ 15. 울프 웨이브
@register("wolfe_bull", "wolfe_bear")
def detect_wolfe(ctx: Ctx) -> List[Hit]:
    """5개 전환점 + 1-3 / 2-4 선이 미래에서 수렴. 목표는 1-4 선(EPA)."""
    out, pv = [], ctx.piv
    for k in range(max(0, len(pv) - 10), len(pv) - 4):
        p1, p2, p3, p4, p5 = pv[k:k + 5]
        if not (20 <= p5.i - p1.i <= MAX_DUR):
            continue
        bull = (p1.kind == "L")
        want = ["L", "H", "L", "H", "L"] if bull else ["H", "L", "H", "L", "H"]
        if [p.kind for p in (p1, p2, p3, p4, p5)] != want:
            continue
        if bull and not (p3.p < p1.p and p4.p < p2.p and p5.p < p3.p):
            continue
        if (not bull) and not (p3.p > p1.p and p4.p > p2.p and p5.p > p3.p):
            continue
        l13 = fitline([p1.i, p3.i], [p1.p, p3.p])
        l24 = fitline([p2.i, p4.i], [p2.p, p4.p])
        epa = fitline([p1.i, p4.i], [p1.p, p4.p])
        if l13 is None or l24 is None or epa is None:
            continue
        gap_now = abs(float(l24.at(p5.i) - l13.at(p5.i)))
        gap_fwd = abs(float(l24.at(p5.i + 30) - l13.at(p5.i + 30)))
        if gap_now <= 0 or gap_fwd >= gap_now:            # 미래에서 수렴해야
            continue
        target = float(epa.at(p5.i + 30))
        direction = "up" if bull else "down"
        hit = Hit("wolfe_bull" if bull else "wolfe_bear", direction,
                  p1.i, p5.i, status="forming",
                  breakout_i=None,
                  height_pct=abs(target - p5.p) / p5.p * 100.0,
                  target=target, quality=0.6,
                  points=[(p.i, p.p) for p in (p1, p2, p3, p4, p5)],
                  notes={"epa": round(target, 4),
                         "converge_ratio": round(gap_fwd / gap_now, 2)})
        got = _finish(ctx, hit)
        if got:
            out.append(got)
    return out


# ══════════════════════════════════════════════════════════════ 실행 진입점
def run(o, h, l, c, v, dates: Optional[Sequence[str]] = None,
        recent: int = 12, pids: Optional[Sequence[str]] = None) -> List[Hit]:
    """일봉 + (필요 시) 주봉 컨텍스트를 만들어 등록된 탐지기를 모두 실행."""
    from .ohlcv import Series

    want = set(pids) if pids else None
    hits: List[Hit] = []

    ctx_d = Ctx(o, h, l, c, v, recent=recent, scale="D")
    daily_fns, weekly_fns = [], []
    for pid, fn in REGISTRY.items():
        if want and pid not in want:
            continue
        (weekly_fns if SCALE[pid] == "W" else daily_fns).append(fn)

    for fn in dict.fromkeys(daily_fns):
        try:
            hits += list(fn(ctx_d))
        except Exception:                                        # noqa: BLE001
            continue

    if weekly_fns and dates is not None:
        s = Series("", "", "", list(dates), o, h, l, c, v).weekly()
        if len(s) >= 40:
            ctx_w = Ctx(s.o, s.h, s.l, s.c, s.v,
                        recent=max(2, recent // 5), scale="W")
            for fn in dict.fromkeys(weekly_fns):
                try:
                    for hit in fn(ctx_w):
                        hit.notes["weekly_index"] = [hit.start, hit.end]
                        hit.notes["week_end"] = s.dates[min(hit.end, len(s) - 1)]
                        hits.append(hit)
                except Exception:                                # noqa: BLE001
                    continue

    if want:
        hits = [x for x in hits if x.pid in want]

    # 같은 종목에서 동일 패턴이 여러 번 잡히면 가장 완성도 높은 것만 남긴다
    best: Dict[str, Hit] = {}
    for x in hits:
        k = x.pid
        cur = best.get(k)
        rank = (x.status == "breakout", x.quality, x.end)
        if cur is None or rank > (cur.status == "breakout", cur.quality, cur.end):
            best[k] = x
    return list(best.values())
