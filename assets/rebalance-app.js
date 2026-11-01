/* 指數調整套利頁：讀取 data/rebalance.json（scripts/build_rebalance.mjs 產生） */
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = (v, d = 2) => (v == null || !isFinite(v) ? 'NA' : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));
  const pct = (v, d = 1, sign = false) => (v == null || !isFinite(v) ? '—' : `${sign && v > 0 ? '+' : ''}${(v * 100).toFixed(d)}%`);
  const signCls = v => (v == null || v === 0 ? 'text-slate-500' : v > 0 ? 'text-red-500' : 'text-green-600');
  const SIDE = { add: '新增', delete: '刪除' };
  const SIDE_CLS = { add: 'bg-red-100 text-red-700', delete: 'bg-green-100 text-green-700' };
  const stockLink = (code, name) => `<a href="./stock.html?id=${encodeURIComponent(code)}" class="text-indigo-600 hover:underline font-semibold">${esc(code)} ${esc(name)}</a>`;
  let D = null;

  const STAGE_ROWS = [
    ['stage1', '階段 1：公告前 15 日卡位 → 公告日（事後名單、上限）', true],
    ['stage23', '階段 2→3：公告後開盤進場 → 生效日收盤'],
    ['stage23_normal', '　└ 非擁擠（公告前 20 日漲幅 ≤ 30%）'],
    ['stage23_crowded', '　└ 擁擠（公告前 20 日漲幅 > 30%）'],
    ['stage4_t1open', '階段 4：生效日收盤反手 → T+1 開盤'],
    ['stage4', '階段 4：生效日收盤反手 → T+3 收盤'],
  ];

  function statRow(label, s, dim) {
    if (!s || !s.n) return `<tr class="text-slate-400"><td class="py-2 px-3 whitespace-nowrap">${esc(label)}</td><td colspan="7" class="py-2 px-3 text-center">無樣本</td></tr>`;
    return `<tr class="hover:bg-slate-50 ${dim ? 'text-slate-400 italic' : ''}"><td class="py-2 px-3 whitespace-nowrap">${esc(label)}</td>
      <td class="py-2 px-3 text-right">${s.n}</td><td class="py-2 px-3 text-right">${pct(s.winRate, 0)}</td>
      <td class="py-2 px-3 text-right font-semibold ${dim ? '' : signCls(s.avgRet)}">${pct(s.avgRet, 2, true)}</td><td class="py-2 px-3 text-right ${dim ? '' : signCls(s.median)}">${pct(s.median, 2, true)}</td>
      <td class="py-2 px-3 text-right">${s.profitFactor == null ? '∞' : s.profitFactor.toFixed(2)}</td>
      <td class="py-2 px-3 text-right ${dim ? '' : signCls(s.excessAvg)}">${pct(s.excessAvg, 2, true)}</td><td class="py-2 px-3 text-right">${pct(s.excessWin, 0)}</td></tr>`;
  }
  const statHead = '<thead class="text-slate-700 bg-slate-100"><tr><th class="py-2 px-3 text-left rounded-l-lg">階段</th><th class="py-2 px-3 text-right">筆數</th><th class="py-2 px-3 text-right">勝率</th><th class="py-2 px-3 text-right">平均</th><th class="py-2 px-3 text-right">中位數</th><th class="py-2 px-3 text-right">PF</th><th class="py-2 px-3 text-right">超額平均</th><th class="py-2 px-3 text-right rounded-r-lg">勝過大盤</th></tr></thead>';

  // ---------- 已公告的調整：交易計畫 ----------
  function renderOpportunities() {
    const ops = D.opportunities;
    const pending = ops.filter(o => o.tradeDate >= D.today);
    const done = ops.filter(o => o.tradeDate < D.today);
    const row = o => {
      const dir = o.side === 'add' ? 1 : -1;
      const range = o.exitP25 != null ? `${num(Math.min(o.exitP25, o.exitP75))}～${num(Math.max(o.exitP25, o.exitP75))}` : '—';
      return `<tr class="align-top hover:bg-slate-50">
        <td class="py-3 px-3"><span class="px-2 py-0.5 rounded text-xs font-bold ${SIDE_CLS[o.side]}">${SIDE[o.side]}</span><div class="text-[11px] text-slate-400 mt-1 whitespace-nowrap">${esc(o.indexName)}</div><div class="text-[11px] text-slate-400">${o.etfs.map(esc).join('、')}</div></td>
        <td class="py-3 px-3">${stockLink(o.code, o.name)}<div class="text-[11px] text-slate-400">公告 ${esc(o.announceDate)}｜生效交易日 ${esc(o.tradeDate)}</div>${o.crowded ? `<div class="text-[11px] font-semibold text-amber-700 mt-1">⚠ 公告前 20 日已漲 ${pct(o.preRun, 0)}（擁擠），建議大幅降低部位</div>` : o.preRun != null ? `<div class="text-[11px] text-slate-400">公告前 20 日 ${pct(o.preRun, 1, true)}</div>` : ''}</td>
        <td class="py-3 px-3 text-xs"><div class="font-semibold text-slate-700">${dir > 0 ? '買進' : '放空'}：${esc(o.buyWhen)}</div><div>參考價 <b>${num(o.entryRef)}</b>${o.entered ? '' : `（${esc(o.lastDate)} 收盤）`}</div>${o.pnlNow != null ? `<div>目前 ${num(o.lastClose)}，<b class="${signCls(o.pnlNow)}">${pct(o.pnlNow, 2, true)}</b></div>` : ''}</td>
        <td class="py-3 px-3 text-xs"><div class="font-semibold text-slate-700">${dir > 0 ? '賣出' : '回補'}：${esc(o.sellWhen)}</div><div>預估點位 <b>${num(o.exitMedian)}</b>（中位數），區間 ${range}</div><div class="text-[11px] text-slate-400">依${esc(o.histFrom)} ${o.histN} 筆歷史分布（勝率 ${pct(o.histWin, 0)}）</div>${o.histMedian != null && o.histMedian <= 0 ? '<div class="text-[11px] font-semibold text-amber-700 mt-1">⚠ 歷史上這個方向的中位數報酬為負，不建議操作</div>' : ''}</td>
        <td class="py-3 px-3 text-xs"><div>${esc(o.reverse.when)}</div><div class="text-[11px] text-slate-400">歷史中位數 ${pct(o.reverse.median, 2, true)}（${o.reverse.n} 筆）</div>
          ${o.result ? `<div class="mt-1">實際：公告→生效 <b class="${signCls(o.result.stage23.ret)}">${pct(o.result.stage23.ret, 2, true)}</b>${o.result.stage4 ? `、反手 <b class="${signCls(o.result.stage4.ret)}">${pct(o.result.stage4.ret, 2, true)}</b>` : ''}</div>` : ''}</td>
      </tr>`;
    };
    const table = list => `<div class="overflow-x-auto"><table class="w-full text-sm border-collapse"><thead class="text-xs text-slate-700 bg-slate-100"><tr><th class="py-2 px-3 text-left rounded-l-lg">方向／指數</th><th class="py-2 px-3 text-left">個股</th><th class="py-2 px-3 text-left">進場</th><th class="py-2 px-3 text-left">出場（被動買賣盤）</th><th class="py-2 px-3 text-left rounded-r-lg">生效後反手（階段 4）</th></tr></thead><tbody class="divide-y divide-slate-100">${list.map(row).join('')}</tbody></table></div>`;
    const next = D.calendar.find(c => c.nextTradeDate);
    $('opportunities').innerHTML = `
      <h2 class="text-lg font-bold text-slate-800">🚨 已公告、即將生效的成分股調整</h2>
      <p class="text-xs text-slate-400 mt-0.5 mb-4">預估點位 = 進場價 ×（1 + 該指數歷史「公告→生效」報酬的中位數），區間為第 25～75 百分位；已扣交易成本。</p>
      ${pending.length ? table(pending) : `<div class="p-4 rounded-lg bg-slate-50 text-sm text-slate-600">目前沒有已公告、尚未生效的調整。${next ? `下一次預估為 <b>${esc(next.index)}</b>：約 ${esc(next.nextAnnounceDate || '—')} 公告、${esc(next.nextTradeDate)} 生效（見右側時程）。` : ''}公告前可參考下方前瞻模型的候選名單。</div>`}
      ${done.length ? `<h3 class="text-sm font-bold text-slate-700 mt-5 mb-2">剛生效（3 個交易日內）</h3>${table(done)}` : ''}`;
  }

  function renderCalendar() {
    const rows = D.calendar.map(c => `<tr class="hover:bg-slate-50"><td class="py-2 px-3"><div class="font-semibold text-slate-700">${esc(c.index)}</div><div class="text-[11px] text-slate-400">${c.etfs.map(esc).join('、') || '被動基金'}｜${esc(c.note)}</div></td>
      <td class="py-2 px-3 text-right whitespace-nowrap">${c.announceAfter ? '<span class="text-amber-600">生效後才公布</span>' : esc(c.nextAnnounceDate || '—')}</td><td class="py-2 px-3 text-right whitespace-nowrap font-semibold">${esc(c.nextTradeDate || '—')}</td></tr>`).join('');
    $('calendar').innerHTML = `
      <h2 class="text-lg font-bold text-slate-800">🗓 下一次審核（預估）</h2>
      <p class="text-xs text-slate-400 mt-0.5 mb-3">依各指數的審核月份與生效規則推估；公告日 = 生效交易日 − 歷史平均間隔</p>
      <div class="overflow-x-auto"><table class="w-full text-xs border-collapse"><thead class="text-slate-700 bg-slate-100"><tr><th class="py-2 px-3 text-left rounded-l-lg">指數</th><th class="py-2 px-3 text-right">預估公告</th><th class="py-2 px-3 text-right rounded-r-lg">預估生效交易日</th></tr></thead><tbody class="divide-y divide-slate-100">${rows}</tbody></table></div>`;
  }

  function renderModel() {
    const m = D.model;
    if (!m) { $('model').innerHTML = '<h2 class="text-lg font-bold text-slate-800">🔭 高股息 ETF 前瞻模型</h2><div class="text-sm text-slate-400 mt-3">尚無模型資料</div>'; return; }
    const ck = ok => (ok ? '<span class="text-red-600">✔</span>' : '<span class="text-slate-300">✘</span>');
    const row = s => `<tr class="hover:bg-slate-50"><td class="py-2 px-3 whitespace-nowrap">${stockLink(s.code, s.name)}<div class="text-[11px] text-slate-400">${esc(s.market)}｜收盤 ${num(s.close)}</div></td>
      <td class="py-2 px-3 text-right">${ck(s.checks.yield)} ${s.yield == null ? 'NA' : `${s.yield.toFixed(2)}%`}</td>
      <td class="py-2 px-3 text-right">${ck(s.checks.turnover)} ${num(s.turnover60, 1)} 億</td>
      <td class="py-2 px-3 text-right">${ck(s.checks.rank)} 第 ${s.rank} 名</td>
      <td class="py-2 px-3 text-right">${ck(s.checks.profit)} EPS ${s.eps == null ? 'NA' : num(s.eps)}<div class="text-[10px] text-slate-400">${esc(s.period || '')}</div></td></tr>`;
    const head = '<thead class="text-slate-700 bg-slate-100"><tr><th class="py-2 px-3 text-left rounded-l-lg">個股</th><th class="py-2 px-3 text-right">殖利率 ≥ 6.5%</th><th class="py-2 px-3 text-right">60 日均成交 ≥ 1.5 億</th><th class="py-2 px-3 text-right">市值前 200</th><th class="py-2 px-3 text-right rounded-r-lg">EPS／本業皆正</th></tr></thead>';
    $('model').innerHTML = `
      <h2 class="text-lg font-bold text-slate-800">🔭 高股息 ETF 前瞻模型（階段 1 候選）</h2>
      <p class="text-xs text-slate-400 mt-0.5 mb-3">依規格書四項條件篩選 ${m.universe.toLocaleString()} 檔上市櫃普通股（${esc(m.quoteDate)}）；四項全過者依殖利率排序。尚未公告，僅為模型推估。</p>
      <div class="overflow-x-auto"><table class="w-full text-xs border-collapse">${head}<tbody class="divide-y divide-slate-100">${m.candidates.length ? m.candidates.map(row).join('') : '<tr><td colspan="5" class="py-4 text-center text-slate-400">目前沒有四項全過的個股</td></tr>'}</tbody></table></div>
      <details class="mt-3"><summary class="text-xs text-indigo-600 hover:underline">差一項就符合的個股（${m.nearMiss.length} 檔）</summary>
        <div class="overflow-x-auto mt-2"><table class="w-full text-xs border-collapse">${head}<tbody class="divide-y divide-slate-100">${m.nearMiss.map(row).join('')}</tbody></table></div></details>`;
  }

  // ---------- 回測 ----------
  function renderBacktest(sel = 'overall') {
    const groups = [['overall', '全部指數'], ['cap', '市值型（臺灣 50、MSCI）'], ['dividend', '高股息型']].filter(([k]) => k === 'overall' || D.byGroup[k])
      .concat(D.indexes.map(ix => [ix.key, ix.index]));
    const data = sel === 'overall' ? D.overall : D.byGroup[sel] || D.byIndex[sel];
    const btn = ([k, l]) => `<button data-sel="${esc(k)}" class="sel px-3 py-1.5 rounded-lg text-xs font-semibold ${k === sel ? 'bg-indigo-600 text-white' : 'bg-slate-100 text-slate-600 hover:bg-slate-200'}">${esc(l)}</button>`;
    const table = (side, title) => `<div><div class="text-sm font-bold text-slate-700 mb-2">${title}</div><div class="overflow-x-auto"><table class="w-full text-xs border-collapse">${statHead}<tbody class="divide-y divide-slate-100">${STAGE_ROWS.map(([k, l, dim]) => statRow(l, data[side][k], dim)).join('')}</tbody></table></div></div>`;
    const a = D.overall.add, d = D.overall.delete;
    $('backtest').innerHTML = `
      <h2 class="text-lg font-bold text-slate-800">🧪 回測驗證（${D.rows.length} 筆個股事件，扣除交易成本）</h2>
      <p class="text-xs text-slate-400 mt-0.5 mb-3">新增股做多、刪除股做空；階段 4 方向相反。「超額平均」為相對加權指數的同期間超額報酬（未扣成本），「勝過大盤」為超額報酬為正的比例。</p>
      <div class="flex flex-wrap gap-2 mb-4">${groups.map(btn).join('')}</div>
      <div class="grid grid-cols-1 2xl:grid-cols-2 gap-5">${table('add', '📈 新增股')}${table('delete', '📉 刪除股')}</div>
      <div class="mt-4 p-4 rounded-lg bg-amber-50 border border-amber-200 text-xs text-amber-800 leading-relaxed"><b>驗證結論（全部指數）：</b>
        新增股「公告後進場 → 生效日收盤賣出」共 ${a.stage23.n} 筆，勝率 ${pct(a.stage23.winRate, 0)}、平均 ${pct(a.stage23.avgRet, 2, true)}、中位數 ${pct(a.stage23.median, 2, true)}；扣除大盤後平均超額 ${pct(a.stage23.excessAvg, 2, true)}，${pct(a.stage23.excessWin, 0)} 勝過大盤。
        ${a.stage23_crowded.n ? `其中擁擠標的 ${a.stage23_crowded.n} 筆平均 ${pct(a.stage23_crowded.avgRet, 2, true)}，非擁擠 ${a.stage23_normal.n} 筆平均 ${pct(a.stage23_normal.avgRet, 2, true)}。` : ''}
        生效日收盤反手放空新增股、T+3 回補：平均 ${pct(a.stage4.avgRet, 2, true)}（勝率 ${pct(a.stage4.winRate, 0)}）。
        刪除股「公告後放空 → 生效日回補」平均 ${pct(d.stage23.avgRet, 2, true)}、超額 ${pct(d.stage23.excessAvg, 2, true)}。
        ${D.byGroup.cap && D.byGroup.dividend ? `分類來看，<b>市值型指數（臺灣 50、MSCI）</b>新增股公告後進場平均 ${pct(D.byGroup.cap.add.stage23.avgRet, 2, true)}（勝率 ${pct(D.byGroup.cap.add.stage23.winRate, 0)}，超額 ${pct(D.byGroup.cap.add.stage23.excessAvg, 2, true)}），生效後反手放空 T+3 平均 ${pct(D.byGroup.cap.add.stage4.avgRet, 2, true)}（勝率 ${pct(D.byGroup.cap.add.stage4.winRate, 0)}），與規格書描述的「生效日收最高、隔天回落」一致；<b>高股息型</b>新增股公告後進場只有 ${pct(D.byGroup.dividend.add.stage23.avgRet, 2, true)}（中位數 ${pct(D.byGroup.dividend.add.stage23.median, 2, true)}），反手放空平均 ${pct(D.byGroup.dividend.add.stage4.avgRet, 2, true)}，效果明顯較弱，可能因為高股息 ETF 多在生效前後數日分批換股、名單也常在生效當天才公布。` : ''}
        規格書宣稱單次勝率 70%～80%；本回測期間台股大多頭，做多的絕對報酬偏高、放空偏低，請以「超額」欄判斷指數調整本身的效果。各指數樣本數有限（每檔指數 1～2 年才累積數十筆），請勿過度解讀單一指數的結果。
      </div>`;
    document.querySelectorAll('#backtest .sel').forEach(b => b.addEventListener('click', () => renderBacktest(b.dataset.sel)));
  }

  // ---------- 歷史事件 ----------
  function renderHistory() {
    const name = Object.fromEntries(D.indexes.map(ix => [ix.key, ix.index]));
    const rowsBy = {};
    for (const r of D.rows) (rowsBy[`${r.index}|${r.review}`] ||= []).push(r);
    const evs = [...D.events].sort((a, b) => (a.tradeDate < b.tradeDate ? 1 : -1));
    const cell = (e, side) => {
      const list = side === 'add' ? e.adds : e.deletes;
      if (!list.length) return '<span class="text-slate-300">—</span>';
      const rs = rowsBy[`${e.index}|${e.review}`] || [];
      return list.map(s => { const r = rs.find(x => x.code === s.code && x.side === side); const v = r && r.stage23 ? r.stage23.ret : null; return `<span class="whitespace-nowrap">${stockLink(s.code, s.name)}${v != null ? ` <span class="${signCls(v)}">${pct(v, 1, true)}</span>` : ''}</span>`; }).join('、');
    };
    const rows = evs.map(e => `<tr class="align-top hover:bg-slate-50"><td class="py-2 px-3 whitespace-nowrap"><div class="font-semibold text-slate-700">${esc(name[e.index])}</div><div class="text-[11px] text-slate-400">${esc(e.review)}${e.confidence === 'partial' ? '｜<span class="text-amber-600">部分資料待確認</span>' : ''}</div></td>
      <td class="py-2 px-3 whitespace-nowrap text-[11px]">公告 ${esc(e.announceDate)}<br>生效交易日 ${esc(e.tradeDate)}</td>
      <td class="py-2 px-3 text-xs">${cell(e, 'add')}</td><td class="py-2 px-3 text-xs">${cell(e, 'delete')}</td>
      <td class="py-2 px-3 text-[11px]">${(e.sources || []).slice(0, 2).map((u, k) => `<a href="${esc(u)}" target="_blank" rel="noopener" class="text-indigo-500 hover:underline">來源${k + 1}</a>`).join(' ')}</td></tr>`).join('');
    $('history').innerHTML = `
      <details><summary class="flex items-center gap-2"><span class="chev text-slate-400 transition-transform">▶</span><h2 class="text-lg font-bold text-slate-800">📜 歷史換股事件（${evs.length} 次）</h2></summary>
        <p class="text-xs text-slate-400 mt-1 mb-3">個股旁數字為「公告後進場 → 生效日收盤」報酬（刪除股為放空報酬），已扣成本</p>
        <div class="overflow-x-auto"><table class="w-full text-sm border-collapse"><thead class="text-xs text-slate-700 bg-slate-100"><tr><th class="py-2 px-3 text-left rounded-l-lg">指數／審核</th><th class="py-2 px-3 text-left">日期</th><th class="py-2 px-3 text-left">新增</th><th class="py-2 px-3 text-left">刪除</th><th class="py-2 px-3 text-left rounded-r-lg">來源</th></tr></thead><tbody class="divide-y divide-slate-100">${rows}</tbody></table></div>
      </details>`;
  }

  async function init() {
    try { D = await (await fetch('./data/rebalance.json', { cache: 'no-cache' })).json(); } catch (e) {
      $('status').className = 'rounded-xl border p-4 text-sm bg-red-50 border-red-200 text-red-700';
      $('status').textContent = '❌ 尚無指數調整資料（data/rebalance.json），請等待下一次自動更新。';
      return;
    }
    $('meta').innerHTML = `<div>資料日：<span class="font-semibold text-slate-700">${esc(D.today)}</span></div>
      <div>事件紀錄更新：${esc(D.eventsUpdated)}</div>
      <div class="text-slate-400">${D.indexes.length} 個指數、${D.events.length} 次審核</div>`;
    renderOpportunities();
    renderCalendar();
    renderModel();
    renderBacktest();
    renderHistory();
  }

  init();
})();
