"""
export_chart.py — 종목 차트를 '파일 하나'로 내보낸다.

로컬 서버(chart_router)에서 데이터를 받아 HTML 안에 통째로 박아 넣는다.
받은 사람은 서버도 파이썬도 필요 없고, 파일을 더블클릭하면 차트가 뜬다.
(차트 라이브러리만 CDN 에서 받으므로 볼 때 인터넷은 필요하다)

사용:
    python export_chart.py 005930
    python export_chart.py NVDA --days 1250 --out ~/Desktop/nvda.html
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime

API = os.getenv("CHART_API", "http://127.0.0.1:8010")


def fetch(path, **params):
    url = f"{API}/api/chart/{path}?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=180) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


TEMPLATE = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<script src="https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js"></script>
<style>
 :root{--bg:#0d1117;--panel:#131a24;--border:#243040;--text:#e6edf3;--muted:#8b98a9;--accent:#2dd4bf}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);
   font:13px/1.5 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo",sans-serif}
 header{padding:12px 16px;border-bottom:1px solid var(--border);background:var(--panel);
   display:flex;gap:12px;align-items:baseline;flex-wrap:wrap}
 h1{font-size:15px;margin:0} .mk{color:var(--muted);font-size:12px}
 .up{color:#e2445c} .dn{color:#3b82f6}
 main{padding:10px 14px 30px}
 .panel{background:var(--panel);border:1px solid var(--border);border-radius:10px;
   margin-bottom:10px;overflow:hidden}
 .ptitle{font-size:11.5px;color:var(--muted);padding:7px 12px;
   border-bottom:1px solid var(--border);display:flex;justify-content:space-between;gap:10px}
 .tag{display:inline-block;background:#1b2432;border:1px solid var(--border);border-radius:5px;
   padding:2px 7px;margin:3px 4px 0 0;font-size:11px;color:#c9d4e2}
 .badge{display:inline-block;border-radius:5px;padding:2px 8px;font-weight:600;font-size:11.5px}
 .ok{background:#0f3f33;color:#4ade80} .no{background:#3f1620;color:#fb7185}
 table{width:100%;border-collapse:collapse;font-size:11.5px}
 th,td{padding:7px 10px;border-bottom:1px solid #1b2432;text-align:right;white-space:nowrap}
 th{color:var(--muted);font-weight:500} td:first-child,th:first-child{text-align:left;font-weight:600}
 .lamp{display:inline-block;width:11px;height:11px;border-radius:50%}
 .g{background:#22c55e}.y{background:#facc15}.r{background:#f87171}
 footer{padding:14px;text-align:center;color:var(--muted);font-size:11px;
   border-top:1px solid var(--border)}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
 @media(max-width:760px){.grid{grid-template-columns:1fr}}
</style></head><body>
<header>
  <h1>덕장 차트</h1>
  <select id="pick"></select>
  <span id="hmeta"></span>
</header>
<main>
  <div class="panel"><div class="ptitle"><span>가격 · 이동평균 · 볼린저</span><span id="mnv"></span></div>
    <div id="c1" style="height:380px"></div><div id="pats" style="padding:6px 10px"></div></div>
  <div class="panel"><div class="ptitle"><span id="bt">밸류에이션 밴드</span><span id="bn"></span></div>
    <div id="c2" style="height:190px"></div></div>
  <div class="grid">
    <div class="panel"><div class="ptitle"><span>RSI(14)</span></div><div id="c3" style="height:150px"></div></div>
    <div class="panel"><div class="ptitle"><span>신호등 · 밸류</span></div><div id="sig" style="padding:4px 0"></div></div>
  </div>
</main>
<footer>덕장 차트 · 내보낸 시각 __NOW__ · 투자 판단의 근거로 쓰지 마세요</footer>
<script>
const ALL = __DATA__;                 // {티커: {full, bands, signals}}
const KEYS = Object.keys(ALL);
let D = ALL[KEYS[0]];
const C={layout:{background:{color:"transparent"},textColor:"#8b98a9",fontSize:11},
 grid:{vertLines:{color:"#151b26"},horzLines:{color:"#151b26"}},
 rightPriceScale:{borderColor:"#243040"},timeScale:{borderColor:"#243040"},autoSize:true};
const c1=LightweightCharts.createChart(document.getElementById("c1"),{...C,height:380});
const c2=LightweightCharts.createChart(document.getElementById("c2"),{...C,height:190});
const c3=LightweightCharts.createChart(document.getElementById("c3"),{...C,height:150});
const C1=[],C2=[],C3=[];               // 시리즈 보관 (종목 바꿀 때 지운다)
function clearAll(){ [[c1,C1],[c2,C2],[c3,C3]].forEach(([ch,arr])=>{
  arr.forEach(sr=>{try{ch.removeSeries(sr)}catch(e){}}); arr.length=0; }); }

function draw(key){
  D = ALL[key];
  clearAll();
  const o=D.full.ohlcv, ind=D.full.indicators, dt=o.dates;
  const pair=(a)=>dt.map((t,i)=>a&&a[i]!=null?{time:t,value:a[i]}:null).filter(Boolean);
  const us=D.full.market==="US";
  const last=o.close[o.close.length-1], prev=o.close[o.close.length-2]??last;
  const chg=prev?((last-prev)/prev*100):0;
  document.getElementById("hmeta").innerHTML =
    `<span class="mk">${D.full.ticker} · ${D.full.market||""}</span> `
    + `<b>${us?"$"+last.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2})
             :last.toLocaleString()+"원"}</b> `
    + `<span class="${chg>=0?"up":"dn"}">${chg>=0?"+":""}${chg.toFixed(2)}%</span> `
    + `<span class="mk">${dt[0]} ~ ${dt[dt.length-1]} (${dt.length}봉)</span>`;

  const cs=c1.addCandlestickSeries({upColor:"#e2445c",downColor:"#3b82f6",borderUpColor:"#e2445c",
   borderDownColor:"#3b82f6",wickUpColor:"#e2445c",wickDownColor:"#3b82f6"});
  cs.setData(dt.map((t,i)=>({time:t,open:o.open[i],high:o.high[i],low:o.low[i],close:o.close[i]})));
  C1.push(cs);
  [["sma20","#f0b90b"],["sma50","#a78bfa"],["sma120","#2dd4bf"]].forEach(([k,c])=>{
   const sr=c1.addLineSeries({color:c,lineWidth:1,priceLineVisible:false,lastValueVisible:false});
   sr.setData(pair(ind[k])); C1.push(sr);});
  ["bb_upper","bb_lower"].forEach(k=>{
   const sr=c1.addLineSeries({color:"#8b98a9",lineWidth:1,lineStyle:2,priceLineVisible:false,lastValueVisible:false});
   sr.setData(pair(ind[k])); C1.push(sr);});
  const rs=c3.addLineSeries({color:"#e6edf3",lineWidth:1}); rs.setData(pair(ind.rsi)); C3.push(rs);
  [70,30].forEach(v=>{const sr=c3.addLineSeries({color:"#3a4658",lineWidth:1,lineStyle:2,lastValueVisible:false});
   sr.setData([{time:dt[0],value:v},{time:dt[dt.length-1],value:v}]); C3.push(sr);});

  document.getElementById("pats").innerHTML=(ind.patterns||[]).map(p=>
 `<span class="tag">${p.name} ${dt[p.start]}~${dt[p.end]}</span>`).join(" ");
  const mv=ind.minervini&&ind.minervini.latest;
  document.getElementById("mnv").innerHTML = !mv ? "" :
 `<span class="badge ${mv.pass?"ok":"no"}">Minervini ${mv.pass?"통과":"미충족"}</span>`;

  // 밸류에이션 밴드
  const B=D.bands;
  if(B&&B.available){
  const M="PER", arr=(B.series[M]||[]).filter(x=>x[0]>=dt[0]&&x[0]<=dt[dt.length-1]);
  if(arr.length){
    const bl=c2.addLineSeries({color:"#38bdf8",lineWidth:2});
    bl.setData(arr.map(x=>({time:x[0],value:x[1]}))); C2.push(bl);
    const v=arr.map(x=>x[1]), m=v.reduce((a,b)=>a+b,0)/v.length;
    const sd=Math.sqrt(v.reduce((a,b)=>a+(b-m)**2,0)/v.length);
    [[m,"#f0b90b",0],[m+sd,"#8b98a9",2],[m-sd,"#8b98a9",2]].forEach(([lv,c,st])=>{
      const sr=c2.addLineSeries({color:c,lineWidth:1,lineStyle:st,lastValueVisible:false});
      sr.setData([{time:arr[0][0],value:lv},{time:arr[arr.length-1][0],value:lv}]); C2.push(sr);});
    const cur=v[v.length-1], z=sd?(cur-m)/sd:0;
    document.getElementById("bt").textContent=`밸류에이션 밴드 · ${M} (${B.basis})`;
    document.getElementById("bn").innerHTML=
      `현재 <b>${cur.toFixed(2)}</b> · 평균 ${m.toFixed(2)} · ${z>=0?"+":""}${z.toFixed(1)}σ`;
  }
  } else { document.getElementById("bt").textContent="밸류에이션 밴드";
           document.getElementById("bn").textContent=(B&&B.reason)||"데이터 없음"; }

  // 신호등
  const S=D.signals||{}, L={green:"g",yellow:"y",red:"r"};
let html="<table><tr><th>TF</th><th>조건</th><th></th></tr>";
(S.frames||[]).forEach(f=>{ html += f.available
 ? `<tr><td>${f.tf}</td><td>${f.passed}/${f.total}</td><td><span class="lamp ${L[f.light]}"></span></td></tr>`
 : `<tr><td>${f.tf}</td><td colspan=2 class="mk">${(f.reason||"").slice(0,18)}</td></tr>`;});
html+="</table>";
const m2=(S.valuation&&S.valuation.metrics)||{};
if(Object.keys(m2).length){
  html+="<table><tr><th>지표</th><th>현재</th><th>편차</th><th></th></tr>";
  for(const k in m2){const x=m2[k];
    html+=`<tr><td>${k}</td><td>${x.current.toFixed(2)}</td><td>${x.z>=0?"+":""}${x.z.toFixed(1)}σ</td>`
        + `<td><span class="lamp ${L[x.light]}"></span></td></tr>`;}
  html+="</table>";
}
  document.getElementById("sig").innerHTML=html;
  requestAnimationFrame(()=>[c1,c2,c3].forEach(c=>c.timeScale().fitContent()));
}

const sel=document.getElementById("pick");
sel.innerHTML=KEYS.map(k=>`<option value="${k}">${ALL[k].full.name||k} (${k})</option>`).join("");
sel.addEventListener("change",()=>draw(sel.value));
draw(KEYS[0]);
</script></body></html>
"""


def collect(t, days, tf):
    """종목 1개 데이터 묶음. 밴드/신호등은 실패해도 차트는 나오게 분리."""
    full = fetch("full", ticker=t, days=days, tf=tf)
    try:
        bands = fetch("bands", ticker=t)
    except Exception as e:  # noqa: BLE001
        bands = {"available": False, "reason": str(e)}
    try:
        signals = fetch("signals", ticker=t)
    except Exception as e:  # noqa: BLE001
        signals = {"frames": [], "valuation": {"error": str(e)}}
    return {"full": full, "bands": bands, "signals": signals}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="+",
                    help="종목 여러 개 가능 (예: 005930 000660 NVDA)")
    ap.add_argument("--days", type=int, default=750)
    ap.add_argument("--tf", default="D", choices=["D", "W", "M"])
    ap.add_argument("--out", default=None)
    ap.add_argument("--json", action="store_true",
                    help="원본 API 응답도 .json 으로 함께 저장 (백엔드 데이터)")
    a = ap.parse_args()

    raw = []
    for x in a.tickers:
        for t in x.split(","):
            t = t.strip()
            if t:
                raw.append(t.upper() if t.isalpha() else t)

    data, names = {}, []
    for i, t in enumerate(raw, 1):
        print(f"[{i}/{len(raw)}] {t} …", end=" ", flush=True)
        try:
            d = collect(t, a.days, a.tf)
        except Exception as e:  # noqa: BLE001
            print(f"실패 ({e})")
            continue
        data[t] = d
        nm = d["full"].get("name") or t
        names.append(nm)
        b = d["bands"]
        print(f"{nm} · {len(d['full']['ohlcv']['dates'])}봉"
              f" · 밴드 {'O' if b.get('available') else 'X'}")

    if not data:
        print("내보낼 데이터가 없습니다.")
        sys.exit(1)

    title = names[0] if len(names) == 1 else f"{names[0]} 외 {len(names)-1}종목"
    html = (TEMPLATE
            .replace("__TITLE__", f"{title} 차트")
            .replace("__NOW__", datetime.now().strftime("%Y-%m-%d %H:%M"))
            .replace("__DATA__", json.dumps(data, ensure_ascii=False,
                                            separators=(",", ":"))))

    out = a.out or os.path.join(
        os.path.expanduser("~/Desktop"),
        f"덕장차트_{'모음' if len(data) > 1 else names[0]}_{datetime.now():%Y%m%d}.html")
    out = os.path.expanduser(out)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\n✅ 저장: {out}  ({os.path.getsize(out)/1024/1024:.1f}MB, {len(data)}종목)")
    print("   더블클릭하면 열립니다. 상단 드롭다운으로 종목 전환.")
    print("   (서버·파이썬·API키 불필요 / 차트 라이브러리만 인터넷 필요)")

    if a.json:
        jp = os.path.splitext(out)[0] + "_data.json"
        with open(jp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        print(f"✅ 원본 데이터: {jp}  ({os.path.getsize(jp)/1024/1024:.1f}MB)")
        print("   구조: {티커: {full(시세+지표), bands(밸류에이션), signals(신호등)}}")


if __name__ == "__main__":
    main()
