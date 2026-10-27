/*
 * 個股策略引擎（瀏覽器與 Node 共用）
 * 規則來源：《哥有籌必爆》/ 權證小哥 策略文件（s-strategy.md）
 *          Larry Williams 波動率收斂突破（VCB）策略企劃書
 *
 * 資料限制（免費 FinMind 等級）：
 *  - 「主力買賣超」(前 15 大分點) 無法取得 → 以「三大法人合計買賣超」代理 (mf)
 *  - 集保股權分散、券商分點、盤前試撮、新聞利多 → 無法取得，相關條件標示為未驗證
 *
 * 時序原則（避免偷看未來）：所有訊號以當日收盤後資料判斷，回測一律於「次一交易日開盤」成交；
 * 價格型停損/停利以盤中觸價成交 (跳空則以開盤價成交)。
 * 例外：VCB 為收盤後確認型態、次一交易日盤中「觸價突破」才進場（預掛突破買單），未突破則不進場。
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.StrategyEngine = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const COST = { buyFee: 0.001425, sellFee: 0.001425, tax: 0.003 };

  // ==========================================
  // 資料整理：FinMind 原始資料 → bars
  // ==========================================
  function buildBars(raw) {
    const prices = (raw.prices || []).filter(p => p.open > 0 && p.close > 0 && p.max > 0 && p.min > 0);
    prices.sort((a, b) => (a.date < b.date ? -1 : 1));

    // 除權息還原（向前調整）：ex-date 之前的價格乘上 after/before
    const divs = (raw.dividends || [])
      .filter(d => d.before_price > 0 && d.after_price > 0 && d.after_price < d.before_price)
      .map(d => ({ date: d.date, factor: d.after_price / d.before_price }))
      .sort((a, b) => (a.date < b.date ? -1 : 1));

    const inst = {};
    for (const r of raw.institutional || []) {
      const o = inst[r.date] || (inst[r.date] = { foreign: 0, trust: 0, dealer: 0 });
      const net = ((r.buy || 0) - (r.sell || 0)) / 1000;
      if (r.name === 'Foreign_Investor' || r.name === 'Foreign_Dealer_Self') o.foreign += net;
      else if (r.name === 'Investment_Trust') o.trust += net;
      else o.dealer += net;
    }
    // 大盤（加權指數）是否在月線之上：VCB 趨勢輔助濾網
    const mktUp = {};
    {
      const mk = (raw.market || []).filter(p => p.close > 0).sort((a, b) => (a.date < b.date ? -1 : 1));
      const ma = sma(mk.map(p => p.close), 20);
      mk.forEach((p, k) => { if (ma[k] != null) mktUp[p.date] = p.close > ma[k]; });
    }
    const margin = {};
    for (const r of raw.margin || []) {
      margin[r.date] = { marginBal: r.MarginPurchaseTodayBalance, shortBal: r.ShortSaleTodayBalance };
    }

    const bars = prices.map(p => {
      let f = 1;
      for (const d of divs) if (p.date < d.date) f *= d.factor;
      const io = inst[p.date];
      const m = margin[p.date];
      return {
        date: p.date,
        open: p.open * f, high: p.max * f, low: p.min * f, close: p.close * f,
        rawClose: p.close,
        // 當日成交均價（成交金額 ÷ 成交股數），同樣做除權息還原
        vwap: p.Trading_Volume > 0 && p.Trading_money > 0 ? (p.Trading_money / p.Trading_Volume) * f : p.close * f,
        volume: p.Trading_Volume / 1000,
        foreign: io ? io.foreign : null,
        trust: io ? io.trust : null,
        dealer: io ? io.dealer : null,
        mf: io ? io.foreign + io.trust + io.dealer : null,
        marginBal: m ? m.marginBal : null,
        shortBal: m ? m.shortBal : null,
        mktUp: p.date in mktUp ? mktUp[p.date] : null,
      };
    });
    return bars;
  }

  // ==========================================
  // 指標
  // ==========================================
  function sma(arr, n) {
    const out = new Array(arr.length).fill(null);
    let s = 0;
    for (let i = 0; i < arr.length; i++) {
      s += arr[i];
      if (i >= n) s -= arr[i - n];
      if (i >= n - 1) out[i] = s / n;
    }
    return out;
  }

  function rollingStd(arr, n, mean) {
    const out = new Array(arr.length).fill(null);
    for (let i = n - 1; i < arr.length; i++) {
      let v = 0;
      for (let k = i - n + 1; k <= i; k++) v += (arr[k] - mean[i]) ** 2;
      out[i] = Math.sqrt(v / n);
    }
    return out;
  }

  function percentile(sorted, q) {
    if (!sorted.length) return null;
    const pos = (sorted.length - 1) * q;
    const lo = Math.floor(pos), hi = Math.ceil(pos);
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
  }

  function computeIndicators(bars) {
    const n = bars.length;
    const close = bars.map(b => b.close);
    const vol = bars.map(b => b.volume);
    const ma5 = sma(close, 5), ma10 = sma(close, 10), ma20 = sma(close, 20), ma60 = sma(close, 60);
    const std20 = rollingStd(close, 20, ma20);
    const upper = ma20.map((m, i) => (m == null ? null : m + 2 * std20[i]));
    const lower = ma20.map((m, i) => (m == null ? null : m - 2 * std20[i]));
    const floor99 = ma20.map((m, i) => (m == null ? null : m - 2.58 * std20[i]));
    const bw = upper.map((u, i) => (u == null || lower[i] <= 0 ? null : (u - lower[i]) / lower[i]));
    const slope = (a, i, k = 1) => (i >= k && a[i] != null && a[i - k] != null && a[i - k] !== 0 ? (a[i] - a[i - k]) / a[i - k] : null);
    const upperSlope = upper.map((_, i) => slope(upper, i));
    const lowerSlope = lower.map((_, i) => slope(lower, i));
    const ma20Slope = ma20.map((_, i) => slope(ma20, i));
    const ma20Slope5 = ma20.map((_, i) => slope(ma20, i, 5));
    const ma60Slope5 = ma60.map((_, i) => slope(ma60, i, 5));
    const bias = close.map((c, i) => (ma20[i] == null ? null : (c - ma20[i]) / ma20[i]));
    // 「過去 20 日均量」：不含當日
    const volMa20Prev = new Array(n).fill(null);
    { let s = 0; for (let i = 0; i < n; i++) { if (i >= 20) volMa20Prev[i] = s / 20; s += vol[i]; if (i >= 20) s -= vol[i - 20]; } }
    // 乖離率歷史分佈（滾動 250 日，僅用過去資料）
    const biasP01 = new Array(n).fill(null), biasP05 = new Array(n).fill(null);
    const biasP95 = new Array(n).fill(null), biasP99 = new Array(n).fill(null);
    for (let i = 0; i < n; i++) {
      if (i < 140) continue;
      const w = bias.slice(Math.max(19, i - 249), i + 1).filter(v => v != null).sort((a, b) => a - b);
      biasP01[i] = percentile(w, 0.01); biasP05[i] = percentile(w, 0.05);
      biasP95[i] = percentile(w, 0.95); biasP99[i] = percentile(w, 0.99);
    }
    const mf = bars.map(b => (b.mf == null ? 0 : b.mf));
    const mfCum = []; { let s = 0; for (let i = 0; i < n; i++) { s += mf[i]; mfCum.push(s); } }
    const volCum = []; { let s = 0; for (let i = 0; i < n; i++) { s += vol[i]; volCum.push(s); } }
    // 主力成本線：近 N 日主力(法人)買超日，以買超張數加權的成交均價
    const mfCost = N => bars.map((_, i) => {
      if (i < N - 1) return null;
      let amt = 0, qty = 0;
      for (let k = i - N + 1; k <= i; k++) {
        const m = bars[k].mf;
        if (m != null && m > 0) { amt += m * (bars[k].vwap ?? bars[k].close); qty += m; }
      }
      return qty > 0 ? amt / qty : null;
    });
    const mfCost20 = mfCost(20), mfCost60 = mfCost(60);
    // 真實區間 TrueRange 與其 5 日均值（VCB）
    const tr = bars.map((b, i) => (i === 0 ? b.high - b.low : Math.max(b.high, bars[i - 1].close) - Math.min(b.low, bars[i - 1].close)));
    const atr5 = sma(tr, 5);
    const volMa5 = sma(vol, 5);
    return {
      close, vol, ma5, ma10, ma20, ma60, std20, upper, lower, floor95: lower, floor99, bw,
      upperSlope, lowerSlope, ma20Slope, ma20Slope5, ma60Slope5, bias, volMa20Prev,
      biasP01, biasP05, biasP95, biasP99, mf, mfCum, volCum, mfCost20, mfCost60, tr, atr5, volMa5,
    };
  }

  // ==========================================
  // 輔助函式
  // ==========================================
  function makeCtx(bars) {
    const ind = computeIndicators(bars);
    const h = {
      mfSum(i, k) { const a = i - k; return ind.mfCum[i] - (a >= 0 ? ind.mfCum[a] : 0); },
      volSum(i, k) { const a = i - k; return ind.volCum[i] - (a >= 0 ? ind.volCum[a] : 0); },
      highest(i0, i1) { let m = -Infinity, at = -1; for (let k = Math.max(0, i0); k <= i1; k++) if (bars[k].high > m) { m = bars[k].high; at = k; } return { v: m, at }; },
      lowest(i0, i1) { let m = Infinity, at = -1; for (let k = Math.max(0, i0); k <= i1; k++) if (bars[k].low < m) { m = bars[k].low; at = k; } return { v: m, at }; },
      pctChg(i) { return i > 0 ? bars[i].close / bars[i - 1].close - 1 : 0; },
      body(i) { return Math.abs(bars[i].close - bars[i].open); },
      upperShadow(i) { return bars[i].high - Math.max(bars[i].open, bars[i].close); },
      lowerShadow(i) { return Math.min(bars[i].open, bars[i].close) - bars[i].low; },
      hasChip(i) { return bars[i].mf != null; },
      trustSum(i, k) { let s = 0, seen = false; for (let x = Math.max(0, i - k + 1); x <= i; x++) if (bars[x].trust != null) { s += bars[x].trust; seen = true; } return seen ? s : null; },
      hasMargin(i) { return bars[i].marginBal != null && bars[i].shortBal != null; },
      // 盤整期：過去 N 天（不含當日）窄幅震盪
      consolidation(i, N, maxRange) {
        if (i - N < 0) return null;
        const hi = h.highest(i - N, i - 1).v, lo = h.lowest(i - N, i - 1).v;
        const range = hi / lo - 1;
        return { ok: range <= maxRange, range, hi, lo };
      },
      // 月線扣抵：明日若收平盤，MA20 是否上揚（今日收盤 > 明日扣抵值 close[i-19]）
      ma20TurningUp(i) { return i >= 19 && bars[i].close > bars[i - 19].close; },
      // 吊首線（純型態）
      hangingShape(i) {
        const b = bars[i], range = b.high - b.low;
        if (range <= 0) return false;
        const body = h.body(i), lo = h.lowerShadow(i), up = h.upperShadow(i);
        return body <= 0.2 * range && lo >= 2 * body && lo >= 0.5 * range && up <= 0.1 * range;
      },
      // 沿布林上軌（近 5 日至少 3 日收盤 ≥ 上軌 98%）
      ridingUpper(i) {
        let c = 0;
        for (let k = Math.max(0, i - 4); k <= i; k++) if (ind.upper[k] != null && bars[k].close >= ind.upper[k] * 0.98) c++;
        return c >= 3;
      },
    };
    return { bars, ind, h, opts: {} };
  }

  const fmt = (v, d = 2) => (v == null || !isFinite(v) ? 'NA' : Number(v).toFixed(d));
  const pct = (v, d = 1) => (v == null || !isFinite(v) ? 'NA' : (v * 100).toFixed(d) + '%');
  const lots = v => (v == null ? 'NA' : (v > 0 ? '+' : '') + Math.round(v).toLocaleString() + ' 張');

  // ==========================================
  // 訊號定義
  // exit: 回測出場規則（皆依文件）
  //   bald     → 停損：發動紅K開盤價（最大風險 10%）；停利：爆量滯漲或吊首線
  //   bbUpper  → 收盤離開上軌即出場；風控 5% 停損
  //   midBand  → 目標中軌（月線）；風控 5% 停損
  //   trail5   → 自進場價或進場後最高價回檔 5% 出場；月線下彎跌破（葛蘭碧⑤）出場
  //   ignition → 停損 min(近20日低點, 進場價×0.93)；月線下彎跌破出場
  //   squeeze  → 跌破月線或軋空衰竭（資減券減＋主力賣）出場
  //   floorSop → 搶反彈 SOP：1/10 資金、有賺就跑、最多加碼一次、破前低認賠、最多持有 5 日
  //   vcb      → 停損 max(進場價−4%, 型態日低點)；收盤獲利 ≥5% 後停損移至成本，
  //              之後以前一日低點（盤中）或收盤跌破 5MA 移動停利；持有第 3 日收盤獲利 <1.5% 時間停損
  // ==========================================
  const SIGNALS = [
    // ---------------- 多方 ----------------
    {
      id: 'BALD_RED', side: 'buy', name: '橫盤爆量光頭大紅棒', ref: '第一課 策略一', exit: 'bald',
      detect({ bars, ind, h }, i) {
        if (i < 61) return null;
        const b = bars[i];
        const cons = h.consolidation(i, 40, 0.20);
        if (!cons || !cons.ok) return null;
        const tangled = k => ind.ma60[k] != null && Math.abs(ind.ma20[k] - ind.ma60[k]) / ind.ma60[k] <= 0.03
          && Math.abs(ind.ma20Slope5[k]) <= 0.015;
        if (!(tangled(i) || tangled(i - 1))) return null;
        const bodyPct = (b.close - b.open) / b.open;
        if (!(b.close > b.open && (bodyPct >= 0.05 || h.pctChg(i) >= 0.05))) return null;
        if ((b.high - b.close) / b.close > 0.005) return null;
        if (!(b.volume >= 2 * ind.volMa20Prev[i])) return null;
        if (!(b.close > Math.max(ind.ma5[i], ind.ma10[i], ind.ma20[i], ind.ma60[i]))) return null;
        if (!(b.close > cons.hi)) return null;
        const volX = b.volume / ind.volMa20Prev[i];
        return {
          strength: h.pctChg(i) >= 0.095 ? 3 : 2,
          reasons: [
            `前 40 日橫盤振幅 ${pct(cons.range)}（≤20%），收盤突破區間高點 ${fmt(cons.hi)}`,
            `月線/季線糾結（差距 ${pct(Math.abs(ind.ma20[i] - ind.ma60[i]) / ind.ma60[i])}），紅K一次站上 5/10/20/60MA`,
            `實體漲幅 ${pct(bodyPct)}、上影線 ${pct((b.high - b.close) / b.close, 2)}（光頭）`,
            `成交量為 20 日均量 ${volX.toFixed(1)} 倍（爆量）`,
          ],
          stop: Math.max(b.open, b.close * 0.9),
          stopNote: '跌破發動紅K開盤價（最大風險 10%）',
          targetNote: '抱到高檔爆量滯漲或吊首線出場',
        };
      },
    },
    {
      id: 'BB_SQUEEZE_BREAK', side: 'buy', name: '窄帶寬開布林＋主力買超', ref: '布林 戰略一 多方', exit: 'bbUpper',
      detect({ bars, ind, h }, i) {
        if (i < 21 || ind.bw[i - 1] == null) return null;
        if (!h.hasChip(i)) return null;
        const b = bars[i];
        if (!(ind.bw[i - 1] <= 0.05 && ind.upperSlope[i] >= 0.01 && b.mf > 0 && b.close > ind.upper[i])) return null;
        return {
          strength: ind.upperSlope[i] >= 0.03 ? 3 : 2,
          reasons: [
            `前一日帶寬 ${pct(ind.bw[i - 1])}（≤5% 壓縮）`,
            `上軌斜率 ${pct(ind.upperSlope[i], 2)}（≥1% 開布林）`,
            `收盤 ${fmt(b.close)} 突破上軌 ${fmt(ind.upper[i])}`,
            `主力(法人)買超 ${lots(b.mf)}`,
          ],
          stop: b.close * 0.95, stopNote: '短線 5% 停損',
          targetNote: '沿上軌續抱，收盤離開上軌即停利',
        };
      },
    },
    {
      id: 'BB_WIDE_LOWER', side: 'buy', name: '大帶寬回測下軌＋主力吃貨', ref: '布林 戰略二-1', exit: 'midBand',
      detect({ bars, ind, h }, i) {
        if (i < 40 || ind.bw[i] == null || !h.hasChip(i)) return null;
        const b = bars[i];
        const mf20 = h.mfSum(i, 20);
        if (!(ind.bw[i] >= 0.20 && b.low <= ind.lower[i] * 1.02 && mf20 > 0)) return null;
        return {
          strength: 2,
          reasons: [
            `帶寬 ${pct(ind.bw[i])}（≥20% 區間震盪型）`,
            `最低價 ${fmt(b.low)} 觸及下軌 ${fmt(ind.lower[i])}`,
            `近 20 日主力(法人)累計買超 ${lots(mf20)}`,
          ],
          stop: b.close * 0.95, stopNote: '短線 5% 停損',
          target: ind.ma20[i], targetNote: `目標中軌（月線）${fmt(ind.ma20[i])}`,
        };
      },
    },
    {
      id: 'GRANVILLE_1', side: 'buy', name: '葛蘭碧① 帶量突破上揚月線', ref: '葛蘭碧 買點①', exit: 'trail5',
      detect({ bars, ind, h }, i) {
        if (i < 25 || !h.hasChip(i)) return null;
        const b = bars[i], p = bars[i - 1];
        if (!(ind.ma20[i] > ind.ma20[i - 1] && p.close <= ind.ma20[i - 1] && b.close > ind.ma20[i])) return null;
        if (!(b.volume > ind.volMa20Prev[i])) return null;
        const mf5 = h.mfSum(i, 5);
        if (mf5 < 0) return null; // 主力在賣的均線買點一律是送命題
        return {
          strength: 1,
          reasons: [`月線上揚，收盤由下往上突破月線 ${fmt(ind.ma20[i])}`, `量 ${(b.volume / ind.volMa20Prev[i]).toFixed(1)} 倍均量`, `近 5 日主力(法人) ${lots(mf5)}（未翻賣）`],
          stop: b.close * 0.95, stopNote: '進場價或最高點回檔 5% 停損',
          targetNote: '回升架構，回檔不破月線續抱',
        };
      },
    },
    {
      id: 'GRANVILLE_2', side: 'buy', name: '葛蘭碧② 上揚月線假跌破', ref: '葛蘭碧 買點②', exit: 'trail5',
      detect({ bars, ind, h }, i) {
        if (i < 25 || !h.hasChip(i)) return null;
        const b = bars[i], p = bars[i - 1];
        if (!(ind.ma20Slope5[i] > 0 && ind.ma20[i] > ind.ma20[i - 1])) return null;
        if (!(p.close >= ind.ma20[i - 1] && b.close < ind.ma20[i] && b.close >= ind.ma20[i] * 0.97)) return null;
        const mf5 = h.mfSum(i, 5);
        if (mf5 < 0) return null;
        return {
          strength: 1,
          reasons: [`月線持續上揚（5 日斜率 ${pct(ind.ma20Slope5[i], 2)}）`, `收盤短線跌破月線（${pct(b.close / ind.ma20[i] - 1, 2)}）＝多頭洗盤`, `近 5 日主力(法人) ${lots(mf5)}（未翻賣）`],
          stop: b.close * 0.95, stopNote: '進場價或最高點回檔 5% 停損',
          targetNote: '回升架構，站回月線續抱',
        };
      },
    },
    {
      id: 'GRANVILLE_3', side: 'buy', name: '葛蘭碧③ 回測上揚月線支撐', ref: '葛蘭碧 買點③ / 矩陣2', exit: 'trail5',
      detect({ bars, ind, h }, i) {
        if (i < 25 || !h.hasChip(i)) return null;
        const b = bars[i], p = bars[i - 1];
        if (!(ind.ma20[i] > ind.ma20[i - 1] && ind.ma20Slope5[i] > 0)) return null;
        if (!(b.low <= ind.ma20[i] * 1.01 && b.close > ind.ma20[i] && b.close > b.open && p.close > ind.ma20[i - 1])) return null;
        const mf5 = h.mfSum(i, 5);
        if (mf5 < 0) return null;
        return {
          strength: 1,
          reasons: [`月線上揚，盤中回測月線 ${fmt(ind.ma20[i])} 收紅K獲撐`, `近 5 日主力(法人) ${lots(mf5)}（未翻賣）`],
          stop: b.close * 0.95, stopNote: '進場價或最高點回檔 5% 停損（矩陣2 嚴格停損）',
          targetNote: '順勢波段，沿月線推升續抱',
        };
      },
    },
    {
      id: 'FLOOR_REBOUND', side: 'buy', name: '葛蘭碧④ 地板線爆量搶反彈', ref: '葛蘭碧④ / 搶反彈 SOP', exit: 'floorSop',
      detect({ bars, ind }, i) {
        if (i < 21 || ind.floor95[i] == null) return null;
        const b = bars[i];
        if (!(b.close <= ind.floor95[i] && b.volume >= 2 * ind.volMa20Prev[i])) return null;
        return {
          strength: b.close <= ind.floor99[i] ? 2 : 1,
          reasons: [
            `收盤 ${fmt(b.close)} 跌破 95% 地板線 ${fmt(ind.floor95[i])}${b.close <= ind.floor99[i] ? `（並跌破 99% 地板 ${fmt(ind.floor99[i])}）` : ''}`,
            `爆量 ${(b.volume / ind.volMa20Prev[i]).toFixed(1)} 倍均量（有水花＝有換手）`,
            '列入觀察池：次日再度殺低才以 1/10 資金進場',
          ],
          stop: b.low, stopNote: '反彈失敗跌破前低無條件認賠',
          targetNote: '極短線：有賺要跑、第三天開高無力即了結',
        };
      },
    },
    {
      id: 'BOTTOM_ACCUM', side: 'buy', name: '矩陣1 底部吃飽突破', ref: '扣抵 矩陣1 / Pseudo 3', exit: 'trail5',
      detect({ bars, ind, h }, i) {
        if (i < 61 || !h.hasChip(i)) return null;
        const b = bars[i];
        const cons = h.consolidation(i, 30, 0.20);
        if (!cons || !cons.ok) return null;
        const mf30 = h.mfSum(i - 1, 30), vol30 = h.volSum(i - 1, 30);
        if (!(mf30 > 0.03 * vol30)) return null;
        if (!(ind.upperSlope[i] >= 0.01)) return null;
        if (!(b.close > b.open && (b.close - b.open) / b.open >= 0.04)) return null;
        return {
          strength: 2,
          reasons: [`橫盤 30 日（振幅 ${pct(cons.range)}）`, `盤整期主力(法人)累計吃貨 ${lots(mf30)}（${pct(mf30 / vol30)} 成交量）`, `布林上軌翹起 ${pct(ind.upperSlope[i], 2)}`, `發動大紅K 實體 ${pct((b.close - b.open) / b.open)}`],
          stop: b.close * 0.95, stopNote: '短線 5% 停損',
          targetNote: '突破放量後波段續抱',
        };
      },
    },
    {
      id: 'BOTTOM_REVERSAL', side: 'buy', name: '矩陣4 低檔賣不下去反轉', ref: '扣抵 矩陣4', exit: 'trail5',
      detect({ bars, ind, h }, i) {
        if (i < 120 || !h.hasChip(i) || !h.hasChip(i - 1)) return null;
        const b = bars[i];
        const low120 = h.lowest(i - 119, i).v;
        if (!(b.close <= low120 * 1.15 && bars[i - 60].close > 0 && b.close / bars[i - 60].close - 1 <= -0.15)) return null;
        if (!(h.mfSum(i - 1, 10) < 0 && bars[i - 1].mf < 0 && b.mf > 0)) return null;
        const recentLow = h.lowest(i - 9, i).v, priorLow = h.lowest(i - 30, i - 10).v;
        if (!(recentLow >= priorLow * 0.98)) return null;
        if (!h.ma20TurningUp(i)) return null;
        if (!(b.close > b.open && b.volume > ind.volMa20Prev[i] && b.close > ind.ma20[i])) return null;
        return {
          strength: 2,
          reasons: [`低檔（距 120 日低點 ${pct(b.close / low120 - 1)}，60 日跌幅 ${pct(b.close / bars[i - 60].close - 1)}）`, `主力(法人)前 10 日累計賣超 ${lots(h.mfSum(i - 1, 10))} 但股價不再破底`, `主力由賣轉買第一天 ${lots(b.mf)}`, `扣抵低值→月線翻揚，放量紅K站上月線`],
          stop: b.close * 0.95, stopNote: '短線 5% 停損',
          targetNote: '波段強烈起漲買點',
        };
      },
    },
    {
      id: 'MF_IGNITION', side: 'buy', name: '主力由賣轉買點火', ref: '關鍵分點 兩階段進場（簡化）', exit: 'ignition',
      detect({ bars, ind, h }, i) {
        if (i < 25 || !h.hasChip(i) || !h.hasChip(i - 2)) return null;
        const b = bars[i];
        if (!(bars[i - 2].mf < 0 && bars[i - 1].mf > 0 && b.mf > 0)) return null;
        if (!(b.close > ind.ma20[i] && b.volume > 1.5 * ind.volMa20Prev[i])) return null;
        const low20 = h.lowest(i - 19, i).v;
        return {
          strength: 1,
          reasons: [`主力(法人)由賣轉買，連續 2 日買超（${lots(bars[i - 1].mf)}、${lots(b.mf)}）`, `收盤站上月線，量 ${(b.volume / ind.volMa20Prev[i]).toFixed(1)} 倍均量`, '⚠ 關鍵分點囤貨條件需付費分點資料，未驗證'],
          stop: Math.min(low20, b.close * 0.93), stopNote: '近 20 日低點或 7% 停損',
          targetNote: '火車開動，波段續抱',
        };
      },
    },
    {
      id: 'SQUEEZE_ROCKET', side: 'buy', name: '軋空火箭（沿上軌＋券資比>30%）', ref: '軋空 策略A', exit: 'squeeze',
      detect({ bars, ind, h }, i) {
        if (i < 25 || !h.hasMargin(i) || !h.hasMargin(i - 5) || !h.hasChip(i)) return null;
        const b = bars[i], p5 = bars[i - 5];
        if (!(b.marginBal > 0)) return null;
        const ratio = b.shortBal / b.marginBal;
        if (!(ratio >= 0.30 && b.marginBal > p5.marginBal && b.shortBal > p5.shortBal)) return null;
        if (!(b.close > ind.upper[i] && b.close > ind.ma20[i])) return null;
        const mf5 = h.mfSum(i, 5);
        if (!(mf5 > 0)) return null;
        return {
          strength: 2,
          reasons: [`券資比 ${pct(ratio)}（≥30%）`, `5 日資增 ${b.marginBal - p5.marginBal} 張、券增 ${b.shortBal - p5.shortBal} 張（多空火拼）`, `收盤沿布林上軌噴發`, `近 5 日主力(法人) ${lots(mf5)}`, '⚠ 現增套利（假軋空）需人工排除'],
          stop: ind.ma20[i], stopNote: '跌破月線出場',
          targetNote: '搭火箭至強制回補日前，資減券減即下車',
        };
      },
    },
    {
      // 型態日 i 收盤後成立；次一交易日盤中突破近 5 日高點才進場（見 vcbEntry）
      // standalone：獨立策略，不參與《哥有籌必爆》綜合策略與買賣評分（跨個股驗證扣成本後為負，見 validate_strategy.mjs）
      id: 'VCB', side: 'buy', name: 'Larry Williams 波動收斂突破', ref: 'VCB 企劃書 進場規則', exit: 'vcb', standalone: true,
      detect({ bars, ind, h, opts }, i) {
        if (i < 25 || ind.ma20[i] == null) return null;
        const b = bars[i], p = bars[i - 1];
        // 條件一～三：昨收上漲、真實區間收斂（小於前一日且小於 5 日 ATR）、收在 K 棒中點之上
        if (!(b.close > p.close)) return null;
        if (!(ind.tr[i] < ind.tr[i - 1] && ind.tr[i] < ind.atr5[i])) return null;
        if (!(b.close >= (b.high + b.low) / 2)) return null;
        // 濾網：5 日均量 ≥ 1000 張、股價 > 10 元、站上 20MA
        if (!(ind.volMa5[i] >= 1000)) return null;
        if (!((b.rawClose ?? b.close) > 10 && b.close > ind.ma20[i])) return null;
        // 籌碼/趨勢輔助：投信近 5 日買超 或 大盤在月線之上
        const trust5 = h.trustSum(i, 5);
        const aux = (trust5 != null && trust5 > 0) || b.mktUp === true;
        if (opts.vcbAux !== false && !aux) return null;
        const trigger = h.highest(i - 4, i).v;
        const auxNote = [trust5 != null && trust5 > 0 ? `投信近 5 日買超 ${lots(trust5)}` : null, b.mktUp === true ? '大盤在月線之上' : null].filter(Boolean).join('、');
        return {
          strength: 1,
          trigger,
          reasons: [
            `收盤上漲 ${pct(h.pctChg(i), 2)}，收在 K 棒 ${pct((b.close - b.low) / Math.max(b.high - b.low, 1e-9), 0)} 位置（中點之上）`,
            `真實區間 ${fmt(ind.tr[i])} 小於前一日 ${fmt(ind.tr[i - 1])} 與 5 日 ATR ${fmt(ind.atr5[i])}（波動收斂）`,
            `5 日均量 ${Math.round(ind.volMa5[i]).toLocaleString()} 張、站上 20MA ${fmt(ind.ma20[i])}`,
            auxNote ? `趨勢輔助：${auxNote}` : '⚠ 趨勢輔助濾網未成立（投信未買超、大盤在月線下）',
            `次一交易日盤中突破近 5 日高點 ${fmt(trigger)} 才進場；未突破則型態失效`,
          ],
          stop: Math.max(trigger * 0.96, b.low), stopNote: '跌破型態日低點或進場價 −4%（取較高者）',
          targetNote: '獲利 ≥5% 停損移至成本，之後破前日低點或 5MA 出場；3 日內獲利未達 1.5% 時間停損',
        };
      },
    },

    // ---------------- 空方 / 賣出 ----------------
    {
      id: 'HANGING_MAN', side: 'sell', name: '高檔吊首線', ref: '第一課 策略三',
      detect({ bars, ind, h }, i) {
        if (i < 41) return null;
        const b = bars[i];
        const low40 = h.lowest(i - 39, i).v;
        const gain = b.close / low40 - 1;
        if (!(gain >= 0.30)) return null;
        if (!h.hangingShape(i)) return null;
        const marginBothDown = h.hasMargin(i) && h.hasMargin(i - 1) && b.marginBal < bars[i - 1].marginBal && b.shortBal < bars[i - 1].shortBal;
        const mfSell = h.hasChip(i) && b.mf < 0;
        if (!(marginBothDown || mfSell)) return null;
        const r = [`波段漲幅 ${pct(gain)}（≥30%，高檔）`, `實體 ${pct(h.body(i) / (b.high - b.low))} 振幅、下影線 ${(h.lowerShadow(i) / Math.max(h.body(i), 1e-9)).toFixed(1)} 倍實體`];
        if (marginBothDown) r.push('資券雙減（空單回補、散戶換手）');
        if (mfSell) r.push(`主力(法人)賣超 ${lots(b.mf)}`);
        r.push('次日開平/開低跌破實體下緣＝轉折確立；20 日內不過高則月線下彎');
        return { strength: marginBothDown && mfSell ? 3 : 2, reasons: r, note: '多單立即分批出場，不可攤平' };
      },
    },
    {
      id: 'DEDUCT_PEAK', side: 'sell', name: '扣抵高點月線即將下彎', ref: '扣抵 二-2',
      detect({ bars, ind, h }, i) {
        if (i < 61 || !h.hasChip(i)) return null;
        const pk = h.highest(i - 59, i);
        const d = i - pk.at;
        if (!(d >= 18 && d <= 20)) return null;
        // 波段高點：前 60 日亦無更高
        if (h.highest(pk.at - 60, pk.at).at !== pk.at) return null;
        if (!(bars[i].close < pk.v && h.mfSum(i, 5) < 0)) return null;
        return {
          strength: 2,
          reasons: [`波段高點 ${fmt(pk.v)}（${bars[pk.at].date}）後第 ${d} 天未再創高`, `月線即將扣抵高價區→必然下彎`, `近 5 日主力(法人) ${lots(h.mfSum(i, 5))}（籌碼轉弱）`],
          note: '強烈賣出/放空訊號',
        };
      },
    },
    {
      id: 'GOOD_NEWS_DUMP', side: 'sell', name: '矩陣3 高檔開高走低主力大賣', ref: '扣抵 矩陣3 / Pseudo 2',
      detect({ bars, ind, h }, i) {
        if (i < 121 || !h.hasChip(i)) return null;
        const b = bars[i], p = bars[i - 1];
        const hi120 = h.highest(i - 119, i).v;
        const pk = h.highest(i - 59, i);
        if (!(b.close >= hi120 * 0.85 && b.close / bars[i - 60].close - 1 >= 0.2)) return null;
        if (!(b.open > p.close && b.close < b.open)) return null;
        if (!(b.mf < 0 && -b.mf >= 0.1 * b.volume)) return null;
        if (!(i - pk.at >= 15)) return null;
        return {
          strength: 3,
          reasons: [`高檔區（距 120 日高 ${pct(b.close / hi120 - 1)}，60 日漲 ${pct(b.close / bars[i - 60].close - 1)}）`, '開高走低收黑K', `主力(法人)巨額賣超 ${lots(b.mf)}（${pct(-b.mf / b.volume)} 成交量）`, `距波段高點 ${i - pk.at} 天，即將進入高點扣抵期`, '⚠ 「重磅利多」新聞面需人工確認'],
          note: '絕對不能追，立即出清或伺機放空',
        };
      },
    },
    {
      id: 'BB_SQUEEZE_DOWN', side: 'sell', name: '窄帶寬往下開布林＋主力賣超', ref: '布林 戰略一 空方',
      detect({ bars, ind, h }, i) {
        if (i < 22 || ind.bw[i - 1] == null || !h.hasChip(i)) return null;
        const b = bars[i];
        if (!(ind.bw[i - 1] <= 0.05 && b.close < ind.lower[i] && ind.lowerSlope[i] <= -0.01 && h.mfSum(i, 3) < 0)) return null;
        return {
          strength: 3,
          reasons: [`前一日帶寬 ${pct(ind.bw[i - 1])}（壓縮）`, `跌破下軌 ${fmt(ind.lower[i])}，下軌斜率 ${pct(ind.lowerSlope[i], 2)}`, `近 3 日主力(法人) ${lots(h.mfSum(i, 3))}`],
          note: '持有者立刻出清停損，絕不攤平',
        };
      },
    },
    {
      id: 'BB_WIDE_UPPER', side: 'sell', name: '大帶寬反彈上軌＋主力出貨', ref: '布林 戰略二-2',
      detect({ bars, ind, h }, i) {
        if (i < 40 || ind.bw[i] == null || !h.hasChip(i)) return null;
        const b = bars[i];
        if (!(ind.bw[i] >= 0.20 && b.high >= ind.upper[i] * 0.98 && h.mfSum(i, 5) < 0 && b.mf < 0)) return null;
        return {
          strength: 2,
          reasons: [`帶寬 ${pct(ind.bw[i])}（≥20%）`, `最高 ${fmt(b.high)} 逼近上軌 ${fmt(ind.upper[i])}`, `主力(法人)連續賣超（5 日 ${lots(h.mfSum(i, 5))}）`, '⚠ 關鍵分點倒貨需付費分點資料，以法人代理'],
          note: '絕佳賣點；放空目標下軌',
        };
      },
    },
    {
      id: 'GRANVILLE_5', side: 'sell', name: '葛蘭碧⑤ 跌破下彎月線', ref: '葛蘭碧 賣點⑤',
      detect({ bars, ind }, i) {
        if (i < 22) return null;
        const b = bars[i];
        // 「月線下彎＋收盤在月線下」狀態成立的第一天
        const cond = k => ind.ma20[k] < ind.ma20[k - 1] && bars[k].close < ind.ma20[k];
        if (!(cond(i) && !cond(i - 1))) return null;
        return { strength: 2, reasons: ['月線扣高轉下彎', `收盤 ${fmt(b.close)} 位於下彎月線 ${fmt(ind.ma20[i])} 之下`], note: '出清持股、多單停損' };
      },
    },
    {
      id: 'GRANVILLE_6', side: 'sell', name: '葛蘭碧⑥ 棒打出頭鳥', ref: '葛蘭碧 賣點⑥',
      detect({ bars, ind, h }, i) {
        if (i < 25 || !h.hasChip(i) || !h.hasChip(i - 1)) return null;
        const b = bars[i];
        if (!(ind.ma20Slope5[i] < -0.005 && b.high >= ind.ma20[i] && b.close <= ind.ma20[i] * 1.03 && b.mf < 0 && bars[i - 1].mf < 0)) return null;
        return { strength: 2, reasons: [`月線明顯下彎（5 日斜率 ${pct(ind.ma20Slope5[i], 2)}）`, `反彈觸碰/稍穿月線 ${fmt(ind.ma20[i])}`, `主力(法人)連續賣超（${lots(bars[i - 1].mf)}、${lots(b.mf)}）`], note: '誘多陷阱，絕佳空點' };
      },
    },
    {
      id: 'GRANVILLE_7', side: 'sell', name: '葛蘭碧⑦ 反彈無力碰不到月線', ref: '葛蘭碧 賣點⑦',
      detect({ bars, ind }, i) {
        if (i < 22) return null;
        const b = bars[i];
        if (!(ind.ma20[i] < ind.ma20[i - 1] && b.close < ind.ma20[i] && b.high < ind.ma20[i] && b.close > bars[i - 1].close && ind.ma20Slope5[i] < 0)) return null;
        return { strength: 1, reasons: ['月線下彎', `弱勢反彈最高 ${fmt(b.high)} 碰不到月線 ${fmt(ind.ma20[i])}`], note: '反彈是讓你逃命的，弱勢加空點' };
      },
    },
    {
      id: 'GRANVILLE_8', side: 'sell', name: '葛蘭碧⑧ 正乖離過大', ref: '葛蘭碧 賣點⑧',
      detect({ bars, ind }, i) {
        if (ind.biasP99[i] == null) return null;
        if (!(ind.ma20[i] > ind.ma20[i - 1] && ind.bias[i] >= ind.biasP99[i] && ind.bias[i] > 0)) return null;
        return { strength: 1, reasons: [`乖離率 ${pct(ind.bias[i])} 超過該股 99% 天花板 ${pct(ind.biasP99[i])}`], note: '短線獲利調節點' };
      },
    },
    {
      id: 'SQUEEZE_END', side: 'sell', name: '軋空衰竭右側（資減券減＋破月線）', ref: '軋空 策略B',
      detect({ bars, ind, h }, i) {
        if (i < 25 || !h.hasMargin(i) || !h.hasMargin(i - 3) || !h.hasChip(i)) return null;
        let maxRatio = 0;
        for (let k = i - 19; k <= i; k++) if (bars[k].marginBal > 0 && bars[k].shortBal != null) maxRatio = Math.max(maxRatio, bars[k].shortBal / bars[k].marginBal);
        const b = bars[i], p3 = bars[i - 3];
        if (!(maxRatio >= 0.3 && b.marginBal < p3.marginBal && b.shortBal < p3.shortBal && h.mfSum(i, 3) < 0)) return null;
        if (!(b.close < ind.ma20[i] && ind.ma20[i] < ind.ma20[i - 1])) return null;
        return { strength: 3, reasons: [`20 日內券資比高達 ${pct(maxRatio)}`, '3 日資減、券減（主力跑路、空軍投降）', `近 3 日主力(法人) ${lots(h.mfSum(i, 3))}`, '跌破月線且月線下彎（山的右邊）'], note: '軋空結束，右側順勢放空' };
      },
    },
  ];

  const SIGNAL_MAP = Object.fromEntries(SIGNALS.map(s => [s.id, s]));
  // 持股中觸發即出場的強賣訊號
  const HARD_EXIT_IDS = ['HANGING_MAN', 'DEDUCT_PEAK', 'GOOD_NEWS_DUMP', 'BB_SQUEEZE_DOWN', 'GRANVILLE_5', 'SQUEEZE_END'];

  function scanAll(ctx) {
    const n = ctx.bars.length;
    const hits = new Array(n);
    for (let i = 0; i < n; i++) {
      const day = [];
      for (const s of SIGNALS) {
        const r = s.detect(ctx, i);
        if (r) day.push({ id: s.id, side: s.side, name: s.name, ref: s.ref, ...r });
      }
      hits[i] = day;
    }
    return hits;
  }

  // ==========================================
  // 回測
  // ==========================================
  const netRet = (entry, exit) => (exit * (1 - COST.sellFee - COST.tax)) / (entry * (1 + COST.buyFee)) - 1;

  // 單一部位管理：回傳 { exitIdx, exitPx, reason } 或 null（持有中）
  function managePosition(ctx, pos, j, hits) {
    const { bars, ind, h } = ctx;
    const b = bars[j];
    if (pos.pendingExit) return { exitIdx: j, exitPx: b.open, reason: pos.pendingExit };

    // ---- 盤中價格停損 / 停利 ----
    let stop = null, target = null;
    switch (pos.exit) {
      case 'bald': stop = pos.stop; break;
      case 'bbUpper': case 'midBand': stop = pos.entryPx * 0.95; break;
      case 'trail5': stop = Math.max(pos.entryPx, pos.peak) * 0.95; break;
      case 'ignition': stop = pos.stop; break;
      case 'floorSop': stop = pos.support; break;
      case 'vcb': stop = pos.stop; break;
    }
    if (pos.exit === 'midBand' && j > pos.entryIdx) target = ind.ma20[j - 1];
    // VCB 盤中突破進場：當日低點可能出現在進場之前，進場日改以收盤確認停損（開盤即跳空成交者除外）
    if (pos.exit === 'vcb' && j === pos.entryIdx && !pos.gapFill) {
      if (b.close <= pos.stop) return { exitIdx: j, exitPx: b.close, reason: '進場當日收盤跌破停損' };
      stop = null;
    }
    // 搶反彈進場當日正在創低，前低以收盤後確認
    if (pos.exit === 'floorSop' && j === pos.entryIdx) stop = null;
    if (stop != null && b.low <= stop) return { exitIdx: j, exitPx: Math.min(b.open, stop), reason: '停損/移動停利' };
    if (target != null && b.high >= target) return { exitIdx: j, exitPx: Math.max(b.open, target), reason: '達目標中軌' };

    pos.peak = Math.max(pos.peak, b.close);
    const held = j - pos.entryIdx;

    // ---- 收盤後規則 → 次日開盤出場 ----
    let why = null;
    switch (pos.exit) {
      case 'bald':
        if (j > pos.entryIdx && ((b.volume >= 2 * ind.volMa20Prev[j] && h.pctChg(j) <= 0.005) || h.hangingShape(j))) why = '爆量滯漲/吊首線';
        break;
      case 'bbUpper':
        if (b.close < ind.upper[j]) why = '收盤離開上軌';
        break;
      case 'trail5': case 'ignition':
        if (ind.ma20[j] < ind.ma20[j - 1] && b.close < ind.ma20[j]) why = '月線下彎跌破';
        break;
      case 'squeeze': {
        if (b.close < ind.ma20[j]) why = '跌破月線';
        else if (hits[j].some(x => x.id === 'SQUEEZE_END')) why = '軋空衰竭';
        else if (j >= 3 && h.hasMargin(j) && h.hasMargin(j - 3) && b.marginBal < bars[j - 3].marginBal && b.shortBal < bars[j - 3].shortBal && h.mfSum(j, 3) < 0) why = '資減券減＋主力賣';
        break;
      }
      case 'vcb': {
        if (!pos.trailOn && b.close >= pos.entryPx * 1.05) { pos.trailOn = true; pos.stop = Math.max(pos.stop, pos.entryPx); }
        if (pos.trailOn) {
          pos.stop = Math.max(pos.stop, b.low); // 明日以今日低點（前一日低點）防守
          if (b.close < ind.ma5[j]) why = '跌破 5 日線';
        }
        if (!why && held === 2 && b.close / pos.entryPx - 1 < 0.015) why = '3 日未拉開獲利';
        break;
      }
      case 'floorSop': {
        // SOP4/6：有賺要跑，沒賺還是得跑
        if (held === 1 && b.open > bars[j - 1].close) return { exitIdx: j, exitPx: b.open, reason: '第三天開高即了結' };
        if (held >= 1 && b.close > pos.avgCost) return { exitIdx: j, exitPx: b.close, reason: '有賺要跑' };
        // SOP5：未反彈再度殺低爆量 → 加碼一次
        if (!pos.added && held >= 1 && b.low < bars[j - 1].low && b.volume >= 2 * ind.volMa20Prev[j]) {
          pos.added = true;
          pos.avgCost = (pos.avgCost * pos.units + b.close) / (pos.units + 1);
          pos.units += 1;
          pos.support = b.low;
        }
        if (held >= 5) return { exitIdx: j, exitPx: b.close, reason: '極短線時間到' };
        break;
      }
    }
    if (pos.globalExit && !why) {
      const s = hits[j].find(x => HARD_EXIT_IDS.includes(x.id));
      if (s) why = `賣訊：${s.name}`;
    }
    if (why) pos.pendingExit = why;
    return null;
  }

  function openPosition(ctx, sigId, sig, i, j, fillPx) {
    const def = SIGNAL_MAP[sigId];
    const b = ctx.bars[i];
    const pos = { sigId, exit: def.exit, sigIdx: i, entryIdx: j, entryPx: fillPx, peak: fillPx, units: 1, avgCost: fillPx };
    if (def.exit === 'bald') pos.stop = Math.max(b.open, b.close * 0.9);
    if (def.exit === 'ignition') pos.stop = sig.stop;
    if (def.exit === 'floorSop') pos.support = Math.min(b.low, ctx.bars[j].low);
    if (def.exit === 'vcb') { pos.stop = Math.max(fillPx * 0.96, b.low); pos.gapFill = fillPx === ctx.bars[j].open; }
    return pos;
  }

  // 搶反彈 SOP3：次日盤中再度殺低（跌破昨收）才進場，掛昨收價承接
  function floorEntry(ctx, i) {
    const nb = ctx.bars[i + 1];
    if (!nb) return null;
    const ref = ctx.bars[i].close;
    if (nb.low < ref) return Math.min(nb.open, ref);
    return null;
  }

  // VCB：次一交易日盤中觸及突破價才成交（開盤跳空突破則以開盤價成交）；
  // 一字鎖漲停買不到；成交價仍需站上（含當下價格的）20MA
  function vcbEntry(ctx, i, sig) {
    const { bars } = ctx;
    const nb = bars[i + 1];
    if (!nb || !(nb.high >= sig.trigger)) return null;
    if (nb.open === nb.high && nb.open / bars[i].close - 1 >= 0.095) return null;
    const fill = Math.max(nb.open, sig.trigger);
    let s = fill;
    for (let k = i - 18; k <= i; k++) s += bars[k].close;
    if (!(fill > s / 20)) return null;
    return fill;
  }

  function entryFill(ctx, sig, i) {
    const exit = SIGNAL_MAP[sig.id].exit;
    if (exit === 'floorSop') return floorEntry(ctx, i);
    if (exit === 'vcb') return vcbEntry(ctx, i, sig);
    return ctx.bars[i + 1].open;
  }

  function closeTrade(ctx, pos, ex) {
    const { bars } = ctx;
    const r = netRet(pos.avgCost, ex.exitPx);
    return {
      sigId: pos.sigId, sigDate: bars[pos.sigIdx].date, entryDate: bars[pos.entryIdx].date, entryPx: pos.avgCost,
      exitDate: bars[ex.exitIdx].date, exitPx: ex.exitPx, ret: r, hold: ex.exitIdx - pos.entryIdx, reason: ex.reason,
      units: pos.units, open: !!ex.open,
    };
  }

  function summarize(trades) {
    const closed = trades.filter(t => !t.open);
    const n = closed.length;
    if (!n) return { n: 0, openTrades: trades.length - n };
    const rets = closed.map(t => t.ret);
    const wins = rets.filter(r => r > 0);
    const losses = rets.filter(r => r <= 0);
    let eq = 1, peak = 1, mdd = 0;
    for (const r of rets) { eq *= 1 + r; peak = Math.max(peak, eq); mdd = Math.min(mdd, eq / peak - 1); }
    const sorted = [...rets].sort((a, b) => a - b);
    const gp = wins.reduce((a, b) => a + b, 0), gl = -losses.reduce((a, b) => a + b, 0);
    return {
      n, winRate: wins.length / n, avgRet: rets.reduce((a, b) => a + b, 0) / n, median: percentile(sorted, 0.5),
      compound: eq - 1, mdd, avgHold: closed.reduce((a, t) => a + t.hold, 0) / n,
      profitFactor: gl > 0 ? gp / gl : (gp > 0 ? Infinity : 0), best: sorted[n - 1], worst: sorted[0],
      openTrades: trades.length - n,
    };
  }

  // 單一多方訊號回測（依該訊號自身出場規則）
  function backtestSignal(ctx, hits, sigId, startIdx = 0) {
    const { bars } = ctx;
    const n = bars.length;
    const trades = [];
    let pos = null;
    for (let i = startIdx; i < n; i++) {
      if (pos) {
        const ex = managePosition(ctx, pos, i, hits);
        if (ex) { trades.push(closeTrade(ctx, pos, ex)); pos = null; }
        continue;
      }
      if (i >= n - 1) break;
      const sig = hits[i].find(x => x.id === sigId);
      if (!sig) continue;
      const j = i + 1;
      const fill = entryFill(ctx, sig, i);
      if (fill == null) continue;
      pos = openPosition(ctx, sigId, sig, i, j, fill);
      // 進場當日即適用停損
      const ex = managePosition(ctx, pos, j, hits);
      if (ex) { trades.push(closeTrade(ctx, pos, ex)); pos = null; }
      i = j;
    }
    if (pos) trades.push(closeTrade(ctx, pos, { exitIdx: n - 1, exitPx: bars[n - 1].close, reason: '持有中', open: true }));
    return trades;
  }

  // 賣出訊號：事後 N 日股價下跌機率
  function forwardStats(ctx, hits, sigId, startIdx = 0, horizons = [5, 10, 20]) {
    const { bars } = ctx;
    const out = { n: 0 };
    for (const H of horizons) out['h' + H] = { n: 0, down: 0, sum: 0 };
    let last = -999;
    for (let i = startIdx; i < bars.length; i++) {
      if (!hits[i].some(x => x.id === sigId)) continue;
      if (i - last < 5) continue; // 同一波段連續訊號只計一次
      last = i; out.n++;
      for (const H of horizons) {
        if (i + H >= bars.length) continue;
        const r = bars[i + H].close / bars[i].close - 1;
        const o = out['h' + H]; o.n++; o.sum += r; if (r < 0) o.down++;
      }
    }
    for (const H of horizons) {
      const o = out['h' + H];
      o.downRate = o.n ? o.down / o.n : null; o.avg = o.n ? o.sum / o.n : null;
    }
    return out;
  }

  // 基準：任意一天之後 N 日下跌機率
  function baselineForward(ctx, startIdx = 0, horizons = [5, 10, 20]) {
    const { bars } = ctx;
    const out = {};
    for (const H of horizons) {
      let n = 0, down = 0, sum = 0;
      for (let i = startIdx; i + H < bars.length; i++) { const r = bars[i + H].close / bars[i].close - 1; n++; sum += r; if (r < 0) down++; }
      out['h' + H] = { n, downRate: n ? down / n : null, avg: n ? sum / n : null };
    }
    return out;
  }

  // 綜合策略：任一多方訊號進場（無同日賣訊、反彈架構只允許反轉型訊號），
  // 以該訊號出場規則＋任一強賣訊號出場
  const REBOUND_OK = ['FLOOR_REBOUND', 'BOTTOM_REVERSAL'];
  function trendState(ctx, i) {
    const { bars, ind } = ctx;
    if (ind.ma20[i] == null || i < 1) return 'NA';
    const up = ind.ma20[i] > ind.ma20[i - 1];
    const above = bars[i].close > ind.ma20[i];
    if (up && above) return 'RECOVERY';
    if (!up && !above) return 'REBOUND';
    return 'TRANSITION';
  }

  function backtestComposite(ctx, hits, startIdx = 0) {
    const { bars } = ctx;
    const n = bars.length;
    const trades = [];
    const equity = new Array(n).fill(null);
    let eq = 1, pos = null, size = 1;
    let lastExitIdx = -1, lastExitReason = null;
    for (let i = startIdx; i < n; i++) {
      if (pos) {
        const prevClose = bars[i - 1].close;
        const units = pos.units;
        const ex = managePosition(ctx, pos, i, hits);
        if (ex) {
          const t = closeTrade(ctx, pos, ex);
          trades.push(t);
          // 以實際成交價結算當日
          eq *= 1 + size * units * (ex.exitPx * (1 - COST.sellFee - COST.tax) / prevClose - 1);
          lastExitIdx = i; lastExitReason = ex.reason;
          pos = null;
        } else {
          eq *= 1 + size * units * (bars[i].close / prevClose - 1);
        }
        equity[i] = eq;
        if (pos) continue;
      }
      equity[i] = eq;
      if (i >= n - 1) continue;
      const buys = hits[i].filter(x => x.side === 'buy' && !SIGNAL_MAP[x.id].standalone);
      if (!buys.length || hits[i].some(x => x.side === 'sell')) continue;
      const ts = trendState(ctx, i);
      const allowed = buys.filter(x => ts !== 'REBOUND' || REBOUND_OK.includes(x.id)).sort((a, b) => b.strength - a.strength);
      if (!allowed.length) continue;
      const sig = allowed[0];
      const fill = entryFill(ctx, sig, i);
      if (fill == null) continue;
      const j = i + 1;
      pos = openPosition(ctx, sig.id, sig, i, j, fill);
      pos.globalExit = true;
      size = SIGNAL_MAP[sig.id].exit === 'floorSop' ? 0.1 : 1; // SOP1：資金分 10 等份
      eq *= 1 - size * COST.buyFee;
      const ex = managePosition(ctx, pos, j, hits);
      if (ex) {
        trades.push(closeTrade(ctx, pos, ex));
        eq *= 1 + size * pos.units * (ex.exitPx * (1 - COST.sellFee - COST.tax) / fill - 1);
        lastExitIdx = j; lastExitReason = ex.reason;
        pos = null;
      } else {
        eq *= 1 + size * pos.units * (bars[j].close / fill - 1);
      }
      equity[j] = eq;
      i = j;
    }
    let openPos = null;
    if (pos) {
      const t = closeTrade(ctx, pos, { exitIdx: n - 1, exitPx: bars[n - 1].close, reason: '持有中', open: true });
      trades.push(t);
      const def = SIGNAL_MAP[pos.sigId];
      let stop = null;
      switch (pos.exit) {
        case 'bald': case 'ignition': stop = pos.stop; break;
        case 'bbUpper': case 'midBand': stop = pos.entryPx * 0.95; break;
        case 'trail5': stop = Math.max(pos.entryPx, pos.peak) * 0.95; break;
        case 'floorSop': stop = pos.support; break;
        case 'squeeze': stop = ctx.ind.ma20[n - 1]; break;
        case 'vcb': stop = pos.stop; break;
      }
      openPos = { ...t, name: def.name, stop, pendingExit: pos.pendingExit || null };
    }
    const bh = bars[n - 1].close / bars[startIdx].close - 1;
    let pk = 1, mdd = 0;
    for (let i = startIdx; i < n; i++) { if (equity[i] == null) continue; pk = Math.max(pk, equity[i]); mdd = Math.min(mdd, equity[i] / pk - 1); }
    let bpk = 0, bmdd = 0;
    for (let i = startIdx; i < n; i++) { bpk = Math.max(bpk, bars[i].close); bmdd = Math.min(bmdd, bars[i].close / bpk - 1); }
    const inMarket = trades.reduce((a, t) => a + t.hold + 1, 0) / Math.max(1, n - startIdx);
    return {
      trades, equity, stats: summarize(trades), totalRet: eq - 1, mdd, buyHold: bh, buyHoldMdd: bmdd, exposure: inMarket,
      openPos, lastExit: lastExitIdx >= 0 ? { date: bars[lastExitIdx].date, reason: lastExitReason, daysAgo: n - 1 - lastExitIdx } : null,
    };
  }

  // ==========================================
  // 當日判讀（給前端）
  // ==========================================
  function deductInfo(ctx, i) {
    const { bars, ind, h } = ctx;
    const out = {};
    for (const nMa of [5, 10, 20, 60]) {
      if (i - nMa + 1 < 0) continue;
      const ded = bars[i - nMa + 1].close;
      const maKey = { 5: 'ma5', 10: 'ma10', 20: 'ma20', 60: 'ma60' }[nMa];
      const upcoming = [];
      for (let k = 1; k <= 5; k++) { const idx = i - nMa + k; if (idx >= 0) upcoming.push(bars[idx].close); }
      out[nMa] = {
        ma: ind[maKey][i], deduct: ded, turningUp: bars[i].close > ded,
        breakEven: ded, // 明日收盤需高於此價，均線才會上揚
        upcoming, upcomingAvg: upcoming.reduce((a, b) => a + b, 0) / upcoming.length,
        nowUp: ind[maKey][i - 1] != null ? ind[maKey][i] > ind[maKey][i - 1] : null,
      };
    }
    const pk = h.highest(i - 59, i), tr = h.lowest(i - 59, i);
    out.peak = { price: pk.v, date: bars[pk.at].date, daysSince: i - pk.at, deductInDays: pk.at + 20 - i };
    out.trough = { price: tr.v, date: bars[tr.at].date, daysSince: i - tr.at, deductInDays: tr.at + 20 - i };
    return out;
  }

  function analyze(bars, opts = {}) {
    const ctx = makeCtx(bars);
    if (opts.vcbAux === false) ctx.opts.vcbAux = false;
    const hits = scanAll(ctx);
    const n = bars.length;
    const i = n - 1;
    const { ind, h } = ctx;
    const startIdx = Math.max(130, opts.startIdx || 0);

    // ---- 各訊號在本股歷史表現 ----
    const perSignal = {};
    for (const s of SIGNALS) {
      if (s.side === 'buy') {
        const tr = backtestSignal(ctx, hits, s.id, startIdx);
        perSignal[s.id] = { side: 'buy', name: s.name, ref: s.ref, stats: summarize(tr), trades: tr };
      } else {
        perSignal[s.id] = { side: 'sell', name: s.name, ref: s.ref, fwd: forwardStats(ctx, hits, s.id, startIdx) };
      }
    }
    const baseline = baselineForward(ctx, startIdx);
    const composite = backtestComposite(ctx, hits, startIdx);

    // ---- 近 3 日觸發訊號 ----
    const decay = [1, 0.6, 0.35];
    const recent = [];
    // VCB 只在型態日的次一交易日有效：較早的型態已突破（反映在部位）或已失效
    for (let d = 0; d < 3 && i - d >= 0; d++) for (const s of hits[i - d]) if (!(s.id === 'VCB' && d > 0)) recent.push({ ...s, daysAgo: d, date: bars[i - d].date });

    // ---- 狀態判讀 ----
    const ts = trendState(ctx, i);
    const mf10 = h.mfSum(i, 10);
    const ma20Up = h.ma20TurningUp(i) || ind.ma20[i] > ind.ma20[i - 1];
    const quadrant = mf10 >= 0 ? (ma20Up ? 1 : 3) : (ma20Up ? 2 : 4);
    const b = bars[i];
    const ratio = b.marginBal > 0 && b.shortBal != null ? b.shortBal / b.marginBal : null;
    const riding = h.ridingUpper(i);
    const bwv = ind.bw[i];
    const bbRegime = bwv == null ? 'NA' : bwv <= 0.05 ? 'SQUEEZE' : bwv >= 0.20 ? 'WIDE' : 'NORMAL';
    const strongTrend = ind.ma20Slope[i] >= 0.004 && b.close > ind.ma20[i] && b.close <= ind.upper[i] * 1.02;

    // ---- 評分 ----
    let score = 0;
    const scoreParts = [];
    for (const s of recent) {
      if (SIGNAL_MAP[s.id].standalone) continue;
      const w = (s.side === 'buy' ? 1 : -1) * s.strength * decay[s.daysAgo];
      score += w; scoreParts.push({ label: `${s.name}${s.daysAgo ? `（${s.daysAgo} 日前）` : ''}`, w });
    }
    const tw = { RECOVERY: 1, REBOUND: -1, TRANSITION: 0, NA: 0 }[ts];
    if (tw) { score += tw; scoreParts.push({ label: ts === 'RECOVERY' ? '回升架構（月線上揚、價在月線上）' : '反彈架構（月線下彎、價在月線下）', w: tw }); }
    const qw = { 1: 1, 2: -1, 3: 0.5, 4: -1.5 }[quadrant];
    score += qw; scoreParts.push({ label: `籌碼×扣抵矩陣：${['', '主力買＋月線上揚', '主力賣＋月線上揚', '主力買＋月線下彎', '主力賣＋月線下彎'][quadrant]}`, w: qw });
    if (composite.openPos && !composite.openPos.pendingExit) { score += 1; scoreParts.push({ label: `策略部位持有中（${composite.openPos.name}）`, w: 1 }); }
    if (composite.openPos && composite.openPos.pendingExit) { score -= 1.5; scoreParts.push({ label: `策略部位觸發出場：${composite.openPos.pendingExit}`, w: -1.5 }); }

    let verdict, verdictKey;
    if (score >= 4) { verdict = '強力買進'; verdictKey = 'STRONG_BUY'; }
    else if (score >= 1.5) { verdict = '買進'; verdictKey = 'BUY'; }
    else if (score <= -4) { verdict = '強力賣出'; verdictKey = 'STRONG_SELL'; }
    else if (score <= -1.5) { verdict = '賣出'; verdictKey = 'SELL'; }
    else { verdict = '觀望'; verdictKey = 'HOLD'; }

    // ---- 執行建議 ----
    const advice = [];
    const buyNow = recent.filter(s => s.side === 'buy' && !SIGNAL_MAP[s.id].standalone).sort((a, b2) => b2.strength - a.strength || a.daysAgo - b2.daysAgo)[0];
    const sellNow = recent.filter(s => s.side === 'sell').sort((a, b2) => b2.strength - a.strength || a.daysAgo - b2.daysAgo)[0];
    if (verdictKey === 'STRONG_BUY' || verdictKey === 'BUY') {
      if (buyNow) {
        advice.push(`進場依據：${buyNow.name}（${buyNow.ref}）`);
        if (buyNow.id === 'FLOOR_REBOUND') advice.push('執行搶反彈 SOP：次日盤中再度殺低時以 1/10 資金承接，有賺就跑，最多加碼一次');
        else advice.push('建議於次一交易日開盤附近進場（回測即以次日開盤價成交）');
        if (buyNow.stop) advice.push(`停損：${fmt(buyNow.stop)}（${buyNow.stopNote}）`);
        if (buyNow.targetNote) advice.push(`停利：${buyNow.targetNote}`);
      } else advice.push('多方架構延續，可依月線支撐順勢操作；嚴守 5% 停損');
    } else if (verdictKey === 'SELL' || verdictKey === 'STRONG_SELL') {
      if (sellNow) { advice.push(`賣出依據：${sellNow.name}（${sellNow.ref}）`); if (sellNow.note) advice.push(sellNow.note); }
      advice.push('持股者分批出場，絕不向下攤平');
      if (riding) advice.push('⚠ 股價仍沿布林上軌：空單嚴禁進場（只在山的右邊出手）');
      else if (ts === 'REBOUND') advice.push('空方架構：反彈至月線附近、主力續賣時為順勢空點');
    } else {
      advice.push(ts === 'RECOVERY' ? '持有者續抱，空手者等待回測月線或突破訊號' : ts === 'REBOUND' ? '空頭架構，反彈是用來逃命的；空手觀望' : '方向未明，等待訊號確認');
    }
    if (riding && ratio != null && ratio >= 0.3) advice.push('🚀 沿上軌＋券資比 ≥30%：軋空條件成立，空單絕不可摸頭');
    const vcb = recent.find(s => s.id === 'VCB');
    if (vcb) advice.push(`📐 Larry Williams VCB（獨立策略，不計入評分）：次一交易日盤中突破 ${fmt(vcb.trigger)} 才進場，停損 ${fmt(vcb.stop)}；未突破則型態失效`);

    return {
      ctx, hits, last: b, prev: bars[i - 1], date: b.date, verdict, verdictKey, score, scoreParts, advice,
      recent, trend: ts, quadrant, mf: { d1: b.mf, d5: h.mfSum(i, 5), d10: mf10, d20: h.mfSum(i, 20), foreign: b.foreign, trust: b.trust, dealer: b.dealer },
      margin: { marginBal: b.marginBal, shortBal: b.shortBal, ratio, marginChg5: b.marginBal != null && bars[i - 5].marginBal != null ? b.marginBal - bars[i - 5].marginBal : null, shortChg5: b.shortBal != null && bars[i - 5].shortBal != null ? b.shortBal - bars[i - 5].shortBal : null },
      bb: { upper: ind.upper[i], mid: ind.ma20[i], lower: ind.lower[i], bw: bwv, regime: bbRegime, upperSlope: ind.upperSlope[i], lowerSlope: ind.lowerSlope[i], ma20Slope: ind.ma20Slope[i], riding, strongTrend },
      mfCost: { c20: ind.mfCost20[i], c60: ind.mfCost60[i] },
      floor: { f95: ind.floor95[i], f99: ind.floor99[i], bias: ind.bias[i], p01: ind.biasP01[i], p05: ind.biasP05[i], p95: ind.biasP95[i], p99: ind.biasP99[i] },
      deduct: deductInfo(ctx, i), perSignal, baseline, composite, startIdx,
    };
  }

  return {
    COST, SIGNALS, SIGNAL_MAP, HARD_EXIT_IDS,
    buildBars, computeIndicators, makeCtx, scanAll, backtestSignal, backtestComposite, forwardStats, baselineForward, summarize, analyze, trendState,
  };
});
