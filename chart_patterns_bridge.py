# -*- coding: utf-8 -*-
"""
chart_patterns_bridge.py — Bulkowski 패턴 탐지를 덕장차트 좌표계로 옮기는 어댑터.

`chart_patterns` 는 원래 스크리너다. '전종목 × 최근 N봉' 을 전제로 만들어졌고,
결과도 표(점수 순 랭킹)로 쓰는 걸 상정한다. 여기서는 반대 방향으로 쓴다 —
**종목 1개 × 지금 화면에 그려진 봉**에 대해 돌려서, 차트 위에 그릴 수 있는
좌표(날짜·가격)와 책 통계를 붙인다.

두 가지가 어댑터를 필요하게 만든다.

1. **좌표계** — 탐지기는 자기가 받은 배열의 정수 인덱스로 답한다. 프론트는
   lightweight-charts 라 시간축이 날짜다(패널 동기화를 논리 인덱스에서 시간
   기준으로 바꾼 뒤로는 더더욱). 그래서 인덱스를 전부 날짜로 환산한다.
   주봉 스케일 패턴(파이프·혼 등)은 인덱스가 *주봉* 배열 기준이라 같은 방식으로
   주봉 시계열을 다시 만들어 매핑해야 한다.

2. **국면(regime)** — 책 통계는 강세/약세장별로 다른 수치를 준다. 스크리너는
   유니버스 중앙값으로 국면을 판정하지만 여기엔 종목이 하나뿐이라, `/full` 이
   이미 받아둔 벤치마크 지수(KOSPI 등)의 200일선 위/아래로 대신한다.

`chart_patterns` 가 없어도 덕장차트는 그대로 떠야 한다 — import 실패는
`available()` False 로만 드러나고 예외를 올리지 않는다.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence

import numpy as np

try:
    from chart_patterns import detectors as D
    from chart_patterns import registry as R
    from chart_patterns.ohlcv import Series
    from chart_patterns.scan import _volume_conforms
    _IMPORT_ERROR: Optional[str] = None
except Exception as e:  # noqa: BLE001
    D = R = Series = None  # type: ignore[assignment]
    _volume_conforms = None  # type: ignore[assignment]
    _IMPORT_ERROR = f"{type(e).__name__}: {e}"


# 차트 오버레이의 기본값. 스크리너 기본(10)보다 넉넉하게 잡는다 — 스크리너는
# '오늘 사도 되는가'를 묻지만 차트는 '이 종목이 어떤 모양을 그려왔나'를 본다.
# 실측(2026-08-07): recent=20 이면 삼성전자에 히트가 1건뿐이라 화면이 비어 보이고,
# 60(약 3개월)이면 4건으로 오버레이가 의미 있게 찬다.
DEFAULT_RECENT = 60

# 탐지에 넣을 최대 봉 수. 20년(약 5,200봉) 화면을 통째로 돌리면 0.4초가 넘는데,
# 탐지기는 어차피 `recent` 봉 안에 앵커가 있는 패턴만 보고하고 패턴 하나의
# 최대 길이도 189봉(MAX_DUR)이라 그 앞은 계산해도 버려진다. 주봉 스케일
# 패턴까지 여유 있게 덮도록 1,500봉(≈6년 = 주봉 300개)으로 자른다.
MAX_DETECT_BARS = 1500


def available() -> bool:
    """모듈이 import 되고 **책 데이터도 있는지**.

    `book_patterns.json` 은 저작물이라 레포에 커밋하지 않는다(.gitignore).
    파일이 없으면 탐지기는 돌지만 패턴 이름·책 통계·점수를 못 만들어
    `registry.book()` 에서 FileNotFoundError 가 난다. 그 예외가 요청 경로
    한복판에서 터지지 않도록 여기서 미리 끊는다.
    """
    return D is not None and R.BOOK_JSON.exists()


def import_error() -> Optional[str]:
    if _IMPORT_ERROR:
        return _IMPORT_ERROR
    if R is not None and not R.BOOK_JSON.exists():
        return ("book_patterns.json 없음 — 저작물이라 레포에 포함하지 않는다. "
                "책 PDF 를 두고 `python chart_patterns/extract_book.py` 로 생성할 것")
    return None


def pattern_catalog() -> List[dict]:
    """구현된 패턴 목록 (프론트 필터용)."""
    if not available():
        return []
    return sorted(
        ({"pid": pid, "name_kr": R.name_kr(pid), "name_en": R.name_en(pid),
          "scale": D.SCALE.get(pid, "D")} for pid in D.REGISTRY),
        key=lambda x: x["name_kr"])


# --------------------------------------------------------------------------
# 내부 헬퍼
# --------------------------------------------------------------------------
def _jsonable(v):
    """numpy 스칼라·배열이 섞인 notes 를 JSON 직렬화 가능하게 만든다."""
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        f = float(v)
        return None if (f != f or f in (float("inf"), float("-inf"))) else round(f, 6)
    if isinstance(v, np.ndarray):
        return [_jsonable(x) for x in v.tolist()]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, float):
        return None if (v != v or v in (float("inf"), float("-inf"))) else round(v, 6)
    return v


def _regime(benchmark_close: Optional[Sequence[float]]) -> str:
    """벤치마크 지수의 200일선 위/아래 → bull / bear.

    스크리너는 유니버스 중앙값으로 재지만 여기선 종목이 하나뿐이다. 벤치마크가
    없으면 스크리너와 같은 기본값(bull)을 쓴다 — 국면 자료가 없을 때 책 통계는
    반대 국면 값으로 대체되므로 점수가 통째로 무의미해지진 않는다.
    """
    if benchmark_close is None:
        return "bull"
    arr = np.asarray([x for x in benchmark_close if x is not None], dtype=float)
    if len(arr) < 210:
        return "bull"
    ma = float(np.mean(arr[-200:]))
    return "bull" if ma > 0 and float(arr[-1]) > ma else "bear"


def _date_at(dates: Sequence[str], i: Optional[int]) -> Optional[str]:
    if i is None or not dates:
        return None
    return dates[max(0, min(int(i), len(dates) - 1))]


# --------------------------------------------------------------------------
# 본체
# --------------------------------------------------------------------------
def detect(dates: Sequence[str], open_, high, low, close, volume,
           tf: str = "D", recent: int = DEFAULT_RECENT,
           pids: Optional[Sequence[str]] = None,
           benchmark_close: Optional[Sequence[float]] = None) -> dict:
    """화면에 그려진 봉에 대해 Bulkowski 패턴을 탐지해 오버레이용으로 반환.

    tf 는 호출부가 이미 재집계해서 넘긴 봉의 단위다(D/W/M). 일봉일 때만
    탐지기 내부의 주봉 재집계를 허용한다 — 주봉 화면에서 또 주봉으로 묶으면
    사실상 월봉이 되어 화면의 봉과 좌표가 어긋난다.
    """
    if not available():
        return {"available": False, "reason": import_error(), "hits": []}

    o = np.asarray(open_, dtype=float)
    h = np.asarray(high, dtype=float)
    lo = np.asarray(low, dtype=float)
    c = np.asarray(close, dtype=float)
    v = np.asarray(volume, dtype=float)
    if len(c) < 60:
        return {"available": False, "reason": f"봉 부족({len(c)}개, 60개 이상 필요)",
                "hits": []}

    # 뒤쪽만 잘라 쓴다. 자른 배열 기준으로 인덱스를 날짜로 되돌리므로 좌표는
    # 그대로 맞는다 (dates 도 같이 잘라야 한다는 뜻).
    if len(c) > MAX_DETECT_BARS:
        k = -MAX_DETECT_BARS
        o, h, lo, c, v = o[k:], h[k:], lo[k:], c[k:], v[k:]
        dates = list(dates)[k:]
    n = len(c)

    regime = _regime(benchmark_close)

    # 일봉일 때만 dates 를 넘긴다. dates=None 이면 detectors.run 이 주봉 탐지기를
    # 건너뛴다 — 주/월봉 화면에서는 이미 집계된 봉을 그대로 일봉처럼 다룬다.
    try:
        hits = D.run(o, h, lo, c, v,
                     dates=list(dates) if tf == "D" else None,
                     recent=int(recent), pids=list(pids) if pids else None)
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": f"탐지 실패: {e}", "hits": []}

    # 주봉 스케일 히트가 있으면 좌표 환산용 주봉 시계열을 한 번만 만든다.
    wk_dates: Optional[List[str]] = None
    if any("weekly_index" in x.notes for x in hits):
        try:
            wk_dates = Series("", "", "", list(dates), o, h, lo, c, v).weekly().dates
        except Exception:  # noqa: BLE001
            wk_dates = None

    last = float(c[-1])
    rows: List[dict] = []
    for x in hits:
        weekly = "weekly_index" in x.notes
        axis = wk_dates if (weekly and wk_dates) else list(dates)

        # 돌파선(트리거) — 패턴마다 이름이 달라 스크리너와 같은 순서로 고른다.
        lvl = (x.notes.get("trigger") or x.notes.get("neckline")
               or x.notes.get("board_top") or x.notes.get("base")
               or x.notes.get("cap"))
        lvl = float(lvl) if isinstance(lvl, (int, float, np.floating)) else None

        # 측정룰은 `트리거 ± 패턴높이` 라서, 높이가 가격보다 큰 패턴(장기 원형천정
        # 등)에서는 목표가가 0 이하로 내려간다. 실측: SK하이닉스 범프앤런 반전
        # 천정 -1,193,000. 가격으로 성립하지 않는 값이므로 '목표 없음' 으로 본다.
        # (같은 값이 스크리너 표의 목표여력 칼럼에도 그대로 나온다)
        target = float(x.target) if x.target else None
        if target is not None and target <= 0:
            target = None

        vol_ok = bool(_volume_conforms(x))
        sc, det = R.score(x.pid, regime, x.direction, x.quality, x.status, vol_ok)

        # 이미 목표가를 지나친 셋업은 진입 관점에서 가치가 낮다 → 스크리너와 동일 감점
        spent = bool(target and last and (
            (x.direction == "up" and target < last)
            or (x.direction == "down" and target > last)))
        if spent:
            sc = round(sc * 0.72, 1)

        bo_px = float(x.breakout_px) if x.breakout_px is not None else None
        rows.append({
            "pid": x.pid,
            "name_kr": R.name_kr(x.pid),
            "name_en": R.name_en(x.pid),
            "direction": x.direction,
            "status": x.status,                       # forming | breakout
            "scale": "W" if weekly else tf,
            "start": _date_at(axis, x.start),
            "end": _date_at(axis, x.end),
            "breakout_date": _date_at(axis, x.breakout_i)
                             if x.breakout_i is not None else None,
            "breakout_px": bo_px,
            # 외곽선 — 프론트가 그대로 이어 그린다
            "points": [[_date_at(axis, i), round(float(p), 4)]
                       for i, p in x.points if _date_at(axis, i)],
            "trigger": round(lvl, 4) if lvl else None,
            "target": round(target, 4) if target else None,
            "upside_pct": (round((target / last - 1) * 100, 1)
                           if target and last else None),
            "to_trigger_pct": (round((lvl / last - 1) * 100, 1)
                               if lvl and bo_px is None else None),
            "since_breakout_pct": (round((last / bo_px - 1) * 100, 1)
                                   if bo_px else None),
            # 돌파 후 되돌림 — 진입 판단에 결정적이라 스크리너와 같이 계산한다
            "back_inside": (((last < lvl) if x.direction == "up" else (last > lvl))
                            if (bo_px is not None and lvl) else None),
            "target_reached": spent,
            "height_pct": round(float(x.height_pct), 2),
            "bars": int(x.notes.get("bars", x.end - x.start + 1)),
            "quality": round(float(x.quality), 3),
            "vol_conforms": vol_ok,
            "score": sc,
            "book": {"perf": det["book_perf"], "fail": det["book_fail"],
                     "rank": det["book_rank"], "regime": regime},
            "notes": _jsonable(x.notes),
        })

    rows.sort(key=lambda r: (-r["score"], r["pid"]))
    return {"available": True, "regime": regime, "recent": int(recent),
            "tf": tf, "bars": n, "count": len(rows), "hits": rows}


def pattern_detail(pid: str) -> dict:
    """책의 식별규칙·매매전술 원문 (패턴 클릭 시 참고용)."""
    if not available():
        return {"available": False, "reason": import_error()}
    p = R.by_id().get(pid)
    if p is None:
        return {"available": False, "reason": f"알 수 없는 패턴: {pid}"}
    return {
        "available": True, "pid": pid,
        "name_kr": p["name_kr"], "name_en": p["name_en"],
        "chapter": p.get("chapter"),
        "snapshot": p.get("snapshot", {}),
        "identification": p.get("identification", {}),
        "trading_tactics": p.get("trading_tactics", {}),
        "stats": R.stats(pid),
    }
