/* 期貨夜盤走勢頁：讀取 data/night_futures.json（scripts/build_night_futures.py 產生） */
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = (v, d = 0) => (v == null || !isFinite(v) ? '—' : Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d }));
  const dec = v => (v == null ? 0 : Math.abs(v) >= 1000 ? 0 : Math.abs(v) >= 100 ? 1 : 2);
  const signCls = v => (v == null || v === 0 ? 'text-slate-500' : v > 0 ? 'text-red-500' : 'text-green-600');
  const pctStr = v => (v == null ? '—' : `${v > 0 ? '▲ +' : v < 0 ? '▼ ' : ''}${Number(v).toFixed(2)}%`);
  const pctPlain = v => (v == null ? '—' : `${v > 0 ? '+' : ''}${Number(v).toFixed(2)}%`);
  const ptsStr = (v, d) => (v == null ? '—' : `${v > 0 ? '+' : ''}${num(v, d)}`);
  const WD = ['日', '一', '二', '三', '四', '五', '六'];
  const md = d => { if (!d) return '—'; const t = new Date(`${d}T00:00:00`); return `${t.getMonth() + 1}/${t.getDate()}(${WD[t.getDay()]})`; };
  const KIND = { index: '大盤', etf: '台灣 50 ETF', stock: '個股' };

  /** 交易日期 D 的夜盤 = D 的前一個交易日晚上 → 隔天清晨 05:00（週五晚上的夜盤記在下週一，但週六清晨就收盤了）。
   *  前一個交易日從序列裡拿（沒有就寫「前一晚」） */
  const nextDay = d => { const t = new Date(`${d}T00:00:00`); t.setDate(t.getDate() + 1); return `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, '0')}-${String(t.getDate()).padStart(2, '0')}`; };
  function nightLabel(series, i) {
    const d = series[i]?.date;
    const prev = i > 0 ? series[i - 1].date : null;
    return prev ? `${md(prev)} 晚 → ${md(nextDay(prev))} 清晨` : `${md(d)} 前一晚`;
  }

  function sparkline(values, { w = 120, h = 32 } = {}) {
    const vs = (values || []).filter(v => v != null);
    if (vs.length < 2) return '';
    const min = Math.min(...vs), max = Math.max(...vs), span = max - min || 1;
    const pts = vs.map((v, i) => `${(i / (vs.length - 1) * w).toFixed(1)},${(h - 2 - (v - min) / span * (h - 4)).toFixed(1)}`).join(' ');
    const color = vs[vs.length - 1] >= vs[0] ? '#ef4444' : '#22c55e';
    return `<svg width="${w}" height="${h}" class="inline-block overflow-visible" aria-label="近 ${vs.length} 個夜盤收盤走勢"><polyline fill="none" stroke="${color}" stroke-width="1.8" points="${pts}"/></svg>`;
  }

  /** 最上面的卡片：最新一晚的夜盤 */
  function card(c) {
    const s = c.series || [];
    const i = s.length - 1;
    const last = s[i];
    if (!last) return '';
    const n = last.night;
    const d = dec(n?.c);
    const gap = last.day ? `<span>當天日盤開盤跳空 <b class="${signCls(last.gap_pct)}">${pctPlain(last.gap_pct)}</b></span>`
      : `<span class="text-slate-400">${md(last.date)} 日盤還沒開</span>`;
    const spark = sparkline(s.slice(-30).map(r => r.night?.c));
    return `
      <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200 min-w-0">
        <div class="flex justify-between items-start gap-2">
          <div class="min-w-0">
            <div class="text-xs font-semibold text-slate-400 tracking-wider">${esc(KIND[c.kind] || '')}</div>
            <div class="font-bold text-slate-800 truncate">${esc(c.name)} <span class="text-xs font-normal text-slate-400">${esc(c.kind === 'index' ? c.fut : `${c.code}｜${c.fut}`)}</span></div>
          </div>
          <span class="text-[11px] text-slate-400 text-right shrink-0">${esc(nightLabel(s, i))}</span>
        </div>
        ${n ? `
        <div class="flex items-end justify-between mt-2 gap-2">
          <div>
            <div class="text-3xl font-extrabold ${signCls(n.chg_pct)}">${pctStr(n.chg_pct)}</div>
            <div class="text-sm text-slate-600 mt-0.5">夜盤收 <b>${num(n.c, d)}</b>　<span class="${signCls(n.chg)}">${ptsStr(n.chg, d)}</span></div>
          </div>
          <div class="shrink-0">${spark}</div>
        </div>
        <div class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500">
          <span>成交 <b class="text-slate-700">${num(n.vol)}</b> 口</span>
          <span>高 ${num(n.h, d)}／低 ${num(n.l, d)}</span>
          ${gap}
        </div>` : '<div class="mt-3 text-sm text-slate-400">這一晚沒有成交</div>'}
        <div class="mt-1 text-[11px] text-slate-400">近月 ${esc(last.exp)}｜夜盤時間 ${esc(c.night_hours)}｜走勢線＝近 30 個夜盤收盤</div>
      </div>`;
  }

  /** 走勢圖：長條＝夜盤漲跌 %，圓點＝當天日盤開盤跳空 % */
  function chart(c) {
    const rows = (c.series || []).slice(-40);
    if (rows.length < 5) return '';
    const W = 640, H = 230, L = 40, R = 8, T = 12, B = 30;
    const vals = rows.flatMap(r => [r.night?.chg_pct, r.gap_pct]).filter(v => v != null);
    const m = Math.max(1, Math.ceil(Math.max(...vals.map(Math.abs))));
    const y = v => T + (m - v) / (2 * m) * (H - T - B);
    const step = (W - L - R) / rows.length;
    const x = i => L + step * (i + 0.5);
    const grid = [-m, -m / 2, 0, m / 2, m].map(v => `<line x1="${L}" x2="${W - R}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" stroke="${v === 0 ? '#cbd5e1' : '#f1f5f9'}"/>`
      + `<text x="${L - 6}" y="${(y(v) + 3).toFixed(1)}" font-size="10" text-anchor="end" fill="#94a3b8">${v > 0 ? '+' : ''}${Number(v.toFixed(1))}%</text>`).join('');
    const bw = Math.max(2, step * 0.6);
    const bars = rows.map((r, i) => {
      const v = r.night?.chg_pct;
      if (v == null) return '';
      const y0 = y(0), y1 = y(v);
      const tip = `${r.date} 夜盤 ${pctPlain(v)}（收 ${num(r.night.c, dec(r.night.c))}）／開盤跳空 ${pctPlain(r.gap_pct)}`;
      return `<rect x="${(x(i) - bw / 2).toFixed(1)}" y="${Math.min(y0, y1).toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(1, Math.abs(y1 - y0)).toFixed(1)}" fill="${v >= 0 ? '#fca5a5' : '#86efac'}"><title>${esc(tip)}</title></rect>`;
    }).join('');
    const dots = rows.map((r, i) => (r.gap_pct == null ? '' : `<circle cx="${x(i).toFixed(1)}" cy="${y(r.gap_pct).toFixed(1)}" r="2.6" fill="#334155"><title>${esc(`${r.date} 開盤跳空 ${pctPlain(r.gap_pct)}`)}</title></circle>`)).join('');
    const every = Math.ceil(rows.length / 6);
    const labels = rows.map((r, i) => (i % every === 0 || i === rows.length - 1 ? `<text x="${x(i).toFixed(1)}" y="${H - 10}" font-size="10" text-anchor="middle" fill="#94a3b8">${r.date.slice(5).replace('-', '/')}</text>` : '')).join('');
    const st = c.stats;
    const f2 = v => Number(v).toFixed(2);
    const stat = st ? `近 ${st.n} 個交易日（${md(st.from)}～${md(st.to)}）：相關係數 <b class="text-slate-700">${f2(st.r)}</b>、斜率 <b class="text-slate-700">${f2(st.slope)}</b>（夜盤漲 1%，開盤平均 ${st.slope > 0 ? '+' : ''}${f2(st.slope)}%）、方向一致 <b class="text-slate-700">${st.same_dir_pct}%</b>（夜盤 ≥±0.3% 的日子）` : '資料還不夠算統計';
    return `
      <div class="min-w-0">
        <div class="flex flex-wrap items-baseline justify-between gap-2">
          <div class="font-bold text-slate-800">${esc(c.name)} <span class="text-xs font-normal text-slate-400">${esc(c.kind === 'index' ? c.fut : `${c.code}｜${c.fut}`)}</span></div>
          <div class="flex items-center gap-3 text-[11px] text-slate-500">
            <span><span class="inline-block w-2.5 h-2.5 align-middle bg-red-300 mr-1"></span><span class="inline-block w-2.5 h-2.5 align-middle bg-green-300 mr-1"></span>夜盤漲跌</span>
            <span><span class="inline-block w-2 h-2 rounded-full align-middle bg-slate-700 mr-1"></span>開盤跳空</span>
          </div>
        </div>
        <svg viewBox="0 0 ${W} ${H}" class="w-full h-auto mt-1" role="img" aria-label="${esc(c.name)} 夜盤漲跌與開盤跳空">${grid}${bars}${dots}${labels}</svg>
        <div class="text-xs text-slate-500">${stat}</div>
      </div>`;
  }

  /** 市值前 50 × 夜盤期貨 */
  function top50Table(d, byCode) {
    const rows = d.top50 || [];
    const has = rows.filter(r => (r.night_futures || []).length);
    const rest = rows.filter(r => !(r.night_futures || []).length);
    const line = r => {
      const c = byCode[r.code];
      const last = c?.series?.[c.series.length - 1];
      const v = last?.night?.chg_pct;
      return `<tr class="${r.night_futures.length ? 'bg-indigo-50/60' : 'hover:bg-slate-50'}">
        <td class="py-2 px-2 text-slate-400">${r.rank}</td>
        <td class="py-2 px-2 font-semibold text-slate-800 whitespace-nowrap">${esc(r.code)} <span class="font-normal text-slate-600">${esc(r.name)}</span></td>
        <td class="py-2 px-2 text-right whitespace-nowrap hidden sm:table-cell">${num(r.mcap_e8)}</td>
        <td class="py-2 px-2 whitespace-nowrap">${r.night_futures.length ? `<span class="font-semibold text-indigo-700">${esc(r.night_futures.join('、'))}</span>` : '<span class="text-slate-400">沒有</span>'}</td>
        <td class="py-2 px-2 text-right font-bold whitespace-nowrap ${signCls(v)}">${r.night_futures.length ? pctStr(v) : ''}</td>
      </tr>`;
    };
    const head = `<thead class="text-[11px] text-slate-500 bg-slate-50"><tr>
        <th class="py-2 px-2 w-10">#</th><th class="py-2 px-2">股票</th><th class="py-2 px-2 text-right hidden sm:table-cell">市值（億）</th>
        <th class="py-2 px-2">夜盤期貨</th><th class="py-2 px-2 text-right">最新夜盤</th></tr></thead>`;
    const others = (d.night_eligible || []).filter(e => !rows.some(r => r.code === e.code) && e.code !== '0050');
    return `
      <div class="flex flex-col md:flex-row md:items-end justify-between gap-1 mb-3">
        <div>
          <h2 class="text-lg font-bold text-slate-800">🏆 台灣市值前 50 名 × 夜盤期貨</h2>
          <p class="text-xs text-slate-400 mt-0.5">上市普通股市值（${esc(md(d.top_date))} 收盤 × 已發行股數）前 50 名；期交所目前只有 <b>${has.length}</b> 檔有夜盤期貨，其他 ${rest.length} 檔晚上沒有任何價格</p>
        </div>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-sm text-left text-slate-600">${head}<tbody class="divide-y divide-slate-100">${has.map(line).join('')}</tbody></table>
      </div>
      <details class="mt-3 rounded-lg border border-slate-200">
        <summary class="flex items-center gap-2 px-4 py-3 hover:bg-slate-50 rounded-lg text-sm text-slate-600">
          <span class="chev text-slate-400 transition-transform">▶</span>其餘 ${rest.length} 檔（沒有夜盤期貨）
        </summary>
        <div class="px-2 pb-3 overflow-x-auto">
          <table class="w-full text-sm text-left text-slate-600">${head}<tbody class="divide-y divide-slate-100">${rest.map(line).join('')}</tbody></table>
        </div>
      </details>
      ${others.length ? `<p class="mt-3 text-xs text-slate-400">不在前 50 名、但也有夜盤的：${others.map(e => `${esc(e.code)} ${esc(e.name)}（${esc(e.fut)}）`).join('、')}</p>` : ''}`;
  }

  function renderMethod(d, contracts) {
    const st = contracts.filter(c => c.primary && c.stats).map(c => `${esc(c.name)} 相關係數 ${Number(c.stats.r).toFixed(2)}、斜率 ${Number(c.stats.slope).toFixed(2)}`).join('；');
    $('method').innerHTML = `
      <h2 class="text-lg font-bold text-slate-800 mb-1">📖 說明</h2>
      <p><b>夜盤是什麼</b>：期貨在日盤（08:45–13:45）收盤後的盤後交易時段。台指期是 15:00 到隔天 05:00，有夜盤的股票期貨是 17:25 到隔天 05:00。這段時間股市已經收盤，只有期貨在交易，剛好涵蓋整個美股交易時段，所以夜盤反映的是市場對前一晚國際消息的反應。</p>
      <p><b>哪些股票有夜盤</b>：要不要開夜盤是期交所一檔一檔決定的，目前只有台積電（台積電期貨、小型台積電期貨）、聯電、0050、00679B。日月光投控等其他股票期貨都只有日盤。頁面上的名單每次更新都會重新讀期交所的標的表。</p>
      <p><b>數字怎麼算</b>：夜盤漲跌是夜盤收盤對「前一個日盤的結算價」，跟期交所的每日行情表一致；開盤跳空是當天日盤開盤對同一個結算價。都用近月合約（當天日盤有成交、到期月份最近的那一個）。期交所把「D 前一晚 → D 清晨」的夜盤記在交易日期 D，所以卡片上寫的是那一晚的起訖日。</p>
      <p><b>回測的結論</b>：夜盤的漲跌幾乎一比一反映在隔天開盤（${st || '資料不足'}），但開盤之後平均沒有繼續往同一個方向走。所以夜盤告訴你的是「大概開在哪裡」，不是「開盤後會往哪裡走」。</p>
      <p><b>限制</b>：期貨和現貨之間有價差，05:00 到 09:00 之間也可能有新消息，個別日子會差很多；統計只涵蓋頁面上列的這段期間。</p>
      <p><b>更新時間</b>：跟著本站的排程更新（台北時間週一到週六 06:12 起，一天數次），夜盤結束、期交所公布之後才會出現。夜盤進行中不會即時更新，要看即時報價請到 <a href="https://mis.taifex.com.tw/futures/" target="_blank" rel="noopener" class="text-indigo-600 hover:underline">期交所行情資訊網</a> →「盤後交易時段行情」→「股票類契約報價」。</p>`;
    const s = d.sources || {};
    $('sources').innerHTML = `資料來源：<a class="hover:underline" href="${esc(s.list)}" target="_blank" rel="noopener">期交所股票期貨標的表</a>、`
      + `<a class="hover:underline" href="${esc(s.daily)}" target="_blank" rel="noopener">期交所期貨每日交易行情</a>、`
      + `<a class="hover:underline" href="${esc(s.shares)}" target="_blank" rel="noopener">證交所上市公司基本資料</a>、`
      + `<a class="hover:underline" href="${esc(s.prices)}" target="_blank" rel="noopener">證交所每日收盤行情</a> ｜ 自動化發布 via GitHub Actions & Pages`;
  }

  async function load() {
    try {
      const d = await fetch('./data/night_futures.json', { cache: 'no-store' }).then(r => { if (!r.ok) throw new Error(`night_futures.json ${r.status}`); return r.json(); });
      const contracts = d.contracts || [];
      const order = { index: 0, etf: 1, stock: 2 };
      const primary = contracts.filter(c => c.primary && (c.series || []).length).sort((a, b) => order[a.kind] - order[b.kind]);
      const byCode = Object.fromEntries(primary.filter(c => c.kind === 'stock').map(c => [c.code, c]));
      const latest = primary.map(c => c.series[c.series.length - 1].date).sort().pop();
      $('meta').innerHTML = `
        <div>最新夜盤歸屬交易日：<span class="font-semibold text-slate-700">${esc(md(latest))}</span></div>
        <div>資料產生時間：<span class="font-semibold text-slate-700">${esc(d.updated_at)}</span> <span class="text-slate-400">(UTC+8)</span></div>`;
      $('cardsSub').textContent = '同一檔有好幾個期貨時（台積電期貨／小型台積電期貨、兩檔 0050 期貨），卡片用夜盤成交量大的那一個';
      $('cards').innerHTML = primary.map(card).join('');
      $('charts').innerHTML = primary.map(chart).join('');
      $('top50').innerHTML = top50Table(d, byCode);
      renderMethod(d, contracts);
    } catch (e) {
      const st = $('status');
      st.className = 'bg-white rounded-xl shadow-sm p-6 border border-red-200 text-sm text-red-600';
      st.textContent = `資料載入失敗：${e.message}。請確認 GitHub Actions 已執行 scripts/build_night_futures.py 產生 data/night_futures.json。`;
    }
  }
  load();
})();
