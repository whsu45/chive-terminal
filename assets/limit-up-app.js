/* 最新漲停個股頁：讀取 data/limit_up.json（scripts/build_limit_up.py 產生） */
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = (v, d = 0) => (v == null || !isFinite(v) ? '—' : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));
  const dec = v => (v == null ? 2 : v >= 1000 ? 0 : v >= 100 ? 1 : 2);
  const px = v => num(v, dec(v));
  const WD = ['日', '一', '二', '三', '四', '五', '六'];
  const md = d => { if (!d) return '—'; const t = new Date(`${d}T00:00:00`); return `${t.getMonth() + 1}/${t.getDate()}(${WD[t.getDay()]})`; };
  const hhmm = m => (m == null ? '—' : `${String(9 + Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`);
  const KEY = 'limit-up-filters';
  const store = {
    get() { try { return JSON.parse(localStorage.getItem(KEY) || 'null'); } catch (e) { return null; } },
    set(v) { try { localStorage.setItem(KEY, JSON.stringify(v)); } catch (e) { /* 無痕模式等 */ } },
  };
  let DATA = null;

  /** 不適合放空的原因（被排除的理由） */
  function reasons(s, f) {
    const r = [];
    if (f.locked && s.locked) r.push('一字鎖');
    if (f.strong && s.strong) r.push('強勢股');
    return r;
  }

  function tags(s) {
    const t = [];
    const tag = (txt, cls, tip) => `<span class="px-1.5 py-0.5 rounded text-[11px] font-semibold ${cls}" title="${esc(tip)}">${esc(txt)}</span>`;
    if (s.locked) t.push(tag('一字鎖', 'bg-red-600 text-white', '開盤就漲停，盤中最低價也沒離開漲停價'));
    if (s.pos === 'upper') t.push(tag('月線上＆布林上通道', 'bg-red-100 text-red-700', `收盤介於布林中軌（月線 ${px(s.ma20)}）與上軌（${px(s.bb_up)}）之間`));
    else if (s.pos === 'above') t.push(tag('突破布林上軌', 'bg-amber-100 text-amber-800', `收盤 ${px(s.c)} 在布林上軌 ${px(s.bb_up)} 之上`));
    else if (s.pos === 'below') t.push(tag('月線之下', 'bg-emerald-100 text-emerald-800', `收盤 ${px(s.c)} 仍在月線 ${px(s.ma20)} 之下`));
    else t.push(tag('上市未滿一個月', 'bg-slate-100 text-slate-500', '日線不到 21 根，算不出月線與布林通道'));
    const it = s.intraday;
    if (it && !s.locked && it.first_limit != null) {
      t.push(it.opened_min > 0
        ? tag(`漲停打開 ${it.opened_min} 分`, 'bg-sky-100 text-sky-800', '第一次碰到漲停之後，有幾分鐘最低價跌破漲停價（1 分 K）')
        : tag('漲停後沒打開', 'bg-slate-200 text-slate-700', '第一次碰到漲停之後就一路鎖住'));
    }
    return t.join(' ');
  }

  /** 江波圖：價格線＋成交量柱，虛線＝昨收（參考價）、紅線＝漲停價 */
  function chart(s) {
    const it = s.intraday;
    if (!it || !it.t?.length) return '<div class="h-40 flex items-center justify-center text-sm text-slate-400">沒有分時資料</div>';
    const W = 360, H = 190, L = 4, R = 44, T = 8, PB = 132, VB = 176, B = 14;
    const lo = Math.min(...it.c, s.ref ?? Infinity, s.l ?? Infinity);
    const hi = Math.max(s.limit, ...it.c);
    const pad = (hi - lo) * 0.06 || hi * 0.01;
    const y = v => T + (hi + pad - v) / (hi - lo + 2 * pad) * (PB - T);
    const x = m => L + Math.min(270, Math.max(0, m)) / 270 * (W - L - R);
    const vmax = Math.max(1, ...it.v.slice(0, -1), 1);
    const vy = v => VB - Math.min(1, v / vmax) * (VB - PB - 6);
    const line = it.t.map((m, i) => `${x(m).toFixed(1)},${y(it.c[i]).toFixed(1)}`).join(' ');
    const area = `${x(it.t[0]).toFixed(1)},${PB} ${line} ${x(it.t[it.t.length - 1]).toFixed(1)},${PB}`;
    const bw = Math.max(0.8, (W - L - R) / 270 * 0.7);
    const vols = it.t.map((m, i) => {
      const v = it.v[i];
      if (!v) return '';
      const up = i === 0 ? it.c[0] >= (s.ref ?? it.c[0]) : it.c[i] >= it.c[i - 1];
      const last = i === it.t.length - 1 && m >= 270;           // 13:30 收盤集合競價，量通常很大，截頂
      return `<rect x="${(x(m) - bw / 2).toFixed(1)}" y="${vy(v).toFixed(1)}" width="${bw.toFixed(1)}" height="${(VB - vy(v)).toFixed(1)}" fill="${last ? '#a5b4fc' : up ? '#fca5a5' : '#86efac'}"/>`;
    }).join('');
    const hl = (v, color, dash, label) => (v == null ? '' : `<line x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" stroke="${color}" stroke-width="1" ${dash ? 'stroke-dasharray="3 3"' : ''}/>`
      + `<text x="${W - R + 3}" y="${(y(v) + 3).toFixed(1)}" font-size="10" fill="${color}">${esc(label)}</text>`);
    const xs = [0, 60, 120, 180, 240, 270].map(m => `<line x1="${x(m).toFixed(1)}" x2="${x(m).toFixed(1)}" y1="${T}" y2="${VB}" stroke="#f1f5f9"/>`
      + (m === 240 ? '' : `<text x="${x(m).toFixed(1)}" y="${H - 2}" font-size="9" text-anchor="${m === 0 ? 'start' : m === 270 ? 'end' : 'middle'}" fill="#94a3b8">${hhmm(m)}</text>`)).join('');
    const fl = it.first_limit != null ? `<circle cx="${x(it.first_limit).toFixed(1)}" cy="${y(s.limit).toFixed(1)}" r="3" fill="#dc2626"/>` : '';
    const ma = s.ma20 != null && s.ma20 >= lo - pad && s.ma20 <= hi + pad ? hl(s.ma20, '#f59e0b', true, '月線') : '';
    return `<svg viewBox="0 0 ${W} ${H}" class="w-full h-auto select-none" role="img" aria-label="${esc(s.name)} 分時走勢" data-code="${esc(s.code)}">
      ${xs}
      <line x1="${L}" x2="${W - R}" y1="${PB}" y2="${PB}" stroke="#e2e8f0"/>
      ${vols}
      ${ma}
      ${hl(s.ref, '#64748b', true, px(s.ref))}
      ${hl(s.limit, '#dc2626', false, px(s.limit))}
      <polygon points="${area}" fill="#ef4444" fill-opacity="0.08"/>
      <polyline points="${line}" fill="none" stroke="#dc2626" stroke-width="1.5" stroke-linejoin="round"/>
      ${fl}
      <line class="cross" x1="0" x2="0" y1="${T}" y2="${VB}" stroke="#334155" stroke-width="0.8" visibility="hidden"/>
    </svg>`;
  }

  /** 隔日沖分點：當天買超這檔的隔日沖分點（隔天開盤的潛在賣壓） */
  function dtBlock(s) {
    const d = s.dt;
    const box = inner => `<div class="mt-2 rounded-lg bg-slate-50 border border-slate-100 px-3 py-2 text-xs text-slate-600">${inner}</div>`;
    if (!d || d.status === 'error') return box('<span class="text-slate-400">隔日沖分點：分點資料抓不到</span>');
    if (d.status === 'pending') return box('<span class="text-slate-400">隔日沖分點：分點表還沒公布（傍晚後更新）</span>');
    if (!d.list?.length) return box('隔日沖分點：<b class="text-slate-700">無</b><span class="text-slate-400">（買超前 15 名裡沒有隔日沖分點）</span>');
    const heavy = (d.buy_pct ?? 0) >= 5;
    const rows = d.list.map(r => `<span class="inline-flex items-baseline gap-1 whitespace-nowrap mr-3" title="${esc(`近 60 日大買 ${r.n} 次、隔天倒回 ${Math.round((r.rate ?? 0) * 100)}%`)}">
        <span class="text-slate-700">${esc(r.broker)}</span><b class="text-red-600">${num(r.net)}</b><span class="text-slate-400">${r.pct == null ? '' : `${num(r.pct, 1)}%`}</span></span>`).join('');
    return box(`<div class="flex flex-wrap items-baseline justify-between gap-x-2">
        <span>隔日沖分點買超 <b class="${heavy ? 'text-red-600' : 'text-slate-800'}">${num(d.buy)}</b> 張（<b>${num(d.buy_pct, 1)}%</b> 量）</span>
        <span class="text-slate-400">照習慣隔天約倒回 ${num(d.expect_sell)} 張</span></div>
      <div class="mt-1 leading-5">${rows}</div>`);
  }

  function card(s, rank) {
    const it = s.intraday;
    const slope = s.ma20 != null && s.ma20_prev != null ? (s.ma20 > s.ma20_prev ? '↑' : s.ma20 < s.ma20_prev ? '↓' : '→') : '';
    return `
      <div class="tick bg-white p-4 rounded-xl shadow-sm border border-slate-200 min-w-0" data-code="${esc(s.code)}">
        <div class="flex justify-between items-start gap-2">
          <div class="min-w-0">
            <div class="font-bold text-slate-800 truncate"><span class="text-slate-400 font-normal text-xs mr-1">#${rank}</span>${esc(s.name)}
              <span class="text-xs font-normal text-slate-400">${esc(s.code)}｜${esc(s.market)}</span></div>
            <div class="mt-1 flex flex-wrap gap-1">${tags(s)}</div>
          </div>
          <div class="text-right shrink-0">
            <div class="text-xl font-extrabold text-red-500">${px(s.c)}</div>
            <div class="text-xs font-bold text-red-500">▲ +${num(s.chg_pct, 2)}%</div>
          </div>
        </div>
        <div class="mt-2">${chart(s)}</div>
        <div class="hover-info h-4 text-[11px] text-slate-600 tabular-nums"></div>
        <div class="mt-1 grid grid-cols-3 gap-x-2 gap-y-0.5 text-xs text-slate-500">
          <span>量 <b class="text-slate-700">${num(s.vol_lots)}</b> 張</span>
          <span>值 <b class="text-slate-700">${num(s.value_e8, s.value_e8 >= 10 ? 0 : 1)}</b> 億</span>
          <span>首次漲停 <b class="text-slate-700">${s.locked ? '開盤' : hhmm(it?.first_limit)}</b></span>
          <span>開 ${px(s.o)}</span><span>低 ${px(s.l)}</span><span>昨收 ${px(s.ref)}</span>
          <span>月線 ${px(s.ma20)}${slope}</span><span>上軌 ${px(s.bb_up)}</span><span>筆數 ${num(s.trades)}</span>
        </div>
        ${dtBlock(s)}
      </div>`;
  }

  /** 滑過江波圖顯示時間、價格、量 */
  function bindHover(root) {
    root.querySelectorAll('.tick svg[data-code]').forEach(svg => {
      const s = DATA.stocks.find(x => x.code === svg.dataset.code);
      const it = s?.intraday;
      if (!it) return;
      const info = svg.closest('.tick').querySelector('.hover-info');
      const cross = svg.querySelector('.cross');
      const move = e => {
        const r = svg.getBoundingClientRect();
        const vx = (e.clientX - r.left) / r.width * 360;
        const m = (vx - 4) / (360 - 4 - 44) * 270;
        let k = 0;
        for (let i = 1; i < it.t.length; i++) if (Math.abs(it.t[i] - m) < Math.abs(it.t[k] - m)) k = i;
        const cx = 4 + it.t[k] / 270 * (360 - 4 - 44);
        cross.setAttribute('x1', cx); cross.setAttribute('x2', cx); cross.setAttribute('visibility', 'visible');
        const pct = s.ref ? (it.c[k] / s.ref - 1) * 100 : null;
        info.textContent = `${hhmm(it.t[k])}　${px(it.c[k])}（${pct == null ? '—' : `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`}）　量 ${num(it.v[k])} 張${it.t[k] >= 270 ? '（收盤集合競價）' : ''}`;
      };
      svg.addEventListener('pointermove', move);
      svg.addEventListener('pointerleave', () => { cross.setAttribute('visibility', 'hidden'); info.textContent = ''; });
    });
  }

  function filters() {
    return { locked: $('fLocked').checked, strong: $('fStrong').checked, market: $('fMarket').value, sort: $('fSort').value };
  }

  function render() {
    const f = filters();
    store.set(f);
    const dtPct = s => (s.dt?.status === 'ok' ? s.dt.buy_pct ?? 0 : -1);
    const all = (DATA.stocks || []).filter(s => !f.market || s.market === f.market)
      .sort((a, b) => (f.sort === 'dt' ? dtPct(b) - dtPct(a) : 0) || b.vol_lots - a.vol_lots);
    const shown = all.filter(s => !reasons(s, f).length);
    const out = all.filter(s => reasons(s, f).length);
    $('shown').textContent = `顯示 ${shown.length} ／ ${all.length} 檔`;
    $('cards').innerHTML = shown.length ? shown.map((s, i) => card(s, i + 1)).join('')
      : '<div class="col-span-full bg-white rounded-xl border border-slate-200 p-8 text-center text-slate-400">這個條件下沒有符合的漲停股</div>';
    bindHover($('cards'));
    $('excluded').innerHTML = out.length ? `
      <details class="bg-white rounded-xl shadow-sm border border-slate-200">
        <summary class="flex items-center gap-2 px-5 py-4 hover:bg-slate-50 rounded-xl text-sm font-semibold text-slate-700">
          <span class="chev text-slate-400 transition-transform">▶</span>被排除的強勢股 ${out.length} 檔（不適合放空）
        </summary>
        <div class="px-3 pb-4 overflow-x-auto">
          <table class="w-full text-sm text-left text-slate-600">
            <thead class="text-[11px] text-slate-500 bg-slate-50"><tr>
              <th class="py-2 px-2">股票</th><th class="py-2 px-2">原因</th><th class="py-2 px-2 text-right">量（張）</th>
              <th class="py-2 px-2 text-right hidden sm:table-cell">隔日沖（張）</th><th class="py-2 px-2 text-right hidden sm:table-cell">收盤</th><th class="py-2 px-2 text-right hidden sm:table-cell">月線／上軌</th></tr></thead>
            <tbody class="divide-y divide-slate-100">${out.map(s => `<tr>
              <td class="py-2 px-2 whitespace-nowrap"><b class="text-slate-800">${esc(s.code)}</b> ${esc(s.name)} <span class="text-[11px] text-slate-400">${esc(s.market)}</span></td>
              <td class="py-2 px-2 text-red-600 font-semibold whitespace-nowrap">${esc(reasons(s, f).join('、'))}</td>
              <td class="py-2 px-2 text-right">${num(s.vol_lots)}</td>
              <td class="py-2 px-2 text-right hidden sm:table-cell">${s.dt?.status === 'ok' ? num(s.dt.buy) : '—'}</td>
              <td class="py-2 px-2 text-right hidden sm:table-cell">${px(s.c)}</td>
              <td class="py-2 px-2 text-right hidden sm:table-cell whitespace-nowrap">${px(s.ma20)}／${px(s.bb_up)}</td></tr>`).join('')}</tbody>
          </table>
        </div>
      </details>` : '';
  }

  function summary(d) {
    const st = d.stocks || [];
    const box = (label, v, sub, cls = 'text-slate-800') => `<div class="rounded-lg bg-slate-50 border border-slate-100 p-3 min-w-0">
      <div class="text-xs text-slate-500">${esc(label)}</div><div class="text-2xl font-extrabold ${cls}">${v}</div><div class="text-[11px] text-slate-400 truncate">${esc(sub)}</div></div>`;
    const locked = st.filter(s => s.locked).length;
    const strong = st.filter(s => s.strong).length;
    const ok = st.filter(s => !s.locked && !s.strong).length;
    $('summary').innerHTML = box('漲停家數', st.length, `上市 ${d.counts?.twse ?? '—'}／上櫃 ${d.counts?.tpex ?? '—'}${d.tpex_ok ? '' : '（上櫃未更新）'}`, 'text-red-500')
      + box('一字鎖', locked, '開盤就鎖死、沒打開過')
      + box('強勢股', strong, '月線之上＆布林上通道')
      + box('可留意放空', ok, '排除以上兩種之後', 'text-indigo-600');
  }

  function renderMethod(d) {
    $('method').innerHTML = `
      <h2 class="text-lg font-bold text-slate-800 mb-1">📖 說明</h2>
      <p><b>名單</b>：最新一個交易日「收盤價 = 當天漲停價」的上市、上櫃普通股（不含 ETF、權證、特別股；上市前五天沒有漲跌幅限制的新股不算），依當天成交量由大到小排序。漲停價直接用證交所公布的「股價升降幅度」與櫃買中心的「次日漲停價」，除權息的參考價調整已經算在裡面。</p>
      <p><b>一字鎖</b>：開盤價 = 最低價 = 漲停價，也就是一開盤就漲停、整天沒有打開過。賣壓完全被吃掉，隔天常常繼續開高，不適合放空。</p>
      <p><b>強勢股（月線之上＆布林上通道）</b>：用含當天在內的最近 20 個收盤價算月線（20 日均線 = 布林中軌）與布林通道（中軌 ± 2 個標準差）。收盤介於中軌和上軌之間的，代表多頭趨勢還在、離上軌還有空間，也不適合放空。收盤已經衝出上軌的，標成「突破布林上軌」，不在排除條件裡（偏離較大，留給你自己判斷）。</p>
      <p><b>江波圖</b>：當天 09:00～13:30 的 1 分鐘收盤價。紅色實線＝漲停價、灰色虛線＝昨收（參考價）、橘色虛線＝月線（在畫面範圍內時才畫）、紅點＝第一次碰到漲停的時間。下方是每分鐘成交量（張），最後一根藍色的是 13:30 收盤集合競價的量（全天成交量扣掉盤中的推算值）。「漲停打開 N 分」是第一次碰到漲停之後，1 分 K 最低價跌破漲停價的分鐘數，數字越大代表漲停越不穩。滑過圖可以看每分鐘的價格。</p>
      <p><b>隔日沖分點</b>：當天買超前 15 名分點裡，屬於隔日沖名單的分點買了多少（張、佔當天成交量 %），以及照它們過去的習慣隔天大約會倒回多少張。這些買盤通常隔天開盤就賣，對放空是加分。名單不是寫死的：近 60 個交易日在強勢股「大買（≥ 成交量 1% 且 ≥30 張）、隔天又倒回一半以上」的比例夠高的分點才算${d.dt_meta ? `（目前 ${d.dt_meta.branches} 個分點，統計 ${esc(md(d.dt_meta.first))}～${esc(md(d.dt_meta.last))} 共 ${d.dt_meta.days} 天${d.dt_meta.source === 'dynamic' ? '' : '，資料不足，暫用固定名單'}）` : ''}。滑過分點名稱可以看它的大買次數與倒回率。分點表只列前 15 名，買得少的隔日沖分點不會出現。分點表傍晚才公布，15:11 那次更新通常還沒有，20:11 會補上。</p>
      <p><b>更新時間</b>：跟著本站排程（台北時間週一到週六 06:12 起，一天數次，含 15:11、20:11）。交易所收盤行情約 14:30～15:00 公布，之前跑的都是前一個交易日；抓不到新資料時保留現有那一份。上櫃行情公布得比上市晚，15:11 那次偶爾只有上市，20:11 會補齊。</p>`;
    const s = d.sources || {};
    $('sources').innerHTML = `資料來源：<a class="hover:underline" href="${esc(s.twse)}" target="_blank" rel="noopener">證交所每日收盤行情</a>、`
      + `<a class="hover:underline" href="${esc(s.twse_limits)}" target="_blank" rel="noopener">證交所股價升降幅度</a>、`
      + `<a class="hover:underline" href="${esc(s.tpex)}" target="_blank" rel="noopener">櫃買中心上櫃股票行情</a>、`
      + `<a class="hover:underline" href="${esc(s.yahoo)}" target="_blank" rel="noopener">Yahoo Finance（日線、1 分鐘分時）</a>、`
      + `<a class="hover:underline" href="${esc(s.broker || 'https://5850web.moneydj.com/')}" target="_blank" rel="noopener">MoneyDJ 券商分點進出</a> ｜ 自動化發布 via GitHub Actions & Pages`;
  }

  async function load() {
    try {
      DATA = await fetch('./data/limit_up.json', { cache: 'no-store' }).then(r => { if (!r.ok) throw new Error(`limit_up.json ${r.status}`); return r.json(); });
      $('meta').innerHTML = `
        <div>交易日：<span class="font-semibold text-slate-700">${esc(md(DATA.date))}</span></div>
        <div>資料產生時間：<span class="font-semibold text-slate-700">${esc(DATA.updated_at)}</span> <span class="text-slate-400">(UTC+8)</span></div>`;
      const saved = store.get();
      if (saved) {
        $('fLocked').checked = saved.locked !== false;
        $('fStrong').checked = saved.strong !== false;
        $('fMarket').value = saved.market || '';
        $('fSort').value = saved.sort === 'dt' ? 'dt' : 'vol';
      }
      ['fLocked', 'fStrong', 'fMarket', 'fSort'].forEach(id => $(id).addEventListener('change', render));
      summary(DATA);
      render();
      renderMethod(DATA);
    } catch (e) {
      const st = $('status');
      st.className = 'bg-white rounded-xl shadow-sm p-6 border border-red-200 text-sm text-red-600';
      st.textContent = `資料載入失敗：${e.message}。請確認 GitHub Actions 已執行 scripts/build_limit_up.py 產生 data/limit_up.json。`;
    }
  }
  load();
})();
