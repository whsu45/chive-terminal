/*
 * 開盤區間突破（ORB）當沖引擎（瀏覽器與 Node 共用）
 * 規則來源：《開盤區間突破系統 (Open Range Breakout, ORB)》策略規格書（現貨版本，5 分 K）
 *
 * 時序原則（避免偷看未來）：
 *  - 開盤區間 = 09:00～09:30 的 6 根 5 分 K；09:30 之後才開始找突破
 *  - 突破以「5 分 K 收盤價穿越觸發價」確認，並以該根收盤價成交
 *  - 停損 / 移動停利以盤中觸價成交（跳空則以開盤價成交）；移動停利的防守價在 K 棒收盤後才更新
 *  - 13:20 強制平倉 = 13:15 那根 5 分 K 的收盤價
 * 資料：Yahoo Finance 5 分 K。09:00 那根的成交量通常為 0（開盤集合競價量未計入），
 *       計算開盤區間平均量時只採用量 > 0 的 K 棒。
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.OrbEngine = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // 現股當沖：手續費 0.1425% × 2、當沖證交稅減半 0.15%
  const COST = { fee: 0.001425, tax: 0.0015 };
  const DEFAULTS = {
    k: 0.2, // 突破延伸係數
    orBars: 6, // 開盤區間 09:00～09:30
    volMult: 1.5, // 突破 K 量 > 開盤區間平均量 × 1.5
    stopPct: 0.01, // 進場點 ±1% 硬停損
    trailMult: 1.5, // 浮盈達 1.5 × Range 啟動移動停利
    trailBars: 3, // 以最近 3 根 K 棒低點（多）／高點（空）防守
    lastEntry: '13:10', // 最後一根可進場的 K 棒（13:15 收盤即平倉）
    exitBar: '13:15', // 13:20 強制平倉
    maxTrades: 2, // 當日觸發 2 次即停止
    narrowFilter: true, // 避坑 1：Range < 前 5 日 ATR × 15% 暫停進場
    narrowPct: 0.15,
    marketFilter: true, // 避坑 2：指數紅盤才做多、翻黑才做空
  };

  // Yahoo chart API → 依交易日分組的 5 分 K（剔除空值 K 棒與整天無資料的休市日）
  function sessionsFromYahoo(result) {
    const ts = result.timestamp || [];
    const q = (result.indicators && result.indicators.quote && result.indicators.quote[0]) || {};
    const off = (result.meta && result.meta.gmtoffset) || 28800;
    const days = new Map();
    for (let i = 0; i < ts.length; i++) {
      const o = q.open[i], h = q.high[i], l = q.low[i], c = q.close[i];
      if (o == null || h == null || l == null || c == null) continue;
      const d = new Date((ts[i] + off) * 1000);
      const date = d.toISOString().slice(0, 10), time = d.toISOString().slice(11, 16);
      if (!days.has(date)) days.set(date, []);
      days.get(date).push({ time, o, h, l, c, v: q.volume[i] || 0 });
    }
    return [...days.entries()].sort((a, b) => (a[0] < b[0] ? -1 : 1)).map(([date, bars]) => ({ date, bars }));
  }

  // 日 K（由 5 分 K 合成）與前 5 日平均真實區間
  function dailyAtr(sessions) {
    const daily = sessions.map(s => ({
      date: s.date, o: s.bars[0].o, h: Math.max(...s.bars.map(b => b.h)), l: Math.min(...s.bars.map(b => b.l)), c: s.bars[s.bars.length - 1].c,
      v: s.bars.reduce((a, b) => a + b.v, 0),
    }));
    const tr = daily.map((d, i) => (i === 0 ? d.h - d.l : Math.max(d.h, daily[i - 1].c) - Math.min(d.l, daily[i - 1].c)));
    // atrPrev[i]：第 i 天之前 5 天的平均 TR（不含當日）
    const atrPrev = daily.map((_, i) => (i < 5 ? null : tr.slice(i - 5, i).reduce((a, b) => a + b, 0) / 5));
    return { daily, atrPrev };
  }

  // 大盤：每根 5 分 K 收盤時，加權指數相對昨收是紅盤 (1)、黑盤 (-1) 或平盤 (0)
  function marketTone(mktSessions) {
    const out = {};
    for (let i = 1; i < mktSessions.length; i++) {
      const prev = mktSessions[i - 1].bars[mktSessions[i - 1].bars.length - 1].c;
      const m = {};
      for (const b of mktSessions[i].bars) m[b.time] = Math.sign(b.c - prev);
      out[mktSessions[i].date] = m;
    }
    return out;
  }

  const netRet = (side, entry, exit) => (side === 'long'
    ? (exit * (1 - COST.fee - COST.tax) - entry * (1 + COST.fee)) / entry
    : (entry * (1 - COST.fee - COST.tax) - exit * (1 + COST.fee)) / entry);

  // 單日模擬：回傳開盤區間、略過原因與交易
  function simulateDay(session, opts = {}, ctx = {}) {
    const p = { ...DEFAULTS, ...opts };
    const bars = session.bars;
    const day = { date: session.date, trades: [], complete: bars.some(b => b.time >= p.exitBar) };
    const orb = bars.filter(b => b.time < '09:30');
    if (orb.length < p.orBars || bars[0].time !== '09:00') { day.skip = '開盤區間資料不完整'; return day; }
    const H = Math.max(...orb.map(b => b.h)), L = Math.min(...orb.map(b => b.l)), R = H - L;
    const vols = orb.map(b => b.v).filter(v => v > 0);
    const avgVol = vols.length ? vols.reduce((a, b) => a + b, 0) / vols.length : 0;
    day.or = { high: H, low: L, range: R, mid: (H + L) / 2, longTrig: H + p.k * R, shortTrig: L - p.k * R, avgVol, atrPrev: ctx.atrPrev ?? null };
    if (!(R > 0)) { day.skip = '開盤區間無波動'; return day; }
    if (p.narrowFilter && ctx.atrPrev != null && R < p.narrowPct * ctx.atrPrev) { day.skip = `開盤窄幅（Range ${R.toFixed(2)} < 前 5 日 ATR 的 ${p.narrowPct * 100}%）`; return day; }

    const tone = ctx.tone || null;
    const used = { long: false, short: false };
    let pos = null;
    // 13:15 那根若缺資料，改以之後第一根 K 棒平倉
    const after = bars.filter(b => b.time >= '09:30');
    const cut = after.findIndex(b => b.time >= p.exitBar);
    if (cut >= 0) after.length = cut + 1;
    for (let k = 0; k < after.length; k++) {
      const b = after[k];
      const prevC = k === 0 ? orb[orb.length - 1].c : after[k - 1].c;
      if (pos) {
        // 盤中觸價停損
        const hitStop = pos.side === 'long' ? b.l <= pos.stop : b.h >= pos.stop;
        if (hitStop) {
          const px = pos.side === 'long' ? Math.min(b.o, pos.stop) : Math.max(b.o, pos.stop);
          close(pos, b, px, pos.trailOn ? '移動停利' : '停損');
        } else {
          // 收盤後更新移動停利
          const fav = pos.side === 'long' ? b.h - pos.entryPx : pos.entryPx - b.l;
          if (!pos.trailOn && fav >= p.trailMult * R) pos.trailOn = true;
          if (pos.trailOn) {
            const recent = after.slice(Math.max(0, k - p.trailBars + 1), k + 1);
            pos.stop = pos.side === 'long'
              ? Math.max(pos.stop, pos.entryPx, Math.min(...recent.map(x => x.l)))
              : Math.min(pos.stop, pos.entryPx, Math.max(...recent.map(x => x.h)));
          }
          if (b.time >= p.exitBar) close(pos, b, b.c, '13:20 強制平倉');
        }
        continue;
      }
      if (b.time > p.lastEntry || day.trades.length >= p.maxTrades) continue;
      const volOk = avgVol > 0 && b.v > p.volMult * avgVol;
      const t = tone && tone[b.time];
      for (const side of ['long', 'short']) {
        if (used[side] || pos) continue;
        const trig = side === 'long' ? day.or.longTrig : day.or.shortTrig;
        const cross = side === 'long' ? prevC <= trig && b.c > trig : prevC >= trig && b.c < trig;
        if (!cross || !volOk) continue;
        if (p.marketFilter && tone && !(side === 'long' ? t === 1 : t === -1)) continue;
        used[side] = true;
        const stop = side === 'long' ? Math.max(day.or.mid, b.c * (1 - p.stopPct)) : Math.min(day.or.mid, b.c * (1 + p.stopPct));
        pos = { side, entryTime: b.time, entryPx: b.c, stop, trailOn: false };
      }
    }
    // 資料未到 13:20（盤中）→ 部位仍持有
    if (pos) day.trades.push({ side: pos.side, entryTime: pos.entryTime, entryPx: pos.entryPx, stop: pos.stop, open: true });
    return day;

    function close(ps, b, px, reason) {
      day.trades.push({ side: ps.side, entryTime: ps.entryTime, entryPx: ps.entryPx, exitTime: b.time, exitPx: px, reason, ret: netRet(ps.side, ps.entryPx, px) });
      pos = null;
    }
  }

  function summarize(trades) {
    const closed = trades.filter(t => !t.open);
    const n = closed.length;
    if (!n) return { n: 0 };
    const rets = closed.map(t => t.ret);
    const wins = rets.filter(r => r > 0), losses = rets.filter(r => r <= 0);
    const gp = wins.reduce((a, b) => a + b, 0), gl = -losses.reduce((a, b) => a + b, 0);
    const avgWin = wins.length ? gp / wins.length : 0, avgLoss = losses.length ? gl / losses.length : 0;
    let cum = 0, peak = 0, mdd = 0;
    for (const r of rets) { cum += r; peak = Math.max(peak, cum); mdd = Math.min(mdd, cum - peak); }
    return {
      n, winRate: wins.length / n, avgRet: rets.reduce((a, b) => a + b, 0) / n, total: cum, mdd,
      payoff: avgLoss > 0 ? avgWin / avgLoss : null, profitFactor: gl > 0 ? gp / gl : null,
    };
  }

  // 回測：已收盤的交易日（盤中的最新一天另外回傳 latest）
  function backtest(sessions, opts = {}, mktSessions = null) {
    const { atrPrev } = dailyAtr(sessions);
    const tones = mktSessions ? marketTone(mktSessions) : null;
    const days = sessions.map((s, i) => simulateDay(s, opts, { atrPrev: atrPrev[i], tone: tones ? tones[s.date] || null : null }));
    const done = days.filter(d => d.complete);
    const trades = done.flatMap(d => d.trades.map(t => ({ ...t, date: d.date })));
    return { days, trades, stats: summarize(trades), long: summarize(trades.filter(t => t.side === 'long')), short: summarize(trades.filter(t => t.side === 'short')), sessions: done.length };
  }

  // 短沖判斷：以本股近 60 個交易日的回測結果分級
  function verdict(stats, avgVolLots) {
    if (avgVolLots != null && avgVolLots < 1000) return { key: 'ILLIQUID', label: '流動性不足', note: `日均量約 ${Math.round(avgVolLots).toLocaleString()} 張（< 1,000 張），滑價風險高` };
    if (!stats.n || stats.n < 8) return { key: 'FEW', label: '樣本不足', note: `近期觸發 ${stats.n || 0} 次（< 8 次），無法判斷` };
    if (stats.profitFactor != null && stats.profitFactor >= 1.2 && stats.avgRet > 0) return { key: 'GOOD', label: '適合 ORB 短沖', note: '扣除當沖成本後歷史期望值為正' };
    if (stats.avgRet <= 0 || (stats.profitFactor != null && stats.profitFactor < 1)) return { key: 'BAD', label: '不適合 ORB 短沖', note: '扣除當沖成本後歷史期望值為負' };
    return { key: 'MEH', label: '效果普通', note: '期望值略正，但優勢不明顯' };
  }

  return { COST, DEFAULTS, sessionsFromYahoo, dailyAtr, marketTone, simulateDay, summarize, backtest, verdict, netRet };
});
