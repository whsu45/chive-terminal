// 多檔個股歷史回測驗證：使用與網頁相同的 assets/strategy-engine.js
// 用法：node scripts/validate_strategy.mjs [--start 2018-01-01] [--stocks 2330,2317]
// 輸出：data/strategy_validation.json（stock.html 讀取顯示）
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const Engine = require(path.join(ROOT, 'assets/strategy-engine.js'));
const CACHE_DIR = process.env.CACHE_DIR || path.join(ROOT, '.cache', 'finmind');
const API = 'https://api.finmindtrade.com/api/v4/data';
const TOKEN = process.env.FINMIND_TOKEN || '';

// 權值股 + 中小型股 + 策略文件提及之案例股
const DEFAULT_STOCKS = [
  '2330', '2317', '2454', '2308', '2412', '2882', '2881', '2891', '1301', '1303',
  '2002', '2603', '3711', '2303', '2382', '2357', '3008', '2327', '3034', '2379',
  '3661', '6669', '3037', '2368', '3406', '6488', '5009', '9921', '1477', '2429',
  '3535', '5201', '6538', '4935', '3019', '2023', '2474', '3105', '6150', '3346',
  '4979', '3081', // 華星光、聯亞（上櫃光通訊）
];

const args = process.argv.slice(2);
const arg = (k, d) => { const i = args.indexOf(k); return i >= 0 ? args[i + 1] : d; };
const START = arg('--start', '2018-01-01');
const STOCKS = arg('--stocks', DEFAULT_STOCKS.join(',')).split(',');
const SPLIT = '2023-01-01'; // 前後期分段檢驗穩定度

const sleep = ms => new Promise(r => setTimeout(r, ms));

async function fetchDataset(dataset, id) {
  fs.mkdirSync(CACHE_DIR, { recursive: true });
  const f = path.join(CACHE_DIR, `${dataset}_${id}_${START}.json`);
  if (fs.existsSync(f)) return JSON.parse(fs.readFileSync(f, 'utf8'));
  const url = `${API}?dataset=${dataset}&data_id=${id}&start_date=${START}${TOKEN ? `&token=${TOKEN}` : ''}`;
  for (let attempt = 0; attempt < 3; attempt++) {
    const res = await fetch(url);
    const j = await res.json();
    if (j.status === 200) { fs.writeFileSync(f, JSON.stringify(j.data)); await sleep(400); return j.data; }
    console.warn(`  ${dataset} ${id}: ${j.msg}`);
    await sleep(5000 * (attempt + 1));
  }
  throw new Error(`fetch failed ${dataset} ${id}`);
}

// 加權指數：VCB「大盤在月線之上」輔助濾網
const MARKET = await fetchDataset('TaiwanStockPrice', 'TAIEX');

async function loadStock(id) {
  const [prices, institutional, margin, dividends, info] = await Promise.all([
    fetchDataset('TaiwanStockPrice', id),
    fetchDataset('TaiwanStockInstitutionalInvestorsBuySell', id),
    fetchDataset('TaiwanStockMarginPurchaseShortSale', id),
    fetchDataset('TaiwanStockDividendResult', id),
    fetchDataset('TaiwanStockInfo', id),
  ]);
  return { bars: Engine.buildBars({ prices, institutional, margin, dividends, market: MARKET }), name: info[0]?.stock_name || id };
}

const pool = () => ({ all: [], early: [], late: [] });
const buyPool = {}, sellPool = {}, basePool = { h5: [0, 0], h10: [0, 0], h20: [0, 0] };
const composite = [];
// 對照組：VCB 只用 XScript 核心條件（不加投信／大盤輔助濾網）
const variantPool = { VCB_CORE: pool() };
const vcbExitReasons = {};
for (const s of Engine.SIGNALS) { if (s.side === 'buy') buyPool[s.id] = pool(); else sellPool[s.id] = { h5: [0, 0, 0], h10: [0, 0, 0], h20: [0, 0, 0], n: 0 }; }

for (const id of STOCKS) {
  let stock;
  try { stock = await loadStock(id); } catch (e) { console.error(`skip ${id}: ${e.message}`); continue; }
  const { bars, name } = stock;
  if (bars.length < 300) { console.warn(`skip ${id}: only ${bars.length} bars`); continue; }
  const res = Engine.analyze(bars);
  for (const [sid, ps] of Object.entries(res.perSignal)) {
    if (ps.side === 'buy') {
      for (const t of ps.trades.filter(t => !t.open)) {
        buyPool[sid].all.push(t.ret);
        (t.entryDate < SPLIT ? buyPool[sid].early : buyPool[sid].late).push(t.ret);
      }
    } else {
      const sp = sellPool[sid];
      sp.n += ps.fwd.n;
      for (const H of ['h5', 'h10', 'h20']) { const o = ps.fwd[H]; sp[H][0] += o.n; sp[H][1] += o.down; sp[H][2] += o.sum; }
    }
  }
  for (const t of res.perSignal.VCB.trades.filter(t => !t.open)) vcbExitReasons[t.reason] = (vcbExitReasons[t.reason] || 0) + 1;
  for (const t of Engine.analyze(bars, { vcbAux: false }).perSignal.VCB.trades.filter(t => !t.open)) {
    variantPool.VCB_CORE.all.push(t.ret);
    (t.entryDate < SPLIT ? variantPool.VCB_CORE.early : variantPool.VCB_CORE.late).push(t.ret);
  }
  for (const H of ['h5', 'h10', 'h20']) { const o = res.baseline[H]; basePool[H][0] += o.n; basePool[H][1] += Math.round(o.downRate * o.n); }
  const c = res.composite;
  composite.push({
    id, name, from: bars[res.startIdx].date, to: bars[bars.length - 1].date,
    trades: c.stats.n, winRate: c.stats.winRate ?? null, avgRet: c.stats.avgRet ?? null,
    totalRet: c.totalRet, mdd: c.mdd, buyHold: c.buyHold, buyHoldMdd: c.buyHoldMdd, exposure: c.exposure,
    verdict: res.verdict, date: res.date,
  });
  console.log(`${id} ${name}: 綜合 ${(c.totalRet * 100).toFixed(1)}% (MDD ${(c.mdd * 100).toFixed(1)}%) vs 買進持有 ${(c.buyHold * 100).toFixed(1)}% (MDD ${(c.buyHoldMdd * 100).toFixed(1)}%), ${c.stats.n} 筆, 今日：${res.verdict}`);
}

function stat(rets) {
  if (!rets.length) return { n: 0 };
  const s = [...rets].sort((a, b) => a - b);
  const wins = rets.filter(r => r > 0);
  const gp = wins.reduce((a, b) => a + b, 0), gl = -rets.filter(r => r <= 0).reduce((a, b) => a + b, 0);
  return { n: rets.length, winRate: wins.length / rets.length, avgRet: rets.reduce((a, b) => a + b, 0) / rets.length, median: s[Math.floor(s.length / 2)], profitFactor: gl ? gp / gl : null };
}

const out = {
  generatedAt: new Date().toISOString(), start: START, split: SPLIT, stocks: composite.length,
  cost: Engine.COST,
  buySignals: Object.fromEntries(Object.entries(buyPool).map(([id, p]) => [id, { name: Engine.SIGNAL_MAP[id].name, all: stat(p.all), early: stat(p.early), late: stat(p.late) }])),
  sellSignals: Object.fromEntries(Object.entries(sellPool).map(([id, p]) => [id, {
    name: Engine.SIGNAL_MAP[id].name, n: p.n,
    ...Object.fromEntries(['h5', 'h10', 'h20'].map(H => [H, { n: p[H][0], downRate: p[H][0] ? p[H][1] / p[H][0] : null, avg: p[H][0] ? p[H][2] / p[H][0] : null }])),
  }])),
  variants: { VCB_CORE: { name: 'VCB 僅核心條件（無投信／大盤濾網）', ...Object.fromEntries(['all', 'early', 'late'].map(k => [k, stat(variantPool.VCB_CORE[k])])) } },
  vcbExitReasons,
  baseline: Object.fromEntries(['h5', 'h10', 'h20'].map(H => [H, { n: basePool[H][0], downRate: basePool[H][1] / basePool[H][0] }])),
  composite,
};
const med = a => { const s = [...a].sort((x, y) => x - y); return s[Math.floor(s.length / 2)]; };
out.compositeSummary = {
  medianRet: med(composite.map(c => c.totalRet)), medianBuyHold: med(composite.map(c => c.buyHold)),
  medianMdd: med(composite.map(c => c.mdd)), medianBuyHoldMdd: med(composite.map(c => c.buyHoldMdd)),
  medianExposure: med(composite.map(c => c.exposure)),
  beatBuyHold: composite.filter(c => c.totalRet > c.buyHold).length,
  shallowerMdd: composite.filter(c => c.mdd > c.buyHoldMdd).length,
  positive: composite.filter(c => c.totalRet > 0).length,
};
fs.writeFileSync(path.join(ROOT, 'data', 'strategy_validation.json'), JSON.stringify(out, null, 1));

console.log('\n==== 多方訊號（合併所有個股，扣除手續費與證交稅）====');
for (const [id, v] of Object.entries(out.buySignals)) {
  const f = s => s.n ? `n=${s.n} 勝率 ${(s.winRate * 100).toFixed(0)}% 均報 ${(s.avgRet * 100).toFixed(2)}% PF ${s.profitFactor?.toFixed(2)}` : 'n=0';
  console.log(`${v.name.padEnd(18)} 全期 ${f(v.all)} | 前期 ${f(v.early)} | 後期 ${f(v.late)}`);
}
for (const v of Object.values(out.variants)) {
  const f = s => s.n ? `n=${s.n} 勝率 ${(s.winRate * 100).toFixed(0)}% 均報 ${(s.avgRet * 100).toFixed(2)}% PF ${s.profitFactor?.toFixed(2)}` : 'n=0';
  console.log(`[對照] ${v.name} 全期 ${f(v.all)} | 前期 ${f(v.early)} | 後期 ${f(v.late)}`);
}
console.log('VCB 出場原因：', JSON.stringify(vcbExitReasons));
console.log('\n==== 賣出訊號（事後下跌機率 vs 基準）====');
const b = out.baseline;
console.log(`基準（任意日）            5日 ${(b.h5.downRate * 100).toFixed(0)}%  10日 ${(b.h10.downRate * 100).toFixed(0)}%  20日 ${(b.h20.downRate * 100).toFixed(0)}%`);
for (const [id, v] of Object.entries(out.sellSignals)) {
  const f = o => o.n ? `${(o.downRate * 100).toFixed(0)}% (${(o.avg * 100).toFixed(1)}%)` : 'NA';
  console.log(`${v.name.padEnd(18)} n=${v.n}  5日 ${f(v.h5)}  10日 ${f(v.h10)}  20日 ${f(v.h20)}`);
}
console.log('\n==== 綜合策略 ====');
console.log(JSON.stringify(out.compositeSummary, null, 1));
