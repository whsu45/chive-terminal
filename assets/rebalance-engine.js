/*
 * 指數成分股調整（被動資金）事件回測引擎（瀏覽器與 Node 共用）
 * 規則來源：《指數成分股調整與被動前瞻套利》策略規格書（STRAT-EVENT-IDX-02）
 *
 * 時間點（全部以日 K 計算，股價已做除權息還原）：
 *  - 公告日 A：名單公布日。盤後公告（臺灣 50、高股息指數）於「A 之後第一個交易日開盤」進場；
 *    盤前公告（MSCI、精選高息、價值高息，事件標記 announceTiming = 'preOpen'）於 A 當天開盤進場
 *  - 生效交易日 T：被動基金在收盤最後一盤換股的那天（規格書「生效日 13:25～13:30」）；13:25 委託賣出 ≈ 收盤價成交
 *  - 階段 1（事後名單、上限）：公告前 15 個交易日開盤卡位 → 公告日收盤
 *  - 階段 2→3：公告後第一個交易日開盤 → T 收盤
 *  - 階段 4：T 收盤反手 → T+1 開盤／T+1 收盤／T+3 收盤回補
 * 新增股做多、刪除股做空（階段 4 方向相反）。
 * 超額報酬 = 同方向下個股漲跌 − 加權指數同期間漲跌（未扣成本），用來排除大盤多空的影響。
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.RebalanceEngine = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // 現股：手續費 0.1425% × 2、證交稅 0.3%（放空以融券或個股期操作，成本以現股估算）
  const COST = { fee: 0.001425, tax: 0.003 };
  const PRE_DAYS = 15; // 階段 1：公告前 15 個交易日（約 3 週）
  const CROWD_LOOKBACK = 20; // 擁擠交易：公告前 20 個交易日漲幅
  const CROWD_LIMIT = 0.30;

  // Yahoo 日 K → 還原權息後的 bars（剔除無成交量的假 K 棒，例如休市日）
  function barsFromYahoo(result) {
    const ts = result.timestamp || [];
    const q = result.indicators.quote[0];
    const adj = result.indicators.adjclose ? result.indicators.adjclose[0].adjclose : null;
    const off = (result.meta && result.meta.gmtoffset) || 28800;
    const out = [];
    for (let i = 0; i < ts.length; i++) {
      const o = q.open[i], h = q.high[i], l = q.low[i], c = q.close[i], v = q.volume[i];
      if (o == null || h == null || l == null || c == null || !v) continue;
      const f = adj && adj[i] != null && c > 0 ? adj[i] / c : 1;
      out.push({ date: new Date((ts[i] + off) * 1000).toISOString().slice(0, 10), o: o * f, h: h * f, l: l * f, c: c * f, v, raw: c });
    }
    return out;
  }

  // 第一個日期 ≥ date 的交易日索引（after=true 時為 > date）
  function idxFrom(bars, date, after = false) {
    for (let i = 0; i < bars.length; i++) if (after ? bars[i].date > date : bars[i].date >= date) return i;
    return -1;
  }
  // 最後一個日期 ≤ date 的交易日索引
  function idxUpTo(bars, date) {
    let k = -1;
    for (let i = 0; i < bars.length; i++) { if (bars[i].date <= date) k = i; else break; }
    return k;
  }

  // 公告後第一個可進場的交易日
  const entryIdx = (bars, ev) => (ev.announceDate ? idxFrom(bars, ev.announceDate, ev.announceTiming !== 'preOpen') : -1);

  const ret = (dir, entry, exit) => (dir > 0
    ? (exit * (1 - COST.fee - COST.tax) - entry * (1 + COST.fee)) / entry
    : (entry * (1 - COST.fee - COST.tax) - exit * (1 + COST.fee)) / entry);

  // 單一股票、單一事件的各階段交易；side = 'add' | 'delete'；mkt = 加權指數 { date: bar }（可省略）
  function eventStages(bars, ev, side, mkt = null) {
    const dir = side === 'add' ? 1 : -1;
    const t = idxUpTo(bars, ev.tradeDate);
    if (t < 0 || bars[t].date !== ev.tradeDate) return { error: '價格資料不足或生效交易日不是交易日' };
    // 名單在生效後才公開（或沒有公告日）時，公告後進場的階段無法執行，只保留生效後的階段 4
    const e = entryIdx(bars, ev);
    const a = e - 1; // 公告前最後一個收盤
    const preOk = a >= 0 && e <= t;
    const out = { side, entryDate: preOk ? bars[e].date : null, tradeDate: bars[t].date };
    if (!preOk) out.lateAnnounce = true;
    // k0 / k1：'o' 開盤或 'c' 收盤
    const leg = (i0, k0, i1, k1, d) => {
      const px0 = bars[i0][k0], px1 = bars[i1][k1];
      const l = { from: bars[i0].date, to: bars[i1].date, entry: px0, exit: px1, ret: ret(d, px0, px1), days: i1 - i0 };
      const m0 = mkt && mkt[bars[i0].date], m1 = mkt && mkt[bars[i1].date];
      if (m0 && m1) l.excess = d * ((px1 / px0 - 1) - (m1[k1] / m0[k0] - 1));
      return l;
    };
    if (preOk && a - PRE_DAYS >= 0) out.stage1 = leg(a - PRE_DAYS, 'o', a, 'c', dir);
    if (preOk) out.stage23 = leg(e, 'o', t, 'c', dir);
    if (t + 1 < bars.length) {
      out.stage4_t1open = leg(t, 'c', t + 1, 'o', -dir);
      out.stage4_t1close = leg(t, 'c', t + 1, 'c', -dir);
    }
    if (t + 3 < bars.length) out.stage4 = leg(t, 'c', t + 3, 'c', -dir);
    if (preOk && a - CROWD_LOOKBACK >= 0) {
      out.preRun = bars[a].c / bars[a - CROWD_LOOKBACK].c - 1;
      out.crowded = dir > 0 && out.preRun > CROWD_LIMIT;
    }
    return out;
  }

  function summarize(rets) {
    const n = rets.length;
    if (!n) return { n: 0 };
    const s = [...rets].sort((x, y) => x - y);
    const q = p => { const k = (n - 1) * p, lo = Math.floor(k), hi = Math.ceil(k); return s[lo] + (s[hi] - s[lo]) * (k - lo); };
    const gp = rets.filter(r => r > 0).reduce((a, b) => a + b, 0), gl = -rets.filter(r => r <= 0).reduce((a, b) => a + b, 0);
    return { n, winRate: rets.filter(r => r > 0).length / n, avgRet: rets.reduce((a, b) => a + b, 0) / n, median: q(0.5), p25: q(0.25), p75: q(0.75), profitFactor: gl > 0 ? gp / gl : null };
  }

  // 報酬分布 + 超額報酬（相對加權指數）
  function legStats(legs) {
    const ex = legs.filter(l => l.excess != null).map(l => l.excess);
    return {
      ...summarize(legs.map(l => l.ret)),
      excessAvg: ex.length ? ex.reduce((x, y) => x + y, 0) / ex.length : null,
      excessMedian: ex.length ? summarize(ex).median : null,
      excessWin: ex.length ? ex.filter(x => x > 0).length / ex.length : null,
    };
  }

  // 依指數與方向彙整各階段
  const STAGES = ['stage1', 'stage23', 'stage4_t1open', 'stage4_t1close', 'stage4'];
  function aggregate(rows) {
    const out = {};
    for (const side of ['add', 'delete']) {
      const rs = rows.filter(r => r.side === side && !r.error);
      out[side] = Object.fromEntries(STAGES.map(st => [st, legStats(rs.filter(r => r[st]).map(r => r[st]))]));
      out[side].stage23_crowded = legStats(rs.filter(r => r.crowded && r.stage23).map(r => r.stage23));
      out[side].stage23_normal = legStats(rs.filter(r => r.crowded === false && r.stage23).map(r => r.stage23));
    }
    return out;
  }

  // 交易計畫：用該指數的歷史分布推估出場點位
  function plan(bars, ev, side, hist, today) {
    const dir = side === 'add' ? 1 : -1;
    const last = bars[bars.length - 1];
    const e = entryIdx(bars, ev);
    const entered = e >= 0 && bars[e].date <= today;
    const entry = entered ? bars[e].o : last.c;
    const h23 = hist && hist[side] && hist[side].stage23;
    const h4 = hist && hist[side] && hist[side].stage4;
    const a = e >= 0 ? e - 1 : bars.length - 1;
    const preRun = a - CROWD_LOOKBACK >= 0 ? bars[a].c / bars[a - CROWD_LOOKBACK].c - 1 : null;
    const target = r => (r == null ? null : entry * (1 + dir * r));
    return {
      side, entered, entryDate: entered ? bars[e].date : null, entryRef: entry, lastClose: last.c, lastDate: last.date,
      pnlNow: entered ? ret(dir, entry, last.c) : null,
      buyWhen: entered ? `${bars[e].date} 開盤（已進場）` : ev.announceTiming === 'preOpen' ? '公告當天開盤' : '公告後第一個交易日開盤',
      sellWhen: `${ev.tradeDate} 13:25 委託市價賣出（收盤集合競價成交）`,
      exitMedian: h23 && h23.n ? target(h23.median) : null, exitP25: h23 && h23.n ? target(h23.p25) : null, exitP75: h23 && h23.n ? target(h23.p75) : null,
      histN: h23 ? h23.n || 0 : 0, histMedian: h23 && h23.n ? h23.median : null, histWin: h23 && h23.n ? h23.winRate : null,
      reverse: { when: `${ev.tradeDate} 收盤反手${dir > 0 ? '放空' : '回補做多'}，T+3 收盤前回補`, median: h4 && h4.n ? h4.median : null, n: h4 ? h4.n || 0 : 0 },
      preRun, crowded: dir > 0 && preRun != null && preRun > CROWD_LIMIT,
    };
  }

  return { COST, PRE_DAYS, CROWD_LIMIT, STAGES, barsFromYahoo, idxFrom, idxUpTo, eventStages, summarize, aggregate, plan, ret };
});
