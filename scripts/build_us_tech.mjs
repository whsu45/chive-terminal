// 美股科技族群趨勢 × 台股連動：抓取美股族群成分股、基準指數與台股候選的日線，
// 計算族群漲跌、相關係數最高的台股、連動回測，並累積每日「漲跌前三族群」歷史。
// 輸出 data/us_tech.json（最新快照）與 data/us_tech_history.json（逐日累積）
// 用法：node scripts/build_us_tech.mjs
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const E = require(path.join(ROOT, 'assets/us-tech-engine.js'));

const CONFIG = JSON.parse(fs.readFileSync(path.join(ROOT, 'data/us_tech_config.json'), 'utf8'));
const OUT_FILE = path.join(ROOT, 'data/us_tech.json');
const HISTORY_FILE = path.join(ROOT, 'data/us_tech_history.json');
const THEMES_FILE = path.join(ROOT, 'data/themes.json');
const W = CONFIG.windows;

const YAHOO = 'https://query1.finance.yahoo.com/v8/finance/chart/';
const HEADERS = { 'User-Agent': 'Mozilla/5.0 (chive-terminal us-tech linkage)' };
const CONCURRENCY = 4;
const sleep = ms => new Promise(r => setTimeout(r, ms));

/** Yahoo 日線 → [{date, close}]，日期以交易所時區表示（台股 Asia/Taipei、美股 America/New_York） */
async function fetchDaily(symbol, range = '2y') {
  const url = `${YAHOO}${encodeURIComponent(symbol)}?interval=1d&range=${range}`;
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const resp = await fetch(url, { headers: HEADERS });
      if (resp.status === 429 || resp.status >= 500) { await sleep(1500 * (attempt + 1)); continue; }
      if (!resp.ok) return null;
      const j = await resp.json();
      const r = j?.chart?.result?.[0];
      if (!r?.timestamp?.length) return null;
      const tz = r.meta?.exchangeTimezoneName || 'UTC';
      const fmt = new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' });
      const closes = r.indicators?.quote?.[0]?.close || [];
      const bars = [];
      for (let i = 0; i < r.timestamp.length; i++) {
        const c = closes[i];
        if (c == null || !isFinite(c)) continue;
        const date = fmt.format(new Date(r.timestamp[i] * 1000));
        if (bars.length && bars[bars.length - 1].date === date) bars[bars.length - 1].close = c; // 同日重複 bar 取最後
        else bars.push({ date, close: c });
      }
      return bars.length > 5 ? bars : null;
    } catch (e) {
      await sleep(1000 * (attempt + 1));
    }
  }
  return null;
}

async function mapLimit(items, limit, fn) {
  const out = new Array(items.length);
  let next = 0;
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, async () => {
    while (next < items.length) {
      const i = next++;
      out[i] = await fn(items[i], i);
      await sleep(120);
    }
  }));
  return out;
}

/** 台股候選：設定檔名單 ＋ themes.json 中科技相關族群的成分股（流動性門檻） */
function buildTwUniverse() {
  const map = new Map();
  for (const s of CONFIG.twUniverse) if (!s.skip) map.set(s.code, { code: s.code, name: s.name, market: s.market, themes: [] });
  try {
    const themes = JSON.parse(fs.readFileSync(THEMES_FILE, 'utf8')).themes || [];
    for (const t of themes) {
      if (!CONFIG.twThemeKeywords.some(k => t.title.includes(k))) continue;
      for (const m of t.members) {
        if (!/^\d{4}$/.test(m.code)) continue; // 排除 ETF／特別股
        if ((m.volume ?? 0) < CONFIG.twMinVolume && !map.has(m.code)) continue;
        if (!map.has(m.code)) map.set(m.code, { code: m.code, name: m.name, market: m.market, themes: [] });
        map.get(m.code).themes.push(t.title);
      }
    }
  } catch (e) { console.log(`themes.json 不可用，僅使用設定檔名單：${e.message}`); }
  return [...map.values()].slice(0, CONFIG.twMaxSymbols);
}

async function fetchTw(stock) {
  const order = stock.market === '上櫃' ? ['.TWO', '.TW'] : ['.TW', '.TWO'];
  for (const suf of order) {
    const bars = await fetchDaily(`${stock.code}${suf}`);
    if (bars) return { ...stock, bars, yahoo: `${stock.code}${suf}` };
  }
  return null;
}

const round = (v, d = 2) => (v == null || !isFinite(v) ? null : Number(v.toFixed(d)));
const spark = (bars, n) => bars.slice(-n).map(b => round(b.close, 2));

function memberStats(m, bars) {
  return {
    symbol: m.symbol, name: m.name, close: round(bars[bars.length - 1].close), date: bars[bars.length - 1].date,
    pct: round(E.horizonPct(bars, 1)), pct5: round(E.horizonPct(bars, 5)), pct20: round(E.horizonPct(bars, 20)), pct60: round(E.horizonPct(bars, 60)),
  };
}

function seriesStats(series) {
  const idx = E.indexFromReturns(series);
  const bars = idx.map(p => ({ date: p.date, close: p.value }));
  return {
    date: series[series.length - 1].date,
    pct: round(E.horizonPct(bars, 1)), pct5: round(E.horizonPct(bars, 5)), pct20: round(E.horizonPct(bars, 20)), pct60: round(E.horizonPct(bars, 60)),
    spark: spark(bars, W.spark),
  };
}

function linkage(usSeries, twSeries) {
  const pairs = E.pairSeries(usSeries, twSeries);
  const hit = E.hitRate(pairs, W.main);
  const corr = E.corrWindow(pairs, W.main);
  const cond = E.conditional(pairs, W.long, 0.01);
  return {
    pairs: pairs.length,
    corr60: round(E.corrWindow(pairs, W.alt), 3), corr120: round(corr, 3), corr250: round(E.corrWindow(pairs, W.long), 3),
    beta120: round(E.betaWindow(pairs, W.main), 3),
    hit120: hit ? { n: hit.n, rate: round(hit.rate, 3) } : null,
    condUp: { n: cond.up.n, mean: round(cond.up.mean == null ? null : cond.up.mean * 100), sameDirRate: round(cond.up.sameDirRate, 3) },
    condDown: { n: cond.down.n, mean: round(cond.down.mean == null ? null : cond.down.mean * 100), sameDirRate: round(cond.down.sameDirRate, 3) },
    verdict: E.verdict({ corr, hit: hit ? hit.rate : null }),
  };
}

function top3(usSeries, twMap) {
  return E.rankTwByCorr(usSeries, twMap, { main: W.main, alt: W.alt, topN: 3 }).map(r => ({
    ...r, corr: round(r.corr, 3), corrAlt: round(r.corrAlt, 3), beta: round(r.beta, 2), hit: round(r.hit, 3),
    themes: twMap[r.code].themes.slice(0, 3),
  }));
}

async function main() {
  const t0 = Date.now();
  // 1. 美股成分股
  const usMembers = CONFIG.groups.flatMap(g => g.members.map(m => ({ ...m, group: g.key })));
  const usBars = await mapLimit(usMembers, CONCURRENCY, async m => ({ m, bars: await fetchDaily(m.symbol) }));
  const failed = usBars.filter(x => !x.bars).map(x => x.m.symbol);
  const barsBySymbol = Object.fromEntries(usBars.filter(x => x.bars).map(x => [x.m.symbol, x.bars]));
  console.log(`美股成分股 ${usMembers.length} 檔，失敗 ${failed.length}：${failed.join(', ') || '無'}`);

  // 2. 基準指數
  const bench = {};
  for (const b of CONFIG.benchmarks) {
    const bars = await fetchDaily(b.symbol);
    if (bars) bench[b.key] = { ...b, bars };
    else console.log(`基準 ${b.symbol} 抓取失敗`);
  }
  if (!bench.TWII) throw new Error('台股加權指數抓取失敗，無法計算連動');
  const twiiSeries = E.dailyReturns(bench.TWII.bars);

  // 3. 台股候選
  const universe = buildTwUniverse();
  const twFetched = (await mapLimit(universe, CONCURRENCY, fetchTw)).filter(Boolean);
  console.log(`台股候選 ${universe.length} 檔，成功 ${twFetched.length} 檔`);
  const twMap = {};
  for (const s of twFetched) {
    twMap[s.code] = {
      name: s.name, market: s.market, themes: s.themes, series: E.dailyReturns(s.bars),
      lastPct: round(E.horizonPct(s.bars, 1)), lastDate: s.bars[s.bars.length - 1].date,
    };
  }

  // 4. 族群統計
  const groups = [];
  const groupSeries = {};
  for (const g of CONFIG.groups) {
    const members = g.members.filter(m => barsBySymbol[m.symbol]);
    if (members.length < 2) { console.log(`族群 ${g.title} 成分股不足，略過`); continue; }
    const series = E.equalWeightSeries(members.map(m => E.dailyReturns(barsBySymbol[m.symbol])));
    groupSeries[g.key] = series;
    const stats = seriesStats(series);
    groups.push({
      key: g.key, title: g.title, desc: g.desc, twThemes: g.twThemes || [],
      ...stats,
      members: members.map(m => memberStats(m, barsBySymbol[m.symbol])).sort((a, b) => (b.pct ?? -99) - (a.pct ?? -99)),
      top3: top3(series, twMap),
      taiex: linkage(series, twiiSeries),
    });
  }
  groups.sort((a, b) => (b.pct ?? -99) - (a.pct ?? -99));
  groups.forEach((g, i) => { g.rank = i + 1; });

  // 5. 科技整體（全部族群等權）
  const composite = E.equalWeightSeries(Object.values(groupSeries), 0.6);
  const compositeStats = seriesStats(composite);
  const compositePairs = E.pairSeries(composite, twiiSeries);
  const strategy = E.followStrategy(compositePairs, W.long);

  const benchOut = {};
  for (const [k, b] of Object.entries(bench)) {
    benchOut[k] = { name: b.name, symbol: b.symbol, date: b.bars[b.bars.length - 1].date, close: round(b.bars[b.bars.length - 1].close), pct: round(E.horizonPct(b.bars, 1)), pct5: round(E.horizonPct(b.bars, 5)), pct20: round(E.horizonPct(b.bars, 20)), spark: spark(b.bars, W.spark) };
    if (k !== 'TWII') benchOut[k].taiex = linkage(E.dailyReturns(b.bars), twiiSeries);
  }

  // 6. 歷史累積：每個美股交易日的漲跌前三族群與台股隔日反應
  let history = [];
  try { history = JSON.parse(fs.readFileSync(HISTORY_FILE, 'utf8')); } catch (e) { /* 首次建立 */ }
  const byDate = new Map(history.map(h => [h.date, h]));
  const twiiByDate = Object.fromEntries(twiiSeries.map(r => [r.date, r.ret]));
  const pairByUs = Object.fromEntries(compositePairs.map(p => [p.usDate, p]));
  const groupRetByDate = {};
  for (const g of groups) for (const r of groupSeries[g.key]) (groupRetByDate[r.date] ||= []).push({ key: g.key, title: g.title, pct: round(r.ret * 100) });
  const usDates = composite.slice(-W.history).map(r => r.date);
  const twLastDate = bench.TWII.bars[bench.TWII.bars.length - 1].date;
  for (const d of usDates) {
    const rows = (groupRetByDate[d] || []).filter(r => r.pct != null).sort((a, b) => b.pct - a.pct);
    if (rows.length < 3) continue;
    const p = pairByUs[d];
    const prev = byDate.get(d) || {};
    byDate.set(d, {
      date: d,
      composite: round(composite.find(r => r.date === d)?.ret * 100),
      sox: bench.SOX ? round((E.dailyReturns(bench.SOX.bars).find(r => r.date === d)?.ret ?? NaN) * 100) : null,
      top3Up: rows.slice(0, 3), top3Down: rows.slice(-3).reverse(),
      // 台股連假時只配對最近一個美股交易日；更早的美股日若已被台股最新收盤日超過，標記為不配對
      taiexNext: p ? { date: p.twDate, pct: round(p.tw * 100) } : d < twLastDate ? { skipped: true } : prev.taiexNext ?? null,
    });
  }
  const mergedHistory = [...byDate.values()].sort((a, b) => (a.date < b.date ? 1 : -1));
  fs.writeFileSync(HISTORY_FILE, JSON.stringify(mergedHistory, null, 1));

  // 7. 輸出快照
  const out = {
    generatedAt: new Date().toISOString(),
    usDate: compositeStats.date, twDate: bench.TWII.bars[bench.TWII.bars.length - 1].date,
    windows: W, config: { groups: CONFIG.groups.length, usSymbols: usMembers.length, usFailed: failed, twCandidates: twFetched.length },
    benchmarks: benchOut,
    composite: { ...compositeStats, top3: top3(composite, twMap), taiex: linkage(composite, twiiSeries) },
    strategy: strategy ? { ...strategy, stratPct: round(strategy.stratPct), bhPct: round(strategy.bhPct), maxDDPct: round(strategy.maxDDPct), hitRate: round(strategy.hitRate, 3), curve: strategy.curve.map(c => ({ date: c.date, strat: round(c.strat), bh: round(c.bh) })) } : null,
    groups,
    historyCount: mergedHistory.length,
    elapsedSec: round((Date.now() - t0) / 1000, 1),
  };
  fs.writeFileSync(OUT_FILE, JSON.stringify(out, null, 1));
  console.log(`完成：美股 ${out.usDate}、台股 ${out.twDate}，族群 ${groups.length} 個，歷史 ${mergedHistory.length} 日，耗時 ${out.elapsedSec}s`);
}

main().catch(e => { console.error(e); process.exit(1); });
