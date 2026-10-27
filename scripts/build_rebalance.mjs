// 指數成分股調整事件：讀取 data/index_events.json（人工整理、附官方／新聞來源），
// 以 Yahoo 日 K（還原權息）回測各階段損益，並為尚未生效或剛生效的事件產生交易計畫，輸出 data/rebalance.json
// 價格可隨時重建，所以 rebalance.json 不進 Git，CI 每次部署時重新產生
// 用法：node scripts/build_rebalance.mjs
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const R = require(path.join(ROOT, 'assets/rebalance-engine.js'));
const EVENTS = JSON.parse(fs.readFileSync(path.join(ROOT, 'data/index_events.json'), 'utf8'));
const YAHOO = 'https://query1.finance.yahoo.com/v8/finance/chart';
const HEADERS = { 'User-Agent': 'Mozilla/5.0 (chive-terminal rebalance)' };
const START = Math.floor(Date.UTC(2022, 9, 1) / 1000);
const MIN_HIST = 8; // 指數本身樣本不足時改用同類指數的分布
// REBALANCE_TODAY 可指定日期，用來檢查「已公告、尚未生效」的畫面
const twToday = process.env.REBALANCE_TODAY || new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10);
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function yahooDaily(code, suffixes = ['.TW', '.TWO']) {
  for (const sfx of suffixes) {
    for (let attempt = 0; attempt < 3; attempt++) {
      const res = await fetch(`${YAHOO}/${code}${sfx}?interval=1d&period1=${START}&period2=${Math.floor(Date.now() / 1000)}`, { headers: HEADERS });
      if (res.status === 429) { await sleep(5000 * (attempt + 1)); continue; }
      const r = res.ok ? (await res.json())?.chart?.result?.[0] : null;
      const bars = r?.timestamp ? R.barsFromYahoo(r) : [];
      if (bars.length > 20) return { symbol: code + sfx, bars };
      break;
    }
    await sleep(200);
  }
  return null;
}

// ---------- 價格 ----------
const twii = await yahooDaily('^TWII', ['']);
if (!twii) { console.error('加權指數日 K 抓取失敗'); process.exit(1); }
const MKT = Object.fromEntries(twii.bars.map(b => [b.date, b]));
const codes = new Set();
for (const ix of EVENTS.indexes) for (const ev of ix.events) for (const s of [...ev.adds, ...ev.deletes]) codes.add(s.code);
const prices = {};
for (const code of codes) {
  prices[code] = await yahooDaily(code);
  if (!prices[code]) console.warn(`  ${code}: 無 Yahoo 日 K`);
  await sleep(250);
}
console.log(`事件個股 ${codes.size} 檔，取得價格 ${Object.values(prices).filter(Boolean).length} 檔`);

// ---------- 逐事件回測 ----------
const rows = [];
for (const ix of EVENTS.indexes) {
  for (const ev of ix.events) {
    if (ev.tradeDate >= twToday) continue; // 尚未生效的事件只產生交易計畫
    for (const side of ['add', 'delete']) {
      for (const s of side === 'add' ? ev.adds : ev.deletes) {
        const p = prices[s.code];
        const st = p ? R.eventStages(p.bars, ev, side, MKT) : { error: '無價格資料' };
        rows.push({ index: ix.key, group: ix.group, review: ev.review, announceDate: ev.announceDate, tradeDate: ev.tradeDate, code: s.code, name: s.name, ...st, side });
      }
    }
  }
}
const round = (v, d = 5) => (v == null ? null : +v.toFixed(d));
const agg = list => {
  const a = R.aggregate(list);
  for (const side of Object.keys(a)) for (const k of Object.keys(a[side])) for (const f of Object.keys(a[side][k])) if (typeof a[side][k][f] === 'number') a[side][k][f] = round(a[side][k][f]);
  return a;
};
const byIndex = Object.fromEntries(EVENTS.indexes.map(ix => [ix.key, agg(rows.filter(r => r.index === ix.key))]));
const byGroup = Object.fromEntries([...new Set(EVENTS.indexes.map(ix => ix.group))].map(g => [g, agg(rows.filter(r => r.group === g))]));
const overall = agg(rows);

// ---------- 交易計畫：尚未生效或生效後 3 個交易日內的事件 ----------
const opportunities = [];
for (const ix of EVENTS.indexes) {
  for (const ev of ix.events) {
    const anyBars = twii.bars;
    const t = R.idxUpTo(anyBars, ev.tradeDate);
    const recent = ev.tradeDate >= twToday || (t >= 0 && anyBars.length - 1 - t <= 3);
    if (!recent) continue;
    const histIx = byIndex[ix.key];
    for (const side of ['add', 'delete']) {
      const hist = histIx[side].stage23.n >= MIN_HIST ? histIx : byGroup[ix.group];
      for (const s of side === 'add' ? ev.adds : ev.deletes) {
        const p = prices[s.code];
        if (!p) continue;
        const pl = R.plan(p.bars.filter(b => b.date <= twToday), ev, side, hist, twToday);
        const live = ev.tradeDate < twToday ? R.eventStages(p.bars, ev, side, MKT) : null;
        opportunities.push({
          index: ix.key, indexName: ix.index, etfs: ix.etfs, review: ev.review, announceDate: ev.announceDate, tradeDate: ev.tradeDate,
          code: s.code, name: s.name, histFrom: hist === histIx ? ix.index : `${ix.group === 'dividend' ? '高股息類' : '市值型'}指數合計`,
          ...pl, result: live && !live.error ? { stage23: live.stage23, stage4: live.stage4 || null } : null,
        });
      }
    }
  }
}

// ---------- 預估下一次審核 ----------
function thirdFriday(y, m) { const d = new Date(Date.UTC(y, m - 1, 1)); const off = (5 - d.getUTCDay() + 7) % 7; return new Date(Date.UTC(y, m - 1, 1 + off + 14)); }
function lastBusinessDay(y, m) { const d = new Date(Date.UTC(y, m, 0)); while ([0, 6].includes(d.getUTCDay())) d.setUTCDate(d.getUTCDate() - 1); return d; }
const iso = d => d.toISOString().slice(0, 10);
const median = a => { const s = [...a].sort((x, y) => x - y); return s.length ? s[Math.floor(s.length / 2)] : null; };
const calendar = EVENTS.indexes.map(ix => {
  const sc = ix.schedule;
  const gaps = ix.events.map(e => (Date.parse(e.tradeDate) - Date.parse(e.announceDate)) / 86400000);
  const gap = median(gaps);
  const last = ix.events.reduce((a, e) => (e.tradeDate > a ? e.tradeDate : a), '');
  let next = null;
  for (let k = 0; k < 24 && !next; k++) {
    const base = new Date(Date.UTC(+twToday.slice(0, 4), +twToday.slice(5, 7) - 1 + k, 1));
    const y = base.getUTCFullYear(), m = base.getUTCMonth() + 1;
    if (!sc.months.includes(m)) continue;
    let td;
    if (sc.rule === 'thirdFriday') td = iso(thirdFriday(y, m));
    else if (sc.rule === 'lastBusinessDay') td = iso(lastBusinessDay(y, m));
    else { // lastYear：沒有固定規則，以去年同月的生效交易日加 52 週估算
      const prev = ix.events.find(e => e.tradeDate.startsWith(`${y - 1}-${String(m).padStart(2, '0')}`));
      if (!prev) continue;
      td = iso(new Date(Date.parse(prev.tradeDate) + 364 * 86400000));
    }
    if (td > last && td >= twToday) next = td;
  }
  // 名單在生效後才公開的指數（00878）沒有可預估的公告日
  const ann = next && gap != null && gap >= 0 ? iso(new Date(Date.parse(next) - gap * 86400000)) : null;
  return { key: ix.key, index: ix.index, etfs: ix.etfs, group: ix.group, months: sc.months, rule: sc.rule, note: sc.note || '', nextTradeDate: next, nextAnnounceDate: ann, announceAfter: gap != null && gap < 0, typicalGapDays: gap, lastTradeDate: last, events: ix.events.length };
}).sort((a, b) => (a.nextTradeDate || '9') < (b.nextTradeDate || '9') ? -1 : 1);

let model = null;
try { model = JSON.parse(fs.readFileSync(path.join(ROOT, 'data/index_model.json'), 'utf8')); } catch (e) { /* 無模型 */ }

const out = {
  generatedAt: new Date().toISOString(), today: twToday, eventsUpdated: EVENTS.updated, cost: R.COST, preDays: R.PRE_DAYS, crowdLimit: R.CROWD_LIMIT,
  indexes: EVENTS.indexes.map(ix => ({ key: ix.key, index: ix.index, etfs: ix.etfs, group: ix.group, events: ix.events.length })),
  overall, byGroup, byIndex, calendar, opportunities,
  model: model ? { quoteDate: model.quoteDate, rules: model.rules, universe: model.universe, candidates: model.candidates, nearMiss: model.nearMiss } : null,
  rows: rows.map(r => ({ ...r, preRun: round(r.preRun, 4), ...Object.fromEntries(R.STAGES.filter(k => r[k]).map(k => [k, { ...r[k], entry: round(r[k].entry, 2), exit: round(r[k].exit, 2), ret: round(r[k].ret) }])) })),
  events: EVENTS.indexes.flatMap(ix => ix.events.map(ev => ({ index: ix.key, ...ev }))),
};
fs.mkdirSync(path.join(ROOT, 'data'), { recursive: true });
fs.writeFileSync(path.join(ROOT, 'data/rebalance.json'), JSON.stringify(out));

const f = s => (s.n ? `n=${s.n} 勝率 ${(s.winRate * 100).toFixed(0)}% 平均 ${(s.avgRet * 100).toFixed(2)}% 中位 ${(s.median * 100).toFixed(2)}% PF ${s.profitFactor?.toFixed(2) ?? '∞'}｜超額 平均 ${(s.excessAvg * 100).toFixed(2)}% 勝 ${(s.excessWin * 100).toFixed(0)}%` : 'n=0');
for (const [name, a] of [['全部', overall], ...Object.entries(byGroup), ...Object.entries(byIndex)]) {
  console.log(`\n== ${name}`);
  for (const side of ['add', 'delete']) for (const st of ['stage1', 'stage23', 'stage23_normal', 'stage23_crowded', 'stage4_t1open', 'stage4']) console.log(`  ${side === 'add' ? '新增' : '刪除'} ${st.padEnd(16)} ${f(a[side][st])}`);
}
console.log(`\n交易計畫 ${opportunities.length} 筆；錯誤 ${rows.filter(r => r.error).length} 筆`);
