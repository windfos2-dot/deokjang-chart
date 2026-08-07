# -*- coding: utf-8 -*-
"""스캔 결과 → 단일 HTML 리포트 (미니 차트 + 필터 + 책 근거)."""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Dict, List, Optional

from . import registry as R

W, H = 240, 78          # 미니차트 크기
PAD = 4


def _spark(chart: Optional[dict], points: List[List[float]],
           direction: str) -> str:
    """종가 라인 + 패턴 꼭짓점 표시 SVG."""
    if not chart or not chart.get("c"):
        return ""
    c = chart["c"]
    hi, lo = max(c), min(c)
    rng = (hi - lo) or 1.0
    n = len(c)
    off = chart.get("offset", 0)

    def xy(i: int, price: float):
        x = PAD + (W - 2 * PAD) * (i / max(1, n - 1))
        y = PAD + (H - 2 * PAD) * (1 - (price - lo) / rng)
        return round(x, 1), round(y, 1)

    line = " ".join(f"{xy(i, p)[0]},{xy(i, p)[1]}" for i, p in enumerate(c))
    color = "var(--up)" if direction == "up" else "var(--dn)"
    dots = []
    for gi, price in points:
        i = int(gi) - off
        if 0 <= i < n:
            x, y = xy(i, float(price))
            dots.append(f'<circle cx="{x}" cy="{y}" r="2.6" fill="{color}"/>')
    lvl = ""
    if chart.get("level") is not None:
        _, y = xy(0, float(chart["level"]))
        lvl = (f'<line x1="{PAD}" y1="{y}" x2="{W - PAD}" y2="{y}" '
               f'stroke="{color}" stroke-width="0.8" stroke-dasharray="3 3" '
               f'opacity=".55"/>')
    return (f'<svg class="spark" viewBox="0 0 {W} {H}" width="{W}" height="{H}">'
            f'{lvl}<polyline points="{line}" fill="none" stroke="var(--fg2)" '
            f'stroke-width="1.1"/>{"".join(dots)}</svg>')


def _rowspan(r: dict) -> str:
    tag = "돌파" if r["status"] == "breakout" else "형성중"
    cls = "bo" if r["status"] == "breakout" else "fm"
    return f'<span class="tag {cls}">{tag}</span>'


def _num(v, suffix="", digits=1):
    if v is None:
        return '<span class="dim">-</span>'
    return f"{v:+.{digits}f}{suffix}" if suffix == "%" else f"{v:,.{digits}f}"


def _card(r: dict) -> str:
    arrow = "▲" if r["direction"] == "up" else "▼"
    dcls = "up" if r["direction"] == "up" else "dn"
    ident = R.identification(r["pid"])
    why = " · ".join(f"{k}" for k in list(ident)[:4])
    notes = {k: v for k, v in (r.get("notes") or {}).items()
             if k not in ("bars", "weekly_index")}
    note_s = ", ".join(f"{k}={v}" for k, v in list(notes.items())[:6])
    book = []
    if r.get("book_perf") is not None:
        book.append(f"책 평균 {r['book_perf']:+.1f}%")
    if r.get("book_fail") is not None:
        book.append(f"실패율 {r['book_fail']:.0f}%")
    if r.get("book_rank") is not None:
        book.append(f"순위 {int(r['book_rank'])}위")
    cap = (f"시총 {r['mktcap_fmt']}"
           if r.get("mktcap_fmt") not in (None, "-") else "")
    scale = "주봉" if r.get("scale") == "W" else "일봉"
    when = r.get("breakout_date") or r.get("end") or ""
    after = ""
    if r.get("to_trigger_pct") is not None:
        after = (f'<span class="dim">트리거 {r["trigger"]:,.2f}까지 '
                 f'{r["to_trigger_pct"]:+.1f}%</span>')
    if r.get("since_breakout_pct") is not None:
        warn = ' <b class="warn">경계 안쪽 회귀</b>' if r.get("back_inside") else ""
        after = (f'<span class="dim">돌파 후 {r["since_breakout_pct"]:+.1f}%'
                 f'{warn}</span>')
    return f"""
<article class="card" data-pid="{r['pid']}" data-dir="{r['direction']}"
         data-status="{r['status']}" data-score="{r['score']}">
  <div class="chart">{_spark(r.get('chart'), r.get('points') or [], r['direction'])}</div>
  <div class="body">
    <div class="hd">
      <span class="score">{r['score']:.0f}</span>
      <b class="nm">{html.escape(r['name'] or r['code'])}</b>
      <span class="code">{r['code']}</span>
      <span class="{dcls}">{arrow}</span>
      <span class="pat">{r['pattern_kr']}</span>
      {_rowspan(r)}
      <span class="dim sm">{scale} · {when}</span>
    </div>
    <div class="mt">
      <span>높이 {r['height_pct']:.1f}%</span>
      <span>현재가 {r['last']:,.2f}</span>
      {f'<span class="dim">{cap}</span>' if cap else ''}
      <span>목표 {_num(r['target'], digits=2)}
        {'<em class="' + dcls + '">(' + f"{r['upside_pct']:+.1f}%" + ')</em>'
         if r.get('upside_pct') is not None else ''}</span>
      <span class="dim">{' · '.join(book)}</span>
      {after}
    </div>
    <div class="dim sm rules">{html.escape(r['pattern_en'])} — {html.escape(why)}</div>
    <div class="dim sm">{html.escape(note_s)}</div>
  </div>
</article>"""


CSS = """
:root{--bg:#fff;--fg:#14161a;--fg2:#5b6270;--line:#e6e8ec;--card:#fff;
      --up:#d4342c;--dn:#1266d6;--acc:#111;--chip:#f4f5f7}
@media (prefers-color-scheme:dark){:root{--bg:#0e1013;--fg:#e8eaee;--fg2:#98a0ae;
  --line:#232833;--card:#15181d;--up:#ff5a4e;--dn:#4f9dff;--acc:#e8eaee;--chip:#1c2028}}
:root[data-theme=dark]{--bg:#0e1013;--fg:#e8eaee;--fg2:#98a0ae;--line:#232833;
  --card:#15181d;--up:#ff5a4e;--dn:#4f9dff;--acc:#e8eaee;--chip:#1c2028}
:root[data-theme=light]{--bg:#fff;--fg:#14161a;--fg2:#5b6270;--line:#e6e8ec;
  --card:#fff;--up:#d4342c;--dn:#1266d6;--acc:#111;--chip:#f4f5f7}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.5 -apple-system,"Segoe UI","Malgun Gothic",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:24px 16px 60px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:var(--fg2);font-size:13px;margin-bottom:18px}
.bar{display:flex;flex-wrap:wrap;gap:6px;margin:14px 0 18px;
  padding-bottom:14px;border-bottom:1px solid var(--line)}
.chip{border:1px solid var(--line);background:var(--chip);color:var(--fg);
  border-radius:99px;padding:4px 11px;font-size:12px;cursor:pointer}
.chip.on{background:var(--acc);color:var(--bg);border-color:var(--acc)}
.card{display:flex;gap:14px;align-items:center;border:1px solid var(--line);
  background:var(--card);border-radius:10px;padding:10px 12px;margin-bottom:8px}
.chart{flex:0 0 240px}
.spark{display:block}
.body{min-width:0;flex:1}
.hd{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline}
.score{font-weight:700;font-variant-numeric:tabular-nums;
  background:var(--chip);border-radius:6px;padding:1px 7px}
.nm{font-size:15px}
.code{color:var(--fg2);font-size:12px;font-variant-numeric:tabular-nums}
.pat{font-weight:600}
.up{color:var(--up)} .dn{color:var(--dn)}
.tag{font-size:11px;border-radius:5px;padding:1px 6px;border:1px solid var(--line)}
.tag.bo{background:var(--acc);color:var(--bg);border-color:var(--acc)}
.mt{display:flex;flex-wrap:wrap;gap:14px;margin-top:4px;
  font-variant-numeric:tabular-nums}
.dim{color:var(--fg2)} .sm{font-size:12px}
.rules{margin-top:3px}
em{font-style:normal;font-weight:600}
.warn{color:var(--dn)}
.sec{margin:26px 0 10px;font-size:15px;font-weight:700}
.empty{color:var(--fg2);padding:20px 0}
@media(max-width:720px){.card{flex-direction:column;align-items:stretch}
  .chart{flex:none}.spark{width:100%;height:auto}}
"""

JS = """
const chips=[...document.querySelectorAll('.chip')];
function apply(){
  const on=k=>chips.filter(c=>c.dataset.kind===k&&c.classList.contains('on'))
                   .map(c=>c.dataset.val);
  const dirs=on('dir'), sts=on('status'), pats=on('pid');
  document.querySelectorAll('.card').forEach(el=>{
    const ok=(!dirs.length||dirs.includes(el.dataset.dir))
          &&(!sts.length||sts.includes(el.dataset.status))
          &&(!pats.length||pats.includes(el.dataset.pid));
    el.style.display=ok?'':'none';
  });
  document.querySelectorAll('.mk').forEach(sec=>{
    const vis=[...sec.querySelectorAll('.card')].some(c=>c.style.display!=='none');
    sec.style.display=vis?'':'none';
  });
}
chips.forEach(c=>c.onclick=()=>{c.classList.toggle('on');apply();});
"""


def write_html(results: List[dict], out: Path, top: int = 60) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    pids, blocks = {}, []

    for res in results:
        rows = res["hits"][:top]
        for r in rows:
            pids[r["pid"]] = r["pattern_kr"]
        label = "국내 (KOSPI/KOSDAQ)" if res["market"] == "KR" else "미국"
        head = (f'<div class="sec mkhd">{label} · {res["universe"]:,}종목 · '
                f'국면 {res["regime"]} · 최근 {res["recent"]}봉 · '
                f'총 {len(res["hits"]):,}건 중 상위 {len(rows)}</div>')
        body = "".join(_card(r) for r in rows) or '<div class="empty">해당 없음</div>'
        blocks.append(f'<section class="mk">{head}{body}</section>')

    chips = ['<button class="chip" data-kind="dir" data-val="up">▲ 상승</button>',
             '<button class="chip" data-kind="dir" data-val="down">▼ 하락</button>',
             '<button class="chip" data-kind="status" data-val="breakout">돌파</button>',
             '<button class="chip" data-kind="status" data-val="forming">형성중</button>']
    for pid, kr in sorted(pids.items(), key=lambda x: x[1]):
        chips.append(f'<button class="chip" data-kind="pid" data-val="{pid}">'
                     f'{kr}</button>')

    gen = results[0].get("generated", "") if results else ""
    doc = f"""<title>차트패턴 스크리너</title>
<style>{CSS}</style>
<div class="wrap">
  <h1>차트패턴 스크리너</h1>
  <div class="sub">Bulkowski <i>Encyclopedia of Chart Patterns</i> (3rd ed.)
    식별규칙 기반 · 점수 = 책 통계 45% + 형태 적합도 35% + 돌파 확인 20%
    · 생성 {gen}</div>
  <div class="bar">{''.join(chips)}</div>
  {''.join(blocks)}
</div>
<script>{JS}</script>"""
    out.write_text(doc, encoding="utf-8")
    return out
