// 開盤區間突破（ORB）短沖判斷：抓取 Yahoo Finance 近 60 個交易日 5 分 K，
// 以 assets/orb-engine.js 逐檔回測，輸出 data/orb/index.json（總覽與排行）與 data/orb/<代號>.json（最新一日 K 線與交易明細）
// 資料可由 Yahoo 隨時重建，所以不進 Git，CI 每次部署時重新產生後隨網站發布
// 用法：node scripts/build_orb.mjs [--stocks 2330,3037]
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const O = require(path.join(ROOT, 'assets/orb-engine.js'));
const OUT_DIR = path.join(ROOT, 'data/orb');
const YAHOO = 'https://query1.finance.yahoo.com/v8/finance/chart';
const HEADERS = { 'User-Agent': 'Mozilla/5.0 (chive-terminal ORB)' };
const MIN_TRADES = 8;

// 回測版本：完整規則（含兩個避坑濾網）與對照組
const VARIANTS = {
  full: { label: '完整規則（含開盤窄幅、大盤方向濾網）', opts: {} },
  core: { label: '核心規則（無濾網）', opts: { narrowFilter: false, marketFilter: false } },
  narrowOnly: { label: '只加開盤窄幅濾網', opts: { marketFilter: false } },
  marketOnly: { label: '只加大盤方向濾網', opts: { narrowFilter: false } },
};

const args = process.argv.slice(2);
const arg = (k, d) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : d; };
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function fetchSessions(symbol) {
  for (let attempt = 0; attempt < 3; attempt++) {
    const res = await fetch(`${YAHOO}/${encodeURIComponent(symbol)}?interval=5m&range=60d`, { headers: HEADERS });
    if (res.status === 429) { await sleep(5000 * (attempt + 1)); continue; }
    if (!res.ok) return null;
    const r = (await res.json())?.chart?.result?.[0];
    return r ? O.sessionsFromYahoo(r) : null;
  }
  return null;
}

// ---------- 標的：族群成分股 + 跨個股驗證股票 ----------
const universe = new Map();
try {
  const themes = JSON.parse(fs.readFileSync(path.join(ROOT, 'data/themes.json'), 'utf8'));
  for (const t of themes.themes) for (const m of t.members) {
    const u = universe.get(m.code) || { code: m.code, name: m.name, market: m.market || null, themes: [] };
    u.themes.push(t.title);
    universe.set(m.code, u);
  }
} catch (e) { console.warn('無 data/themes.json'); }
try {
  const v = JSON.parse(fs.readFileSync(path.join(ROOT, 'data/strategy_validation.json'), 'utf8'));
  for (const c of v.composite) if (!universe.has(c.id)) universe.set(c.id, { code: c.id, name: c.name, market: null, themes: [] });
} catch (e) { /* 無驗證檔 */ }
let codes = [...universe.keys()];
if (arg('--stocks')) codes = arg('--stocks').split(',').map(c => { if (!universe.has(c)) universe.set(c, { code: c, name: c, market: null, themes: [] }); return c; });

const mkt = await fetchSessions('^TWII');
if (!mkt?.length) { console.error('加權指數 5 分 K 抓取失敗'); process.exit(1); }
console.log(`加權指數 ${mkt.length} 個交易日（${mkt[0].date}～${mkt[mkt.length - 1].date}），標的 ${codes.length} 檔`);

fs.mkdirSync(OUT_DIR, { recursive: true });
const round = (v, d = 4) => (v == null ? null : +v.toFixed(d));
const compact = s => (s.n ? { n: s.n, winRate: round(s.winRate), avgRet: round(s.avgRet, 5), total: round(s.total), mdd: round(s.mdd), payoff: round(s.payoff, 3), profitFactor: round(s.profitFactor, 3) } : { n: 0 });

const index = [];
const pooled = Object.fromEntries(Object.keys(VARIANTS).map(k => [k, []]));
const halves = { first: [], second: [] };
let failed = 0;
for (const code of codes) {
  const u = universe.get(code);
  const suffixes = u.market === '上櫃' ? ['.TWO'] : u.market === '上市' ? ['.TW'] : ['.TW', '.TWO'];
  let sessions = null, symbol = null;
  for (const sfx of suffixes) {
    sessions = await fetchSessions(code + sfx);
    if (sessions?.length) { symbol = code + sfx; break; }
    await sleep(200);
  }
  await sleep(250);
  if (!sessions?.length) { failed++; console.warn(`  ${code} ${u.name}: 無 5 分 K 資料`); continue; }

  const results = Object.fromEntries(Object.entries(VARIANTS).map(([k, v]) => [k, O.backtest(sessions, v.opts, mkt)]));
  const full = results.full;
  const done = full.days.filter(d => d.complete);
  const avgVolLots = done.length ? done.reduce((a, d) => a + sessions.find(s => s.date === d.date).bars.reduce((x, b) => x + b.v, 0), 0) / done.length / 1000 : null;
  const v = O.verdict(full.stats, avgVolLots);
  for (const k of Object.keys(VARIANTS)) pooled[k].push(...results[k].trades);
  const mid = done[Math.floor(done.length / 2)]?.date;
  for (const t of full.trades) (t.date < mid ? halves.first : halves.second).push(t);

  const latestSession = sessions[sessions.length - 1];
  const latest = full.days[full.days.length - 1];
  index.push({
    code, name: u.name, market: u.market || (symbol.endsWith('.TWO') ? '上櫃' : '上市'), themes: u.themes.slice(0, 3),
    sessions: full.sessions, avgVolLots: round(avgVolLots, 0), verdict: v.key, verdictLabel: v.label,
    stats: compact(full.stats), long: compact(full.long), short: compact(full.short),
    latest: { date: latest.date, complete: latest.complete, skip: latest.skip || null, trades: latest.trades.length, side: latest.trades[0]?.side || null },
  });
  fs.writeFileSync(path.join(OUT_DIR, `${code}.json`), JSON.stringify({
    code, name: u.name, symbol, verdict: v,
    variants: Object.fromEntries(Object.entries(results).map(([k, r]) => [k, { label: VARIANTS[k].label, stats: compact(r.stats), long: compact(r.long), short: compact(r.short) }])),
    recentTrades: full.trades.slice(-12).reverse().map(t => ({ ...t, entryPx: round(t.entryPx, 2), exitPx: round(t.exitPx, 2), ret: round(t.ret, 5) })),
    latest: { ...latest, bars: latestSession.bars.map(b => [b.time, b.o, b.h, b.l, b.c, b.v]) },
    daily: full.days.filter(d => d.complete).slice(-20).map(d => ({ date: d.date, skip: d.skip || null, n: d.trades.length, ret: round(d.trades.reduce((a, t) => a + (t.ret || 0), 0), 5) })),
  }));
  console.log(`  ${code} ${u.name}: ${full.stats.n} 筆，${v.label}`);
}

// 未扣成本的平均價格變動：用來區分「沒有優勢」與「優勢被當沖成本吃掉」
const grossAvg = trades => (trades.length ? round(trades.reduce((a, t) => a + (t.side === 'long' ? t.exitPx / t.entryPx - 1 : 1 - t.exitPx / t.entryPx), 0) / trades.length, 5) : null);
const summarizeAll = trades => ({ ...compact(O.summarize(trades)), grossAvg: grossAvg(trades.filter(t => !t.open)) });
const out = {
  generatedAt: new Date().toISOString(),
  marketFrom: mkt[0].date, marketTo: mkt[mkt.length - 1].date,
  latestDate: mkt[mkt.length - 1].date, latestComplete: mkt[mkt.length - 1].bars.some(b => b.time >= O.DEFAULTS.exitBar),
  params: O.DEFAULTS, cost: O.COST, minTrades: MIN_TRADES, failed,
  summary: {
    variants: Object.fromEntries(Object.entries(VARIANTS).map(([k, v]) => [k, { label: v.label, all: summarizeAll(pooled[k]), long: summarizeAll(pooled[k].filter(t => t.side === 'long')), short: summarizeAll(pooled[k].filter(t => t.side === 'short')) }])),
    halves: { first: summarizeAll(halves.first), second: summarizeAll(halves.second) },
    verdicts: index.reduce((a, s) => ({ ...a, [s.verdict]: (a[s.verdict] || 0) + 1 }), {}),
  },
  stocks: index,
};
fs.writeFileSync(path.join(OUT_DIR, 'index.json'), JSON.stringify(out));
const f = s => (s.n ? `n=${s.n} 勝率 ${(s.winRate * 100).toFixed(0)}% 均報 ${(s.avgRet * 100).toFixed(3)}% 盈虧比 ${s.payoff?.toFixed(2)} PF ${s.profitFactor?.toFixed(2)}` : 'n=0');
console.log(`\n完成 ${index.length} 檔（失敗 ${failed}）`);
for (const [k, v] of Object.entries(out.summary.variants)) console.log(`${v.label.padEnd(22)} 全部 ${f(v.all)} | 多 ${f(v.long)} | 空 ${f(v.short)}`);
console.log(`前半段 ${f(out.summary.halves.first)}｜後半段 ${f(out.summary.halves.second)}`);
console.log('判斷分布', out.summary.verdicts);
