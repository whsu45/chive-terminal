// 高股息 ETF 換股前瞻模型（規格書「階段 1：量化預測期」）：用證交所／櫃買中心公開資料篩出潛在納入候選
//   條件一：近 60 日平均成交金額 ≥ 1.5 億元
//   條件二：獲利品質，最新累計（年初至今）EPS 與營業利益皆為正（金融業以稅前淨利代替營業利益）
//   條件三：殖利率 ≥ 6.5%（證交所／櫃買公布的殖利率，以最近一次現金股利 ÷ 股價計算，作為預估殖利率的近似值）
//   條件四：上市櫃市值排名 ≤ 200
// 輸出 data/index_model.json；每個台灣日最多重算一次（需抓 60 個交易日的全市場行情，約 3～4 分鐘）
// 用法：node scripts/build_index_model.mjs [--force]
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const OUT_FILE = path.join(ROOT, 'data/index_model.json');
const CACHE_DIR = path.join(ROOT, '.cache', 'market_daily');
const HEADERS = { 'User-Agent': 'Mozilla/5.0 (chive-terminal index model)' };
const RULES = { minTurnover: 150000000, minYield: 6.5, maxRank: 200, days: 60 };

const sleep = ms => new Promise(r => setTimeout(r, ms));
const num = v => { const x = parseFloat(String(v ?? '').replace(/,/g, '')); return isFinite(x) ? x : null; };
const twToday = () => new Date(Date.now() + 8 * 3600 * 1000).toISOString().slice(0, 10);

if (!process.argv.includes('--force')) {
  try {
    const prev = JSON.parse(fs.readFileSync(OUT_FILE, 'utf8'));
    if (prev.builtOn === twToday()) { console.log(`今日（${twToday()}）已產生模型，略過。`); process.exit(0); }
  } catch (e) { /* 首次執行 */ }
}

async function getJson(url, tries = 3) {
  for (let k = 0; k < tries; k++) {
    try {
      const res = await fetch(url, { headers: HEADERS });
      if (res.ok) return await res.json();
    } catch (e) { /* 重試 */ }
    await sleep(3000 * (k + 1));
  }
  return null;
}

// 單日全市場行情（上市 + 上櫃）：{ code: { name, value, close, shares? } }，非交易日回傳 null
async function dailyAll(d) {
  const ymd = d.replace(/-/g, '');
  fs.mkdirSync(CACHE_DIR, { recursive: true });
  const f = path.join(CACHE_DIR, `${ymd}.json`);
  if (fs.existsSync(f)) return JSON.parse(fs.readFileSync(f, 'utf8'));
  const tw = await getJson(`https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date=${ymd}&type=ALLBUT0999&response=json`);
  await sleep(1500);
  const table = tw?.stat === 'OK' && tw.tables?.find(t => t.fields?.includes('證券代號'));
  if (!table) return null;
  const out = {};
  const fi = k => table.fields.indexOf(k);
  for (const r of table.data) out[r[fi('證券代號')]] = { name: r[fi('證券名稱')], value: num(r[fi('成交金額')]), close: num(r[fi('收盤價')]), market: '上市' };
  const tp = await getJson(`https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes?date=${d.replace(/-/g, '%2F')}&id=&response=json`);
  await sleep(1500);
  const t2 = tp?.tables?.[0];
  if (t2?.fields) {
    const gi = k => t2.fields.indexOf(k);
    for (const r of t2.data) out[r[gi('代號')]] = { name: r[gi('名稱')], value: num(r[gi('成交金額(元)')]), close: num(r[gi('收盤')]), shares: num(r[gi('發行股數')]), market: '上櫃' };
  }
  fs.writeFileSync(f, JSON.stringify(out));
  return out;
}

// ---------- 近 60 個交易日 ----------
const days = [];
for (let d = new Date(Date.now() + 8 * 3600 * 1000); days.length < RULES.days; d.setUTCDate(d.getUTCDate() - 1)) {
  if ([0, 6].includes(d.getUTCDay())) continue;
  const ds = d.toISOString().slice(0, 10);
  const q = await dailyAll(ds);
  if (q && Object.keys(q).length > 500) { days.push({ date: ds, q }); process.stdout.write(`\r行情 ${days.length}/${RULES.days} ${ds}`); }
  if (Date.now() - d.getTime() > 150 * 86400000) break; // 資料來源異常時不要無限往回找
}
console.log('');
if (days.length < RULES.days) { console.error(`只取得 ${days.length} 個交易日，放棄`); process.exit(1); }
const latest = days[0];

// ---------- 基本資料、殖利率、獲利 ----------
const twBasic = await getJson('https://openapi.twse.com.tw/v1/opendata/t187ap03_L') || [];
const twYield = await getJson('https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL') || [];
const tpYield = await getJson('https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis') || [];
const income = {};
for (const [mkt, base] of [['L', 'https://openapi.twse.com.tw/v1/opendata/t187ap06_L_'], ['O', 'https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap06_O_']]) {
  for (const kind of ['ci', 'basi', 'bd', 'fh', 'ins', 'mim']) {
    const rows = await getJson(base + kind, 1) || [];
    for (const r of rows) {
      const code = r['公司代號'] || r.SecuritiesCompanyCode;
      const op = num(r['營業利益（損失）']);
      income[code] = {
        eps: num(r['基本每股盈餘（元）']), op: op ?? num(r['稅前淨利（淨損）']) ?? num(r['繼續營業單位稅前淨利（淨損）']) ?? num(r['繼續營業單位稅前損益']),
        opLabel: op != null ? '營業利益' : '稅前淨利', period: `${(r['年度'] || r.Year)} 年 Q${r['季別'] || r.Season}（累計）`,
      };
    }
    await sleep(500);
  }
}
const shares = {};
for (const r of twBasic) {
  const par = num(String(r['普通股每股面額']).replace(/[^0-9.]/g, '')) || 10;
  const cap = num(r['實收資本額']);
  if (cap) shares[r['公司代號']] = (cap - (num(r['特別股']) || 0) * par) / par;
}
for (const [code, x] of Object.entries(latest.q)) if (x.shares) shares[code] = x.shares;
const yieldOf = {};
for (const r of twYield) yieldOf[r.Code] = num(r.DividendYield);
for (const r of tpYield) yieldOf[r.SecuritiesCompanyCode] = num(r.YieldRatio);

// ---------- 篩選 ----------
const stocks = [];
for (const [code, x] of Object.entries(latest.q)) {
  if (!/^[1-9]\d{3}$/.test(code) || !x.close || !shares[code]) continue; // 只看一般普通股（排除 ETF、權證、特別股）
  const vals = days.map(d => d.q[code]?.value || 0);
  stocks.push({
    code, name: x.name.trim(), market: x.market, close: x.close,
    mktCap: x.close * shares[code], turnover60: vals.reduce((a, b) => a + b, 0) / days.length,
    yield: yieldOf[code] ?? null, ...(income[code] || {}),
  });
}
stocks.sort((a, b) => b.mktCap - a.mktCap);
stocks.forEach((s, i) => { s.rank = i + 1; });
for (const s of stocks) {
  s.checks = {
    turnover: s.turnover60 >= RULES.minTurnover,
    profit: s.eps != null && s.op != null && s.eps > 0 && s.op > 0,
    yield: s.yield != null && s.yield >= RULES.minYield,
    rank: s.rank <= RULES.maxRank,
  };
  s.passed = Object.values(s.checks).filter(Boolean).length;
}
const pick = s => ({
  code: s.code, name: s.name, market: s.market, close: s.close, rank: s.rank, mktCap: Math.round(s.mktCap / 1e8), // 億元
  turnover60: Math.round(s.turnover60 / 1e6) / 100, // 億元
  yield: s.yield, eps: s.eps, op: s.op, opLabel: s.opLabel, period: s.period, checks: s.checks,
});
const candidates = stocks.filter(s => s.passed === 4).sort((a, b) => b.yield - a.yield).map(pick);
const nearMiss = stocks.filter(s => s.passed === 3)
  .sort((a, b) => (b.yield ?? 0) - (a.yield ?? 0)).slice(0, 15).map(pick);

const out = {
  builtOn: twToday(), generatedAt: new Date().toISOString(), quoteDate: latest.date, from: days[days.length - 1].date,
  rules: RULES, universe: stocks.length, candidates, nearMiss,
};
fs.writeFileSync(OUT_FILE, JSON.stringify(out, null, 1));
console.log(`上市櫃普通股 ${stocks.length} 檔；四項全過 ${candidates.length} 檔，差一項 ${nearMiss.length} 檔`);
console.log(candidates.slice(0, 15).map(s => `${s.code}${s.name} ${s.yield}% 第${s.rank}名`).join('、'));
