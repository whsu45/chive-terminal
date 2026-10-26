/* 族群漲跌排行頁：讀取 data/themes.json 與 data/theme_picks.json */
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = (v, d = 2) => (v == null || !isFinite(v) ? 'NA' : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));
  const signCls = v => (v == null || v === 0 ? 'text-slate-500' : v > 0 ? 'text-red-500' : 'text-green-600');
  const pctStr = v => (v == null ? 'NA' : `${v > 0 ? '▲ +' : v < 0 ? '▼ ' : ''}${v.toFixed(2)}%`);

  const VERDICT_STYLE = {
    STRONG_BUY: 'bg-red-600 text-white', BUY: 'bg-red-50 text-red-700 border border-red-300',
    HOLD: 'bg-slate-100 text-slate-700 border border-slate-300',
    SELL: 'bg-green-50 text-green-700 border border-green-300', STRONG_SELL: 'bg-green-700 text-white',
  };
  const CONF_STYLE = { 高: 'bg-indigo-600 text-white', 中: 'bg-indigo-100 text-indigo-700', 低: 'bg-slate-200 text-slate-600' };

  function pickCard(s, rank, side) {
    const border = side === 'strong' ? 'border-red-200' : 'border-green-200';
    return `
      <div class="p-4 rounded-xl border ${border} bg-white">
        <div class="flex items-start justify-between gap-2">
          <div>
            <div class="text-[11px] font-semibold text-slate-400">第 ${rank} 名｜${esc(s.market || '')}｜${s.themes.map(esc).join('、')}</div>
            <div class="text-lg font-extrabold text-slate-800">${esc(s.code)} ${esc(s.name)}</div>
            <div class="text-sm"><span class="font-bold text-slate-700">${num(s.close)}</span> <span class="font-bold ${signCls(s.pct)}">${pctStr(s.pct)}</span> <span class="text-xs text-slate-400">量 ${Number(s.volume).toLocaleString()} 張</span></div>
          </div>
          <div class="flex flex-col items-end gap-1 shrink-0">
            <span class="px-2.5 py-1 rounded-lg text-sm font-bold ${VERDICT_STYLE[s.verdictKey] || ''}">${esc(s.verdict)}</span>
            <span class="text-[11px] text-slate-500">分數 <b class="${signCls(s.score)}">${s.score > 0 ? '+' : ''}${s.score.toFixed(2)}</b></span>
            <span class="px-2 py-0.5 rounded text-[11px] font-bold ${CONF_STYLE[s.confidenceLabel] || ''}" title="依觸發訊號在本股及跨個股的歷史回測統計">可信度 ${esc(s.confidenceLabel)}</span>
          </div>
        </div>
        <ul class="mt-3 space-y-1 text-xs text-slate-600">${s.reasons.map(r => `<li class="flex gap-1.5"><span class="${side === 'strong' ? 'text-red-400' : 'text-green-500'}">▸</span><span>${esc(r)}</span></li>`).join('')}</ul>
        <div class="mt-3 flex justify-between items-center text-[11px] text-slate-400">
          <span>策略資料日 ${esc(s.asOf)}</span>
          <a href="./stock.html?id=${encodeURIComponent(s.code)}" class="text-indigo-600 font-semibold hover:underline">完整策略分析 →</a>
        </div>
      </div>`;
  }

  function renderPicks(p) {
    const strongHtml = p.strong.length
      ? p.strong.map((s, i) => pickCard(s, i + 1, 'strong')).join('')
      : `<div class="p-6 text-center text-sm text-slate-400 bg-slate-50 rounded-xl">今日漲幅前段個股中，沒有策略判斷達「買進」以上的標的。<br>（候選 ${p.evaluated.strong} 檔皆未達門檻，寧缺勿濫）</div>`;
    $('picks').innerHTML = `
      <div class="xl:col-span-3 bg-white rounded-xl shadow-sm p-6 border border-slate-200">
        <h2 class="text-lg font-bold text-red-700">🔥 近日高機率強勢個股</h2>
        <p class="text-xs text-slate-400 mt-0.5 mb-4">${esc(p.method.strongRule)}</p>
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-3">${strongHtml}</div>
      </div>
      <div class="xl:col-span-2 bg-white rounded-xl shadow-sm p-6 border border-slate-200">
        <h2 class="text-lg font-bold text-green-700">🧊 前三弱勢個股</h2>
        <p class="text-xs text-slate-400 mt-0.5 mb-4">${esc(p.method.weakRule)}</p>
        <div class="space-y-3">${p.weak.map((s, i) => pickCard(s, i + 1, 'weak')).join('')}</div>
      </div>`;
  }

  function themeRow(t, idx) {
    const maxAbs = Math.max(1, ...t.members.map(m => Math.abs(m.pct ?? 0)));
    const rows = t.members.map((m, i) => {
      const w = m.pct == null ? 0 : Math.abs(m.pct) / maxAbs * 100;
      return `<tr class="hover:bg-slate-50" data-search="${esc(`${m.code} ${m.name}`)}">
        <td class="py-2 px-2 sm:px-3 text-slate-400">${i + 1}</td>
        <td class="py-2 px-2 sm:px-3 font-semibold whitespace-nowrap"><a href="./stock.html?id=${encodeURIComponent(m.code)}" class="text-slate-800 hover:text-indigo-600 hover:underline">${esc(m.code)} ${esc(m.name)}</a></td>
        <td class="py-2 px-2 sm:px-3 text-xs text-slate-500 whitespace-nowrap hidden sm:table-cell">${esc(m.market || '—')}</td>
        <td class="py-2 px-2 sm:px-3 text-right">${num(m.close)}</td>
        <td class="py-2 px-2 sm:px-3 text-right ${signCls(m.change)} whitespace-nowrap hidden sm:table-cell">${m.change == null ? 'NA' : `${m.change > 0 ? '+' : ''}${num(m.change)}`}</td>
        <td class="py-2 px-2 sm:px-3 text-right font-bold ${signCls(m.pct)} whitespace-nowrap">${pctStr(m.pct)}</td>
        <td class="py-2 px-2 sm:px-3 w-32 hidden md:table-cell"><div class="h-2 rounded-full bg-slate-100 overflow-hidden"><div class="h-full rounded-full ${m.pct >= 0 ? 'bg-red-400' : 'bg-green-500'}" style="width:${w.toFixed(1)}%"></div></div></td>
        <td class="py-2 px-2 sm:px-3 text-right text-slate-500 hidden sm:table-cell">${m.volume == null ? 'NA' : Number(m.volume).toLocaleString()}</td>
        <td class="py-2 px-2 sm:px-3 text-right hidden sm:table-cell"><a href="./stock.html?id=${encodeURIComponent(m.code)}" class="text-xs text-indigo-600 hover:underline whitespace-nowrap">策略分析</a></td>
      </tr>`;
    }).join('');
    return `
      <details class="theme rounded-lg border border-slate-200 open:shadow-sm" data-search="${esc(`${t.title} ${t.slug}`)}">
        <summary class="flex items-center gap-3 px-4 py-3 hover:bg-slate-50 rounded-lg">
          <span class="chev text-slate-400 transition-transform">▶</span>
          <span class="w-7 text-xs font-bold text-slate-400">#${idx + 1}</span>
          <span class="flex-1 min-w-0">
            <span class="font-bold text-slate-800">${esc(t.title)}</span>
            <span class="text-xs text-slate-400 ml-2">${t.members.length} 檔｜<span class="text-red-500">漲 ${t.up}</span>・<span class="text-green-600">跌 ${t.down}</span>・平 ${t.flat}</span>
            <span class="block text-xs text-slate-500 truncate mt-0.5">${t.members.slice(0, 4).map(m => `${esc(m.name)} <span class="${signCls(m.pct)}">${m.pct == null ? 'NA' : `${m.pct > 0 ? '+' : ''}${m.pct.toFixed(1)}%`}</span>`).join('、')}${t.members.length > 4 ? '…' : ''}</span>
          </span>
          <span class="text-lg font-extrabold ${signCls(t.avg_pct)} whitespace-nowrap">${pctStr(t.avg_pct)}</span>
        </summary>
        <div class="px-2 sm:px-4 pb-4">
          <p class="text-xs text-slate-500 mb-2">${esc(t.desc)}</p>
          <div class="overflow-x-auto"><table class="w-full text-sm border-collapse">
            <thead class="text-xs text-slate-700 bg-slate-100"><tr><th class="py-2 px-2 sm:px-3 text-left rounded-l-lg">#</th><th class="py-2 px-2 sm:px-3 text-left">個股</th><th class="py-2 px-2 sm:px-3 text-left whitespace-nowrap hidden sm:table-cell">市場</th><th class="py-2 px-2 sm:px-3 text-right">收盤</th><th class="py-2 px-2 sm:px-3 text-right hidden sm:table-cell">漲跌</th><th class="py-2 px-2 sm:px-3 text-right">漲跌幅</th><th class="py-2 px-2 sm:px-3 hidden md:table-cell"></th><th class="py-2 px-2 sm:px-3 text-right whitespace-nowrap hidden sm:table-cell">成交量(張)</th><th class="py-2 px-2 sm:px-3 rounded-r-lg hidden sm:table-cell"></th></tr></thead>
            <tbody class="divide-y divide-slate-100">${rows}</tbody></table></div>
          <a href="https://finlab.finance/stocks/themes/${encodeURIComponent(t.slug)}" target="_blank" rel="noopener" class="inline-block mt-2 text-[11px] text-slate-400 hover:underline">族群說明來源：FinLab</a>
        </div>
      </details>`;
  }

  function applyFilter() {
    const q = $('filter').value.trim().toLowerCase();
    document.querySelectorAll('#themes details.theme').forEach(d => {
      if (!q) { d.classList.remove('hidden'); d.querySelectorAll('tbody tr').forEach(tr => tr.classList.remove('bg-amber-50')); return; }
      const themeHit = d.dataset.search.toLowerCase().includes(q);
      let memberHit = false;
      d.querySelectorAll('tbody tr').forEach(tr => {
        const hit = tr.dataset.search.toLowerCase().includes(q);
        tr.classList.toggle('bg-amber-50', hit);
        memberHit = memberHit || hit;
      });
      d.classList.toggle('hidden', !(themeHit || memberHit));
      if (memberHit && !themeHit) d.open = true;
    });
  }

  async function init() {
    let themes, picks;
    try {
      themes = await (await fetch('./data/themes.json', { cache: 'no-cache' })).json();
    } catch (e) {
      $('status').className = 'rounded-xl border p-4 text-sm bg-red-50 border-red-200 text-red-700';
      $('status').textContent = '❌ 尚無族群資料（data/themes.json），請等待下一次自動更新。';
      return;
    }
    try { picks = await (await fetch('./data/theme_picks.json', { cache: 'no-cache' })).json(); } catch (e) { picks = null; }

    $('meta').innerHTML = `<div>行情日：<span class="font-semibold text-slate-700">${esc(themes.quote_date)}</span></div>
      <div>族群名單更新：<span class="font-semibold text-slate-700">${esc(themes.members_fetched)}</span></div>
      <div class="text-slate-400">共 ${themes.themes.length} 個族群</div>`;

    if (picks && picks.quote_date === themes.quote_date) renderPicks(picks);
    else if (picks) { renderPicks(picks); $('picks').insertAdjacentHTML('afterbegin', `<div class="xl:col-span-5 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg p-3">強弱勢個股為 ${esc(picks.quote_date)} 的判讀結果，與目前行情日不同。</div>`); }
    else $('picks').classList.add('hidden');

    $('themes').innerHTML = themes.themes.map(themeRow).join('');
    $('filter').addEventListener('input', applyFilter);
    $('expandAll').addEventListener('click', () => document.querySelectorAll('#themes details.theme:not(.hidden)').forEach(d => { d.open = true; }));
    $('collapseAll').addEventListener('click', () => document.querySelectorAll('#themes details.theme').forEach(d => { d.open = false; }));

    $('method').innerHTML = `
      <h2 class="text-lg font-bold text-slate-800">⚠️ 資料與方法說明</h2>
      <ul class="list-disc pl-5 space-y-1">
        <li><b>族群名單</b>：每天從 <a class="text-indigo-600 hover:underline" href="${esc(themes.source)}" target="_blank" rel="noopener">FinLab 族群頁</a> 抓取一次成分股名單。抓取失敗時沿用上次的名單。</li>
        <li><b>漲跌幅</b>：取自證交所、櫃買中心 OpenAPI 的最新收盤行情，漲跌幅 = 漲跌 ÷（收盤 − 漲跌）。族群排序依成分股的平均漲跌幅。停牌或無報價的個股顯示 NA，不納入平均。</li>
        ${picks ? `<li><b>強勢個股</b>：${esc(picks.method.strongRule)}。</li>
        <li><b>弱勢個股</b>：${esc(picks.method.weakRule)}。</li>
        <li><b>可信度</b>：${esc(picks.method.confidenceRule)}。</li>
        <li>策略判斷沿用「個股策略分析」頁的同一套引擎（K 線、扣抵、布林、葛蘭碧、籌碼、軋空），回測近 ${picks.method.historyYears} 年。主力買賣超以三大法人合計代理。</li>` : ''}
        <li>本頁為規則化的統計呈現，<b>不構成投資建議</b>。</li>
      </ul>`;
  }

  init();
})();
