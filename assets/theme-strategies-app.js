/* 族群戰法損益排行頁：讀取 data/theme_strategies.json（scripts/build_theme_strategies.mjs 產生） */
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const signCls = v => (v == null || v === 0 ? 'text-slate-500' : v > 0 ? 'text-red-500' : 'text-green-600');
  const money = v => (v == null ? 'NA' : `${v > 0 ? '+' : v < 0 ? '−' : ''}${Math.abs(v).toLocaleString()} 元`);
  const pct = (v, d = 1, sign = false) => (v == null || !isFinite(v) ? 'NA' : `${sign && v > 0 ? '+' : ''}${(v * 100).toFixed(d)}%`);
  const pf = v => (v == null ? '∞' : v.toFixed(2));
  const pctStr = v => (v == null ? 'NA' : `${v > 0 ? '▲ +' : v < 0 ? '▼ ' : ''}${v.toFixed(2)}%`);
  const tag = s => (s.standalone ? '<span class="ml-1 px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 text-[10px] font-semibold align-middle">獨立策略</span>' : '');
  const stockLink = s => `<a href="./stock.html?id=${encodeURIComponent(s.code)}" class="hover:underline">${esc(s.code)} ${esc(s.name)}</a>`;

  function rankCard(s, rank, side) {
    const list = side === 'top' ? s.topStocks : s.bottomStocks;
    const label = side === 'top' ? '主要獲利來源' : '主要虧損來源';
    return `
      <div class="p-4 rounded-xl border ${side === 'top' ? 'border-red-200' : 'border-green-200'} bg-white">
        <div class="flex items-start justify-between gap-3">
          <div class="min-w-0">
            <div class="text-[11px] font-semibold text-slate-400">第 ${rank} 名｜${esc(s.ref)}</div>
            <div class="font-extrabold text-slate-800">${esc(s.name)}${tag(s)}</div>
          </div>
          <div class="text-right whitespace-nowrap"><div class="text-xl font-extrabold ${signCls(s.profit)}">${money(s.profit)}</div><div class="text-[11px] text-slate-400">近 3 年合計損益</div></div>
        </div>
        <div class="grid grid-cols-4 gap-2 mt-3 text-center">
          <div class="rounded-lg bg-slate-50 py-1.5"><div class="text-[10px] text-slate-400">交易筆數</div><div class="text-sm font-bold text-slate-700">${s.n.toLocaleString()}</div><div class="text-[10px] text-slate-400">${s.stockCount} 檔</div></div>
          <div class="rounded-lg bg-slate-50 py-1.5"><div class="text-[10px] text-slate-400">勝率</div><div class="text-sm font-bold text-slate-700">${pct(s.winRate, 0)}</div></div>
          <div class="rounded-lg bg-slate-50 py-1.5"><div class="text-[10px] text-slate-400">平均每筆</div><div class="text-sm font-bold ${signCls(s.avgRet)}">${pct(s.avgRet, 2, true)}</div></div>
          <div class="rounded-lg bg-slate-50 py-1.5"><div class="text-[10px] text-slate-400">PF</div><div class="text-sm font-bold text-slate-700">${pf(s.profitFactor)}</div></div>
        </div>
        <div class="mt-3 text-xs text-slate-600"><span class="text-slate-400">${label}：</span>${list.map(x => `${stockLink(x)} <span class="${signCls(x.profit)}">${money(x.profit)}</span>（${x.n} 筆）`).join('、')}</div>
      </div>`;
  }

  function renderRanking(d) {
    const col = (title, sub, items, side) => `
      <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
        <h2 class="text-lg font-bold ${side === 'top' ? 'text-red-600' : 'text-green-700'}">${title}</h2>
        <p class="text-xs text-slate-400 mt-0.5 mb-4">${sub}</p>
        <div class="space-y-3">${items.length ? items.map((s, k) => rankCard(s, k + 1, side)).join('') : '<div class="text-sm text-slate-400 py-6 text-center">沒有符合的戰法</div>'}</div>
      </div>`;
    $('ranking').innerHTML =
      col('💰 賺最多的前五個戰法', `近 3 年合計損益為正的戰法，依損益由高到低（每筆投入 ${d.method.capitalPerTrade.toLocaleString()} 元）`, d.top, 'top') +
      col('💸 賠最多的前五個戰法', '近 3 年合計損益為負的戰法，依虧損由大到小', d.bottom, 'bottom');
  }

  // 全部戰法：以 0 為中軸的橫條圖（紅＝獲利、綠＝虧損，台股慣例）
  function renderAll(d) {
    const max = Math.max(1, ...d.all.map(s => Math.abs(s.profit)));
    const rows = d.all.map(s => {
      const w = (Math.abs(s.profit) / max) * 50;
      const bar = s.profit >= 0
        ? `<div class="absolute top-1 bottom-1 left-1/2 rounded-r bg-red-400" style="width:${w.toFixed(2)}%"></div>`
        : `<div class="absolute top-1 bottom-1 right-1/2 rounded-l bg-green-500" style="width:${w.toFixed(2)}%"></div>`;
      const tip = `${s.name}：${money(s.profit)}｜${s.n} 筆、${s.stockCount} 檔｜勝率 ${pct(s.winRate, 0)}｜平均 ${pct(s.avgRet, 2, true)}｜PF ${pf(s.profitFactor)}`;
      return `<div class="grid grid-cols-[1fr_6.5rem] sm:grid-cols-[16rem_1fr_7.5rem] items-center gap-x-2 py-0.5 hover:bg-slate-50 rounded" title="${esc(tip)}">
        <div class="col-span-2 sm:col-span-1 text-xs text-slate-700 truncate">${esc(s.name)}${tag(s)}</div>
        <div class="relative h-6"><div class="absolute inset-y-0 left-1/2 border-l border-slate-300"></div>${bar}</div>
        <div class="text-xs font-semibold text-right ${signCls(s.profit)}">${money(s.profit)}</div>
      </div>`;
    }).join('');
    $('allChart').innerHTML = `
      <h2 class="text-lg font-bold text-slate-800">📊 全部戰法近 3 年損益</h2>
      <p class="text-xs text-slate-400 mt-0.5 mb-4">所有族群成分股合計；滑鼠移到列上可看筆數、勝率與 PF</p>
      <div>${rows}</div>`;
  }

  function themeRow(t, idx) {
    const cell = (s, empty) => (s ? `<span class="font-semibold text-slate-700">${esc(s.name)}</span> <span class="${signCls(s.profit)} whitespace-nowrap">${money(s.profit)}</span>` : `<span class="text-slate-300">${empty}</span>`);
    const rows = t.signals.map(s => `<tr class="hover:bg-slate-50">
        <td class="py-2 px-2 sm:px-3">${esc(s.name)}${tag(s)}</td>
        <td class="py-2 px-2 sm:px-3 text-right">${s.n}<span class="text-[11px] text-slate-400 hidden sm:inline">／${s.stockCount} 檔</span></td>
        <td class="py-2 px-2 sm:px-3 text-right hidden sm:table-cell">${pct(s.winRate, 0)}</td>
        <td class="py-2 px-2 sm:px-3 text-right ${signCls(s.avgRet)} hidden sm:table-cell">${pct(s.avgRet, 2, true)}</td>
        <td class="py-2 px-2 sm:px-3 text-right hidden md:table-cell">${pf(s.profitFactor)}</td>
        <td class="py-2 px-2 sm:px-3 text-right font-semibold ${signCls(s.profit)} whitespace-nowrap">${money(s.profit)}</td></tr>`).join('');
    return `
      <details class="theme rounded-lg border border-slate-200 open:shadow-sm" data-search="${esc(`${t.title} ${t.signals.map(s => s.name).join(' ')}`)}">
        <summary class="flex items-center gap-3 px-4 py-3 hover:bg-slate-50 rounded-lg">
          <span class="chev text-slate-400 transition-transform">▶</span>
          <span class="w-7 text-xs font-bold text-slate-400">#${idx + 1}</span>
          <span class="flex-1 min-w-0">
            <span class="font-bold text-slate-800">${esc(t.title)}</span>
            <span class="text-xs text-slate-400 ml-2">已回測 ${t.covered} / ${t.members} 檔｜當日 <span class="${signCls(t.avg_pct)}">${pctStr(t.avg_pct)}</span></span>
            <span class="block text-xs mt-0.5 truncate">最賺：${cell(t.best, '無獲利戰法')}<span class="text-slate-300 mx-2">|</span>最賠：${cell(t.worst, '無虧損戰法')}</span>
          </span>
        </summary>
        <div class="px-2 sm:px-4 pb-4">
          ${t.signals.length ? `<div class="overflow-x-auto"><table class="w-full text-sm border-collapse">
            <thead class="text-xs text-slate-700 bg-slate-100"><tr><th class="py-2 px-2 sm:px-3 text-left rounded-l-lg">戰法</th><th class="py-2 px-2 sm:px-3 text-right">筆數</th><th class="py-2 px-2 sm:px-3 text-right hidden sm:table-cell">勝率</th><th class="py-2 px-2 sm:px-3 text-right hidden sm:table-cell">平均每筆</th><th class="py-2 px-2 sm:px-3 text-right hidden md:table-cell">PF</th><th class="py-2 px-2 sm:px-3 text-right rounded-r-lg">合計損益</th></tr></thead>
            <tbody class="divide-y divide-slate-100">${rows}</tbody></table></div>` : '<div class="text-sm text-slate-400 py-4 text-center">此族群尚無回測結果</div>'}
        </div>
      </details>`;
  }

  function applyFilter() {
    const q = $('filter').value.trim().toLowerCase();
    document.querySelectorAll('#themes details.theme').forEach(el => el.classList.toggle('hidden', !!q && !el.dataset.search.toLowerCase().includes(q)));
  }

  async function init() {
    let d;
    try {
      d = await (await fetch('./data/theme_strategies.json', { cache: 'no-cache' })).json();
    } catch (e) {
      $('status').className = 'rounded-xl border p-4 text-sm bg-red-50 border-red-200 text-red-700';
      $('status').textContent = '❌ 尚無戰法回測資料（data/theme_strategies.json），請等待下一次自動更新。';
      return;
    }
    const c = d.coverage;
    $('meta').innerHTML = `<div>已回測：<span class="font-semibold text-slate-700">${c.computed} / ${c.total} 檔</span></div>
      <div>回測資料至：<span class="font-semibold text-slate-700">${esc(c.asOfTo || 'NA')}</span></div>
      <div class="text-slate-400">每 ${d.method.refreshDays} 天輪流重算</div>`;
    if (c.computed < c.total - c.errors) {
      $('status').className = 'rounded-xl border p-4 text-sm bg-amber-50 border-amber-200 text-amber-800';
      $('status').textContent = `⏳ 目前已回測 ${c.computed} / ${c.total} 檔，其餘個股會在接下來幾次自動更新時陸續補上，排名可能還會變動。`;
    }
    renderRanking(d);
    renderAll(d);
    $('themes').innerHTML = d.themes.map(themeRow).join('');
    $('filter').addEventListener('input', applyFilter);
    $('method').innerHTML = `
      <h2 class="text-lg font-bold text-slate-800">📐 計算方式</h2>
      <ul class="list-disc pl-5 space-y-1">
        <li>${esc(d.method.rule)}。</li>
        <li>戰法與出場規則和「個股策略分析」頁完全相同：訊號於收盤後判斷、次一交易日開盤成交，價格停損以盤中觸價成交，股價已做除權息還原。</li>
        <li>只計算多方（買進）戰法；賣出訊號沒有自己的損益，不列入排行。「合計損益」偏重交易次數多的戰法，請同時參考平均每筆與 PF。</li>
        <li>族群的最賺／最賠戰法，至少要有 ${d.method.minTradesTheme} 筆交易才列入。同一檔股票屬於多個族群時，會分別計入每個族群。</li>
        <li>FinMind 免費額度有限，每次自動更新只重算一部分個股，每檔約每 ${d.method.refreshDays} 天更新一次；上市未滿約 8 個月、資料不足的個股不列入（目前 ${c.errors} 檔）。</li>
        <li>歷史回測不代表未來報酬，<b>不構成投資建議</b>。</li>
      </ul>`;
  }

  init();
})();
