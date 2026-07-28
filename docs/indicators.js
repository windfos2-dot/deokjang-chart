/* indicators.js — 브라우저에서 계산하는 배열형 지표
 *
 * 정적 사이트(GitHub Pages)에서는 종목 수가 많아 JSON 용량이 곧 비용이다.
 * SMA/EMA/HMA/볼린저/RSI/이치모쿠처럼 OHLCV 만 있으면 되는 배열 지표는
 * 서버가 내려주지 않고 여기서 만든다 (종목당 약 61% 절감).
 *
 * 패턴/오더블록/S-R 돌파/미너비니처럼 계산이 무겁거나 결과가 작은 것은
 * 빌드 시점에 파이썬(chart_indicators.py)이 계산해 JSON 에 담는다.
 * 계산식은 파이썬 구현과 일치시켰다.
 */
(function (global) {
  "use strict";

  function sma(x, n) {
    const out = new Array(x.length).fill(null);
    let sum = 0, cnt = 0;
    for (let i = 0; i < x.length; i++) {
      const v = x[i];
      if (v == null) { sum = 0; cnt = 0; continue; }
      sum += v; cnt++;
      if (cnt > n) { sum -= x[i - n]; cnt = n; }
      if (cnt === n) out[i] = sum / n;
    }
    return out;
  }

  // 파이썬 ema(): 첫 유효 윈도우를 SMA 로 시드, 이후 중간 NaN 은 직전값 유지
  function ema(x, n) {
    const out = new Array(x.length).fill(null);
    if (x.length < n) return out;
    let start = -1;
    for (let i = n - 1; i < x.length; i++) {
      let ok = true;
      for (let j = i - n + 1; j <= i; j++) if (x[j] == null) { ok = false; break; }
      if (ok) { start = i; break; }
    }
    if (start < 0) return out;
    let s = 0;
    for (let j = start - n + 1; j <= start; j++) s += x[j];
    out[start] = s / n;
    const a = 2 / (n + 1);
    for (let i = start + 1; i < x.length; i++) {
      out[i] = x[i] == null ? out[i - 1] : a * x[i] + (1 - a) * out[i - 1];
    }
    return out;
  }

  function wma(x, n) {
    const out = new Array(x.length).fill(null);
    const denom = (n * (n + 1)) / 2;
    for (let i = n - 1; i < x.length; i++) {
      let acc = 0, ok = true;
      for (let k = 0; k < n; k++) {
        const v = x[i - n + 1 + k];
        if (v == null) { ok = false; break; }
        acc += v * (k + 1);
      }
      if (ok) out[i] = acc / denom;
    }
    return out;
  }

  function hma(x, p) {
    const half = Math.round(p / 2), sq = Math.round(Math.sqrt(p));
    const a = wma(x, half), b = wma(x, p);
    const raw = x.map((_, i) =>
      (a[i] == null || b[i] == null) ? null : 2 * a[i] - b[i]);
    return wma(raw, sq);
  }

  function bollinger(close, n, k) {
    const mid = sma(close, n);
    const up = new Array(close.length).fill(null);
    const lo = new Array(close.length).fill(null);
    for (let i = n - 1; i < close.length; i++) {
      if (mid[i] == null) continue;
      let s = 0, ok = true;
      for (let j = i - n + 1; j <= i; j++) {
        if (close[j] == null) { ok = false; break; }
        s += (close[j] - mid[i]) ** 2;
      }
      if (!ok) continue;
      const sd = Math.sqrt(s / n);
      up[i] = mid[i] + k * sd;
      lo[i] = mid[i] - k * sd;
    }
    return { mid: mid, upper: up, lower: lo };
  }

  // Wilder 방식 RSI (파이썬 rsi_wilder 와 동일)
  function rsi(close, n) {
    const out = new Array(close.length).fill(null);
    if (close.length <= n) return out;
    let ag = 0, al = 0;
    for (let i = 1; i <= n; i++) {
      const d = close[i] - close[i - 1];
      if (d > 0) ag += d; else al -= d;
    }
    ag /= n; al /= n;
    const calc = (g, l) => (l === 0 ? 100 : 100 - 100 / (1 + g / l));
    out[n] = calc(ag, al);
    for (let i = n + 1; i < close.length; i++) {
      const d = close[i] - close[i - 1];
      const gain = d > 0 ? d : 0, loss = d < 0 ? -d : 0;
      ag = (ag * (n - 1) + gain) / n;
      al = (al * (n - 1) + loss) / n;
      out[i] = calc(ag, al);
    }
    return out;
  }

  function disparity(close, n) {
    const s = sma(close, n);
    return close.map((c, i) => (s[i] == null || !s[i]) ? null : (c / s[i] - 1) * 100);
  }

  // 이치모쿠 (9, 26, 52), 선행스팬은 26봉 앞으로
  function ichimoku(high, low, close, p1, p2, p3, shift) {
    const N = close.length;
    const hh = (n, i) => {
      let m = -Infinity;
      for (let j = Math.max(0, i - n + 1); j <= i; j++) m = Math.max(m, high[j]);
      return i >= n - 1 ? m : null;
    };
    const ll = (n, i) => {
      let m = Infinity;
      for (let j = Math.max(0, i - n + 1); j <= i; j++) m = Math.min(m, low[j]);
      return i >= n - 1 ? m : null;
    };
    const tenkan = [], kijun = [], spanA = [], spanB = [], chikou = [];
    for (let i = 0; i < N; i++) {
      const t = (hh(p1, i) == null) ? null : (hh(p1, i) + ll(p1, i)) / 2;
      const k = (hh(p2, i) == null) ? null : (hh(p2, i) + ll(p2, i)) / 2;
      tenkan.push(t); kijun.push(k);
      spanB.push(hh(p3, i) == null ? null : (hh(p3, i) + ll(p3, i)) / 2);
      spanA.push(t == null || k == null ? null : (t + k) / 2);
      chikou.push(i + shift < N ? close[i + shift] : null);
    }
    const shifted = (arr) => {
      const o = new Array(N).fill(null);
      for (let i = 0; i < N; i++) if (i - shift >= 0) o[i] = arr[i - shift];
      return o;
    };
    return { tenkan, kijun, span_a: shifted(spanA), span_b: shifted(spanB), chikou };
  }

  global.DJIndicators = {
    sma, ema, wma, hma, bollinger, rsi, disparity, ichimoku,
    // 종목 JSON(OHLCV) -> 차트가 쓰는 지표 묶음
    computeArrays: function (d) {
      const c = d.c, h = d.h, l = d.l;
      const ma = {};
      [5, 10, 20, 50, 120, 150, 200].forEach(p => { ma["sma" + p] = sma(c, p); });
      ma.ema50 = ema(c, 50);
      ma.ema200 = ema(c, 200);
      ma.hma60 = hma(c, 60);
      return {
        ma: ma,
        bb: bollinger(c, 20, 2),
        rsi: rsi(c, 14),
        disparity50: disparity(c, 50),
        ichimoku: ichimoku(h, l, c, 9, 26, 52, 26),
      };
    }
  };
})(window);
