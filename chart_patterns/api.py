# -*- coding: utf-8 -*-
"""
차트패턴 스크리너 HTTP API.

다른 PC에 모듈을 깔지 않고 이 서버만 호출해서 쓰기 위한 것.
시세 DB와 스캔 연산이 서버 한 곳에만 있으면 되므로, 클라이언트는 curl 이면 충분하다.

  uvicorn chart_patterns.api:app --host 0.0.0.0 --port 8020
  python -m chart_patterns.api                      # 위와 동일 (개발용)

엔드포인트
  GET /healthz                 시세 커버리지·구현 패턴 수
  GET /api/patterns            패턴 목록 (한글명·책 통계·탐지기 구현 여부)
  GET /api/patterns/{pid}      해당 패턴의 책 식별규칙·매매전술 원문
  GET /api/scan                스캔 결과 JSON
  GET /api/report              스캔 결과 HTML (브라우저용)

인증
  환경변수 CHART_API_KEY 를 설정하면 모든 /api/* 에 대해
  헤더 `X-API-Key` 또는 쿼리 `?key=` 를 요구한다. 미설정이면 인증 없음
  (로컬 전용으로 쓸 때). 외부에 열 거라면 반드시 설정할 것.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import detectors as D
from . import registry as R
from .ohlcv import connect
from .report import write_html
from .scan import (CAP_LABEL, DEFAULT_MIN_PRICE, DEFAULT_MIN_VALUE,
                   scan_market)

HERE = Path(__file__).resolve().parent
CACHE_TTL = 60 * 30                     # 시세가 T+1 이라 30분 캐시로 충분

app = FastAPI(title="Chart Pattern Screener",
              description="Bulkowski 식별규칙 기반 국장/미장 차트패턴 스크리너",
              version="1.0")

_cache: Dict[tuple, Tuple[float, dict]] = {}


# ------------------------------------------------------------------ 인증
def require_key(request: Request) -> None:
    want = os.environ.get("CHART_API_KEY")
    if not want:
        return                                    # 키 미설정 = 로컬 전용 모드
    got = request.headers.get("X-API-Key") or request.query_params.get("key")
    if got != want:
        raise HTTPException(status_code=401, detail="invalid or missing API key")


# ------------------------------------------------------------------ 캐시
def _data_stamp(market: str) -> str:
    """시세 DB 의 최신 일자 — 바뀌면 캐시를 자동 무효화한다."""
    conn = connect()
    row = conn.execute("SELECT MAX(dt), COUNT(*) FROM bars WHERE market=?",
                       (market,)).fetchone()
    conn.close()
    return f"{row[0]}|{row[1]}"


def cached_scan(market: str, recent: int, pids: Optional[List[str]],
                direction: Optional[str], min_value: Optional[float],
                min_price: Optional[float], min_score: float,
                min_cap: float = 0.0, max_cap: float = 0.0,
                index: str = "") -> dict:
    key = (market, recent, tuple(pids or ()), direction, min_value, min_price,
           min_score, min_cap, max_cap, index, _data_stamp(market))
    hit = _cache.get(key)
    now = time.time()
    if hit and now - hit[0] < CACHE_TTL:
        out = dict(hit[1])
        out["cached"] = True
        return out

    res = scan_market(market, recent=recent, pids=pids, direction=direction,
                      min_value=min_value, min_price=min_price,
                      min_score=min_score, min_cap=min_cap, max_cap=max_cap,
                      index=index, verbose=False)
    if res.get("error"):
        raise HTTPException(status_code=503, detail=res["error"])
    _cache[key] = (now, res)
    if len(_cache) > 40:                           # 무한 증식 방지
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:10]:
            _cache.pop(k, None)
    out = dict(res)
    out["cached"] = False
    return out


def _pids(pattern: str) -> Optional[List[str]]:
    ids = [x.strip() for x in pattern.split(",") if x.strip()]
    if not ids:
        return None
    unknown = [p for p in ids if p not in D.REGISTRY]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={"unknown_pattern": unknown,
                    "available": sorted(D.REGISTRY)})
    return ids


# --------------------------------------------------------------- 엔드포인트
@app.get("/healthz")
def healthz() -> dict:
    conn = connect()
    cov = {}
    for m in ("KR", "US"):
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT code), MIN(dt), MAX(dt) "
            "FROM bars WHERE market=?", (m,)).fetchone()
        cov[m] = {"rows": row[0], "codes": row[1],
                  "from": row[2], "to": row[3]}
    conn.close()
    conn = connect()
    idx = {r[0]: r[1] for r in conn.execute(
        "SELECT idx, COUNT(*) FROM universe_index GROUP BY idx")}
    conn.close()
    return {"ok": True, "coverage": cov, "indices": idx,
            "patterns_in_book": len(R.by_id()),
            "detectors_implemented": len(D.REGISTRY),
            "auth": bool(os.environ.get("CHART_API_KEY"))}


@app.get("/api/patterns", dependencies=[Depends(require_key)])
def list_patterns(implemented_only: bool = True) -> list:
    out = []
    for pid, p in R.by_id().items():
        impl = pid in D.REGISTRY
        if implemented_only and not impl:
            continue
        out.append({
            "id": pid, "name_kr": p["name_kr"], "name_en": p["name_en"],
            "chapter": p["chapter"], "implemented": impl,
            "scale": D.SCALE.get(pid, "D"),
            "appearance": p["snapshot"].get("appearance", ""),
            "stats": R.stats(pid),
        })
    out.sort(key=lambda x: x["name_kr"])
    return out


@app.get("/api/patterns/{pid}", dependencies=[Depends(require_key)])
def pattern_detail(pid: str) -> dict:
    p = R.by_id().get(pid)
    if p is None:
        raise HTTPException(status_code=404,
                            detail=f"unknown pattern id: {pid}")
    return {
        "id": pid, "name_kr": p["name_kr"], "name_en": p["name_en"],
        "chapter": p["chapter"], "pdf_pages": p["pdf_pages"],
        "implemented": pid in D.REGISTRY,
        "scale": D.SCALE.get(pid, "D"),
        "snapshot": p["snapshot"],
        "identification": p["identification"],
        "trading_tactics": p["trading_tactics"],
        "stats": R.stats(pid),
    }


def _scan_params(
    market: str = Query("KR", pattern="^(KR|US)$"),
    recent: int = Query(10, ge=1, le=60),
    pattern: str = Query("", description="쉼표구분 pid. 비우면 전체"),
    direction: str = Query("", description="up | down. 비우면 전체"),
    min_score: float = Query(0.0, ge=0, le=100),
    min_value: Optional[float] = Query(None, description="20일 평균 거래대금 하한"),
    min_price: Optional[float] = Query(None),
    min_cap: float = Query(0, ge=0, description="시총 하한 (국장 억원 / 미장 백만$)"),
    max_cap: float = Query(0, ge=0, description="시총 상한 (같은 단위, 0=무제한)"),
    index: str = Query("", description="지수 구성종목만 (미장: russell3000)"),
) -> dict:
    # HTML 폼은 '전체'를 빈 문자열로 보낸다 → None 으로 정규화
    direction = (direction or "").strip().lower()
    if direction not in ("", "up", "down"):
        raise HTTPException(status_code=400,
                            detail="direction 은 up / down / 빈값만 허용")
    return {"market": market, "recent": recent, "pids": _pids(pattern),
            "direction": direction or None, "min_score": min_score,
            "min_value": min_value, "min_price": min_price,
            "min_cap": min_cap, "max_cap": max_cap,
            "index": index.strip()}


@app.get("/api/scan", dependencies=[Depends(require_key)])
def scan(p: dict = Depends(_scan_params),
         top: int = Query(50, ge=1, le=500),
         charts: bool = Query(False, description="미니차트용 종가 배열 포함")) -> dict:
    res = cached_scan(**p)
    hits = res["hits"][:top]
    if not charts:
        hits = [{k: v for k, v in h.items() if k != "chart"} for h in hits]
    return {
        "market": res["market"], "regime": res["regime"],
        "universe": res["universe"], "recent": res["recent"],
        "generated": res["generated"], "cached": res["cached"],
        "cap_unit": CAP_LABEL[res["market"]],
        "total_hits": len(res["hits"]), "returned": len(hits),
        "hits": hits,
    }


@app.get("/api/report", response_class=HTMLResponse,
         dependencies=[Depends(require_key)])
def report(p: dict = Depends(_scan_params),
           top: int = Query(60, ge=1, le=300)) -> HTMLResponse:
    res = cached_scan(**p)
    tmp = HERE.parent / "output" / f"_api_report_{res['market']}.html"
    write_html([res], tmp, top=top)
    return HTMLResponse(tmp.read_text(encoding="utf-8"))


@app.get("/api")
def api_index() -> JSONResponse:
    return JSONResponse({
        "service": "chart pattern screener",
        "endpoints": ["/healthz", "/api/patterns", "/api/patterns/{pid}",
                      "/api/scan", "/api/report", "/docs"],
        "example": "/api/scan?market=KR&recent=10&min_score=62&top=20",
    })


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    """브라우저로 여는 첫 화면 — 조건 골라서 리포트로 보내는 폼."""
    from .report import CSS

    conn = connect()
    cov = []
    for m, label in (("KR", "국내"), ("US", "미국")):
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT code), MAX(dt) "
            "FROM bars WHERE market=?", (m,)).fetchone()
        cov.append(f"{label} {row[1]:,}종목 · {row[0]:,}행 · ~{row[2] or '없음'}"
                   if row[0] else f"{label} 시세 없음")
    conn.close()

    opts = "".join(
        f'<option value="{pid}">{R.name_kr(pid)}</option>'
        for pid in sorted(D.REGISTRY, key=R.name_kr))

    return HTMLResponse(f"""<title>차트패턴 스크리너</title>
<style>{CSS}
form{{display:grid;gap:14px;max-width:520px;margin-top:22px}}
label{{display:grid;gap:5px;font-size:13px;color:var(--fg2)}}
select,input{{font:inherit;padding:7px 9px;border:1px solid var(--line);
  border-radius:8px;background:var(--card);color:var(--fg)}}
button.go{{font:inherit;font-weight:600;padding:10px 16px;border:0;
  border-radius:8px;background:var(--acc);color:var(--bg);cursor:pointer}}
.row{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.links{{margin-top:26px;padding-top:16px;border-top:1px solid var(--line);
  font-size:13px;line-height:2}}
a{{color:var(--dn)}}
code{{background:var(--chip);padding:1px 5px;border-radius:4px;font-size:12px}}
</style>
<div class="wrap">
  <h1>차트패턴 스크리너</h1>
  <div class="sub">Bulkowski <i>Encyclopedia of Chart Patterns</i> (3rd ed.)
    식별규칙 기반 · 73/75 패턴<br>{' &nbsp;/&nbsp; '.join(cov)}</div>

  <form action="/api/report" method="get">
    <div class="row">
      <label>시장
        <select name="market">
          <option value="KR">국내 (KOSPI/KOSDAQ)</option>
          <option value="US">미국 (S&amp;P 500)</option>
        </select>
      </label>
      <label>방향
        <select name="direction">
          <option value="">전체</option>
          <option value="up">상승 ▲</option>
          <option value="down">하락 ▼</option>
        </select>
      </label>
    </div>
    <label id="idxrow">미장 유니버스
      <select name="index">
        <option value="">S&amp;P 500 (기본)</option>
        <option value="russell3000">러셀 3000 (시총 상위 3,000 근사)</option>
      </select>
    </label>
    <div class="row">
      <label>최근 N봉 이내
        <input type="number" name="recent" value="10" min="1" max="60">
      </label>
      <label>최소 점수
        <input type="number" name="min_score" value="62" min="0" max="100">
      </label>
    </div>
    <div class="row">
      <label>시가총액 하한 <b id="capu">억원</b> (0 = 무제한)
        <input type="number" name="min_cap" value="0" min="0" step="100">
      </label>
      <label>시가총액 상한 (0 = 무제한)
        <input type="number" name="max_cap" value="0" min="0" step="100">
      </label>
    </div>
    <label>패턴 (선택 안 하면 전체 · Ctrl 로 복수 선택)
      <select name="pattern" multiple size="8">{opts}</select>
    </label>
    <label>표시 개수
      <input type="number" name="top" value="60" min="1" max="300">
    </label>
    <button class="go" type="submit">리포트 보기</button>
  </form>

  <div class="links">
    바로가기 —
    <a href="/api/report?market=KR&amp;recent=10&amp;min_score=62&amp;top=60">국장 상위 60</a> ·
    <a href="/api/report?market=US&amp;recent=10&amp;min_score=62&amp;top=60">미장 상위 60</a> ·
    <a href="/docs">API 문서</a> ·
    <a href="/healthz">상태</a><br>
    JSON 예시 — <code>/api/scan?market=KR&amp;recent=10&amp;min_score=62&amp;top=20</code><br>
    책 원문 예시 — <code>/api/patterns/cup_handle</code>
  </div>
</div>
<script>
// 시총 단위 라벨을 시장에 맞게 (국장 억원 / 미장 백만달러)
const mk = document.querySelector('select[name=market]');
const capu = document.getElementById('capu');
const idxrow = document.getElementById('idxrow');
const syncUnit = () => {{
  const us = mk.value === 'US';
  capu.textContent = us ? '백만$' : '억원';
  idxrow.style.display = us ? '' : 'none';     // 지수 선택은 미장에서만
}};
mk.addEventListener('change', syncUnit); syncUnit();

// multiple select → 쉼표 문자열 한 개로 (서버는 콤마 구분을 받는다)
document.querySelector('form').addEventListener('submit', e => {{
  const sel = e.target.pattern;
  const vals = [...sel.selectedOptions].map(o => o.value).join(',');
  const hidden = document.createElement('input');
  hidden.type = 'hidden'; hidden.name = 'pattern'; hidden.value = vals;
  sel.removeAttribute('name');
  e.target.appendChild(hidden);
}});
</script>""")


def main() -> int:
    import argparse
    import uvicorn
    from . import utf8_stdout

    utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8020)
    ap.add_argument("--reload", action="store_true")
    a = ap.parse_args()
    print(f"차트패턴 API → http://{a.host}:{a.port}/docs")
    if not os.environ.get("CHART_API_KEY") and a.host != "127.0.0.1":
        print("⚠ 외부 바인딩인데 CHART_API_KEY 가 없습니다 — 인증 없이 열립니다.")
    uvicorn.run("chart_patterns.api:app", host=a.host, port=a.port,
                reload=a.reload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
