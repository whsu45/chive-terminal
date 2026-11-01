// 族群戰法損益排行：對 data/themes.json 全部成分股，以 assets/strategy-engine.js（與個股分析頁相同的引擎）
// 回測近 3 年各多方戰法，彙整出「賺最多」與「賠最多」的戰法，輸出 data/theme_strategies.json
//
// FinMind 免費額度有限（每檔需 4 次 API），所以採增量更新：每次最多重算 MAX_STOCKS 檔，
// 優先處理尚未計算、戰法清單已變動、或超過 REFRESH_DAYS 天未更新的個股；碰到額度上限就保存進度結束。
// 用法：node scripts/build_theme_strategies.mjs [--max 40]   （可設 FINMIND_TOKEN 提高 API 上限）
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const Engine = require(path.join(ROOT, 'assets/strategy-engine.js'));
const API = 'https://api.finmindtrade.com/api/v4/data';
const TOKEN = process.env.FINMIND_TOKEN || '';
const OUT_FILE = path.join(ROOT, 'data/theme_strategies.json');

const args = process.argv.slice(2);
const arg = (k, d) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : d; };
const MAX_STOCKS = Number(arg('--max', process.env.THEME_STRATEGY_MAX || 40));
const HISTORY_YEARS = 3;
const REFRESH_DAYS = 7;
const CAPITAL_PER_TRADE = 100000; // 每筆固定投入金額（元），損益 = 報酬率 × 本金
const MIN_TRADES_THEME = 3; // 族群內戰法至少 N 筆才列入最佳／最差

const BUY_SIGNALS = Engine.SIGNALS.filter(s => s.side === 'buy');
const SIG_KEY = BUY_SIGNALS.map(s => s.id).join(',');

const sleep = ms => new Promise(r => setTimeout(r, ms));
class QuotaError extends Error {}

async function fetchDataset(dataset, id, start) {
  const url = `${API}?dataset=${dataset}&data_id=${id}&start_date=${start}${TOKEN ? `&token=${TOKEN}` : ''}`;
  for (let attempt = 0; attempt < 2; attempt++) {
    const j = await (await fetch(url)).json().catch(() => ({}));
    if (j.status === 200) return j.data;
    if (/upper limit/i.test(j.msg || '')) throw new QuotaError(j.msg);
    if (attempt === 0) await sleep(3000);
    else throw new Error(j.msg || 'FinMind error');
  }
}

const isoYearsAgo = y => { const d = new Date(); d.setFullYear(d.getFullYear() - y); return d.toISOString().slice(0, 10); };

// 單檔：近 3 年各多方戰法的已平倉交易彙總
async function backtestStock(code) {
  const start = isoYearsAgo(HISTORY_YEARS + 1); // 多抓 1 年作指標暖機
  const prices = await fetchDataset('TaiwanStockPrice', code, start);
  const [institutional, margin, dividends] = await Promise.all([
    fetchDataset('TaiwanStockInstitutionalInvestorsBuySell', code, start),
    fetchDataset('TaiwanStockMarginPurchaseShortSale', code, start),
    fetchDataset('TaiwanStockDividendResult', code, start),
  ]);
  const bars = Engine.buildBars({ prices, institutional, margin, dividends });
  if (bars.length < 160) return { error: `資料不足（${bars.length} 個交易日）` };
  const bs = isoYearsAgo(HISTORY_YEARS);
  const res = Engine.analyze(bars, { startIdx: Math.max(0, bars.findIndex(b => b.date >= bs)) });
  const signals = {};
  for (const s of BUY_SIGNALS) {
    const closed = res.perSignal[s.id].trades.filter(t => !t.open);
    if (!closed.length) continue;
    const rets = closed.map(t => t.ret);
    signals[s.id] = {
      n: rets.length,
      wins: rets.filter(r => r > 0).length,
      sum: rets.reduce((a, b) => a + b, 0),
      gp: rets.filter(r => r > 0).reduce((a, b) => a + b, 0),
      gl: -rets.filter(r => r <= 0).reduce((a, b) => a + b, 0),
    };
  }
  return { from: bars[res.startIdx].date, asOf: res.date, signals };
}

// ---------- 讀取族群與既有結果 ----------
const themes = JSON.parse(fs.readFileSync(path.join(ROOT, 'data/themes.json'), 'utf8'));
let prev = {};
try { prev = JSON.parse(fs.readFileSync(OUT_FILE, 'utf8')).stocks || {}; } catch (e) { /* 首次執行 */ }

const members = new Map();
for (const t of themes.themes) for (const m of t.members) if (!members.has(m.code)) members.set(m.code, { code: m.code, name: m.name });

const ageDays = r => (Date.now() - Date.parse(r.computedAt)) / 86400000;
const priority = code => {
  const r = prev[code];
  if (!r) return 0; // 尚未計算
  if (r.sigKey !== SIG_KEY) return 1; // 戰法清單變動
  return ageDays(r) >= REFRESH_DAYS ? 2 : 9;
};
const queue = [...members.keys()]
  .map(code => ({ code, p: priority(code), at: prev[code]?.computedAt || '' }))
  .filter(x => x.p < 9)
  .sort((a, b) => a.p - b.p || (a.at < b.at ? -1 : 1))
  .slice(0, MAX_STOCKS);

console.log(`族群成分股 ${members.size} 檔，本次重算 ${queue.length} 檔（上限 ${MAX_STOCKS}）`);
const stocks = { ...prev };
let done = 0;
for (const { code } of queue) {
  try {
    const r = await backtestStock(code);
    stocks[code] = { name: members.get(code).name, computedAt: new Date().toISOString(), sigKey: SIG_KEY, ...r };
    done++;
    console.log(`  ${code} ${members.get(code).name}: ${r.error || Object.values(r.signals).reduce((a, s) => a + s.n, 0) + ' 筆交易'}`);
  } catch (e) {
    if (e instanceof QuotaError) { console.warn(`FinMind 額度已滿，保存進度後結束：${e.message}`); break; }
    console.warn(`  ${code} 失敗：${e.message}`);
  }
  await sleep(300);
}
// 已不在任何族群的個股移除
for (const code of Object.keys(stocks)) if (!members.has(code)) delete stocks[code];

// ---------- 彙整 ----------
const usable = Object.entries(stocks).filter(([code, r]) => r.sigKey === SIG_KEY && r.signals);
const emptyAgg = () => ({ n: 0, wins: 0, sum: 0, gp: 0, gl: 0, stocks: [] });
function finish(id, a) {
  const def = Engine.SIGNAL_MAP[id];
  return {
    id, name: def.name, ref: def.ref, standalone: !!def.standalone,
    n: a.n, stockCount: a.stocks.length,
    winRate: a.n ? a.wins / a.n : null, avgRet: a.n ? a.sum / a.n : null,
    profitFactor: a.gl > 0 ? a.gp / a.gl : (a.gp > 0 ? null : 0),
    profit: Math.round(a.sum * CAPITAL_PER_TRADE),
    topStocks: [...a.stocks].sort((x, y) => y.sum - x.sum).slice(0, 3).map(s => ({ ...s, profit: Math.round(s.sum * CAPITAL_PER_TRADE) })),
    bottomStocks: [...a.stocks].sort((x, y) => x.sum - y.sum).slice(0, 3).map(s => ({ ...s, profit: Math.round(s.sum * CAPITAL_PER_TRADE) })),
  };
}
function aggregate(codes) {
  const agg = Object.fromEntries(BUY_SIGNALS.map(s => [s.id, emptyAgg()]));
  for (const code of codes) {
    const r = stocks[code];
    if (!r || r.sigKey !== SIG_KEY || !r.signals) continue;
    for (const [id, s] of Object.entries(r.signals)) {
      const a = agg[id];
      a.n += s.n; a.wins += s.wins; a.sum += s.sum; a.gp += s.gp; a.gl += s.gl;
      a.stocks.push({ code, name: r.name, n: s.n, sum: s.sum });
    }
  }
  return Object.entries(agg).map(([id, a]) => finish(id, a));
}

const overall = aggregate(usable.map(([code]) => code)).filter(s => s.n > 0);
const byProfit = [...overall].sort((a, b) => b.profit - a.profit);
const themeRows = themes.themes.map(t => {
  const rows = aggregate(t.members.map(m => m.code)).filter(s => s.n > 0);
  const ranked = rows.filter(s => s.n >= MIN_TRADES_THEME).sort((a, b) => b.profit - a.profit);
  const covered = t.members.filter(m => stocks[m.code]?.sigKey === SIG_KEY && stocks[m.code]?.signals).length;
  return {
    slug: t.slug, title: t.title, avg_pct: t.avg_pct, members: t.members.length, covered,
    best: ranked[0] && ranked[0].profit > 0 ? ranked[0] : null,
    worst: ranked.length && ranked[ranked.length - 1].profit < 0 ? ranked[ranked.length - 1] : null,
    signals: rows.sort((a, b) => b.profit - a.profit).map(({ topStocks, bottomStocks, ...s }) => s),
  };
});

const asOfs = usable.map(([, r]) => r.asOf).sort();
const out = {
  generatedAt: new Date().toISOString(),
  quote_date: themes.quote_date,
  method: {
    historyYears: HISTORY_YEARS, capitalPerTrade: CAPITAL_PER_TRADE, refreshDays: REFRESH_DAYS, minTradesTheme: MIN_TRADES_THEME,
    rule: `族群成分股逐檔回測近 ${HISTORY_YEARS} 年，每個多方戰法依各自的出場規則交易，每筆固定投入 ${CAPITAL_PER_TRADE.toLocaleString()} 元，合計所有已平倉交易的損益（已扣手續費與證交稅）`,
  },
  coverage: { total: members.size, computed: usable.length, errors: Object.values(stocks).filter(r => r.error).length, asOfFrom: asOfs[0] || null, asOfTo: asOfs[asOfs.length - 1] || null },
  top: byProfit.filter(s => s.profit > 0).slice(0, 5),
  bottom: [...byProfit].reverse().filter(s => s.profit < 0).slice(0, 5),
  all: byProfit.map(({ topStocks, bottomStocks, ...s }) => s),
  themes: themeRows,
  stocks,
};
fs.writeFileSync(OUT_FILE, JSON.stringify(out));
console.log(`\n本次更新 ${done} 檔；已涵蓋 ${usable.length} / ${members.size} 檔`);
console.log('賺最多：', out.top.map(s => `${s.name} ${s.profit.toLocaleString()}`).join('、'));
console.log('賠最多：', out.bottom.map(s => `${s.name} ${s.profit.toLocaleString()}`).join('、'));
