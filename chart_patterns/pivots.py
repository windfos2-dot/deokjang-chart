# -*- coding: utf-8 -*-
"""
패턴 탐지용 기하 프리미티브.

Bulkowski 용어 정의(Glossary)를 그대로 따른다.
  - minor high : 좌우 최소 5봉 이상 떨어진 고점  (기본 sep=5)
  - minor low  : 좌우 최소 5봉 이상 떨어진 저점
  - confirmation(breakout) : 종가가 패턴 경계를 이탈하는 시점
  - measure rule : 패턴 높이를 돌파가에 가감한 목표가
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np


@dataclass(frozen=True)
class Pivot:
    i: int          # 봉 인덱스
    kind: str       # 'H' | 'L'
    p: float        # 가격 (고점이면 high, 저점이면 low)


# ------------------------------------------------------------------ pivots
def find_pivots(h: np.ndarray, l: np.ndarray, sep: int = 5) -> List[Pivot]:
    """좌우 sep봉 내 최고/최저인 봉을 minor high / minor low 로 잡는다."""
    n = len(h)
    out: List[Pivot] = []
    for i in range(n):
        a, b = max(0, i - sep), min(n, i + sep + 1)
        if h[i] >= h[a:b].max() and (i == 0 or h[i] > h[a:i].max(initial=-np.inf)):
            out.append(Pivot(i, "H", float(h[i])))
        if l[i] <= l[a:b].min() and (i == 0 or l[i] < l[a:i].min(initial=np.inf)):
            out.append(Pivot(i, "L", float(l[i])))
    out.sort(key=lambda p: (p.i, p.kind))
    return out


def alternate(pivs: Sequence[Pivot]) -> List[Pivot]:
    """H/L 이 번갈아 나오도록 정리 (연속 고점은 더 높은 쪽, 연속 저점은 더 낮은 쪽)."""
    out: List[Pivot] = []
    for p in pivs:
        if out and out[-1].kind == p.kind:
            keep = (p.p > out[-1].p) if p.kind == "H" else (p.p < out[-1].p)
            if keep:
                out[-1] = p
        else:
            out.append(p)
    return out


def zigzag(h: np.ndarray, l: np.ndarray, sep: int = 5) -> List[Pivot]:
    return alternate(find_pivots(h, l, sep))


# ----------------------------------------------------------------- 추세선
@dataclass(frozen=True)
class Line:
    slope: float
    intercept: float

    def at(self, x: float | np.ndarray):
        return self.slope * x + self.intercept


def fitline(xs: Sequence[float], ys: Sequence[float]) -> Optional[Line]:
    if len(xs) < 2:
        return None
    a, b = np.polyfit(np.asarray(xs, float), np.asarray(ys, float), 1)
    return Line(float(a), float(b))


def touches(line: Line, pts: Sequence[Pivot], tol: float) -> int:
    return sum(1 for p in pts if abs(p.p - line.at(p.i)) <= tol)


def drift(line: Line, n: int, ref: float) -> float:
    """추세선이 패턴 구간(n봉) 동안 움직인 폭 / 기준가 → 부호 있는 기울기(%)."""
    return (line.slope * n) / ref if ref else 0.0


# ----------------------------------------------------------------- 보조지표
def atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    out = np.empty_like(tr)
    out[:n] = tr[:n].mean() if n <= len(tr) else tr.mean()
    for i in range(n, len(tr)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def slope_pct(y: np.ndarray) -> float:
    """구간 선형회귀 기울기를 '전체 구간 변화율'로 환산 (거래량 추세 판정용)."""
    if len(y) < 3:
        return 0.0
    m = float(np.mean(y))
    if m <= 0:
        return 0.0
    a = np.polyfit(np.arange(len(y)), y, 1)[0]
    return float(a * len(y) / m)


def trend_before(c: np.ndarray, start: int, lookback: int = 60) -> float:
    """패턴 진입 직전 추세 강도(%). 양수=상승 추세 후 진입."""
    a = max(0, start - lookback)
    if start - a < 10:
        return 0.0
    return float((c[start] - c[a]) / c[a] * 100.0)


def rounded_r2(y: np.ndarray) -> float:
    """2차 곡선 적합도 (원형 바닥/천정, 컵 판정)."""
    if len(y) < 8:
        return 0.0
    x = np.arange(len(y), dtype=float)
    coef = np.polyfit(x, y, 2)
    pred = np.polyval(coef, x)
    ss_res = float(((y - pred) ** 2).sum())
    ss_tot = float(((y - y.mean()) ** 2).sum())
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0


def curvature(y: np.ndarray) -> float:
    """2차항 계수 부호/크기 (양수=U자, 음수=∩자)."""
    if len(y) < 8:
        return 0.0
    x = np.arange(len(y), dtype=float)
    return float(np.polyfit(x, y, 2)[0])


# -------------------------------------------------------------- Adam / Eve
def shape_class(h: np.ndarray, l: np.ndarray, atr_: np.ndarray,
                i: int, kind: str, width: int = 12) -> str:
    """Bulkowski 의 Adam(좁고 뾰족) / Eve(넓고 둥근) 분류.

    바닥(천정) 저점에서 0.25*진폭 위(아래) 레벨을 자르는 봉 수로 폭을 잰다.
    """
    a, b = max(0, i - width), min(len(l), i + width + 1)
    if kind == "L":
        base = float(l[i])
        span = float(h[a:b].max() - base)
        if span <= 0:
            return "eve"
        lvl = base + 0.25 * span
        cnt = int((l[a:b] <= lvl).sum())
        spike = (float(np.median(l[a:b])) - base) >= 1.2 * float(atr_[i])
    else:
        base = float(h[i])
        span = float(base - l[a:b].min())
        if span <= 0:
            return "eve"
        lvl = base - 0.25 * span
        cnt = int((h[a:b] >= lvl).sum())
        spike = (base - float(np.median(h[a:b]))) >= 1.2 * float(atr_[i])

    if cnt <= 4 or spike:
        return "adam"
    return "eve"


# ----------------------------------------------------------------- 돌파
def breakout_up(c: np.ndarray, level: float, frm: int) -> Optional[int]:
    idx = np.nonzero(c[frm:] > level)[0]
    return int(frm + idx[0]) if idx.size else None


def breakout_down(c: np.ndarray, level: float, frm: int) -> Optional[int]:
    idx = np.nonzero(c[frm:] < level)[0]
    return int(frm + idx[0]) if idx.size else None


def fib_near(value: float, targets: Sequence[float], tol: float = 0.06
             ) -> Optional[float]:
    """피보나치 비율 매칭 (하모닉 패턴용). 허용오차는 비율의 절대편차."""
    best, bd = None, 1e9
    for t in targets:
        d = abs(value - t)
        if d < bd:
            best, bd = t, d
    return best if bd <= tol * max(1.0, best or 1.0) else None
