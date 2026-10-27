/* 個股策略分析頁：資料抓取、呈現與圖表 */
(function () {
  'use strict';
  const E = window.StrategyEngine;
  const API = 'https://api.finmindtrade.com/api/v4/data';
  const $ = id => document.getElementById(id);

  const C = { up: '#ef4444', down: '#16a34a', ma20: '#f59e0b', cost: '#7c3aed', band: '#94a3b8', strat: '#4f46e5', bh: '#94a3b8', grid: '#e2e8f0', axis: '#94a3b8' };

  // ---------- 格式化 ----------
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = (v, d = 2) => (v == null || !isFinite(v) ? 'NA' : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));
  const pct = (v, d = 1, sign = false) => (v == null || !isFinite(v) ? 'NA' : `${sign && v > 0 ? '+' : ''}${(v * 100).toFixed(d)}%`);
  const lots = v => (v == null || !isFinite(v) ? 'NA' : `${v > 0 ? '+' : ''}${Math.round(v).toLocaleString()} 張`);
  const signCls = v => (v == null || !isFinite(v) || v === 0 ? 'text-slate-600' : v > 0 ? 'text-red-500' : 'text-green-600');

  const storage = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 無痕模式 */ } },
  };

  // ---------- 資料抓取 ----------
  async function fetchDataset(dataset, id, start, token) {
    // FinMind 的 CORS 預檢不接受 Authorization header，改以 token 參數傳遞
    const url = `${API}?dataset=${dataset}&data_id=${encodeURIComponent(id)}${start ? `&start_date=${start}` : ''}${token ? `&token=${encodeURIComponent(token)}` : ''}`;
    const res = await fetch(url);
    const j = await res.json().catch(() => ({}));
    if (j.status !== 200) throw new Error(j.msg || `HTTP ${res.status}`);
    return j.data || [];
  }

  async function loadStock(id, years, token) {
    const start = new Date();
    start.setFullYear(start.getFullYear() - years - 1); // 多抓 1 年作指標暖機
    const s = start.toISOString().slice(0, 10);
    const [prices, institutional, margin, dividends, info, market] = await Promise.all([
      fetchDataset('TaiwanStockPrice', id, s, token),
      fetchDataset('TaiwanStockInstitutionalInvestorsBuySell', id, s, token).catch(() => []),
      fetchDataset('TaiwanStockMarginPurchaseShortSale', id, s, token).catch(() => []),
      fetchDataset('TaiwanStockDividendResult', id, s, token).catch(() => []),
      fetchDataset('TaiwanStockInfo', id, null, token).catch(() => []),
      fetchDataset('TaiwanStockPrice', 'TAIEX', s, token).catch(() => []), // VCB 大盤月線濾網
    ]);
    const bars = E.buildBars({ prices, institutional, margin, dividends, market });
    const btStart = new Date();
    btStart.setFullYear(btStart.getFullYear() - years);
    const bs = btStart.toISOString().slice(0, 10);
    const startIdx = Math.max(0, bars.findIndex(b => b.date >= bs));
    return { bars, startIdx, info: info[0] || null };
  }

  // ---------- 狀態列 ----------
  function setStatus(kind, html) {
    const el = $('status');
    if (!kind) { el.className = 'hidden'; el.innerHTML = ''; return; }
    const cls = kind === 'error' ? 'bg-red-50 border-red-200 text-red-700' : 'bg-indigo-50 border-indigo-200 text-indigo-700';
    el.className = `rounded-xl border p-4 text-sm ${cls}`;
    el.innerHTML = html;
  }

  // ---------- 主流程 ----------
  async function run(id) {
    id = String(id || '').trim().toUpperCase();
    if (!/^[0-9A-Z]{4,6}$/.test(id)) { setStatus('error', '請輸入 4~6 碼股票代號（例如 2330）'); return; }
    const years = +$('yearsInput').value;
    const token = $('tokenInput').value.trim();
    storage.set('finmindToken', token);
    storage.set('lastStock', id);
    history.replaceState(null, '', `?id=${id}`);
    $('submitBtn').disabled = true;
    $('result').classList.add('hidden');
    setStatus('info', `⏳ 正在抓取 ${esc(id)} 的日K、三大法人、融資融券、除權息資料並執行策略回測…`);
    try {
      const { bars, startIdx, info } = await loadStock(id, years, token);
      if (!bars.length) throw new Error(`查無股票代號 ${id} 的交易資料`);
      if (bars.length < 160) throw new Error(`資料不足（僅 ${bars.length} 個交易日），無法計算 60MA/120 日位階與回測`);
      const res = E.analyze(bars, { startIdx });
      render(id, info, res, years);
      setStatus(null);
    } catch (e) {
      setStatus('error', `❌ 分析失敗：${esc(e.message)}${/level|limit|上限/i.test(e.message) ? '<br>FinMind 免費額度每小時有上限，可稍後再試或填入 Token。' : ''}`);
    } finally {
      $('submitBtn').disabled = false;
    }
  }

  // ---------- 呈現 ----------
  const VERDICT_STYLE = {
    STRONG_BUY: 'bg-red-600 text-white', BUY: 'bg-red-50 text-red-700 border-2 border-red-300',
    HOLD: 'bg-slate-100 text-slate-700 border-2 border-slate-300',
    SELL: 'bg-green-50 text-green-700 border-2 border-green-300', STRONG_SELL: 'bg-green-700 text-white',
  };
  const TREND_LABEL = { RECOVERY: ['回升（多頭架構）', 'text-red-600', '月線上揚、股價站上月線 → 搭高鐵，回檔不破月線可加碼'], REBOUND: ['反彈（空頭架構）', 'text-green-600', '月線下彎、股價在月線下 → 搶銀行，有賺要跑、沒賺也要跑'], TRANSITION: ['轉折過渡', 'text-amber-600', '月線方向與股價位置不一致，等待確認'], NA: ['NA', 'text-slate-400', ''] };

  function evidence(res, s) {
    const ps = res.perSignal[s.id];
    if (!ps) return { label: '—', cls: 'text-slate-400', text: '' };
    if (ps.side === 'buy') {
      const st = ps.stats;
      if (!st.n) return { label: '無歷史樣本', cls: 'bg-slate-100 text-slate-500', text: '本股回測期間未曾觸發' };
      const text = `本股 ${st.n} 次：勝率 ${pct(st.winRate, 0)}、平均 ${pct(st.avgRet, 2, true)}、PF ${isFinite(st.profitFactor) ? st.profitFactor.toFixed(2) : '∞'}`;
      if (st.n < 5) return { label: '樣本不足', cls: 'bg-slate-100 text-slate-500', text };
      if (st.profitFactor >= 1.2) return { label: '歷史有效', cls: 'bg-red-100 text-red-700', text };
      if (st.profitFactor < 1) return { label: '歷史無效', cls: 'bg-amber-100 text-amber-700', text };
      return { label: '效果普通', cls: 'bg-slate-100 text-slate-600', text };
    }
    const f = ps.fwd.h10, base = res.baseline.h10.downRate;
    if (!f.n) return { label: '無歷史樣本', cls: 'bg-slate-100 text-slate-500', text: '本股回測期間未曾觸發' };
    const text = `本股 ${f.n} 次：10 日後下跌機率 ${pct(f.downRate, 0)}（基準 ${pct(base, 0)}）、平均 ${pct(f.avg, 1, true)}`;
    if (f.n < 5) return { label: '樣本不足', cls: 'bg-slate-100 text-slate-500', text };
    if (f.downRate - base >= 0.05) return { label: '歷史有效', cls: 'bg-green-100 text-green-700', text };
    if (f.downRate - base <= 0) return { label: '歷史無效', cls: 'bg-amber-100 text-amber-700', text };
    return { label: '效果普通', cls: 'bg-slate-100 text-slate-600', text };
  }

  function card(title, body, extra = '') {
    return `<div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200 ${extra}"><h3 class="text-sm font-bold text-slate-800 mb-3">${title}</h3>${body}</div>`;
  }
  const standaloneTag = id => (E.SIGNAL_MAP[id]?.standalone ? '<span class="ml-1 px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 text-[10px] font-semibold align-middle">獨立策略・不計分</span>' : '');
  const row = (k, v, cls = 'text-slate-700') => `<div class="flex justify-between gap-3 py-1 border-b border-slate-50 last:border-0"><span class="text-slate-500">${k}</span><span class="font-semibold text-right ${cls}">${v}</span></div>`;

  function render(id, info, res, years) {
    const b = res.last, p = res.prev;
    const chg = b.close / p.close - 1;
    const name = info ? info.stock_name : '';
    $('dataDate').textContent = `最新資料日：${res.date}`;
    const op = res.composite.openPos;

    // ---- 1. 綜合判斷 ----
    const parts = res.scoreParts.map(x => `<div class="flex justify-between gap-3 text-xs py-0.5"><span class="text-slate-600">${esc(x.label)}</span><span class="font-bold ${x.w > 0 ? 'text-red-500' : 'text-green-600'}">${x.w > 0 ? '+' : ''}${x.w.toFixed(2)}</span></div>`).join('');
    let posHtml;
    if (op) {
      posHtml = `<div class="text-sm"><span class="font-bold text-indigo-700">策略部位：持有中</span>（${esc(op.entryDate)} 依「${esc(op.name)}」進場 @ ${num(op.entryPx)}，未實現 <span class="${signCls(op.ret)} font-semibold">${pct(op.ret, 1, true)}</span>）</div>
        <div class="text-xs text-slate-500 mt-1">目前停損／出場參考價：<b>${num(op.stop)}</b>${op.pendingExit ? `　<span class="text-green-700 font-bold">⚠ 今日觸發出場：${esc(op.pendingExit)}，次日開盤出清</span>` : ''}</div>`;
    } else {
      const le = res.composite.lastExit;
      posHtml = `<div class="text-sm"><span class="font-bold text-slate-700">策略部位：空手</span>${le ? `（上次出場 ${esc(le.date)}，${esc(le.reason)}）` : ''}</div>`;
    }
    const verdictHtml = `
      <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
        <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <div>
            <div class="text-xs font-semibold text-slate-400">${esc(info ? `${info.type === 'tpex' ? '上櫃' : '上市'}｜${info.industry_category}` : '')}</div>
            <div class="text-2xl font-extrabold text-slate-800 mt-1">${esc(id)} ${esc(name)}</div>
            <div class="flex items-baseline gap-3 mt-2">
              <span class="text-4xl font-extrabold ${signCls(chg)}">${num(b.rawClose)}</span>
              <span class="text-lg font-bold ${signCls(chg)}">${pct(chg, 2, true)}</span>
            </div>
            <div class="text-xs text-slate-400 mt-1">${esc(res.date)} 收盤｜量 ${Math.round(b.volume).toLocaleString()} 張（${num(b.volume / res.ctx.ind.volMa20Prev[res.ctx.bars.length - 1], 1)} 倍均量）</div>
            <div class="mt-4">${posHtml}</div>
          </div>
          <div class="flex flex-col items-center justify-center">
            <div class="text-xs font-semibold text-slate-400 mb-2">策略綜合判斷</div>
            <div class="px-8 py-4 rounded-2xl text-3xl font-extrabold ${VERDICT_STYLE[res.verdictKey]}">${res.verdict}</div>
            <div class="text-sm text-slate-500 mt-2">綜合分數 <b class="${signCls(res.score)}">${res.score > 0 ? '+' : ''}${res.score.toFixed(2)}</b></div>
            <div class="text-sm mt-1"><span class="font-bold ${TREND_LABEL[res.trend][1]}">${TREND_LABEL[res.trend][0]}</span></div>
            <div class="text-[11px] text-slate-400 text-center mt-1">${TREND_LABEL[res.trend][2]}</div>
          </div>
          <div>
            <div class="text-xs font-semibold text-slate-400 mb-2">執行建議</div>
            <ul class="space-y-1.5 text-sm text-slate-700">${res.advice.map(a => `<li class="flex gap-2"><span class="text-indigo-500">▸</span><span>${esc(a)}</span></li>`).join('')}</ul>
            <details class="mt-3"><summary class="text-xs text-indigo-600 hover:underline">分數組成</summary><div class="mt-2 p-3 bg-slate-50 rounded-lg">${parts}</div></details>
          </div>
        </div>
      </div>`;

    // ---- 2. 觸發訊號 ----
    const recent = res.recent;
    const sigRows = recent.length ? recent.map(s => {
      const ev = evidence(res, s);
      return `<tr class="align-top hover:bg-slate-50">
        <td class="py-3 px-3 whitespace-nowrap"><span class="px-2 py-0.5 rounded text-xs font-bold ${s.side === 'buy' ? 'bg-red-100 text-red-700' : 'bg-green-100 text-green-700'}">${s.side === 'buy' ? '買進' : '賣出'}</span></td>
        <td class="py-3 px-3"><div class="font-bold text-slate-800">${esc(s.name)} <span class="text-amber-500">${'★'.repeat(s.strength)}</span>${standaloneTag(s.id)}</div><div class="text-[11px] text-slate-400">${esc(s.ref)}｜${esc(s.date)}${s.daysAgo ? `（${s.daysAgo} 日前）` : '（今日）'}</div></td>
        <td class="py-3 px-3 text-xs text-slate-600"><ul class="list-disc pl-4 space-y-0.5">${s.reasons.map(r => `<li>${esc(r)}</li>`).join('')}</ul>${s.stop ? `<div class="mt-1 text-slate-500">停損 ${num(s.stop)}（${esc(s.stopNote)}）</div>` : ''}${s.note ? `<div class="mt-1 font-semibold text-slate-700">${esc(s.note)}</div>` : ''}</td>
        <td class="py-3 px-3 text-xs"><span class="px-2 py-0.5 rounded font-bold ${ev.cls}">${ev.label}</span><div class="text-slate-500 mt-1">${esc(ev.text)}</div></td>
      </tr>`;
    }).join('') : `<tr><td colspan="4" class="py-8 text-center text-slate-400">近 3 個交易日沒有觸發任何戰法訊號</td></tr>`;
    const signalsHtml = `
      <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
        <h2 class="text-lg font-bold text-slate-800">🚦 近 3 日觸發戰法訊號</h2>
        <p class="text-xs text-slate-400 mt-0.5 mb-3">每個訊號旁附上該訊號在本股過去 ${years} 年的實際回測表現（買訊依文件出場規則計算報酬，賣訊計算 10 日後下跌機率）</p>
        <div class="overflow-x-auto"><table class="w-full text-sm text-left border-collapse">
          <thead class="text-xs text-slate-700 bg-slate-100"><tr><th class="py-2 px-3 rounded-l-lg">方向</th><th class="py-2 px-3">戰法</th><th class="py-2 px-3">觸發條件</th><th class="py-2 px-3 rounded-r-lg">本股歷史驗證</th></tr></thead>
          <tbody class="divide-y divide-slate-100">${sigRows}</tbody></table></div>
      </div>`;

    // ---- 3. 圖表 ----
    const chartHtml = `
      <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
        <div class="flex flex-wrap items-center justify-between gap-2 mb-2">
          <h2 class="text-lg font-bold text-slate-800">📈 近 120 日 K 線、布林通道與戰法訊號</h2>
          <div class="flex flex-wrap gap-3 text-xs text-slate-500">
            <span><i class="inline-block w-3 h-0.5 align-middle" style="background:${C.ma20}"></i> 月線 20MA</span>
            <span><i class="inline-block w-3 border-t border-dashed align-middle" style="border-color:${C.band}"></i> 布林上/下軌</span>
            <span title="近 20 日主力(法人)買超日，以買超張數加權的成交均價"><i class="inline-block w-3 h-0.5 align-middle" style="background:${C.cost}"></i> 主力成本線 (20日)</span>
            <span style="color:${C.up}">▲ 買訊</span><span style="color:${C.down}">▼ 賣訊</span>
          </div>
        </div>
        <div class="relative" id="kWrap"></div>
        <div class="text-xs font-semibold text-slate-500 mt-3 mb-1">主力(法人)每日買賣超（張）</div>
        <div class="relative" id="mfWrap"></div>
      </div>`;

    // ---- 4. 指標面板 ----
    const d = res.deduct, bb = res.bb, fl = res.floor, mf = res.mf, mg = res.margin;
    const dedRows = [5, 10, 20, 60].filter(k => d[k]).map(k => {
      const x = d[k];
      return `<tr class="border-b border-slate-50"><td class="py-1.5 font-semibold text-slate-700">${k}MA</td><td class="py-1.5 text-right">${num(x.ma)}</td><td class="py-1.5 text-right">${num(x.deduct)}</td><td class="py-1.5 text-right font-bold ${x.turningUp ? 'text-red-500' : 'text-green-600'}">${x.turningUp ? '↗ 上揚' : '↘ 下彎'}</td></tr>`;
    }).join('');
    const pk = d.peak, tr = d.trough;
    const pkNote = pk.deductInDays > 0 && pk.deductInDays <= 20 ? `高點將於 <b>${pk.deductInDays}</b> 個交易日後被扣抵${res.last.close < pk.price ? '，若無法過高月線將下彎' : ''}` : pk.daysSince >= 20 ? '高點已扣抵完畢' : '';
    const deductCard = card('🧮 均線扣抵（明日收平盤時方向）', `
      <table class="w-full text-xs"><thead class="text-slate-400"><tr><th class="text-left font-medium pb-1">均線</th><th class="text-right font-medium pb-1">今日</th><th class="text-right font-medium pb-1">明日扣抵值</th><th class="text-right font-medium pb-1">方向</th></tr></thead><tbody>${dedRows}</tbody></table>
      <div class="text-xs text-slate-500 mt-3 space-y-1">
        <div>60 日高點 <b>${num(pk.price)}</b>（${esc(pk.date)}，${pk.daysSince} 日前）${pkNote ? `→ ${pkNote}` : ''}</div>
        <div>60 日低點 <b>${num(tr.price)}</b>（${esc(tr.date)}，${tr.daysSince} 日前）${tr.deductInDays > 0 && tr.deductInDays <= 20 ? `→ 低點將於 <b>${tr.deductInDays}</b> 日後扣抵，月線易翻揚` : ''}</div>
        <div class="text-slate-400">明日收盤需高於 20MA 扣抵值 ${num(d[20]?.deduct)}，月線才會上揚</div>
      </div>`);

    const q = res.quadrant;
    const qCell = (n, title, sub) => `<div class="p-2.5 rounded-lg border ${q === n ? 'border-indigo-400 bg-indigo-50 ring-2 ring-indigo-200' : 'border-slate-200 bg-slate-50'}"><div class="text-xs font-bold ${q === n ? 'text-indigo-700' : 'text-slate-600'}">${title}</div><div class="text-[11px] text-slate-500">${sub}</div></div>`;
    const matrixCard = card('🧭 籌碼 × 扣抵 四大矩陣', `
      <div class="grid grid-cols-[auto_1fr_1fr] gap-1.5 text-center items-center">
        <div></div><div class="text-[11px] text-slate-400">月線上揚/即將上揚</div><div class="text-[11px] text-slate-400">月線下彎/即將下彎</div>
        <div class="text-[11px] text-slate-400 pr-1">主力買</div>${qCell(1, '順勢攻擊', '底部吃飽拉升')}${qCell(3, '低檔抗跌', '賣不下去反轉')}
        <div class="text-[11px] text-slate-400 pr-1">主力賣</div>${qCell(2, '利多出貨', '假突破陷阱')}${qCell(4, '多殺多', '空頭崩盤')}
      </div>
      <div class="text-xs text-slate-500 mt-3">近 10 日主力(法人) <b class="${signCls(mf.d10)}">${lots(mf.d10)}</b></div>`);

    const regimeTxt = { SQUEEZE: '極窄帶寬（≤5%）→ 找突破（開布林）', WIDE: '超寬帶寬（≥20%）→ 區間上下軌逆向操作', NORMAL: '常態帶寬', NA: 'NA' }[bb.regime];
    const bbCard = card('🎰 布林通道（20, 2σ）', `
      <div class="text-xs">${row('上軌 / 中軌 / 下軌', `${num(bb.upper)} / ${num(bb.mid)} / ${num(bb.lower)}`)}
      ${row('帶寬', `${pct(bb.bw)}`, bb.regime === 'SQUEEZE' ? 'text-indigo-600' : bb.regime === 'WIDE' ? 'text-amber-600' : 'text-slate-700')}
      ${row('上軌斜率', pct(bb.upperSlope, 2, true), bb.upperSlope >= 0.01 ? 'text-red-500' : 'text-slate-700')}
      ${row('下軌斜率', pct(bb.lowerSlope, 2, true), bb.lowerSlope <= -0.01 ? 'text-green-600' : 'text-slate-700')}
      ${row('月線斜率', pct(bb.ma20Slope, 2, true), bb.ma20Slope >= 0.004 ? 'text-red-500' : bb.ma20Slope < 0 ? 'text-green-600' : 'text-slate-700')}</div>
      <div class="text-xs text-slate-500 mt-2">${regimeTxt}${bb.riding ? '<br><b class="text-red-600">沿上軌推升中（山的左邊，空單禁入）</b>' : ''}${bb.strongTrend ? '<br><b class="text-red-600">月線斜率 ≥0.4%：通道與月線間推升，未破月線續抱</b>' : ''}</div>`);

    const floorCard = card('🪂 統計地板線 / 乖離率', `
      <div class="text-xs">${row('20MA 乖離率', pct(fl.bias, 2, true), signCls(fl.bias))}
      ${row('95% 地板價（MA20−2σ）', num(fl.f95))}
      ${row('99% 地板價（MA20−2.58σ）', num(fl.f99))}
      ${row('本股乖離 5% / 1% 分位', `${pct(fl.p05)} / ${pct(fl.p01)}`, 'text-green-600')}
      ${row('本股乖離 95% / 99% 分位', `${pct(fl.p95)} / ${pct(fl.p99)}`, 'text-red-500')}</div>
      <div class="text-[11px] text-slate-400 mt-2">分位數取自本股近 250 日乖離分佈，作為專屬天花板／地板參考</div>`);

    const chipCard = card('💰 籌碼與信用交易', `
      <div class="text-xs">${row('主力(法人) 今日', lots(mf.d1), signCls(mf.d1))}
      ${row('　外資 / 投信 / 自營', `<span class="${signCls(mf.foreign)}">${lots(mf.foreign)}</span> / <span class="${signCls(mf.trust)}">${lots(mf.trust)}</span> / <span class="${signCls(mf.dealer)}">${lots(mf.dealer)}</span>`)}
      ${row('主力成本 20 日 / 60 日', `${num(res.mfCost.c20)} / ${num(res.mfCost.c60)}`, 'text-violet-700')}
      ${row('現價相對 20 日主力成本', pct(res.mfCost.c20 ? res.ctx.bars[res.ctx.bars.length - 1].close / res.mfCost.c20 - 1 : null, 1, true), signCls(res.mfCost.c20 ? res.ctx.bars[res.ctx.bars.length - 1].close - res.mfCost.c20 : null))}
      ${row('主力(法人) 5 日 / 20 日', `<span class="${signCls(mf.d5)}">${lots(mf.d5)}</span> / <span class="${signCls(mf.d20)}">${lots(mf.d20)}</span>`)}
      ${row('融資餘額（5 日變化）', `${mg.marginBal != null ? mg.marginBal.toLocaleString() : 'NA'} 張（${lots(mg.marginChg5)}）`)}
      ${row('融券餘額（5 日變化）', `${mg.shortBal != null ? mg.shortBal.toLocaleString() : 'NA'} 張（${lots(mg.shortChg5)}）`)}
      ${row('券資比', pct(mg.ratio), mg.ratio >= 0.3 ? 'text-red-600' : 'text-slate-700')}</div>
      <div class="text-[11px] text-slate-400 mt-2">${mg.ratio >= 0.3 ? '券資比 ≥30%：空單密度高，若沿上軌＋主力買為軋空火箭' : '券資比 <30%：未具軋空燃料'}</div>`);

    const naCard = card('🔒 未納入（需付費／人工）', `
      <ul class="text-xs text-slate-500 space-y-1 list-disc pl-4">
        <li>集保大戶 / 散戶比例 ±3% 法則</li><li>前 15 大分點主力買賣超、關鍵分點、隔日沖分點</li>
        <li>董監申報轉讓（一般交易 / 鉅額逐筆）</li><li>盤前試撮、期現貨價差比（當沖）</li>
        <li>公開收購一字線、重磅利多新聞</li></ul>`);

    const panelsHtml = `<div class="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">${deductCard}${matrixCard}${bbCard}${floorCard}${chipCard}${naCard}</div>`;

    // ---- 5. 回測 ----
    const cp = res.composite, cs = cp.stats;
    const fromDate = res.ctx.bars[res.startIdx].date;
    const statTile = (label, v, cls, sub = '') => `<div class="p-4 rounded-xl bg-slate-50 border border-slate-100"><div class="text-[11px] font-semibold text-slate-400">${label}</div><div class="text-2xl font-extrabold mt-1 ${cls}">${v}</div>${sub ? `<div class="text-[11px] text-slate-400 mt-0.5">${sub}</div>` : ''}</div>`;
    const buyRows = Object.entries(res.perSignal).filter(([, v]) => v.side === 'buy').map(([sid, v]) => {
      const s = v.stats;
      return `<tr class="hover:bg-slate-50"><td class="py-2 px-3 font-semibold text-slate-700">${esc(v.name)}${standaloneTag(sid)}<div class="text-[11px] font-normal text-slate-400">${esc(v.ref)}</div></td>
        <td class="py-2 px-3 text-right">${s.n}${s.openTrades ? `<span class="text-[11px] text-indigo-500"> +${s.openTrades}持有</span>` : ''}</td>
        <td class="py-2 px-3 text-right">${s.n ? pct(s.winRate, 0) : '—'}</td>
        <td class="py-2 px-3 text-right ${signCls(s.avgRet)}">${s.n ? pct(s.avgRet, 2, true) : '—'}</td>
        <td class="py-2 px-3 text-right ${signCls(s.compound)}">${s.n ? pct(s.compound, 1, true) : '—'}</td>
        <td class="py-2 px-3 text-right">${s.n ? (isFinite(s.profitFactor) ? s.profitFactor.toFixed(2) : '∞') : '—'}</td>
        <td class="py-2 px-3 text-right text-green-600">${s.n ? pct(s.mdd, 1) : '—'}</td>
        <td class="py-2 px-3 text-right">${s.n ? s.avgHold.toFixed(1) : '—'}</td></tr>`;
    }).join('');
    const base = res.baseline;
    const sellRows = Object.entries(res.perSignal).filter(([, v]) => v.side === 'sell').map(([sid, v]) => {
      const f = v.fwd;
      const cell = H => { const o = f[H]; if (!o.n) return '<td class="py-2 px-3 text-right text-slate-300">—</td>'; const edge = o.downRate - base[H].downRate; return `<td class="py-2 px-3 text-right"><span class="${edge >= 0.05 ? 'text-green-600 font-bold' : edge <= 0 ? 'text-amber-600' : ''}">${pct(o.downRate, 0)}</span><div class="text-[11px] ${signCls(o.avg)}">${pct(o.avg, 1, true)}</div></td>`; };
      return `<tr class="hover:bg-slate-50"><td class="py-2 px-3 font-semibold text-slate-700">${esc(v.name)}<div class="text-[11px] font-normal text-slate-400">${esc(v.ref)}</div></td><td class="py-2 px-3 text-right">${f.n}</td>${cell('h5')}${cell('h10')}${cell('h20')}</tr>`;
    }).join('');
    const tradeRows = cp.trades.slice(-15).reverse().map(t => `<tr class="hover:bg-slate-50"><td class="py-1.5 px-3 whitespace-nowrap">${esc(t.entryDate)}</td><td class="py-1.5 px-3">${esc(E.SIGNAL_MAP[t.sigId].name)}</td><td class="py-1.5 px-3 text-right">${num(t.entryPx)}</td><td class="py-1.5 px-3 whitespace-nowrap">${t.open ? '<span class="text-indigo-600 font-semibold">持有中</span>' : esc(t.exitDate)}</td><td class="py-1.5 px-3 text-right">${num(t.exitPx)}</td><td class="py-1.5 px-3 text-right font-semibold ${signCls(t.ret)}">${pct(t.ret, 2, true)}</td><td class="py-1.5 px-3 text-slate-500">${esc(t.reason)}</td></tr>`).join('');
    const backtestHtml = `
      <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200 space-y-5">
        <div>
          <h2 class="text-lg font-bold text-slate-800">🔬 本股歷史回測驗證（${esc(fromDate)} ～ ${esc(res.date)}）</h2>
          <p class="text-xs text-slate-400 mt-0.5">綜合策略：任一買訊進場（同日有賣訊或處於反彈架構時只允許反轉型訊號），以該戰法自身的出場規則或任一強賣訊出場。搶反彈 SOP 依 1/10 資金計算。</p>
        </div>
        <div class="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
          ${statTile('綜合策略報酬', pct(cp.totalRet, 1, true), signCls(cp.totalRet), `最大回撤 ${pct(cp.mdd, 1)}`)}
          ${statTile('買進持有報酬', pct(cp.buyHold, 1, true), signCls(cp.buyHold), `最大回撤 ${pct(cp.buyHoldMdd, 1)}`)}
          ${statTile('交易次數', cs.n || 0, 'text-slate-800', `平均持有 ${cs.n ? cs.avgHold.toFixed(1) : '—'} 日`)}
          ${statTile('勝率', cs.n ? pct(cs.winRate, 0) : '—', 'text-slate-800', `PF ${cs.n ? (isFinite(cs.profitFactor) ? cs.profitFactor.toFixed(2) : '∞') : '—'}`)}
          ${statTile('平均每筆', cs.n ? pct(cs.avgRet, 2, true) : '—', signCls(cs.avgRet), `最佳 ${pct(cs.best, 1, true)}／最差 ${pct(cs.worst, 1, true)}`)}
          ${statTile('持股時間比例', pct(cp.exposure, 0), 'text-slate-800', '其餘時間空手')}
        </div>
        <div>
          <div class="flex flex-wrap items-center justify-between gap-2 mb-1">
            <div class="text-sm font-bold text-slate-700">權益曲線（起始 = 1）</div>
            <div class="flex gap-4 text-xs text-slate-500"><span><i class="inline-block w-4 h-0.5 align-middle" style="background:${C.strat}"></i> 綜合策略</span><span><i class="inline-block w-4 h-0.5 align-middle" style="background:${C.bh}"></i> 買進持有</span></div>
          </div>
          <div class="relative" id="eqWrap"></div>
        </div>
        <div>
          <div class="text-sm font-bold text-slate-700 mb-2">多方戰法逐一回測（依文件出場規則）</div>
          <div class="overflow-x-auto"><table class="w-full text-sm border-collapse">
            <thead class="text-xs text-slate-700 bg-slate-100"><tr><th class="py-2 px-3 text-left rounded-l-lg">戰法</th><th class="py-2 px-3 text-right">次數</th><th class="py-2 px-3 text-right">勝率</th><th class="py-2 px-3 text-right">平均報酬</th><th class="py-2 px-3 text-right">累積報酬</th><th class="py-2 px-3 text-right">PF</th><th class="py-2 px-3 text-right">最大回撤</th><th class="py-2 px-3 text-right rounded-r-lg">平均持有</th></tr></thead>
            <tbody class="divide-y divide-slate-100">${buyRows}</tbody></table></div>
        </div>
        <div>
          <div class="text-sm font-bold text-slate-700 mb-1">賣出戰法：訊號後 N 日下跌機率（下方為平均漲跌）</div>
          <div class="text-xs text-slate-400 mb-2">基準（任意一天）下跌機率：5 日 ${pct(base.h5.downRate, 0)}｜10 日 ${pct(base.h10.downRate, 0)}｜20 日 ${pct(base.h20.downRate, 0)}。<span class="text-green-600 font-bold">綠色粗體</span>＝高於基準 5 個百分點以上；<span class="text-amber-600">橘色</span>＝不優於基準</div>
          <div class="overflow-x-auto"><table class="w-full text-sm border-collapse">
            <thead class="text-xs text-slate-700 bg-slate-100"><tr><th class="py-2 px-3 text-left rounded-l-lg">戰法</th><th class="py-2 px-3 text-right">次數</th><th class="py-2 px-3 text-right">5 日</th><th class="py-2 px-3 text-right">10 日</th><th class="py-2 px-3 text-right rounded-r-lg">20 日</th></tr></thead>
            <tbody class="divide-y divide-slate-100">${sellRows}</tbody></table></div>
        </div>
        <details>
          <summary class="text-sm font-bold text-indigo-600 hover:underline">📋 綜合策略最近 15 筆交易</summary>
          <div class="overflow-x-auto mt-2"><table class="w-full text-xs border-collapse">
            <thead class="text-slate-700 bg-slate-100"><tr><th class="py-2 px-3 text-left">進場日</th><th class="py-2 px-3 text-left">戰法</th><th class="py-2 px-3 text-right">進場價</th><th class="py-2 px-3 text-left">出場日</th><th class="py-2 px-3 text-right">出場價</th><th class="py-2 px-3 text-right">報酬</th><th class="py-2 px-3 text-left">出場原因</th></tr></thead>
            <tbody class="divide-y divide-slate-100">${tradeRows || '<tr><td colspan="7" class="py-4 text-center text-slate-400">無交易</td></tr>'}</tbody></table></div>
          <div class="text-[11px] text-slate-400 mt-1">價格為除權息還原價</div>
        </details>
      </div>`;

    const el = $('result');
    el.innerHTML = verdictHtml + signalsHtml + chartHtml + panelsHtml + backtestHtml;
    el.classList.remove('hidden');
    drawKline(res);
    drawEquity(res);
  }

  // ---------- 圖表 ----------
  const tip = () => $('tip');
  function showTip(html, evt) {
    const t = tip();
    t.innerHTML = html; t.classList.remove('hidden');
    const pad = 14, w = t.offsetWidth, h = t.offsetHeight;
    let x = evt.clientX + pad, y = evt.clientY + pad;
    if (x + w > window.innerWidth - 8) x = evt.clientX - w - pad;
    if (y + h > window.innerHeight - 8) y = evt.clientY - h - pad;
    t.style.left = `${x + window.scrollX}px`; t.style.top = `${y + window.scrollY}px`;
  }
  const hideTip = () => tip().classList.add('hidden');

  function niceTicks(lo, hi, count = 5) {
    const step0 = (hi - lo) / count;
    const mag = 10 ** Math.floor(Math.log10(step0));
    const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= step0) || step0;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10));
    return out;
  }

  function drawKline(res) {
    const { bars, ind } = res.ctx;
    const N = Math.min(120, bars.length);
    const s0 = bars.length - N;
    const W = 1000, H = 360, L = 8, R = 56, T = 12, B = 24;
    const pw = W - L - R, ph = H - T - B;
    let lo = Infinity, hi = -Infinity;
    for (let i = s0; i < bars.length; i++) {
      lo = Math.min(lo, bars[i].low, ind.lower[i] ?? Infinity, ind.mfCost20[i] ?? Infinity); hi = Math.max(hi, bars[i].high, ind.upper[i] ?? -Infinity, ind.mfCost20[i] ?? -Infinity);
    }
    const padY = (hi - lo) * 0.06; lo -= padY; hi += padY;
    const step = pw / N, cw = Math.max(1.5, step * 0.62);
    const x = i => L + (i - s0 + 0.5) * step;
    const y = v => T + (hi - v) / (hi - lo) * ph;
    const line = arr => { let d = ''; for (let i = s0; i < bars.length; i++) if (arr[i] != null) d += `${d ? 'L' : 'M'}${x(i).toFixed(1)},${y(arr[i]).toFixed(1)}`; return d; };
    let g = '';
    for (const t of niceTicks(lo, hi)) g += `<line x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}" stroke="${C.grid}" stroke-width="1"/><text x="${W - R + 6}" y="${y(t) + 4}" font-size="11" fill="${C.axis}">${t}</text>`;
    // 月份刻度
    let lastM = '';
    for (let i = s0; i < bars.length; i++) { const m = bars[i].date.slice(0, 7); if (m !== lastM) { if (lastM) g += `<text x="${x(i)}" y="${H - 6}" font-size="11" fill="${C.axis}" text-anchor="middle">${m}</text>`; lastM = m; } }
    g += `<path d="${line(ind.upper)}" fill="none" stroke="${C.band}" stroke-width="1.5" stroke-dasharray="4 3"/><path d="${line(ind.lower)}" fill="none" stroke="${C.band}" stroke-width="1.5" stroke-dasharray="4 3"/><path d="${line(ind.ma20)}" fill="none" stroke="${C.ma20}" stroke-width="2"/><path d="${line(ind.mfCost20)}" fill="none" stroke="${C.cost}" stroke-width="2"/>`;
    for (let i = s0; i < bars.length; i++) {
      const b = bars[i], col = b.close >= b.open ? C.up : C.down;
      const top = y(Math.max(b.open, b.close)), bh = Math.max(1, Math.abs(y(b.open) - y(b.close)));
      g += `<line x1="${x(i)}" x2="${x(i)}" y1="${y(b.high)}" y2="${y(b.low)}" stroke="${col}" stroke-width="1"/><rect x="${x(i) - cw / 2}" y="${top}" width="${cw}" height="${bh}" fill="${col}" rx="1"/>`;
      const hs = res.hits[i];
      if (hs.some(h => h.side === 'buy')) g += `<text x="${x(i)}" y="${y(b.low) + 14}" font-size="11" fill="${C.up}" text-anchor="middle">▲</text>`;
      if (hs.some(h => h.side === 'sell')) g += `<text x="${x(i)}" y="${y(b.high) - 5}" font-size="11" fill="${C.down}" text-anchor="middle">▼</text>`;
    }
    g += `<line id="kCross" x1="0" x2="0" y1="${T}" y2="${H - B}" stroke="#64748b" stroke-width="1" stroke-dasharray="3 3" visibility="hidden"/>`;
    $('kWrap').innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="w-full h-auto select-none" role="img" aria-label="近 120 日 K 線圖">${g}<rect x="${L}" y="${T}" width="${pw}" height="${ph}" fill="transparent" id="kHit"/></svg>`;

    // 主力買賣超柱狀（獨立圖，不共用 Y 軸）
    const MH = 110, mT = 6, mB = 6, mph = MH - mT - mB;
    let mmax = 1;
    for (let i = s0; i < bars.length; i++) mmax = Math.max(mmax, Math.abs(bars[i].mf || 0));
    const my = v => mT + mph / 2 - (v / mmax) * (mph / 2);
    let m = `<line x1="${L}" x2="${W - R}" y1="${my(0)}" y2="${my(0)}" stroke="${C.grid}"/><text x="${W - R + 6}" y="${mT + 10}" font-size="11" fill="${C.axis}">${Math.round(mmax).toLocaleString()}</text><text x="${W - R + 6}" y="${MH - mB}" font-size="11" fill="${C.axis}">-${Math.round(mmax).toLocaleString()}</text>`;
    for (let i = s0; i < bars.length; i++) {
      const v = bars[i].mf; if (v == null) continue;
      const y0 = my(0), y1 = my(v);
      m += `<rect x="${x(i) - cw / 2}" y="${Math.min(y0, y1)}" width="${cw}" height="${Math.max(1, Math.abs(y1 - y0))}" fill="${v >= 0 ? C.up : C.down}" opacity="0.85" rx="1"/>`;
    }
    m += `<line id="mCross" x1="0" x2="0" y1="0" y2="${MH}" stroke="#64748b" stroke-width="1" stroke-dasharray="3 3" visibility="hidden"/>`;
    $('mfWrap').innerHTML = `<svg viewBox="0 0 ${W} ${MH}" class="w-full h-auto select-none" role="img" aria-label="主力(法人)每日買賣超">${m}<rect x="${L}" y="0" width="${pw}" height="${MH}" fill="transparent" id="mHit"/></svg>`;

    const onMove = (evt, svg) => {
      const r = svg.getBoundingClientRect();
      const sx = (evt.clientX - r.left) / r.width * W;
      const i = Math.min(bars.length - 1, Math.max(s0, s0 + Math.floor((sx - L) / step)));
      const b = bars[i], prev = bars[i - 1];
      for (const id of ['kCross', 'mCross']) { const el = document.getElementById(id); el.setAttribute('x1', x(i)); el.setAttribute('x2', x(i)); el.setAttribute('visibility', 'visible'); }
      const c = prev ? b.close / prev.close - 1 : 0;
      const sigs = res.hits[i].map(h => `<div style="color:${h.side === 'buy' ? '#fca5a5' : '#86efac'}">${h.side === 'buy' ? '▲' : '▼'} ${esc(h.name)}</div>`).join('');
      showTip(`<div class="font-bold mb-1">${b.date}</div>
        <div>開 ${num(b.open)}　高 ${num(b.high)}</div><div>低 ${num(b.low)}　收 <b>${num(b.close)}</b> <span style="color:${c >= 0 ? '#fca5a5' : '#86efac'}">${pct(c, 2, true)}</span></div>
        <div>量 ${Math.round(b.volume).toLocaleString()} 張｜主力(法人) ${lots(b.mf)}</div>
        <div class="text-slate-300">月線 ${num(ind.ma20[i])}｜帶寬 ${pct(ind.bw[i])}</div>
        <div style="color:#c4b5fd">主力成本(20日) ${num(ind.mfCost20[i])}${ind.mfCost20[i] ? `（${pct(b.close / ind.mfCost20[i] - 1, 1, true)}）` : ''}</div>${sigs ? `<div class="mt-1 pt-1 border-t border-slate-700">${sigs}</div>` : ''}`, evt);
    };
    const leave = () => { hideTip(); for (const id of ['kCross', 'mCross']) document.getElementById(id)?.setAttribute('visibility', 'hidden'); };
    for (const [hitId, wrap] of [['kHit', 'kWrap'], ['mHit', 'mfWrap']]) {
      const svg = $(wrap).querySelector('svg');
      document.getElementById(hitId).addEventListener('mousemove', e => onMove(e, svg));
      document.getElementById(hitId).addEventListener('mouseleave', leave);
    }
  }

  function drawEquity(res) {
    const { bars } = res.ctx;
    const eq = res.composite.equity;
    const s0 = Math.max(130, res.startIdx);
    const pts = [];
    for (let i = s0; i < bars.length; i++) pts.push({ i, s: eq[i] ?? (pts.length ? pts[pts.length - 1].s : 1), b: bars[i].close / bars[s0].close });
    if (pts.length < 2) return;
    const W = 1000, H = 260, L = 8, R = 56, T = 10, B = 24;
    const pw = W - L - R, ph = H - T - B;
    let lo = Infinity, hi = -Infinity;
    for (const p of pts) { lo = Math.min(lo, p.s, p.b); hi = Math.max(hi, p.s, p.b); }
    const useLog = hi / lo > 4;
    const f = v => (useLog ? Math.log(v) : v);
    const flo = f(lo), fhi = f(hi);
    const x = k => L + (k / (pts.length - 1)) * pw;
    const y = v => T + (fhi - f(v)) / (fhi - flo || 1) * ph;
    let g = '';
    const ticks = useLog ? [0.25, 0.5, 1, 2, 4, 8, 16, 32, 64].filter(t => t >= lo * 0.98 && t <= hi * 1.02) : niceTicks(lo, hi, 4);
    for (const t of ticks) g += `<line x1="${L}" x2="${W - R}" y1="${y(t)}" y2="${y(t)}" stroke="${t === 1 ? '#cbd5e1' : C.grid}"/><text x="${W - R + 6}" y="${y(t) + 4}" font-size="11" fill="${C.axis}">${t}×</text>`;
    let lastY = '';
    pts.forEach((p, k) => { const yy = bars[p.i].date.slice(0, 4); if (yy !== lastY) { if (lastY) g += `<text x="${x(k)}" y="${H - 6}" font-size="11" fill="${C.axis}" text-anchor="middle">${yy}</text>`; lastY = yy; } });
    const path = key => pts.map((p, k) => `${k ? 'L' : 'M'}${x(k).toFixed(1)},${y(p[key]).toFixed(1)}`).join('');
    g += `<path d="${path('b')}" fill="none" stroke="${C.bh}" stroke-width="2"/><path d="${path('s')}" fill="none" stroke="${C.strat}" stroke-width="2"/>`;
    g += `<line id="eCross" x1="0" x2="0" y1="${T}" y2="${H - B}" stroke="#64748b" stroke-dasharray="3 3" visibility="hidden"/><circle id="eDotS" r="4" fill="${C.strat}" stroke="#fff" stroke-width="2" visibility="hidden"/><circle id="eDotB" r="4" fill="${C.bh}" stroke="#fff" stroke-width="2" visibility="hidden"/>`;
    $('eqWrap').innerHTML = `<svg viewBox="0 0 ${W} ${H}" class="w-full h-auto select-none" role="img" aria-label="綜合策略與買進持有權益曲線">${g}<rect id="eHit" x="${L}" y="${T}" width="${pw}" height="${ph}" fill="transparent"/></svg>${useLog ? '<div class="text-[11px] text-slate-400">對數座標</div>' : ''}`;
    const svg = $('eqWrap').querySelector('svg');
    const hit = document.getElementById('eHit');
    hit.addEventListener('mousemove', evt => {
      const r = svg.getBoundingClientRect();
      const sx = (evt.clientX - r.left) / r.width * W;
      const k = Math.min(pts.length - 1, Math.max(0, Math.round((sx - L) / pw * (pts.length - 1))));
      const p = pts[k];
      const cr = document.getElementById('eCross'); cr.setAttribute('x1', x(k)); cr.setAttribute('x2', x(k)); cr.setAttribute('visibility', 'visible');
      for (const [id, key] of [['eDotS', 's'], ['eDotB', 'b']]) { const d = document.getElementById(id); d.setAttribute('cx', x(k)); d.setAttribute('cy', y(p[key])); d.setAttribute('visibility', 'visible'); }
      showTip(`<div class="font-bold mb-1">${bars[p.i].date}</div><div><span style="color:#a5b4fc">●</span> 綜合策略 ${pct(p.s - 1, 1, true)}</div><div><span style="color:#cbd5e1">●</span> 買進持有 ${pct(p.b - 1, 1, true)}</div>`, evt);
    });
    hit.addEventListener('mouseleave', () => { hideTip(); for (const id of ['eCross', 'eDotS', 'eDotB']) document.getElementById(id).setAttribute('visibility', 'hidden'); });
  }

  // ---------- 多檔驗證 ----------
  async function renderValidation() {
    const el = $('validation');
    let v;
    try { v = await (await fetch('./data/strategy_validation.json', { cache: 'no-cache' })).json(); } catch (e) { el.classList.add('hidden'); return; }
    const sum = v.compositeSummary, b = v.baseline;
    const fmtS = s => (s.n ? `${pct(s.winRate, 0)} / ${pct(s.avgRet, 2, true)} / ${s.profitFactor != null ? s.profitFactor.toFixed(2) : '—'}` : '—');
    const pfCls = s => (!s.n ? 'text-slate-300' : s.profitFactor >= 1.2 ? 'text-red-600 font-bold' : s.profitFactor < 1 ? 'text-amber-600' : 'text-slate-600');
    const buyRows = Object.entries(v.buySignals).map(([sid, s]) => `<tr class="hover:bg-slate-50"><td class="py-2 px-3 font-semibold text-slate-700">${esc(s.name)}${standaloneTag(sid)}</td><td class="py-2 px-3 text-right">${s.all.n}</td><td class="py-2 px-3 text-right ${pfCls(s.all)}">${fmtS(s.all)}</td><td class="py-2 px-3 text-right ${pfCls(s.early)}">${fmtS(s.early)}</td><td class="py-2 px-3 text-right ${pfCls(s.late)}">${fmtS(s.late)}</td></tr>`).join('');
    const variantRows = Object.values(v.variants || {}).map(s => `<tr class="hover:bg-slate-50 text-slate-500"><td class="py-2 px-3 italic">對照：${esc(s.name)}</td><td class="py-2 px-3 text-right">${s.all.n}</td><td class="py-2 px-3 text-right ${pfCls(s.all)}">${fmtS(s.all)}</td><td class="py-2 px-3 text-right ${pfCls(s.early)}">${fmtS(s.early)}</td><td class="py-2 px-3 text-right ${pfCls(s.late)}">${fmtS(s.late)}</td></tr>`).join('');
    const sellRows = Object.values(v.sellSignals).map(s => {
      const c = H => { const o = s[H]; if (!o.n) return '<td class="py-2 px-3 text-right text-slate-300">—</td>'; const e = o.downRate - b[H].downRate; return `<td class="py-2 px-3 text-right ${e >= 0.05 ? 'text-green-600 font-bold' : e <= 0 ? 'text-amber-600' : ''}">${pct(o.downRate, 0)}<span class="text-[11px] text-slate-400">（${e >= 0 ? '+' : ''}${(e * 100).toFixed(0)}pp）</span></td>`; };
      return `<tr class="hover:bg-slate-50"><td class="py-2 px-3 font-semibold text-slate-700">${esc(s.name)}</td><td class="py-2 px-3 text-right">${s.n}</td>${c('h5')}${c('h10')}${c('h20')}</tr>`;
    }).join('');
    el.innerHTML = `
      <h2 class="text-lg font-bold text-slate-800">🧪 跨個股策略驗證（${v.stocks} 檔 × ${esc(v.start)} 起，離線回測）</h2>
      <p class="text-xs text-slate-400 mt-0.5 mb-4">以與本頁相同的引擎，對權值股、中小型股與策略文件案例股（倉和、晶彩科、凱衛、銘旺科、亞光、撼訊、麗清…）及光通訊股（華星光、聯亞）逐一回測。前期／後期以 ${esc(v.split)} 切分，用來檢驗規則的穩定度。所有門檻皆取自策略文件，未依回測結果調整參數。產生時間 ${esc(v.generatedAt.slice(0, 10))}</p>
      <div class="grid grid-cols-2 md:grid-cols-4 gap-3 mb-5">
        <div class="p-4 rounded-xl bg-slate-50 border border-slate-100"><div class="text-[11px] font-semibold text-slate-400">綜合策略報酬（中位數）</div><div class="text-2xl font-extrabold ${signCls(sum.medianRet)}">${pct(sum.medianRet, 0, true)}</div><div class="text-[11px] text-slate-400">買進持有 ${pct(sum.medianBuyHold, 0, true)}</div></div>
        <div class="p-4 rounded-xl bg-slate-50 border border-slate-100"><div class="text-[11px] font-semibold text-slate-400">最大回撤（中位數）</div><div class="text-2xl font-extrabold text-green-600">${pct(sum.medianMdd, 0)}</div><div class="text-[11px] text-slate-400">買進持有 ${pct(sum.medianBuyHoldMdd, 0)}</div></div>
        <div class="p-4 rounded-xl bg-slate-50 border border-slate-100"><div class="text-[11px] font-semibold text-slate-400">回撤小於買進持有</div><div class="text-2xl font-extrabold text-slate-800">${sum.shallowerMdd} / ${v.stocks}</div><div class="text-[11px] text-slate-400">持股時間中位數 ${pct(sum.medianExposure, 0)}</div></div>
        <div class="p-4 rounded-xl bg-slate-50 border border-slate-100"><div class="text-[11px] font-semibold text-slate-400">報酬勝過買進持有</div><div class="text-2xl font-extrabold text-slate-800">${sum.beatBuyHold} / ${v.stocks}</div><div class="text-[11px] text-slate-400">正報酬 ${sum.positive} 檔</div></div>
      </div>
      <div class="grid grid-cols-1 xl:grid-cols-2 gap-5">
        <div><div class="text-sm font-bold text-slate-700 mb-2">多方戰法（勝率 / 平均報酬 / PF）</div>
          <div class="overflow-x-auto"><table class="w-full text-xs border-collapse"><thead class="text-slate-700 bg-slate-100"><tr><th class="py-2 px-3 text-left">戰法</th><th class="py-2 px-3 text-right">次數</th><th class="py-2 px-3 text-right">全期</th><th class="py-2 px-3 text-right">前期</th><th class="py-2 px-3 text-right">後期</th></tr></thead><tbody class="divide-y divide-slate-100">${buyRows}${variantRows}</tbody></table></div>
          <div class="text-[11px] text-slate-400 mt-1"><span class="text-red-600 font-bold">紅色粗體</span> PF ≥ 1.2｜<span class="text-amber-600">橘色</span> PF &lt; 1（扣成本後虧損）</div></div>
        <div><div class="text-sm font-bold text-slate-700 mb-2">賣出戰法：訊號後下跌機率（相對基準）</div>
          <div class="overflow-x-auto"><table class="w-full text-xs border-collapse"><thead class="text-slate-700 bg-slate-100"><tr><th class="py-2 px-3 text-left">戰法</th><th class="py-2 px-3 text-right">次數</th><th class="py-2 px-3 text-right">5 日</th><th class="py-2 px-3 text-right">10 日</th><th class="py-2 px-3 text-right">20 日</th></tr></thead><tbody class="divide-y divide-slate-100">${sellRows}</tbody></table></div>
          <div class="text-[11px] text-slate-400 mt-1">基準下跌機率：5 日 ${pct(b.h5.downRate, 0)}｜10 日 ${pct(b.h10.downRate, 0)}｜20 日 ${pct(b.h20.downRate, 0)}（${esc(v.start.slice(0, 4))} 年後台股為多頭，基準偏低）</div></div>
      </div>
      <div class="mt-4 p-4 rounded-lg bg-amber-50 border border-amber-200 text-xs text-amber-800 leading-relaxed">
        <b>驗證結論：</b>此期間台股為長多格局，持股時間僅約三分之一的綜合策略，總報酬普遍落後買進持有，但多數個股的最大回撤較小。
        多方戰法中，<b>主力由賣轉買點火、軋空火箭、光頭大紅棒</b>在前後兩期都維持正期望值（矩陣4 低檔反轉也是正的，但樣本少）；<b>大帶寬回測下軌</b>前期略虧、後期轉正，不夠穩定；<b>窄帶寬開布林</b>與<b>地板線搶反彈</b>全期扣除成本後為負。
        賣出戰法的預測力普遍有限（多數接近基準），比較適合當作持股的出場紀律，不宜單獨作為放空依據。每個訊號旁都標示了在該股的歷史表現，請一併參考。
      </div>
      ${v.buySignals.VCB ? `<div class="mt-3 p-4 rounded-lg bg-slate-50 border border-slate-200 text-xs text-slate-700 leading-relaxed">
        <b>Larry Williams 波動收斂突破（VCB）：</b>跨 ${v.stocks} 檔共 ${v.buySignals.VCB.all.n.toLocaleString()} 筆，勝率 ${pct(v.buySignals.VCB.all.winRate, 0)}、平均每筆 ${pct(v.buySignals.VCB.all.avgRet, 2, true)}、PF ${v.buySignals.VCB.all.profitFactor.toFixed(2)}，前後兩期都一樣是負值；拿掉投信／大盤輔助濾網後結果幾乎相同。
        原因是進場本身的優勢太小：突破後 5 日平均上漲幅度只比任意一天多約 0.1 個百分點，遠低於來回交易成本 0.585%；停損設在收斂日低點又很近，約三分之二的交易以停損出場。
        因此 VCB 以<b>獨立策略</b>顯示訊號與回測，<b>不計入</b>綜合策略與買賣評分（加入的話，綜合策略報酬中位數會從 +28% 降到 +19%）。
      </div>` : ''}`;
  }

  // ---------- 初始化 ----------
  $('queryForm').addEventListener('submit', e => { e.preventDefault(); run($('stockInput').value); });
  document.querySelectorAll('.quick').forEach(btn => btn.addEventListener('click', () => { $('stockInput').value = btn.dataset.quick; run(btn.dataset.quick); }));
  $('tokenInput').value = storage.get('finmindToken') || '';
  renderValidation();
  const qid = new URLSearchParams(location.search).get('id');
  const init = qid || storage.get('lastStock');
  if (init) { $('stockInput').value = init; run(init); }
})();
