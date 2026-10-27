/* 個股短沖（ORB）頁：讀取 data/orb/index.json 與 data/orb/<代號>.json（scripts/build_orb.mjs 產生） */
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = (v, d = 2) => (v == null || !isFinite(v) ? 'NA' : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));
  const pct = (v, d = 1, sign = false) => (v == null || !isFinite(v) ? 'NA' : `${sign && v > 0 ? '+' : ''}${(v * 100).toFixed(d)}%`);
  const signCls = v => (v == null || v === 0 ? 'text-slate-500' : v > 0 ? 'text-red-500' : 'text-green-600');
  const ratio = v => (v == null ? '—' : v.toFixed(2));
  const C = { up: '#ef4444', down: '#16a34a', long: '#dc2626', short: '#15803d', mid: '#94a3b8', box: '#6366f1', grid: '#e2e8f0', axis: '#94a3b8' };
  const VERDICT_STYLE = {
    GOOD: 'bg-red-600 text-white', MEH: 'bg-amber-50 text-amber-700 border-2 border-amber-300',
    BAD: 'bg-green-50 text-green-700 border-2 border-green-300', FEW: 'bg-slate-100 text-slate-600 border-2 border-slate-300',
    ILLIQUID: 'bg-slate-100 text-slate-600 border-2 border-slate-300',
  };
  const SIDE = { long: '做多', short: '做空' };
  let INDEX = null;

  function setStatus(kind, html) {
    const el = $('status');
    if (!kind) { el.className = 'hidden'; el.innerHTML = ''; return; }
    el.className = `rounded-xl border p-4 text-sm ${kind === 'error' ? 'bg-red-50 border-red-200 text-red-700' : 'bg-indigo-50 border-indigo-200 text-indigo-700'}`;
    el.innerHTML = html;
  }

  const tile = (label, v, cls = 'text-slate-800', sub = '') => `<div class="p-4 rounded-xl bg-slate-50 border border-slate-100"><div class="text-[11px] font-semibold text-slate-400">${label}</div><div class="text-2xl font-extrabold mt-1 ${cls}">${v}</div>${sub ? `<div class="text-[11px] text-slate-400 mt-0.5">${sub}</div>` : ''}</div>`;
  const statCells = s => (s.n ? `<td class="py-2 px-3 text-right">${s.n}</td><td class="py-2 px-3 text-right">${pct(s.winRate, 0)}</td><td class="py-2 px-3 text-right">${ratio(s.payoff)}</td><td class="py-2 px-3 text-right ${signCls(s.avgRet)}">${pct(s.avgRet, 2, true)}</td><td class="py-2 px-3 text-right">${ratio(s.profitFactor)}</td>` : '<td class="py-2 px-3 text-right">0</td><td colspan="4" class="py-2 px-3 text-center text-slate-300">無交易</td>');
  const statHead = '<th class="py-2 px-3 text-right">筆數</th><th class="py-2 px-3 text-right">勝率</th><th class="py-2 px-3 text-right">盈虧比</th><th class="py-2 px-3 text-right">平均每筆</th><th class="py-2 px-3 text-right rounded-r-lg">PF</th>';

  // ---------- 最新一日 5 分 K 圖：開盤區間、觸發價、進出場 ----------
  function chart(latest) {
    const bars = latest.bars.map(([time, o, h, l, c, v]) => ({ time, o, h, l, c, v }));
    if (!bars.length) return '<div class="text-sm text-slate-400">無 K 線資料</div>';
    const or = latest.or;
    const W = 860, H = 340, L = 56, R = 70, T = 16, B = 28, pw = W - L - R, ph = H - T - B;
    const slots = 54; // 09:00～13:25
    const toMin = t => +t.slice(0, 2) * 60 + +t.slice(3);
    const xi = t => (toMin(t) - 540) / 5;
    const x = t => L + (xi(t) + 0.5) * (pw / slots);
    const vals = bars.flatMap(b => [b.h, b.l]);
    if (or) vals.push(or.longTrig, or.shortTrig);
    for (const t of latest.trades) { vals.push(t.entryPx); if (t.exitPx != null) vals.push(t.exitPx); }
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const padv = (hi - lo) * 0.06 || 1; lo -= padv; hi += padv;
    const y = v => T + ph - ((v - lo) / (hi - lo)) * ph;
    const bw = Math.max(3, (pw / slots) * 0.6);
    let g = '';
    for (let k = 0; k <= 4; k++) { const v = lo + ((hi - lo) * k) / 4; g += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="${C.grid}"/><text x="${L - 6}" y="${y(v) + 3.5}" font-size="10" fill="${C.axis}" text-anchor="end">${num(v, v >= 100 ? 0 : 2)}</text>`; }
    for (const t of ['09:00', '09:30', '10:30', '11:30', '12:30', '13:20']) g += `<text x="${x(t)}" y="${H - 8}" font-size="10" fill="${C.axis}" text-anchor="middle">${t}</text>`;
    if (or) {
      g += `<rect x="${L}" y="${y(or.high)}" width="${6 * (pw / slots)}" height="${Math.max(1, y(or.low) - y(or.high))}" fill="${C.box}" fill-opacity="0.08" stroke="${C.box}" stroke-opacity="0.4" stroke-dasharray="3 3"/>`;
      const hl = (v, color, label, dash) => `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" stroke="${color}" stroke-width="1.5" ${dash ? 'stroke-dasharray="5 4"' : ''}/><text x="${W - R + 4}" y="${y(v) + 3.5}" font-size="10" fill="${color}">${label} ${num(v, v >= 100 ? 1 : 2)}</text>`;
      g += hl(or.longTrig, C.long, '多', false) + hl(or.shortTrig, C.short, '空', false) + hl(or.mid, C.mid, '中軸', true);
    }
    for (const b of bars) {
      const col = b.c >= b.o ? C.up : C.down;
      g += `<line x1="${x(b.time)}" x2="${x(b.time)}" y1="${y(b.h)}" y2="${y(b.l)}" stroke="${col}"/><rect x="${x(b.time) - bw / 2}" y="${Math.min(y(b.o), y(b.c))}" width="${bw}" height="${Math.max(1, Math.abs(y(b.o) - y(b.c)))}" fill="${col}" rx="1"/>`;
    }
    for (const t of latest.trades) {
      const up = t.side === 'long';
      // 進場標記畫在 K 棒外側：多單在低點下方 ▲、空單在高點上方 ▼
      const eb = bars.find(b => b.time === t.entryTime);
      const ay = up ? y(eb ? eb.l : t.entryPx) + 5 : y(eb ? eb.h : t.entryPx) - 5;
      g += `<path d="M${x(t.entryTime)},${ay} l-6,${up ? 10 : -10} h12 z" fill="${up ? C.long : C.short}" stroke="white" stroke-width="1.5"/>`;
      if (t.exitTime) g += `<circle cx="${x(t.exitTime)}" cy="${y(t.exitPx)}" r="5" fill="white" stroke="#0f172a" stroke-width="2"/>`;
    }
    const hits = bars.map((b, k) => `<rect class="hit" data-k="${k}" x="${x(b.time) - pw / slots / 2}" y="${T}" width="${pw / slots}" height="${ph}" fill="transparent"/>`).join('');
    return `<div class="overflow-x-auto"><svg id="orbSvg" viewBox="0 0 ${W} ${H}" class="w-full h-auto select-none min-w-[640px]" role="img" aria-label="最新交易日 5 分 K 與開盤區間">${g}${hits}</svg></div>
      <div class="flex flex-wrap gap-x-4 gap-y-1 mt-2 text-[11px] text-slate-500">
        <span><span class="inline-block w-3 h-3 align-middle border border-dashed border-indigo-400 bg-indigo-50 mr-1"></span>開盤區間 09:00～09:30</span>
        <span><span class="inline-block w-4 border-t-2 align-middle mr-1" style="border-color:${C.long}"></span>多方觸發</span>
        <span><span class="inline-block w-4 border-t-2 align-middle mr-1" style="border-color:${C.short}"></span>空方觸發</span>
        <span><span class="inline-block w-4 border-t-2 border-dashed align-middle mr-1" style="border-color:${C.mid}"></span>中軸（停損參考）</span>
        <span>▲▼ 進場　○ 出場</span>
      </div>`;
  }

  function bindTooltip(latest) {
    const svg = $('orbSvg'), tip = $('tip');
    if (!svg) return;
    const bars = latest.bars;
    svg.addEventListener('mousemove', e => {
      const r = e.target.closest('.hit');
      if (!r) { tip.classList.add('hidden'); return; }
      const [time, o, h, l, c, v] = bars[+r.dataset.k];
      const evts = latest.trades.filter(t => t.entryTime === time || t.exitTime === time).map(t => (t.entryTime === time ? `▶ ${SIDE[t.side]}進場 ${num(t.entryPx)}` : `■ 出場 ${num(t.exitPx)}（${esc(t.reason)}）`));
      tip.innerHTML = `<div class="font-bold">${time}</div>開 ${num(o)}　高 ${num(h)}<br>低 ${num(l)}　收 ${num(c)}<br>量 ${Math.round(v / 1000).toLocaleString()} 張${evts.length ? `<div class="mt-1 text-amber-300">${evts.join('<br>')}</div>` : ''}`;
      tip.classList.remove('hidden');
      tip.style.left = `${Math.min(e.pageX + 14, window.scrollX + document.documentElement.clientWidth - tip.offsetWidth - 8)}px`;
      tip.style.top = `${e.pageY + 14}px`;
    });
    svg.addEventListener('mouseleave', () => tip.classList.add('hidden'));
  }

  function latestStatus(d) {
    const lt = d.latest;
    if (!lt.or) return lt.skip || '無開盤區間資料';
    const live = !lt.complete;
    const parts = [];
    if (lt.skip) parts.push(`⏸ ${lt.skip}，當日不進場`);
    for (const t of lt.trades) {
      if (t.open) parts.push(`${SIDE[t.side]} ${t.entryTime} 進場 ${num(t.entryPx)}，${live ? '資料截止時仍持有，' : ''}防守價 ${num(t.stop)}`);
      else parts.push(`${SIDE[t.side]} ${t.entryTime} 進場 ${num(t.entryPx)} → ${t.exitTime} 出場 ${num(t.exitPx)}（${t.reason}）<b class="${signCls(t.ret)}">${pct(t.ret, 2, true)}</b>`);
    }
    if (!lt.skip && !lt.trades.length) parts.push(live ? '尚未出現帶量突破' : '當日未出現帶量突破，不交易');
    return parts.join('<br>');
  }

  async function run(code) {
    code = String(code).trim().toUpperCase();
    if (!code) return;
    history.replaceState(null, '', `?id=${encodeURIComponent(code)}`);
    const entry = INDEX.stocks.find(s => s.code === code);
    if (!entry) { $('result').classList.add('hidden'); setStatus('error', `❌ ${esc(code)} 不在短沖資料範圍內（族群漲跌排行成分股與跨個股驗證股票）。`); return; }
    let d;
    try { d = await (await fetch(`./data/orb/${encodeURIComponent(code)}.json`, { cache: 'no-cache' })).json(); } catch (e) { setStatus('error', '❌ 個股資料載入失敗'); return; }
    setStatus(null);
    const s = d.variants.full.stats, lt = d.latest, or = lt.or;
    const live = !lt.complete;
    const lastTime = lt.bars.length ? lt.bars[lt.bars.length - 1][0] : '';
    const verdictHtml = `
      <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
        <div class="flex flex-col lg:flex-row lg:items-center gap-5">
          <div class="lg:w-72">
            <div class="text-sm text-slate-500">${esc(entry.market)}｜${entry.themes.map(esc).join('、') || '—'}</div>
            <div class="text-2xl font-extrabold text-slate-800">${esc(d.code)} ${esc(d.name)}</div>
            <div class="mt-3 inline-block px-4 py-2 rounded-xl text-xl font-extrabold ${VERDICT_STYLE[d.verdict.key]}">${esc(d.verdict.label)}</div>
            <div class="text-xs text-slate-500 mt-2">${esc(d.verdict.note)}｜日均量約 ${entry.avgVolLots == null ? 'NA' : entry.avgVolLots.toLocaleString()} 張</div>
          </div>
          <div class="flex-1 grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
            ${tile(`近 ${entry.sessions} 日交易`, s.n || 0, 'text-slate-800', `多 ${d.variants.full.long.n || 0}／空 ${d.variants.full.short.n || 0}`)}
            ${tile('勝率', s.n ? pct(s.winRate, 0) : '—', 'text-slate-800', '規格書宣稱 45%～52%')}
            ${tile('盈虧比', s.n ? ratio(s.payoff) : '—', 'text-slate-800', '平均獲利 ÷ 平均虧損')}
            ${tile('平均每筆', s.n ? pct(s.avgRet, 2, true) : '—', signCls(s.avgRet), '已扣當沖成本')}
            ${tile('累計報酬', s.n ? pct(s.total, 1, true) : '—', signCls(s.total), `最大回撤 ${s.n ? pct(s.mdd, 1) : '—'}`)}
            ${tile('PF', s.n ? ratio(s.profitFactor) : '—', 'text-slate-800', '總獲利 ÷ 總虧損')}
          </div>
        </div>
      </div>`;
    const latestHtml = `
      <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
        <div class="flex flex-col md:flex-row md:items-start justify-between gap-3 mb-3">
          <div>
            <h2 class="text-lg font-bold text-slate-800">🕘 ${esc(lt.date)} 開盤區間與突破${live ? `<span class="ml-2 text-xs font-semibold px-2 py-0.5 rounded bg-amber-100 text-amber-700 align-middle">盤中・資料至 ${esc(lastTime)}</span>` : ''}</h2>
            <p class="text-xs text-slate-400 mt-0.5">網站更新有延遲，盤中請以看盤軟體的即時價格對照下方觸發價</p>
          </div>
          ${or ? `<div class="grid grid-cols-3 gap-2 text-center text-xs">
            <div class="px-3 py-2 rounded-lg bg-red-50"><div class="text-red-400">多方觸發</div><div class="text-base font-extrabold text-red-600">${num(or.longTrig)}</div></div>
            <div class="px-3 py-2 rounded-lg bg-slate-50"><div class="text-slate-400">區間 ${num(or.low)}～${num(or.high)}</div><div class="text-base font-extrabold text-slate-700">Range ${num(or.range)}</div></div>
            <div class="px-3 py-2 rounded-lg bg-green-50"><div class="text-green-500">空方觸發</div><div class="text-base font-extrabold text-green-700">${num(or.shortTrig)}</div></div>
          </div>` : ''}
        </div>
        <div class="text-sm text-slate-700 mb-3 leading-relaxed">${latestStatus(d)}</div>
        ${chart(lt)}
      </div>`;
    const varRows = Object.values(d.variants).map((v, k) => `<tr class="hover:bg-slate-50 ${k === 0 ? 'font-semibold' : ''}"><td class="py-2 px-3 whitespace-nowrap">${esc(v.label)}</td>${statCells(v.stats)}</tr>`).join('');
    const tradeRows = d.recentTrades.map(t => `<tr class="hover:bg-slate-50"><td class="py-1.5 px-3 whitespace-nowrap">${esc(t.date)}</td><td class="py-1.5 px-3 ${t.side === 'long' ? 'text-red-600' : 'text-green-700'} font-semibold">${SIDE[t.side]}</td><td class="py-1.5 px-3 text-right whitespace-nowrap">${esc(t.entryTime)} ${num(t.entryPx)}</td><td class="py-1.5 px-3 text-right whitespace-nowrap">${esc(t.exitTime)} ${num(t.exitPx)}</td><td class="py-1.5 px-3 text-right font-semibold ${signCls(t.ret)}">${pct(t.ret, 2, true)}</td><td class="py-1.5 px-3 text-slate-500 hidden sm:table-cell">${esc(t.reason)}</td></tr>`).join('');
    const backtestHtml = `
      <div class="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
          <h2 class="text-lg font-bold text-slate-800">🔬 本股近 ${entry.sessions} 日回測：濾網對照</h2>
          <p class="text-xs text-slate-400 mt-0.5 mb-3">粗體為本頁判斷依據的完整規則</p>
          <div class="overflow-x-auto"><table class="w-full text-xs border-collapse"><thead class="text-slate-700 bg-slate-100"><tr><th class="py-2 px-3 text-left rounded-l-lg">版本</th>${statHead}</tr></thead><tbody class="divide-y divide-slate-100">${varRows}
            <tr class="text-slate-500"><td class="py-2 px-3">　└ 只看多單</td>${statCells(d.variants.full.long)}</tr><tr class="text-slate-500"><td class="py-2 px-3">　└ 只看空單</td>${statCells(d.variants.full.short)}</tr></tbody></table></div>
        </div>
        <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
          <h2 class="text-lg font-bold text-slate-800">🧾 最近交易</h2>
          <p class="text-xs text-slate-400 mt-0.5 mb-3">完整規則，報酬已扣當沖成本</p>
          ${tradeRows ? `<div class="overflow-x-auto"><table class="w-full text-xs border-collapse"><thead class="text-slate-700 bg-slate-100"><tr><th class="py-2 px-3 text-left rounded-l-lg">日期</th><th class="py-2 px-3 text-left">方向</th><th class="py-2 px-3 text-right">進場</th><th class="py-2 px-3 text-right">出場</th><th class="py-2 px-3 text-right">報酬</th><th class="py-2 px-3 text-left rounded-r-lg hidden sm:table-cell">原因</th></tr></thead><tbody class="divide-y divide-slate-100">${tradeRows}</tbody></table></div>` : '<div class="text-sm text-slate-400 py-6 text-center">近期沒有交易</div>'}
        </div>
      </div>`;
    $('result').innerHTML = verdictHtml + latestHtml + backtestHtml;
    $('result').classList.remove('hidden');
    bindTooltip(lt);
  }

  function renderToday() {
    const today = INDEX.stocks.filter(s => s.latest.date === INDEX.latestDate && s.latest.trades > 0);
    const ranked = INDEX.stocks.filter(s => s.stats.n >= INDEX.minTrades && s.verdict !== 'ILLIQUID').sort((a, b) => b.stats.avgRet - a.stats.avgRet);
    const link = s => `<button data-code="${esc(s.code)}" class="pick text-indigo-600 hover:underline font-semibold">${esc(s.code)} ${esc(s.name)}</button>`;
    const row = s => `<tr class="hover:bg-slate-50"><td class="py-2 px-3 whitespace-nowrap">${link(s)}</td><td class="py-2 px-3 text-xs text-slate-500 hidden md:table-cell">${s.themes.map(esc).join('、')}</td>${statCells(s.stats)}</tr>`;
    $('today').innerHTML = `
      <div class="grid grid-cols-1 xl:grid-cols-3 gap-6">
        <div>
          <h2 class="text-lg font-bold text-slate-800">⚡ ${esc(INDEX.latestDate)} 觸發突破</h2>
          <p class="text-xs text-slate-400 mt-0.5 mb-3">${INDEX.latestComplete ? '已收盤' : '盤中資料，可能還會增加'}；依完整規則</p>
          <div class="flex flex-wrap gap-2 text-sm">${today.length ? today.map(s => `<span class="px-2 py-1 rounded-lg ${s.latest.side === 'long' ? 'bg-red-50' : 'bg-green-50'}">${link(s)} <span class="text-xs ${s.latest.side === 'long' ? 'text-red-600' : 'text-green-700'}">${SIDE[s.latest.side]}</span></span>`).join('') : '<span class="text-slate-400">沒有個股觸發</span>'}</div>
        </div>
        <div class="xl:col-span-2">
          <h2 class="text-lg font-bold text-slate-800">🏅 ORB 歷史表現最好的個股</h2>
          <p class="text-xs text-slate-400 mt-0.5 mb-3">近 60 日至少 ${INDEX.minTrades} 筆、日均量 ≥ 1,000 張，依平均每筆報酬排序（前 10 名）</p>
          <div class="overflow-x-auto"><table class="w-full text-xs border-collapse"><thead class="text-slate-700 bg-slate-100"><tr><th class="py-2 px-3 text-left rounded-l-lg">個股</th><th class="py-2 px-3 text-left hidden md:table-cell">族群</th>${statHead}</tr></thead><tbody class="divide-y divide-slate-100">${ranked.slice(0, 10).map(row).join('')}</tbody></table></div>
        </div>
      </div>`;
    document.querySelectorAll('#today .pick').forEach(b => b.addEventListener('click', () => { $('stockInput').value = b.dataset.code; run(b.dataset.code); window.scrollTo({ top: 0, behavior: 'smooth' }); }));
  }

  function renderValidation() {
    const sm = INDEX.summary, full = sm.variants.full.all;
    const rows = Object.values(sm.variants).map((v, k) => `<tr class="hover:bg-slate-50 ${k === 0 ? 'font-semibold' : ''}"><td class="py-2 px-3 whitespace-nowrap">${esc(v.label)}</td>${statCells(v.all)}<td class="py-2 px-3 text-right ${signCls(v.all.grossAvg)}">${pct(v.all.grossAvg, 3, true)}</td></tr>`).join('');
    const sideRows = ['long', 'short'].map(k => `<tr class="text-slate-500"><td class="py-2 px-3">　└ 只看${SIDE[k]}</td>${statCells(sm.variants.full[k])}<td class="py-2 px-3 text-right ${signCls(sm.variants.full[k].grossAvg)}">${pct(sm.variants.full[k].grossAvg, 3, true)}</td></tr>`).join('');
    const halfRows = [['前半段', sm.halves.first], ['後半段', sm.halves.second]].map(([l, s]) => `<tr class="text-slate-500"><td class="py-2 px-3">　└ ${l}</td>${statCells(s)}<td class="py-2 px-3 text-right ${signCls(s.grossAvg)}">${pct(s.grossAvg, 3, true)}</td></tr>`).join('');
    const vc = sm.verdicts, cost = 2 * INDEX.cost.fee + INDEX.cost.tax;
    const conclusion = full.avgRet < 0
      ? `完整規則在 ${INDEX.stocks.length} 檔共 ${full.n.toLocaleString()} 筆交易中，勝率 ${pct(full.winRate, 0)}、盈虧比 ${ratio(full.payoff)}，扣除當沖成本後平均每筆 ${pct(full.avgRet, 2, true)}，遠低於規格書宣稱的勝率 45%～52%、盈虧比 2.5～3.5。未扣成本前平均每筆 ${pct(full.grossAvg, 3, true)}，${Math.abs(full.grossAvg) < cost / 3 ? '幾乎沒有優勢' : full.grossAvg < 0 ? '本身就是負的' : '有小幅優勢'}，來回約 ${pct(cost, 3)} 的當沖成本再把結果壓成明顯虧損。加上兩個避坑濾網後，交易次數減少，但期望值沒有改善。`
      : `完整規則在 ${INDEX.stocks.length} 檔共 ${full.n.toLocaleString()} 筆交易中，勝率 ${pct(full.winRate, 0)}、盈虧比 ${ratio(full.payoff)}，扣除當沖成本後平均每筆 ${pct(full.avgRet, 2, true)}。`;
    $('validation').innerHTML = `
      <h2 class="text-lg font-bold text-slate-800">🧪 跨個股驗證（${INDEX.stocks.length} 檔 × 近 60 個交易日：${esc(INDEX.marketFrom)}～${esc(INDEX.marketTo)}）</h2>
      <p class="text-xs text-slate-400 mt-0.5 mb-4">參數全部取自規格書（K = 0.2、量 1.5 倍、停損 1%、移動停利 1.5 × Range），未依回測結果調整。前後半段以交易日數切分，用來檢查穩定度。</p>
      <div class="overflow-x-auto"><table class="w-full text-xs border-collapse"><thead class="text-slate-700 bg-slate-100"><tr><th class="py-2 px-3 text-left rounded-l-lg">版本</th><th class="py-2 px-3 text-right">筆數</th><th class="py-2 px-3 text-right">勝率</th><th class="py-2 px-3 text-right">盈虧比</th><th class="py-2 px-3 text-right">平均每筆</th><th class="py-2 px-3 text-right">PF</th><th class="py-2 px-3 text-right rounded-r-lg">未扣成本</th></tr></thead>
        <tbody class="divide-y divide-slate-100">${rows}${sideRows}${halfRows}</tbody></table></div>
      <div class="mt-3 text-xs text-slate-500">個股判斷分布：適合 ${vc.GOOD || 0}・普通 ${vc.MEH || 0}・不適合 ${vc.BAD || 0}・樣本不足 ${vc.FEW || 0}・流動性不足 ${vc.ILLIQUID || 0}</div>
      <div class="mt-4 p-4 rounded-lg bg-amber-50 border border-amber-200 text-xs text-amber-800 leading-relaxed"><b>驗證結論：</b>${conclusion}少數個股標示為「適合」，但每檔只有十幾筆交易，樣本很小，很可能只是運氣，使用前請以更長的歷史資料再確認。</div>`;
  }

  async function init() {
    try { INDEX = await (await fetch('./data/orb/index.json', { cache: 'no-cache' })).json(); } catch (e) {
      setStatus('error', '❌ 尚無短沖資料（data/orb/index.json），請等待下一次自動更新。');
      $('today').classList.add('hidden'); $('validation').classList.add('hidden');
      return;
    }
    $('meta').innerHTML = `<div>最新交易日：<span class="font-semibold text-slate-700">${esc(INDEX.latestDate)}</span>${INDEX.latestComplete ? '' : '（盤中）'}</div>
      <div>回測期間：${esc(INDEX.marketFrom)}～${esc(INDEX.marketTo)}</div>
      <div class="text-slate-400">產生時間 ${esc(new Date(INDEX.generatedAt).toLocaleString('zh-TW', { hour12: false }))}</div>`;
    $('stockList').innerHTML = INDEX.stocks.map(s => `<option value="${esc(s.code)}">${esc(s.name)}</option>`).join('');
    renderToday();
    renderValidation();
    $('queryForm').addEventListener('submit', e => { e.preventDefault(); run($('stockInput').value); });
    const q = new URLSearchParams(location.search).get('id');
    if (q) { $('stockInput').value = q; run(q); }
  }

  init();
})();
