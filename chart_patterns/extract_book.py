# -*- coding: utf-8 -*-
"""
Encyclopedia of Chart Patterns (Bulkowski, 3rd ed.) → 구조화 JSON.

책의 75개 패턴 챕터는 포맷이 일정하다.
  - 챕터 첫 페이지: RESULTS SNAPSHOT (상승/하락 돌파별 성과 스냅샷)
  - Table x.1  : Identification Guidelines  (식별 규칙 = 탐지기 사양)
  - Table x.2~9: 통계 테이블
  - Table x.10 : Trading Tactics (measure rule 등)
  - 권말 Statistics Summary: 패턴 간 순위/평균 성과 비교표

본 스크립트는 이 구조를 그대로 뜯어 book_patterns.json 으로 떨군다.
탐지 로직(detectors.py)은 이 JSON의 통계를 스코어링에 사용한다.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF

try:                                    # 패키지로 실행 (python -m chart_patterns.extract_book)
    from . import utf8_stdout
except ImportError:                     # 스크립트로 직접 실행
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from chart_patterns import utf8_stdout

HERE = Path(__file__).resolve().parent
OUT_JSON = HERE / "book_patterns.json"

PDF_NAME = "Encyclopedia_of_Chart_Patterns_Thomas_N_Bulkowski_z_lib_org.pdf"


def find_pdf(explicit: Optional[str] = None) -> Optional[Path]:
    """PDF 위치 탐색: 인자 → $BULKOWSKI_PDF → 모듈 폴더 → 홈 하위 흔한 경로.

    book_patterns.json 이 이미 있으면 PDF 는 필요 없다(다른 PC로 옮길 때 핵심).
    """
    if explicit:
        return Path(explicit)
    env = os.environ.get("BULKOWSKI_PDF")
    if env:
        return Path(env)
    home = Path.home()
    for cand in (HERE / PDF_NAME,
                 HERE.parent / PDF_NAME,
                 home / "Documents" / "카카오톡 받은 파일" / PDF_NAME,
                 home / "Downloads" / PDF_NAME,
                 home / "Documents" / PDF_NAME):
        if cand.exists():
            return cand
    return None

NL = chr(10)

SNAPSHOT_KEYS = {
    "reversal or continuation",
    "performance rank",
    "breakeven failure rate",
    "average rise",
    "average drop",
    "volume trend",
    "throwbacks",
    "pullbacks",
    "percentage meeting price target",
    "see also",
}

# 챕터 영문명 → (내부 id, 한글명)
PATTERN_META: Dict[str, Tuple[str, str]] = {
    "AB=CD, Bearish": ("abcd_bear", "AB=CD 하락형"),
    "AB=CD, Bullish": ("abcd_bull", "AB=CD 상승형"),
    "Bat, Bearish": ("bat_bear", "배트 하락형"),
    "Bat, Bullish": ("bat_bull", "배트 상승형"),
    "Big M": ("big_m", "빅 M"),
    "Big W": ("big_w", "빅 W"),
    "Broadening Bottoms": ("broadening_bottom", "확대형 바닥"),
    "Broadening Formation, Right-Angled and Ascending": (
        "broadening_ra_ascending", "직각 확대형 상승"),
    "Broadening Formation, Right-Angled and Descending": (
        "broadening_ra_descending", "직각 확대형 하락"),
    "Broadening Tops": ("broadening_top", "확대형 천정"),
    "Broadening Wedge, Ascending": ("broadening_wedge_asc", "확대 쐐기 상승"),
    "Broadening Wedge, Descending": ("broadening_wedge_desc", "확대 쐐기 하락"),
    "Bump-and-Run Reversal, Bottom": ("burr_bottom", "범프앤런 반전 바닥"),
    "Bump-and-Run Reversal, Top": ("burr_top", "범프앤런 반전 천정"),
    "Butterfly, Bearish": ("butterfly_bear", "버터플라이 하락형"),
    "Butterfly, Bullish": ("butterfly_bull", "버터플라이 상승형"),
    "Cloudbanks": ("cloudbank", "클라우드뱅크"),
    "Crab, Bearish": ("crab_bear", "크랩 하락형"),
    "Crab, Bullish": ("crab_bull", "크랩 상승형"),
    "Cup with Handle": ("cup_handle", "손잡이 달린 컵"),
    "Cup with Handle, Inverted": ("cup_handle_inv", "역 손잡이 컵"),
    "Diamond Bottoms": ("diamond_bottom", "다이아몬드 바닥"),
    "Diamond Tops": ("diamond_top", "다이아몬드 천정"),
    "Diving Board": ("diving_board", "다이빙 보드"),
    "Double Bottoms, Adam & Adam": ("db_aa", "이중바닥 아담&아담"),
    "Double Bottoms, Adam & Eve": ("db_ae", "이중바닥 아담&이브"),
    "Double Bottoms, Eve & Adam": ("db_ea", "이중바닥 이브&아담"),
    "Double Bottoms, Eve & Eve": ("db_ee", "이중바닥 이브&이브"),
    "Double Tops, Adam & Adam": ("dt_aa", "이중천정 아담&아담"),
    "Double Tops, Adam & Eve": ("dt_ae", "이중천정 아담&이브"),
    "Double Tops, Eve & Adam": ("dt_ea", "이중천정 이브&아담"),
    "Double Tops, Eve & Eve": ("dt_ee", "이중천정 이브&이브"),
    "Flags": ("flag", "깃발형"),
    "Flags, High and Tight": ("flag_high_tight", "고가밀집 깃발"),
    "Gaps": ("gap", "갭"),
    "Gartley, Bearish": ("gartley_bear", "가틀리 하락형"),
    "Gartley, Bullish": ("gartley_bull", "가틀리 상승형"),
    "Head-and-Shoulders Bottoms": ("hs_bottom", "역헤드앤숄더"),
    "Head-and-Shoulders Bottoms, Complex": ("hs_bottom_complex", "복합 역헤드앤숄더"),
    "Head-and-Shoulders Tops": ("hs_top", "헤드앤숄더"),
    "Head-and-Shoulders Tops, Complex": ("hs_top_complex", "복합 헤드앤숄더"),
    "Horn Bottoms": ("horn_bottom", "혼 바닥(주봉)"),
    "Horn Tops": ("horn_top", "혼 천정(주봉)"),
    "Island Reversals": ("island_reversal", "섬꼴 반전"),
    "Measured Move Down": ("mm_down", "측정된 하락"),
    "Measured Move Up": ("mm_up", "측정된 상승"),
    "Pennants": ("pennant", "페넌트"),
    "Pipe Bottoms": ("pipe_bottom", "파이프 바닥(주봉)"),
    "Pipe Tops": ("pipe_top", "파이프 천정(주봉)"),
    "Rectangle Bottoms": ("rectangle_bottom", "직사각형 바닥"),
    "Rectangle Tops": ("rectangle_top", "직사각형 천정"),
    "Roof": ("roof", "루프"),
    "Roof, Inverted": ("roof_inv", "역루프"),
    "Rounding Bottoms": ("rounding_bottom", "원형 바닥"),
    "Rounding Tops": ("rounding_top", "원형 천정"),
    "Scallops, Ascending": ("scallop_asc", "상승 스캘럽"),
    "Scallops, Ascending and Inverted": ("scallop_asc_inv", "역 상승 스캘럽"),
    "Scallops, Descending": ("scallop_desc", "하락 스캘럽"),
    "Scallops, Descending and Inverted": ("scallop_desc_inv", "역 하락 스캘럽"),
    "Three Falling Peaks": ("three_falling_peaks", "세 개의 하락 봉우리"),
    "Three Peaks and Domed House": ("three_peaks_domed", "세 봉우리와 돔 하우스"),
    "Three Rising Valleys": ("three_rising_valleys", "세 개의 상승 골"),
    "Triangles, Ascending": ("triangle_asc", "상승 삼각형"),
    "Triangles, Descending": ("triangle_desc", "하락 삼각형"),
    "Triangles, Symmetrical": ("triangle_sym", "대칭 삼각형"),
    "Triple Bottoms": ("triple_bottom", "삼중바닥"),
    "Triple Tops": ("triple_top", "삼중천정"),
    "V-Bottoms": ("v_bottom", "V자 바닥"),
    "V-Bottoms, Extended": ("v_bottom_ext", "확장 V자 바닥"),
    "V-Tops": ("v_top", "V자 천정"),
    "V-Tops, Extended": ("v_top_ext", "확장 V자 천정"),
    "Wedges, Falling": ("wedge_falling", "하락 쐐기"),
    "Wedges, Rising": ("wedge_rising", "상승 쐐기"),
    "Wolfe Wave, Bearish": ("wolfe_bear", "울프웨이브 하락형"),
    "Wolfe Wave, Bullish": ("wolfe_bull", "울프웨이브 상승형"),
}

DASHES = ("\u2013", "\u2014", "\u2212")


def _clean(s: str) -> str:
    s = s.replace("\u00ae", "").replace("\u00a0", " ")
    s = s.replace("\u2019", "'").replace("\u2018", "'")
    s = s.replace("\u201c", '"').replace("\u201d", '"')
    for d in DASHES:
        s = s.replace(d, "-")
    return re.sub(r"\s+", " ", s).strip()


def _chapter_title(raw: str) -> str:
    """'Chapter 64 Triangles, Ascending' → 'Triangles, Ascending'"""
    t = re.sub(r"^Chapter\s+\d+\s*", "", raw)
    t = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", t)   # 목차 줄바꿈 유실 복구
    return _clean(t)


def _lines(page) -> List[dict]:
    out = []
    for blk in page.get_text("dict")["blocks"]:
        if blk["type"] != 0:
            continue
        for ln in blk["lines"]:
            txt = "".join(sp["text"] for sp in ln["spans"])
            if not txt.strip():
                continue
            sp0 = ln["spans"][0]
            out.append({
                "text": txt,
                "x0": round(ln["bbox"][0], 1),
                "y0": round(ln["bbox"][1], 1),
                "size": round(sp0["size"], 1),
                "font": sp0["font"],
            })
    out.sort(key=lambda r: (r["y0"], r["x0"]))
    return out


# ---------------------------------------------------------------- snapshot
def parse_snapshot(page) -> dict:
    """챕터 첫 페이지의 RESULTS SNAPSHOT 파싱."""
    snap = {"appearance": "", "up": {}, "down": {}}
    section: Optional[str] = None
    pending: Optional[str] = None
    target: Dict[str, str] = {}

    for r in _lines(page):
        t = _clean(r["text"])
        flat = t.lower()
        if flat.startswith("appearance:"):
            snap["appearance"] = t.split(":", 1)[1].strip()
            continue
        if "upward breakouts" in flat:
            section, target, pending = "up", snap["up"], None
            continue
        if "downward breakouts" in flat:
            section, target, pending = "down", snap["down"], None
            continue
        if section is None:
            continue
        if r["x0"] < 200:                        # 좌측 = 라벨
            key = flat.rstrip(":")
            if key in SNAPSHOT_KEYS:
                pending = key
                target.setdefault(key, "")
        elif pending:                            # 우측 = 값(여러 줄 가능)
            target[pending] = (target[pending] + " " + t).strip()
    return snap


# ------------------------------------------------------------------ tables
def _column_split(rows: List[dict]) -> List[float]:
    """표 라인들의 x0 을 클러스터링해서 컬럼 앵커 추출."""
    xs = sorted({r["x0"] for r in rows})
    anchors: List[float] = []
    for x in xs:
        if not anchors or x - anchors[-1] > 14:
            anchors.append(x)
    return anchors


def _rows_to_records(rows: List[dict]) -> List[List[str]]:
    anchors = _column_split(rows)
    by_y: Dict[float, List[dict]] = {}
    for r in rows:
        slot = next((y for y in by_y if abs(y - r["y0"]) < 3), r["y0"])
        by_y.setdefault(slot, []).append(r)

    recs: List[List[str]] = []
    for y in sorted(by_y):
        cells = [""] * len(anchors)
        indents: List[float] = []
        for r in sorted(by_y[y], key=lambda z: z["x0"]):
            ci = min(range(len(anchors)), key=lambda i: abs(anchors[i] - r["x0"]))
            cells[ci] = (cells[ci] + " " + _clean(r["text"])).strip()
            indents.append(r["x0"] - anchors[ci])
        # 들여쓰기된 줄(= 셀 내부 줄바꿈)이거나 첫 칸이 비면 앞 행의 연속
        cont = bool(recs) and (not cells[0] or min(indents, default=0) > 4)
        if cont and any(cells):
            for i, c in enumerate(cells):
                if c:
                    recs[-1][i] = (recs[-1][i] + " " + c).strip()
        else:
            recs.append(cells)
    return [r for r in recs if any(r)]


def parse_tables(doc, first: int, last: int, chap: int) -> Dict[str, dict]:
    """챕터 내 Table {chap}.N 전부 추출."""
    tables: Dict[str, dict] = {}
    cur_key: Optional[str] = None
    cur_rows: List[dict] = []

    def flush():
        nonlocal cur_key, cur_rows
        if cur_key and cur_rows:
            tables[cur_key]["rows"] = _rows_to_records(cur_rows)
        cur_key, cur_rows = None, []

    for pno in range(first - 1, last - 1):
        for r in _lines(doc[pno]):
            t = _clean(r["text"])
            if re.fullmatch(rf"Table {chap}\.(\d+)", t):
                flush()
                cur_key = t.rsplit(".", 1)[1]
                tables[cur_key] = {"caption": "", "rows": []}
                continue
            if cur_key is None:
                continue
            if not tables[cur_key]["caption"]:
                tables[cur_key]["caption"] = t
                continue
            # 표 본문은 StoneSans 9pt. 다른 폰트가 나오면 표 종료.
            if r["font"].startswith("StoneSans") and r["size"] <= 9.5:
                cur_rows.append(r)
            elif cur_rows:
                flush()
    flush()
    return tables


def _as_kv(rows: List[List[str]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for r in rows:
        cells = [c for c in r if c]
        if len(cells) >= 2:
            out[cells[0]] = " ".join(cells[1:]).strip()
    for junk in ("Characteristic", "Description", "Explanation",
                 "Trading Tactic"):
        out.pop(junk, None)
    return out


# ------------------------------------------------------- statistics summary
COLS = ("bull_up", "bear_up", "bull_down", "bear_down")

# Statistics Summary 가 쓰는 패턴 표기(정규화형). 데이터가 없는 패턴의 라벨이
# 다음 행에 흡수되므로, 원시 라벨의 '끝'을 이 목록과 맞춰 소유자를 되찾는다.
CANON = [
    "ab=cd bearish", "ab=cd bullish", "bat bearish", "bat bullish",
    "big m", "big w", "broadening bottom",
    "broadening formation right-angled and ascending",
    "broadening formation right-angled and descending", "broadening top",
    "broadening wedge ascending", "broadening wedge descending",
    "bump-and-run reversal bottom", "bump-and-run reversal top",
    "butterfly bearish", "butterfly bullish", "cloudbank",
    "crab bearish", "crab bullish", "cup with handle",
    "cup with handle inverted", "diamond bottom", "diamond top",
    "diving board",
    "double bottom adam & adam", "double bottom adam & eve",
    "double bottom eve & adam", "double bottom eve & eve",
    "double top adam & adam", "double top adam & eve",
    "double top eve & adam", "double top eve & eve",
    "flag high tight", "flag", "gap", "gartley bearish", "gartley bullish",
    "head-and-shoulders bottom complex", "head-and-shoulders bottom",
    "head-and-shoulders top complex", "head-and-shoulders top",
    "horn bottom", "horn top", "island bottom", "island top",
    "measured move down", "measured move up", "pennant",
    "pipe bottom", "pipe top", "rectangle bottom", "rectangle top",
    "roof inverted", "roof", "rounding bottom", "rounding top",
    "scallop ascending and inverted", "scallop ascending",
    "scallop descending and inverted", "scallop descending",
    "three falling peaks", "three peaks and domed house",
    "three rising valleys", "triangle ascending", "triangle descending",
    "triangle symmetrical", "triple bottom", "triple top",
    "v bottom extended", "v bottom", "v top extended", "v top",
    "wedge falling", "wedge rising", "wolfe wave bearish", "wolfe wave bullish",
]
CANON.sort(key=len, reverse=True)          # 긴 이름 우선 매칭


def _norm_label(s: str) -> str:
    s = _clean(s).lower().replace("*", "")
    s = s.replace(",", " ").replace("(", " ").replace(")", " ")
    return re.sub(r"\s+", " ", s).strip()


def _canon(raw: str) -> Optional[str]:
    """원시 라벨의 꼬리를 정규 패턴명으로 되돌린다."""
    s = _norm_label(raw)
    for cand in CANON:
        if s == cand or s.endswith(" " + cand) or s.endswith(" " + cand + "s") \
                or s == cand + "s":
            return cand
    return None

_STAT_NOISE = re.compile(
    r"^(statistics summary|description|bull market,?|bear market,?|bear|"
    r"market,|up breakout|down breakout|breakout|market, up|market, down|"
    r"\(continued\)|max rank|average for patterns|average rise|average drop|"
    r"failures|these (have|use).*|and are not comparable.*|scale.*|"
    r"\*.*|\d{3,4})$", re.I)


def _stat_rows(lines: List[str], numeric: str) -> Dict[str, List[float]]:
    """'라벨 ... 숫자들' 형태의 요약표를 {라벨: [숫자...]} 로 변환."""
    if numeric == "pct":
        num_re = re.compile(r"-?\d+(?:\.\d+)?%")
        def keep(v: float) -> bool:
            return True
    else:                                           # 순위(1~99 정수)
        num_re = re.compile(r"\b\d{1,2}\b")
        def keep(v: float) -> bool:
            return 0 < v <= 99

    out: Dict[str, List[float]] = {}
    label: List[str] = []
    nums: List[float] = []

    def flush():
        nonlocal label, nums
        if label and nums:
            key = _canon(" ".join(label))
            if key:
                out.setdefault(key, list(nums))
        label, nums = [], []

    for raw in lines:
        line = _clean(raw)
        if not line or _STAT_NOISE.match(line):
            continue
        found = [v for v in (float(x.rstrip("%")) for x in num_re.findall(line))
                 if keep(v)]
        text = num_re.sub("", line).strip(" .,")
        if text and nums:                # 새 라벨 시작 → 직전 행 확정
            flush()
        if text:
            label.append(text)
        nums += found
    flush()
    return out


def parse_stats_summary(doc) -> Dict[str, dict]:
    """권말 Statistics Summary → 패턴별 성과/실패율/순위 (컬럼 정렬 포함)."""
    lines: List[str] = []
    for i in range(1209, 1223):
        lines += doc[i].get_text().split(NL)

    sections: Dict[str, List[str]] = {}
    cur: Optional[str] = None
    for ln in lines:
        low = _clean(ln).lower()
        if low.startswith("alphabetical list, performance"):
            cur = "performance"
        elif low.startswith("alphabetical, performance rank"):
            cur = "perf_rank"
        elif low.startswith("alphabetical list, failure rates"):
            cur = "failure"
        elif low.startswith("alphabetical, failure rate rank"):
            cur = "fail_rank"
        elif low.startswith("top ten"):
            cur = None
        if cur:
            sections.setdefault(cur, []).append(ln)

    perf = _stat_rows(sections.get("performance", []), "pct")
    fail = _stat_rows(sections.get("failure", []), "pct")
    prank = _stat_rows(sections.get("perf_rank", []), "rank")
    frank = _stat_rows(sections.get("fail_rank", []), "rank")

    # 성과표는 부호(+상승돌파 / -하락돌파)로 컬럼 위치가 확정된다.
    # 순위표·실패율표는 부호가 없으므로 성과표의 컬럼 배치를 재사용한다.
    layout: Dict[str, List[str]] = {}
    out: Dict[str, dict] = {}
    for name, vals in perf.items():
        ups = [v for v in vals if v > 0]
        downs = [v for v in vals if v < 0]
        cols = list(COLS[:len(ups)]) + list(COLS[2:2 + len(downs)])
        layout[name] = cols
        out[name] = {f"perf_{c}": v for c, v in zip(cols, ups + downs)}

    def attach(src: Dict[str, List[float]], field: str) -> None:
        for name, vals in src.items():
            cols = layout.get(name)
            if not cols or len(vals) != len(cols):
                cols = list(COLS[:len(vals)])
            rec = out.setdefault(name, {})
            for c, v in zip(cols, vals):
                rec[f"{field}_{c}"] = v

    attach(fail, "failrate")
    attach(prank, "rank")
    attach(frank, "failrank")
    return out


# --------------------------------------------------------------------- main
def extract(pdf_path: Path) -> dict:
    doc = fitz.open(pdf_path)
    toc = [t for t in doc.get_toc() if t[0] == 1 and t[1].startswith("Chapter")]
    stats = parse_stats_summary(doc)

    patterns = []
    unknown = []
    for i, (_lvl, raw, pg) in enumerate(toc):
        chap = int(re.match(r"Chapter\s+(\d+)", raw).group(1))
        if chap == 1:                                   # 트레이딩 개론 챕터
            continue
        end = toc[i + 1][2] if i + 1 < len(toc) else 1210
        title = _chapter_title(raw)
        meta = PATTERN_META.get(title)
        if meta is None:
            unknown.append(title)
            meta = (re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_"), title)
        pid, kr = meta

        tables = parse_tables(doc, pg, end, chap)
        ident_k = next((k for k, v in tables.items()
                        if v["caption"].startswith("Identification")), "1")
        tactic_k = next((k for k, v in tables.items()
                         if v["caption"].startswith("Trading Tactic")), "10")
        patterns.append({
            "chapter": chap,
            "id": pid,
            "name_en": title,
            "name_kr": kr,
            "pdf_pages": [pg, end - 1],
            "snapshot": parse_snapshot(doc[pg - 1]),
            "identification": _as_kv(tables.get(ident_k, {}).get("rows", [])),
            "trading_tactics": _as_kv(tables.get(tactic_k, {}).get("rows", [])),
            "tables": {k: {"caption": v["caption"], "rows": v["rows"]}
                       for k, v in tables.items()},
        })

    if unknown:
        print("[warn] 메타 미등록 챕터:", unknown, file=sys.stderr)

    return {
        "source": "Bulkowski, Encyclopedia of Chart Patterns, 3rd ed.",
        "pattern_count": len(patterns),
        "stats_summary": stats,
        "patterns": patterns,
    }


def main() -> int:
    utf8_stdout()
    pdf = find_pdf(sys.argv[1] if len(sys.argv) > 1 else None)
    if pdf is None or not pdf.exists():
        have = "있음" if OUT_JSON.exists() else "없음"
        print(NL.join([
            "PDF 를 찾지 못했습니다.",
            "  - 경로를 인자로:    python -m chart_patterns.extract_book <PDF경로>",
            "  - 환경변수로 지정:  BULKOWSKI_PDF=<PDF경로>",
            f"  - 또는 {HERE} 에 {PDF_NAME} 을 두세요.",
            f"참고: book_patterns.json 이 이미 있으면 이 단계는 불필요 (현재 {have}).",
        ]), file=sys.stderr)
        return 1
    data = extract(pdf)
    OUT_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    ok = sum(1 for p in data["patterns"] if p["identification"])
    tac = sum(1 for p in data["patterns"] if p["trading_tactics"])
    print(f"패턴 {data['pattern_count']}개 → {OUT_JSON}")
    print(f"  식별규칙 {ok}/{data['pattern_count']}, 매매전술 {tac}/{data['pattern_count']}")
    print(f"  통계요약 {len(data['stats_summary'])}행")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
