# -*- coding: utf-8 -*-
"""
차트패턴 스크리너 — 국장/미장 전종목에서 '지금 잡히는' 패턴을 찾는다.

  python -m chart_patterns.scan --market KR --recent 10 --top 80
  python -m chart_patterns.scan --market US --pattern db_ee,cup_handle,triangle_asc
  python -m chart_patterns.scan --market ALL --html output/chart_patterns.html

옵션
  --recent N     돌파(또는 패턴 완성)가 최근 N봉 이내인 것만
  --pattern      쉼표구분 pid 필터 (미지정 시 전체)
  --direction    up | down
  --min-value    최근 20일 평균 거래대금 하한 (국장 원, 미장 달러)
  --min-score    종합점수 하한
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from . import detectors as D
from . import utf8_stdout
from . import registry as R
from .ohlcv import Series, load_series

OUT_DIR = Path(__file__).resolve().parent.parent / "output"

DEFAULT_MIN_VALUE = {"KR": 5e8, "US": 5e6}     # 국장 5억원 / 미장 500만달러
DEFAULT_MIN_PRICE = {"KR": 500.0, "US": 3.0}
# 시가총액 입력 단위: 국장은 억원, 미장은 백만달러 (각 시장에서 쓰는 감각 그대로)
CAP_UNIT = {"KR": 1e8, "US": 1e6}
CAP_LABEL = {"KR": "억원", "US": "백만$"}


def fmt_cap(cap: Optional[float], market: str) -> str:
    """시가총액을 읽기 좋은 단위로. (2,936,682백만$ → $2.94T)"""
    if not cap:
        return "-"
    if market == "KR":
        if cap >= 1e12:
            return f"{cap / 1e12:,.1f}조"
        return f"{cap / 1e8:,.0f}억"
    if cap >= 1e12:
        return f"${cap / 1e12:,.2f}T"
    if cap >= 1e9:
        return f"${cap / 1e9:,.1f}B"
    return f"${cap / 1e6:,.0f}M"


def market_regime(series: List[Series]) -> str:
    """유니버스 중앙값 기준 200일선 위/아래 → bull / bear."""
    vals = []
    for s in series:
        if len(s) < 210:
            continue
        ma = float(np.mean(s.c[-200:]))
        if ma > 0:
            vals.append(s.c[-1] / ma - 1.0)
    if not vals:
        return "bull"
    return "bull" if float(np.median(vals)) > 0 else "bear"


def scan_market(market: str, recent: int = 12,
                pids: Optional[List[str]] = None,
                direction: Optional[str] = None,
                min_value: Optional[float] = None,
                min_price: Optional[float] = None,
                min_score: float = 0.0,
                min_cap: float = 0.0,
                max_cap: float = 0.0,
                index: str = "",
                chart_top: int = 200,
                verbose: bool = True) -> dict:
    mv = DEFAULT_MIN_VALUE[market] if min_value is None else min_value
    mp = DEFAULT_MIN_PRICE[market] if min_price is None else min_price
    unit = CAP_UNIT[market]
    series = load_series(market, min_bars=150, min_price=mp, min_value=mv,
                         min_cap=min_cap * unit, max_cap=max_cap * unit,
                         index=index)
    if not series:
        return {"market": market, "hits": [], "universe": 0,
                "error": "데이터 없음 — ohlcv 동기화를 먼저 실행하세요"}

    regime = market_regime(series)
    if verbose:
        cap_note = ""
        if min_cap or max_cap:
            u = CAP_LABEL[market]
            cap_note = (f" / 시총 {min_cap:,.0f}~"
                        f"{max_cap:,.0f}{u}" if max_cap
                        else f" / 시총 {min_cap:,.0f}{u} 이상")
        print(f"  {market}: 유니버스 {len(series):,}종목 / 국면 {regime}{cap_note}")

    rows: List[dict] = []
    by_code: Dict[str, Series] = {s.code: s for s in series}
    for si, s in enumerate(series, 1):
        try:
            hits = D.run(s.o, s.h, s.l, s.c, s.v, dates=s.dates,
                         recent=recent, pids=pids)
        except Exception as e:                                    # noqa: BLE001
            if verbose:
                print(f"   ⚠ {s.code} 실패: {e}", file=sys.stderr)
            continue
        for h in hits:
            if direction and h.direction != direction:
                continue
            vol_ok = _volume_conforms(h)
            sc, det = R.score(h.pid, regime, h.direction, h.quality,
                              h.status, vol_ok)
            last = float(s.c[-1])
            # 이미 목표가를 지나친 셋업은 진입 관점에서 가치가 낮다 → 감점
            spent = bool(h.target and last and (
                (h.direction == "up" and h.target < last)
                or (h.direction == "down" and h.target > last)))
            if spent:
                sc = round(sc * 0.72, 1)
                h.notes["target_reached"] = True
            if sc < min_score:
                continue
            weekly = "weekly_index" in h.notes
            if weekly:                       # 주봉 히트는 주 마감일로 표기
                bo_date = h.notes.get("week_end") if h.breakout_i is not None \
                    else None
                d_start = d_end = h.notes.get("week_end")
            else:
                bo_date = (s.dates[h.breakout_i]
                           if h.breakout_i is not None else None)
                d_start, d_end = s.dates[h.start], s.dates[h.end]
            # 돌파 후 되돌림(throwback/pullback) 여부 — 진입 판단에 결정적
            lvl = (h.notes.get("trigger") or h.notes.get("neckline")
                   or h.notes.get("board_top") or h.notes.get("base")
                   or h.notes.get("cap"))
            bo_px = h.breakout_px
            since = round((last / bo_px - 1) * 100, 1) if bo_px else None
            back_inside = None
            if bo_px is not None and lvl:
                back_inside = ((last < float(lvl)) if h.direction == "up"
                               else (last > float(lvl)))
            rows.append({
                "market": market, "code": s.code, "name": s.name,
                "mktcap": s.mktcap,
                "mktcap_disp": (round(s.mktcap / CAP_UNIT[market], 1)
                                if s.mktcap else None),
                "cap_unit": CAP_LABEL[market],
                "mktcap_fmt": fmt_cap(s.mktcap, market),
                "pid": h.pid, "pattern_kr": R.name_kr(h.pid),
                "pattern_en": R.name_en(h.pid),
                "direction": h.direction, "status": h.status,
                "scale": "W" if weekly else "D",
                "start": d_start, "end": d_end,
                "breakout_date": bo_date,
                "bars": h.notes.get("bars", h.end - h.start + 1),
                "height_pct": round(h.height_pct, 2),
                "last": last,
                "target": round(h.target, 4) if h.target else None,
                "upside_pct": (round((h.target / last - 1) * 100, 1)
                               if h.target and last else None),
                "trigger": round(float(lvl), 4) if lvl else None,
                # 아직 형성중이면 '돌파 트리거까지 남은 거리'가 실제 관전 포인트
                "to_trigger_pct": (round((float(lvl) / last - 1) * 100, 1)
                                   if lvl and bo_px is None else None),
                "breakout_px": bo_px,
                "since_breakout_pct": since,
                "back_inside": back_inside,
                "quality": round(h.quality, 3),
                "score": sc,
                "book_perf": det["book_perf"], "book_fail": det["book_fail"],
                "book_rank": det["book_rank"],
                "notes": h.notes,
                "points": [[int(i), float(p)] for i, p in h.points],
            })
        if verbose and si % 400 == 0:
            print(f"   … {si}/{len(series)} 스캔, 히트 {len(rows):,}", flush=True)

    rows.sort(key=lambda r: (-r["score"], r["code"]))
    _attach_charts(rows[:chart_top], by_code)
    return {"market": market, "regime": regime, "universe": len(series),
            "recent": recent, "hits": rows,
            "generated": datetime.now().isoformat(timespec="seconds")}


def _attach_charts(rows: List[dict], by_code: Dict[str, Series],
                   pad: int = 12, max_span: int = 200) -> None:
    """리포트 미니차트용 종가 구간을 상위 히트에만 붙인다(JSON 비대화 방지).

    패턴 구간 + 여백만 잘라야 세로 스케일이 패턴에 맞춰진다.
    (전체 구간을 넣으면 과거 급등락 때문에 패턴이 납작해진다)
    """
    weekly_cache: Dict[str, Series] = {}
    for r in rows:
        s = by_code.get(r["code"])
        if s is None:
            continue
        if r.get("scale") == "W":            # 주봉 히트는 주봉 시계열로 그린다
            s = weekly_cache.get(r["code"]) or s.weekly()
            weekly_cache[r["code"]] = s
        pts = [int(p[0]) for p in r["points"]] or [len(s) - 1]
        pts = [p for p in pts if 0 <= p < len(s)] or [len(s) - 1]
        a = max(0, min(pts) - pad, len(s) - max_span)
        r["chart"] = {"offset": a,
                      "c": [round(float(x), 4) for x in s.c[a:len(s)]],
                      "level": r["notes"].get("neckline")}


def _volume_conforms(hit: D.Hit) -> bool:
    """책이 말하는 거래량 추세(대부분 '감소')와 실제가 맞는지."""
    vt = hit.notes.get("vol_trend")
    if not isinstance(vt, (int, float)):
        return False
    want = (R.snapshot(hit.pid).get("up" if hit.direction == "up" else "down", {})
            .get("volume trend", "")).lower()
    if "down" in want:
        return vt < -0.05
    if "up" in want:
        return vt > 0.05
    return False


# ------------------------------------------------------------------- 출력
def print_table(res: dict, top: int = 40) -> None:
    hits = res["hits"][:top]
    if not hits:
        print("   (해당 조건의 패턴 없음)")
        return
    print(f"\n  [{res['market']}] {res['universe']:,}종목 · 국면 {res['regime']} "
          f"· 최근 {res['recent']}봉 · 히트 {len(res['hits']):,}건 (상위 {len(hits)})")
    hdr = (f"  {'점수':>5} {'종목':<22} {'시총':>9} {'패턴':<18} {'방':<3} "
           f"{'상태':<9} {'높이%':>6} {'목표여력':>7} {'책평균':>7} {'실패율':>6} "
           f"{'순위':>4}")
    print(hdr)
    print("  " + "─" * (len(hdr) + 12))
    for r in hits:
        nm = (r["name"] or r["code"])[:20]
        arrow = "▲" if r["direction"] == "up" else "▼"
        bp = f"{r['book_perf']:+.0f}%" if r["book_perf"] is not None else "  -"
        bf = f"{r['book_fail']:.0f}%" if r["book_fail"] is not None else "  -"
        br = f"{int(r['book_rank'])}" if r["book_rank"] is not None else "-"
        up = f"{r['upside_pct']:+.0f}%" if r["upside_pct"] is not None else "  -"
        cap = r.get("mktcap_fmt") or "-"
        print(f"  {r['score']:>5.1f} {nm:<22} {cap:>9} {r['pattern_kr']:<18} "
              f"{arrow:<3} {r['status']:<9} {r['height_pct']:>6.1f} {up:>7} "
              f"{bp:>7} {bf:>6} {br:>4}")


def main() -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["KR", "US", "ALL"], default="KR")
    ap.add_argument("--recent", type=int, default=12)
    ap.add_argument("--pattern", default="")
    ap.add_argument("--direction", choices=["up", "down"])
    ap.add_argument("--min-value", type=float)
    ap.add_argument("--min-price", type=float)
    ap.add_argument("--min-score", type=float, default=0.0)
    ap.add_argument("--index", default="",
                    help="지수 구성종목만 (미장: russell3000)")
    ap.add_argument("--min-cap", type=float, default=0.0,
                    help="시가총액 하한 (국장 억원 / 미장 백만달러)")
    ap.add_argument("--max-cap", type=float, default=0.0,
                    help="시가총액 상한 (같은 단위)")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--chart-top", type=int, default=200)
    ap.add_argument("--json", default="")
    ap.add_argument("--html", default="")
    a = ap.parse_args()

    pids = [x.strip() for x in a.pattern.split(",") if x.strip()] or None
    if pids:
        unknown = [p for p in pids if p not in D.REGISTRY]
        if unknown:
            print(f"알 수 없는 패턴 id: {unknown}", file=sys.stderr)
            print("사용 가능:", ", ".join(sorted(D.REGISTRY)), file=sys.stderr)
            return 2

    markets = ["KR", "US"] if a.market == "ALL" else [a.market]
    results = []
    for m in markets:
        print(f"▶ {m} 스캔")
        res = scan_market(m, recent=a.recent, pids=pids, direction=a.direction,
                          min_value=a.min_value, min_price=a.min_price,
                          min_score=a.min_score, min_cap=a.min_cap,
                          max_cap=a.max_cap, index=a.index,
                          chart_top=a.chart_top)
        if res.get("error"):
            print(f"   {res['error']}", file=sys.stderr)
            continue
        print_table(res, a.top)
        results.append(res)

    if a.json:
        p = Path(a.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                     encoding="utf-8")
        print(f"\n  JSON → {p}")
    if a.html:
        from .report import write_html
        p = write_html(results, Path(a.html), top=a.top)
        print(f"  HTML → {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
