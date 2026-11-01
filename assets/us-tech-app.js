/* 美股科技族群 × 台股連動頁：讀取 data/us_tech.json 與 data/us_tech_history.json */
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = (v, d = 2) => (v == null || !isFinite(v) ? 'NA' : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));
  const signCls = v => (v == null || v === 0 ? 'text-slate-500' : v > 0 ? 'text-red-500' : 'text-green-600');
  const pctStr = v => (v == null ? 'NA' : `${v > 0 ? '▲ +' : v < 0 ? '▼ ' : ''}${Number(v).toFixed(2)}%`);
  const pctPlain = v => (v == null ? 'NA' : `${v > 0 ? '+' : ''}${Number(v).toFixed(2)}%`);
  const rate = v => (v == null ? 'NA' : `${(v * 100).toFixed(0)}%`);
  const corrStr = v => (v == null ? 'NA' : Number(v).toFixed(2));
  const WD = ['日', '一', '二', '三', '四', '五', '六'];
  const md = d => { if (!d) return 'NA'; const t = new Date(`${d}T00:00:00`); return `${t.getMonth() + 1}/${t.getDate()}(${WD[t.getDay()]})`; };
  const corrCls = v => (v == null ? 'text-slate-400' : v >= 0.4 ? 'text-red-600 font-bold' : v >= 0.25 ? 'text-orange-600 font-semibold' : v >= 0.1 ? 'text-slate-700' : 'text-slate-400');

  function sparkline(values, { w = 110, h = 28 } = {}) {
    const vs = (values || []).filter(v => v != null);
    if (vs.length < 2) return '';
    const min = Math.min(...vs), max = Math.max(...vs), span = max - min || 1;
    const pts = vs.map((v, i) => `${(i / (vs.length - 1) * w).toFixed(1)},${(h - 2 - (v - min) / span * (h - 4)).toFixed(1)}`).join(' ');
    const color = vs[vs.length - 1] >= vs[0] ? '#ef4444' : '#22c55e';
    return `<svg width="${w}" height="${h}" class="inline-block overflow-visible"><polyline fill="none" stroke="${color}" stroke-width="1.8" points="${pts}"/></svg>`;
  }

  function badge(v) { return `<span class="px-2 py-0.5 rounded text-[11px] font-bold ${esc(v?.cls || 'bg-slate-100 text-slate-500')}">${esc(v?.label || 'NA')}</span>`; }

  function card(title, sub, main, mainCls, rows, sparkHtml) {
    return `
      <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200">
        <div class="flex justify-between items-start">
          <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider">${esc(title)}</span>
          <span class="text-[11px] text-slate-400">${esc(sub)}</span>
        </div>
        <div class="flex items-end justify-between mt-2">
          <div class="text-3xl font-extrabold ${mainCls}">${main}</div>
          <div>${sparkHtml || ''}</div>
        </div>
        <div class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500">${rows.map(([k, v, cls]) => `<span>${esc(k)} <b class="${cls || ''}">${v}</b></span>`).join('')}</div>
      </div>`;
  }

  function renderOverview(d) {
    const c = d.composite, b = d.benchmarks || {};
    const bench = (key, title) => {
      const x = b[key];
      if (!x) return card(title, '資料缺漏', 'NA', 'text-slate-400', [], '');
      return card(title, `${md(x.date)} 收 ${num(x.close, 0)}`, pctStr(x.pct), signCls(x.pct),
        [['5 日', pctPlain(x.pct5), signCls(x.pct5)], ['20 日', pctPlain(x.pct20), signCls(x.pct20)], x.taiex ? ['對台股 r', corrStr(x.taiex.corr120), corrCls(x.taiex.corr120)] : ['', '']],
        sparkline(x.spark));
    };
    $('overview').innerHTML = [
      card('美股科技整體（全族群等權）', `美股 ${md(c.date)}`, pctStr(c.pct), signCls(c.pct),
        [['5 日', pctPlain(c.pct5), signCls(c.pct5)], ['20 日', pctPlain(c.pct20), signCls(c.pct20)], ['60 日', pctPlain(c.pct60), signCls(c.pct60)], ['台股連動', badge(c.taiex?.verdict)]],
        sparkline(c.spark)),
      bench('SOX', '費城半導體 (SOX)'),
      bench('IXIC', 'NASDAQ (IXIC)'),
      bench('TWII', '台股加權指數 (TAIEX)'),
    ].join('');
  }

  function twStockCard(s, rank, w) {
    return `
      <div class="p-3 rounded-lg border border-slate-200 bg-slate-50">
        <div class="flex items-start justify-between gap-2">
          <div>
            <div class="text-[11px] font-semibold text-slate-400">第 ${rank} 名｜${esc(s.market || '')}${s.themes?.length ? '｜' + s.themes.map(esc).join('、') : ''}</div>
            <div class="font-extrabold text-slate-800"><a href="./stock.html?id=${encodeURIComponent(s.code)}" class="hover:text-indigo-600 hover:underline">${esc(s.code)} ${esc(s.name)}</a></div>
          </div>
          <div class="text-right shrink-0">
            <div class="text-lg ${corrCls(s.corr)}">r = ${corrStr(s.corr)}</div>
            <div class="text-[11px] text-slate-400">${w.main} 日配對 ${s.n} 組</div>
          </div>
        </div>
        <div class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-slate-500">
          <span>${w.alt} 日 r <b class="${corrCls(s.corrAlt)}">${corrStr(s.corrAlt)}</b></span>
          <span>β <b class="text-slate-700">${s.beta == null ? 'NA' : s.beta.toFixed(2)}</b></span>
          <span>同向率 <b class="text-slate-700">${rate(s.hit)}</b></span>
          <span>最近 ${esc(md(s.lastDate))} <b class="${signCls(s.lastPct)}">${pctPlain(s.lastPct)}</b></span>
        </div>
      </div>`;
  }

  function linkageLine(l, w) {
    if (!l) return '<span class="text-slate-400">NA</span>';
    return `${w.alt}/${w.main}/${w.long} 日 r = <b class="${corrCls(l.corr60)}">${corrStr(l.corr60)}</b> / <b class="${corrCls(l.corr120)}">${corrStr(l.corr120)}</b> / <b class="${corrCls(l.corr250)}">${corrStr(l.corr250)}</b>，同向率 <b>${rate(l.hit120?.rate)}</b>（${l.hit120?.n ?? 0} 日）`;
  }

  function groupRow(g, w) {
    const maxAbs = Math.max(1, ...g.members.map(m => Math.abs(m.pct ?? 0)));
    const members = g.members.map((m, i) => {
      const bw = m.pct == null ? 0 : Math.abs(m.pct) / maxAbs * 100;
      return `<tr class="hover:bg-slate-50">
        <td class="py-2 px-2 text-slate-400">${i + 1}</td>
        <td class="py-2 px-2 font-semibold whitespace-nowrap text-slate-800">${esc(m.symbol)} <span class="text-xs text-slate-500 font-normal">${esc(m.name)}</span></td>
        <td class="py-2 px-2 text-right">${num(m.close)}</td>
        <td class="py-2 px-2 text-right font-bold ${signCls(m.pct)} whitespace-nowrap">${pctStr(m.pct)}</td>
        <td class="py-2 px-2 w-28 hidden md:table-cell"><div class="h-2 rounded-full bg-slate-100 overflow-hidden"><div class="h-full rounded-full ${(m.pct ?? 0) >= 0 ? 'bg-red-400' : 'bg-green-500'}" style="width:${bw.toFixed(1)}%"></div></div></td>
        <td class="py-2 px-2 text-right ${signCls(m.pct5)} hidden sm:table-cell">${pctPlain(m.pct5)}</td>
        <td class="py-2 px-2 text-right ${signCls(m.pct20)} hidden sm:table-cell">${pctPlain(m.pct20)}</td>
        <td class="py-2 px-2 text-right ${signCls(m.pct60)} hidden sm:table-cell">${pctPlain(m.pct60)}</td>
      </tr>`;
    }).join('');
    const top3 = g.top3?.length ? g.top3.map((s, i) => twStockCard(s, i + 1, w)).join('') : '<div class="text-sm text-slate-400">台股資料不足</div>';
    return `
      <details class="group-row rounded-lg border border-slate-200 open:shadow-sm">
        <summary class="flex items-center gap-3 px-4 py-3 hover:bg-slate-50 rounded-lg">
          <span class="chev text-slate-400 transition-transform">▶</span>
          <span class="w-7 text-xs font-bold text-slate-400">#${g.rank}</span>
          <span class="flex-1 min-w-0">
            <span class="font-bold text-slate-800">${esc(g.title)}</span>
            <span class="ml-2 text-[11px] text-slate-400 hidden md:inline">${esc(g.desc)}</span>
            <span class="block md:inline md:ml-2 text-[11px] text-slate-400">成分 ${g.members.length} 檔｜台股前三：${(g.top3 || []).map(s => `${esc(s.code)} ${esc(s.name)} (${corrStr(s.corr)})`).join('、') || 'NA'}</span>
          </span>
          <span class="hidden lg:inline">${sparkline(g.spark)}</span>
          <span class="hidden sm:flex flex-col items-end text-[11px] text-slate-500 w-24">
            <span>5日 <b class="${signCls(g.pct5)}">${pctPlain(g.pct5)}</b></span>
            <span>20日 <b class="${signCls(g.pct20)}">${pctPlain(g.pct20)}</b></span>
          </span>
          <span class="w-24 text-right font-extrabold ${signCls(g.pct)} whitespace-nowrap">${pctStr(g.pct)}</span>
          <span class="w-20 text-right">${badge(g.taiex?.verdict)}</span>
        </summary>
        <div class="px-4 pb-4 grid grid-cols-1 xl:grid-cols-5 gap-4">
          <div class="xl:col-span-3 overflow-x-auto">
            <div class="text-xs font-semibold text-slate-500 mb-1">成分股（美股 ${esc(md(g.date))} 收盤）</div>
            <table class="w-full text-sm text-left text-slate-600">
              <thead class="text-[11px] text-slate-500 bg-slate-50"><tr>
                <th class="py-2 px-2">#</th><th class="py-2 px-2">代號</th><th class="py-2 px-2 text-right">收盤</th><th class="py-2 px-2 text-right">當日</th><th class="py-2 px-2 hidden md:table-cell"></th>
                <th class="py-2 px-2 text-right hidden sm:table-cell">5 日</th><th class="py-2 px-2 text-right hidden sm:table-cell">20 日</th><th class="py-2 px-2 text-right hidden sm:table-cell">60 日</th>
              </tr></thead>
              <tbody class="divide-y divide-slate-100">${members}</tbody>
            </table>
            <div class="mt-3 text-xs text-slate-500">與台股加權隔日連動：${linkageLine(g.taiex, w)}</div>
            ${g.twThemes?.length ? `<div class="mt-1 text-xs text-slate-500">對應台股族群：${g.twThemes.map(t => `<a href="./themes.html" class="text-indigo-600 hover:underline">${esc(t)}</a>`).join('、')}</div>` : ''}
          </div>
          <div class="xl:col-span-2">
            <div class="text-xs font-semibold text-slate-500 mb-1">相關係數最高的前三台股（美股 t 日 → 台股隔一交易日，近 ${w.main} 日）</div>
            <div class="space-y-2">${top3}</div>
          </div>
        </div>
      </details>`;
  }

  function renderGroups(d) {
    $('groups').innerHTML = d.groups.map(g => groupRow(g, d.windows)).join('');
    $('groupsSub').textContent = `依族群成分股等權平均「美股 ${md(d.usDate)} 漲跌幅」由強到弱排序；點擊族群展開成分股、對應台股族群與相關係數最高的前三台股`;
  }

  function renderTop3(d) {
    const c = d.composite, w = d.windows;
    $('top3').innerHTML = `
      <h2 class="text-lg font-bold text-slate-800">🎯 與美股科技整體相關係數最高的前三台股</h2>
      <p class="text-xs text-slate-400 mt-0.5 mb-4">以全族群等權的美股科技日報酬，對 ${d.config.twCandidates} 檔台股候選計算「美股 t 日 → 台股隔一交易日」近 ${w.main} 日 Pearson 相關係數</p>
      <div class="grid grid-cols-1 md:grid-cols-3 gap-3">${(c.top3 || []).map((s, i) => twStockCard(s, i + 1, w)).join('') || '<div class="text-sm text-slate-400">台股資料不足</div>'}</div>
      <div class="mt-4 overflow-x-auto">
        <div class="text-xs font-semibold text-slate-500 mb-1">各族群相關係數最高的台股一覽</div>
        <table class="w-full text-sm text-left text-slate-600">
          <thead class="text-[11px] text-slate-500 bg-slate-50"><tr><th class="py-2 px-2">美股族群</th><th class="py-2 px-2 text-right">族群當日</th><th class="py-2 px-2">第 1 名</th><th class="py-2 px-2">第 2 名</th><th class="py-2 px-2">第 3 名</th></tr></thead>
          <tbody class="divide-y divide-slate-100">${d.groups.map(g => `<tr>
            <td class="py-2 px-2 font-semibold text-slate-800 whitespace-nowrap">${esc(g.title)}</td>
            <td class="py-2 px-2 text-right font-bold ${signCls(g.pct)} whitespace-nowrap">${pctPlain(g.pct)}</td>
            ${[0, 1, 2].map(i => { const s = g.top3?.[i]; return `<td class="py-2 px-2 whitespace-nowrap">${s ? `<a href="./stock.html?id=${encodeURIComponent(s.code)}" class="text-slate-800 hover:text-indigo-600 hover:underline">${esc(s.code)} ${esc(s.name)}</a> <span class="${corrCls(s.corr)}">${corrStr(s.corr)}</span>` : 'NA'}</td>`; }).join('')}
          </tr>`).join('')}</tbody>
        </table>
      </div>`;
  }

  function curveSvg(curve) {
    if (!curve?.length) return '';
    const W = 720, H = 220, L = 44, R = 12, T = 12, B = 28;
    const vals = curve.flatMap(c => [c.strat, c.bh]);
    const min = Math.min(0, ...vals), max = Math.max(0, ...vals), span = max - min || 1;
    const x = i => L + i / (curve.length - 1) * (W - L - R);
    const y = v => T + (max - v) / span * (H - T - B);
    const line = (key, color) => `<polyline fill="none" stroke="${color}" stroke-width="2" points="${curve.map((c, i) => `${x(i).toFixed(1)},${y(c[key]).toFixed(1)}`).join(' ')}"/>`;
    const ticks = [min, 0, max].filter((v, i, a) => a.indexOf(v) === i);
    return `<svg viewBox="0 0 ${W} ${H}" class="w-full h-56">
      ${ticks.map(v => `<line x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" stroke="#e2e8f0" stroke-dasharray="3 3"/><text x="${L - 4}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end" font-size="10" fill="#94a3b8">${v.toFixed(0)}%</text>`).join('')}
      ${line('bh', '#94a3b8')}${line('strat', '#4f46e5')}
      <text x="${L}" y="${H - 8}" font-size="10" fill="#94a3b8">${esc(curve[0].date)}</text>
      <text x="${W - R}" y="${H - 8}" text-anchor="end" font-size="10" fill="#94a3b8">${esc(curve[curve.length - 1].date)}</text>
    </svg>`;
  }

  function renderBacktest(d) {
    const w = d.windows, s = d.strategy, c = d.composite.taiex;
    const rows = [
      { title: '美股科技整體（全族群等權）', l: c, bold: true },
      ...['SOX', 'IXIC'].filter(k => d.benchmarks[k]?.taiex).map(k => ({ title: `${d.benchmarks[k].name} (${k})`, l: d.benchmarks[k].taiex })),
      ...d.groups.map(g => ({ title: g.title, l: g.taiex })),
    ];
    const condCell = (c, dir) => (c && c.n ? `<span class="text-slate-500">${c.n} 次</span> 隔日均 <b class="${signCls(c.mean)}">${pctPlain(c.mean)}</b>，${dir} <b>${rate(c.sameDirRate)}</b>` : '<span class="text-slate-400">樣本不足</span>');
    const level = c?.verdict?.level ?? 0;
    const conclusion = level >= 3
      ? '回測顯示美股科技族群與台股隔日走勢<b>高度連動</b>：相關係數與同向率都明顯高於隨機，美股科技收盤可作為台股開盤方向的主要參考。'
      : level === 2
        ? '回測顯示美股科技族群與台股隔日走勢<b>中度連動</b>：方向有參考價值，但單日幅度仍受台股自身籌碼與法人動向影響，建議搭配夜盤與三大法人資料判讀。'
        : level === 1
          ? '回測顯示美股科技族群與台股隔日走勢<b>連動偏弱</b>：僅在美股大漲大跌時方向較有參考性，一般日子的連動並不穩定。'
          : '回測顯示近期美股科技族群與台股隔日走勢<b>沒有明顯連動</b>，台股走勢較多由自身因素決定。';
    $('backtest').innerHTML = `
      <h2 class="text-lg font-bold text-slate-800">🧪 美股科技族群 × 台股連動回測</h2>
      <p class="text-xs text-slate-400 mt-0.5 mb-4">美股族群 t 日報酬 vs 台股加權指數「t 之後第一個交易日」報酬。相關係數分 ${w.alt}／${w.main}／${w.long} 日三個窗口；同向率為近 ${w.main} 日；條件統計為近 ${w.long} 日美股族群漲跌超過 ±1% 時的台股隔日表現</p>
      <div class="grid grid-cols-1 xl:grid-cols-5 gap-6">
        <div class="xl:col-span-3 overflow-x-auto">
          <table class="w-full text-sm text-left text-slate-600">
            <thead class="text-[11px] text-slate-500 bg-slate-50"><tr>
              <th class="py-2 px-2">美股族群</th><th class="py-2 px-2 text-right">r ${w.alt}</th><th class="py-2 px-2 text-right">r ${w.main}</th><th class="py-2 px-2 text-right">r ${w.long}</th><th class="py-2 px-2 text-right">同向率</th>
              <th class="py-2 px-2">美股漲 &gt; 1% 後</th><th class="py-2 px-2">美股跌 &lt; -1% 後</th><th class="py-2 px-2">判讀</th>
            </tr></thead>
            <tbody class="divide-y divide-slate-100">${rows.map(r => `<tr class="${r.bold ? 'bg-indigo-50/40' : ''}">
              <td class="py-2 px-2 ${r.bold ? 'font-bold' : 'font-semibold'} text-slate-800 whitespace-nowrap">${esc(r.title)}</td>
              <td class="py-2 px-2 text-right ${corrCls(r.l?.corr60)}">${corrStr(r.l?.corr60)}</td>
              <td class="py-2 px-2 text-right ${corrCls(r.l?.corr120)}">${corrStr(r.l?.corr120)}</td>
              <td class="py-2 px-2 text-right ${corrCls(r.l?.corr250)}">${corrStr(r.l?.corr250)}</td>
              <td class="py-2 px-2 text-right">${rate(r.l?.hit120?.rate)}</td>
              <td class="py-2 px-2 text-xs whitespace-nowrap">${condCell(r.l?.condUp, '續漲率')}</td>
              <td class="py-2 px-2 text-xs whitespace-nowrap">${condCell(r.l?.condDown, '續跌率')}</td>
              <td class="py-2 px-2">${badge(r.l?.verdict)}</td>
            </tr>`).join('')}</tbody>
          </table>
        </div>
        <div class="xl:col-span-2">
          <div class="text-xs font-semibold text-slate-500 mb-1">跟單回測：美股科技整體漲 → 隔日做多台股加權、跌 → 做空（近 ${s?.n ?? 0} 個交易日，收盤對收盤、含開盤跳空、不含成本，僅驗證方向）</div>
          ${s ? `
          <div class="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500 mb-2">
            <span>跟單 <b class="${signCls(s.stratPct)}">${pctPlain(s.stratPct)}</b></span>
            <span>買進持有 <b class="${signCls(s.bhPct)}">${pctPlain(s.bhPct)}</b></span>
            <span>方向命中 <b class="text-slate-700">${rate(s.hitRate)}</b></span>
            <span>最大回撤 <b class="text-green-600">${pctPlain(s.maxDDPct)}</b></span>
            <span class="text-slate-400">${esc(s.from)} ~ ${esc(s.to)}</span>
          </div>
          ${curveSvg(s.curve)}
          <div class="flex gap-4 text-[11px] text-slate-500 mt-1"><span><span class="inline-block w-3 h-0.5 bg-indigo-600 align-middle"></span> 跟單美股科技</span><span><span class="inline-block w-3 h-0.5 bg-slate-400 align-middle"></span> 買進持有台股加權</span></div>` : '<div class="text-sm text-slate-400">配對樣本不足</div>'}
          <div class="mt-3 p-3 rounded-lg bg-slate-50 text-sm text-slate-700">${conclusion}</div>
        </div>
      </div>`;
  }

  function renderMethod(d) {
    const w = d.windows;
    $('method').innerHTML = `
      <h2 class="text-base font-bold text-slate-800">📐 方法說明</h2>
      <ul class="list-disc pl-5 space-y-1 text-xs">
        <li><b>美股科技族群</b>：依 <code>data/us_tech_config.json</code> 定義 ${d.config.groups} 個族群、${d.config.usSymbols} 檔成分股；族群報酬為成分股日報酬等權平均（某日需過半成分股有資料），「科技整體」為全族群等權。${d.config.usFailed?.length ? `本次抓取失敗：${d.config.usFailed.map(esc).join('、')}。` : ''}</li>
        <li><b>台股候選</b>：設定檔名單 ＋ FinLab 科技相關族群中成交量達門檻的成分股，共 ${d.config.twCandidates} 檔；行情為 Yahoo Finance 日線收盤。</li>
        <li><b>時序對齊</b>：美股 t 日收盤（台灣時間 t+1 清晨結束）對應「t 之後第一個台股交易日」；台股連假時只配對最近一個美股交易日，避免重複計入。</li>
        <li><b>相關係數</b>：Pearson r，分 ${w.alt}／${w.main}／${w.long} 個配對日三個窗口；「前三台股」依 ${w.main} 日 r 排序，並列出 β（台股對美股族群的斜率）與同向率。配對不足 30 組不計算。</li>
        <li><b>連動判讀</b>：r ≥ 0.4 且同向率 ≥ 60% 為高度連動；r ≥ 0.25 且同向率 ≥ 55% 為中度；r ≥ 0.1 或同向率 ≥ 52% 為弱連動；其餘為無明顯連動。</li>
        <li><b>跟單回測</b>：台股報酬採「收盤對收盤」，包含受美股影響最大的開盤跳空；實際只能於夜盤或開盤後進場，無法完整吃到跳空，因此數字只用來驗證方向訊號是否有效，不代表可實現績效，不含手續費、稅與滑價，亦非交易建議。</li>
        <li><b>歷史累積</b>：每次建置以近 ${w.history} 個美股交易日回填並合併既有紀錄，故歷史表會隨時間持續增長；台股隔日反應待台股收盤後下一次建置補上。</li>
      </ul>`;
  }

  let history = [];
  let shown = 0;
  function renderHistory(more = 30) {
    const rows = history.slice(shown, shown + more).map(h => `<tr class="hover:bg-slate-50">
      <td class="py-2 px-3 font-semibold text-slate-700 whitespace-nowrap">${esc(h.date)} <span class="text-[11px] text-slate-400 font-normal">${esc(md(h.date))}</span></td>
      <td class="py-2 px-3 font-bold ${signCls(h.composite)} whitespace-nowrap">${pctPlain(h.composite)}</td>
      <td class="py-2 px-3 ${signCls(h.sox)} whitespace-nowrap">${pctPlain(h.sox)}</td>
      <td class="py-2 px-3 text-xs">${(h.top3Up || []).map(g => `<span class="inline-block mr-1 mb-0.5 px-1.5 py-0.5 rounded bg-red-50 text-red-700 border border-red-100 whitespace-nowrap">${esc(g.title)} ${pctPlain(g.pct)}</span>`).join('')}</td>
      <td class="py-2 px-3 text-xs">${(h.top3Down || []).map(g => `<span class="inline-block mr-1 mb-0.5 px-1.5 py-0.5 rounded bg-green-50 text-green-700 border border-green-100 whitespace-nowrap">${esc(g.title)} ${pctPlain(g.pct)}</span>`).join('')}</td>
      <td class="py-2 px-3 whitespace-nowrap">${h.taiexNext?.skipped ? '<span class="text-xs text-slate-400" title="台股連假，該美股日不與台股配對">台股休市，不配對</span>' : h.taiexNext ? `<span class="text-[11px] text-slate-400">${esc(md(h.taiexNext.date))}</span> <b class="${signCls(h.taiexNext.pct)}">${pctPlain(h.taiexNext.pct)}</b>` : '<span class="text-xs text-slate-400">待台股收盤</span>'}</td>
    </tr>`).join('');
    $('history').insertAdjacentHTML('beforeend', rows);
    shown += more;
    if (shown >= history.length) $('moreHistory').classList.add('hidden');
  }

  function renderMeta(d) {
    const gen = new Date(d.generatedAt);
    $('meta').innerHTML = `
      <div>美股最新交易日：<span class="font-semibold text-slate-700">${esc(d.usDate)}</span>｜台股最新收盤：<span class="font-semibold text-slate-700">${esc(d.twDate)}</span></div>
      <div>資料產生時間：<span class="font-semibold text-slate-700">${gen.toLocaleString('zh-TW', { timeZone: 'Asia/Taipei', hour12: false })}</span> <span class="text-slate-400">(UTC+8)</span></div>`;
  }

  async function load() {
    try {
      const [d, h] = await Promise.all([
        fetch('./data/us_tech.json', { cache: 'no-store' }).then(r => { if (!r.ok) throw new Error(`us_tech.json ${r.status}`); return r.json(); }),
        fetch('./data/us_tech_history.json', { cache: 'no-store' }).then(r => (r.ok ? r.json() : [])).catch(() => []),
      ]);
      history = Array.isArray(h) ? h : [];
      renderMeta(d); renderOverview(d); renderGroups(d); renderTop3(d); renderBacktest(d); renderMethod(d); renderHistory(30);
      $('expandAll').onclick = () => document.querySelectorAll('details.group-row').forEach(x => { x.open = true; });
      $('collapseAll').onclick = () => document.querySelectorAll('details.group-row').forEach(x => { x.open = false; });
      $('moreHistory').onclick = () => renderHistory(30);
    } catch (e) {
      const st = $('status');
      st.className = 'bg-white rounded-xl shadow-sm p-6 border border-red-200 text-sm text-red-600';
      st.textContent = `資料載入失敗：${e.message}。請確認 GitHub Actions 已執行 scripts/build_us_tech.mjs 產生 data/us_tech.json。`;
    }
  }
  load();
})();
