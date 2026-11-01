/*
 * 美股科技族群 ↔ 台股連動引擎（瀏覽器與 Node 共用，純計算、不抓網路）
 *
 * 時序對齊：美股 t 日收盤（台灣時間隔日清晨結束）→ 對應「t 之後的第一個台股交易日」。
 * 若台股連假（一個台股交易日之前有多個美股交易日），只取最近的那一個美股交易日配對，
 * 其餘美股交易日不配對，避免同一個台股報酬被重複計入。
 *
 * 主要輸出：
 *  - pairSeries：配對後的 (美股報酬, 台股隔日報酬) 序列
 *  - corrWindow / hitRate / conditional：相關係數、同向率、條件機率
 *  - followStrategy：「跟單」回測 — 美股族群當日漲則隔日做多台股、跌則做空，與買進持有比較
 *  - verdict：依相關係數與同向率給出連動強度判讀
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.UsTechEngine = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const MIN_PAIRS = 30;

  /** 收盤序列 [{date, close}] → 日報酬序列 [{date, ret}] */
  function dailyReturns(bars) {
    const out = [];
    for (let i = 1; i < bars.length; i++) {
      const p = bars[i - 1].close, c = bars[i].close;
      if (p > 0 && c > 0) out.push({ date: bars[i].date, ret: c / p - 1 });
    }
    return out;
  }

  /** 近 n 個交易日的累積漲跌幅（n=1 即當日） */
  function horizonPct(bars, n) {
    if (!bars || bars.length <= n) return null;
    const a = bars[bars.length - 1 - n].close, b = bars[bars.length - 1].close;
    return a > 0 ? (b / a - 1) * 100 : null;
  }

  /**
   * 多檔成分股日報酬序列 → 等權族群報酬序列。
   * 某日需有至少 minCoverage 比例的成分股有資料才納入，避免個別缺漏造成失真。
   */
  function equalWeightSeries(memberSeriesList, minCoverage = 0.5) {
    const byDate = new Map();
    for (const series of memberSeriesList) {
      for (const r of series) {
        if (!byDate.has(r.date)) byDate.set(r.date, []);
        byDate.get(r.date).push(r.ret);
      }
    }
    const need = Math.max(1, Math.ceil(memberSeriesList.length * minCoverage));
    return [...byDate.entries()]
      .filter(([, arr]) => arr.length >= need)
      .sort((a, b) => (a[0] < b[0] ? -1 : 1))
      .map(([date, arr]) => ({ date, ret: arr.reduce((s, v) => s + v, 0) / arr.length }));
  }

  /** 報酬序列 → 以 100 為基期的指數序列 */
  function indexFromReturns(series, base = 100) {
    let v = base;
    return series.map(r => { v *= 1 + r.ret; return { date: r.date, value: v }; });
  }

  /**
   * 配對美股與台股報酬：每個台股交易日 d 對應「最後一個 < d 的美股交易日」，每個美股交易日最多配對一次。
   * 回傳依日期遞增的 [{usDate, twDate, us, tw}]
   */
  function pairSeries(usSeries, twSeries) {
    const us = [...usSeries].sort((a, b) => (a.date < b.date ? -1 : 1));
    const tw = [...twSeries].sort((a, b) => (a.date < b.date ? -1 : 1));
    const pairs = [];
    let i = 0;
    let lastUsIdx = -1;
    for (const t of tw) {
      while (i < us.length && us[i].date < t.date) i++;
      const j = i - 1; // 最後一個 < t.date 的美股交易日
      if (j < 0 || j === lastUsIdx) continue;
      pairs.push({ usDate: us[j].date, twDate: t.date, us: us[j].ret, tw: t.ret });
      lastUsIdx = j;
    }
    return pairs;
  }

  function pearson(xs, ys) {
    const n = Math.min(xs.length, ys.length);
    if (n < 3) return null;
    let sx = 0, sy = 0;
    for (let i = 0; i < n; i++) { sx += xs[i]; sy += ys[i]; }
    const mx = sx / n, my = sy / n;
    let sxy = 0, sxx = 0, syy = 0;
    for (let i = 0; i < n; i++) {
      const dx = xs[i] - mx, dy = ys[i] - my;
      sxy += dx * dy; sxx += dx * dx; syy += dy * dy;
    }
    if (sxx === 0 || syy === 0) return null;
    return sxy / Math.sqrt(sxx * syy);
  }

  const tail = (pairs, n) => (n ? pairs.slice(-n) : pairs);

  /** 近 n 組配對的相關係數（不足 MIN_PAIRS 組回傳 null） */
  function corrWindow(pairs, n) {
    const w = tail(pairs, n);
    if (w.length < MIN_PAIRS) return null;
    return pearson(w.map(p => p.us), w.map(p => p.tw));
  }

  /** 台股對美股的 beta（最小平方法斜率） */
  function betaWindow(pairs, n) {
    const w = tail(pairs, n);
    if (w.length < MIN_PAIRS) return null;
    const xs = w.map(p => p.us), ys = w.map(p => p.tw);
    const mx = xs.reduce((s, v) => s + v, 0) / xs.length, my = ys.reduce((s, v) => s + v, 0) / ys.length;
    let sxy = 0, sxx = 0;
    for (let i = 0; i < xs.length; i++) { sxy += (xs[i] - mx) * (ys[i] - my); sxx += (xs[i] - mx) ** 2; }
    return sxx === 0 ? null : sxy / sxx;
  }

  /** 同向率：美股漲台股隔日也漲、美股跌台股隔日也跌的比例（任一方持平不計） */
  function hitRate(pairs, n) {
    const w = tail(pairs, n).filter(p => p.us !== 0 && p.tw !== 0);
    if (w.length < MIN_PAIRS) return null;
    const hit = w.filter(p => Math.sign(p.us) === Math.sign(p.tw)).length;
    return { n: w.length, hit, rate: hit / w.length };
  }

  /** 條件統計：美股大漲（> th）／大跌（< -th）之後，台股隔日的平均報酬與同向機率 */
  function conditional(pairs, n, th = 0.01) {
    const w = tail(pairs, n);
    const stat = arr => {
      if (!arr.length) return { n: 0, mean: null, sameDirRate: null };
      const mean = arr.reduce((s, p) => s + p.tw, 0) / arr.length;
      const same = arr.filter(p => Math.sign(p.tw) === Math.sign(p.us)).length;
      return { n: arr.length, mean, sameDirRate: same / arr.length };
    };
    return { threshold: th, up: stat(w.filter(p => p.us > th)), down: stat(w.filter(p => p.us < -th)) };
  }

  /**
   * 跟單回測：美股族群 t 日上漲 → 台股隔日做多；下跌 → 做空（持平空手），每日結算。
   * 與買進持有比較；不含交易成本（僅驗證連動訊號是否具方向性，非實際策略績效）。
   */
  function followStrategy(pairs, n, { cost = 0 } = {}) {
    const w = tail(pairs, n);
    if (w.length < MIN_PAIRS) return null;
    let strat = 1, bh = 1, peak = 1, maxDD = 0, hit = 0, counted = 0;
    const curve = [];
    for (const p of w) {
      const pos = Math.sign(p.us);
      strat *= 1 + pos * p.tw - (pos !== 0 ? cost : 0);
      bh *= 1 + p.tw;
      if (pos !== 0 && p.tw !== 0) { counted++; if (pos === Math.sign(p.tw)) hit++; }
      peak = Math.max(peak, strat);
      maxDD = Math.min(maxDD, strat / peak - 1);
      curve.push({ date: p.twDate, strat: (strat - 1) * 100, bh: (bh - 1) * 100 });
    }
    return {
      n: w.length, from: w[0].twDate, to: w[w.length - 1].twDate,
      stratPct: (strat - 1) * 100, bhPct: (bh - 1) * 100, maxDDPct: maxDD * 100,
      hitRate: counted ? hit / counted : null, curve,
    };
  }

  /** 連動強度判讀 */
  function verdict({ corr, hit }) {
    if (corr == null || hit == null) return { level: 0, label: '資料不足', cls: 'bg-slate-100 text-slate-500' };
    if (corr >= 0.4 && hit >= 0.6) return { level: 3, label: '高度連動', cls: 'bg-red-600 text-white' };
    if (corr >= 0.25 && hit >= 0.55) return { level: 2, label: '中度連動', cls: 'bg-orange-100 text-orange-700 border border-orange-300' };
    if (corr >= 0.1 || hit >= 0.52) return { level: 1, label: '弱連動', cls: 'bg-slate-100 text-slate-700 border border-slate-300' };
    return { level: 0, label: '無明顯連動', cls: 'bg-slate-100 text-slate-500' };
  }

  /** 對某個美股報酬序列，計算全部台股候選的連動統計並取前 N 名（依主窗口相關係數） */
  function rankTwByCorr(usSeries, twMap, { main = 120, alt = 60, topN = 3 } = {}) {
    const rows = [];
    for (const [code, info] of Object.entries(twMap)) {
      const pairs = pairSeries(usSeries, info.series);
      const c = corrWindow(pairs, main);
      if (c == null) continue;
      rows.push({
        code, name: info.name, market: info.market,
        corr: c, corrAlt: corrWindow(pairs, alt), beta: betaWindow(pairs, main),
        hit: (hitRate(pairs, main) || {}).rate ?? null, n: Math.min(pairs.length, main),
        lastPct: info.lastPct ?? null, lastDate: info.lastDate ?? null,
      });
    }
    rows.sort((a, b) => b.corr - a.corr);
    return rows.slice(0, topN);
  }

  return {
    MIN_PAIRS, dailyReturns, horizonPct, equalWeightSeries, indexFromReturns, pairSeries,
    pearson, corrWindow, betaWindow, hitRate, conditional, followStrategy, verdict, rankTwByCorr,
  };
});
