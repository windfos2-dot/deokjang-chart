# -*- coding: utf-8 -*-
"""
탐지기 pid ↔ 책(Statistics Summary) 통계 연결 + 히트 스코어링.

책은 시장국면(강세/약세) × 돌파방향(상승/하락) 4가지 조합별로
평균 수익률·손익분기 실패율·순위를 제공한다. 탐지된 패턴에 현재 국면과
돌파방향을 대입해 '이 패턴이 통계적으로 얼마나 쓸 만한가'를 점수화한다.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional, Tuple

HERE = Path(__file__).resolve().parent
BOOK_JSON = HERE / "book_patterns.json"

# pid → Statistics Summary 표기
STAT_KEY: Dict[str, str] = {
    "abcd_bull": "ab=cd bullish", "abcd_bear": "ab=cd bearish",
    "bat_bull": "bat bullish", "bat_bear": "bat bearish",
    "butterfly_bull": "butterfly bullish", "butterfly_bear": "butterfly bearish",
    "crab_bull": "crab bullish", "crab_bear": "crab bearish",
    "gartley_bull": "gartley bullish", "gartley_bear": "gartley bearish",
    "wolfe_bull": "wolfe wave bullish", "wolfe_bear": "wolfe wave bearish",
    "big_m": "big m", "big_w": "big w",
    "broadening_bottom": "broadening bottom", "broadening_top": "broadening top",
    "broadening_ra_ascending": "broadening formation right-angled and ascending",
    "broadening_ra_descending": "broadening formation right-angled and descending",
    "broadening_wedge_asc": "broadening wedge ascending",
    "broadening_wedge_desc": "broadening wedge descending",
    "burr_bottom": "bump-and-run reversal bottom",
    "burr_top": "bump-and-run reversal top",
    "cloudbank": "cloudbank",
    "cup_handle": "cup with handle", "cup_handle_inv": "cup with handle inverted",
    "diamond_bottom": "diamond bottom", "diamond_top": "diamond top",
    "diving_board": "diving board",
    "db_aa": "double bottom adam & adam", "db_ae": "double bottom adam & eve",
    "db_ea": "double bottom eve & adam", "db_ee": "double bottom eve & eve",
    "dt_aa": "double top adam & adam", "dt_ae": "double top adam & eve",
    "dt_ea": "double top eve & adam", "dt_ee": "double top eve & eve",
    "flag": "flag", "flag_high_tight": "flag high tight", "gap": "gap",
    "hs_bottom": "head-and-shoulders bottom",
    "hs_bottom_complex": "head-and-shoulders bottom complex",
    "hs_top": "head-and-shoulders top",
    "hs_top_complex": "head-and-shoulders top complex",
    "horn_bottom": "horn bottom", "horn_top": "horn top",
    "mm_down": "measured move down", "mm_up": "measured move up",
    "pennant": "pennant", "pipe_bottom": "pipe bottom", "pipe_top": "pipe top",
    "rectangle_bottom": "rectangle bottom", "rectangle_top": "rectangle top",
    "roof": "roof", "roof_inv": "roof inverted",
    "rounding_bottom": "rounding bottom", "rounding_top": "rounding top",
    "scallop_asc": "scallop ascending",
    "scallop_asc_inv": "scallop ascending and inverted",
    "scallop_desc": "scallop descending",
    "scallop_desc_inv": "scallop descending and inverted",
    "three_falling_peaks": "three falling peaks",
    "three_peaks_domed": "three peaks and domed house",
    "three_rising_valleys": "three rising valleys",
    "triangle_asc": "triangle ascending", "triangle_desc": "triangle descending",
    "triangle_sym": "triangle symmetrical",
    "triple_bottom": "triple bottom", "triple_top": "triple top",
    "v_bottom": "v bottom", "v_bottom_ext": "v bottom extended",
    "v_top": "v top", "v_top_ext": "v top extended",
    "wedge_falling": "wedge falling", "wedge_rising": "wedge rising",
    # 섬꼴반전은 책이 bottom/top 을 나눠 집계 → 방향으로 분기
    "island_reversal": "island bottom",
}
ISLAND_ALT = {"up": "island bottom", "down": "island top"}


@lru_cache(maxsize=1)
def book() -> dict:
    if not BOOK_JSON.exists():
        raise FileNotFoundError(
            f"{BOOK_JSON} 없음 — 먼저 `python chart_patterns/extract_book.py` 실행")
    return json.loads(BOOK_JSON.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def by_id() -> Dict[str, dict]:
    return {p["id"]: p for p in book()["patterns"]}


def name_kr(pid: str) -> str:
    return by_id().get(pid, {}).get("name_kr", pid)


def name_en(pid: str) -> str:
    return by_id().get(pid, {}).get("name_en", pid)


def stats(pid: str, direction: str = "up") -> Dict[str, float]:
    key = ISLAND_ALT.get(direction, STAT_KEY.get(pid)) if pid == "island_reversal" \
        else STAT_KEY.get(pid)
    return book()["stats_summary"].get(key or "", {}) if key else {}


def book_numbers(pid: str, regime: str, direction: str
                 ) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """(평균 수익률%, 손익분기 실패율%, 성과순위) — 없으면 None."""
    st = stats(pid, direction)
    suffix = f"{regime}_{direction}"
    perf = st.get(f"perf_{suffix}")
    fail = st.get(f"failrate_{suffix}")
    rank = st.get(f"rank_{suffix}")
    if perf is None:                       # 해당 국면 자료가 없으면 반대 국면 대용
        alt = "bear" if regime == "bull" else "bull"
        perf = st.get(f"perf_{alt}_{direction}")
        fail = fail if fail is not None else st.get(f"failrate_{alt}_{direction}")
        rank = rank if rank is not None else st.get(f"rank_{alt}_{direction}")
    return perf, fail, rank


def score(pid: str, regime: str, direction: str, quality: float,
          status: str, vol_ok: bool = False) -> Tuple[float, dict]:
    """0~100 종합점수. 책 통계 45% + 형태 적합도 35% + 확인도 20%."""
    perf, fail, rank = book_numbers(pid, regime, direction)

    if perf is None:                       # 책에 성과 자료가 없는 패턴(갭/깃발 등)
        book_part = 0.45
    else:
        mag = min(abs(perf) / 55.0, 1.0)
        rel = 1.0 - min((fail if fail is not None else 25.0) / 50.0, 1.0)
        book_part = 0.6 * mag + 0.4 * rel

    confirm = 1.0 if status == "breakout" else 0.5
    if vol_ok:
        confirm = min(1.0, confirm + 0.15)

    total = 100.0 * (0.45 * book_part + 0.35 * max(0.0, min(1.0, quality))
                     + 0.20 * confirm)
    detail = {"book_perf": perf, "book_fail": fail, "book_rank": rank,
              "book_part": round(book_part, 3), "confirm": confirm}
    return round(total, 1), detail


def identification(pid: str) -> Dict[str, str]:
    return by_id().get(pid, {}).get("identification", {})


def tactics(pid: str) -> Dict[str, str]:
    return by_id().get(pid, {}).get("trading_tactics", {})


def snapshot(pid: str) -> dict:
    return by_id().get(pid, {}).get("snapshot", {})


def coverage_report() -> None:
    """책 75개 중 탐지기가 구현된 패턴 현황."""
    from .detectors import REGISTRY
    impl = set(REGISTRY)
    allp = by_id()
    done = [p for p in allp if p in impl]
    todo = [p for p in allp if p not in impl]
    print(f"구현 {len(done)}/{len(allp)}")
    print("  미구현:", ", ".join(f"{p}({name_kr(p)})" for p in sorted(todo)))
