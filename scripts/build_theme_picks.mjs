// 族群強弱勢個股挑選：讀取 data/themes.json，以當日漲跌初篩候選，
// 再以 assets/strategy-engine.js（與個股分析頁相同的策略引擎）逐檔判讀與回測，輸出 data/theme_picks.json
// 用法：node scripts/build_theme_picks.mjs   （可設 FINMIND_TOKEN 提高 API 上限）
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const Engine = require(path.join(ROOT, 'assets/strategy-engine.js'));
const API = 'https://api.finmindtrade.com/api/v4/data';
const TOKEN = process.env.FINMIND_TOKEN || '';

const STRONG_POOL = 15; // 當日漲幅前 N 名進入強勢候選
const WEAK_POOL = 12; // 當日跌幅前 N 名進入弱勢候選
const MIN_VOLUME = 500; // 成交量門檻（張），排除流動性不足個股
const STRONG_PICKS = 5;
const WEAK_PICKS = 3;
const HISTORY_YEARS = 3;

const sleep = ms => new Promise(r => setTimeout(r, ms));
const pct = (v, d = 1) => (v == null || !isFinite(v) ? 'NA' : `${v > 0 ? '+' : ''}${(v * 100).toFixed(d)}%`);
const lots = v => (v == null ? 'NA' : `${v > 0 ? '+' : ''}${Math.round(v).toLocaleString()} 張`);

async function fetchDataset(dataset, id, start) {
  const url = `${API}?dataset=${dataset}&data_id=${id}&start_date=${start}${TOKEN ? `&token=${TOKEN}` : ''}`;
  for (let attempt = 0; attempt < 2; attempt++) {
    const j = await (await fetch(url)).json().catch(() => ({}));
    if (j.status === 200) return j.data;
    if (attempt === 0) await sleep(3000);
    else throw new Error(j.msg || 'FinMind error');
  }
}

async function analyzeStock(code) {
  const d = new Date();
  d.setFullYear(d.getFullYear() - HISTORY_YEARS - 1);
  const start = d.toISOString().slice(0, 10);
  const prices = await fetchDataset('TaiwanStockPrice', code, start);
  const [institutional, margin, dividends] = await Promise.all([
    fetchDataset('TaiwanStockInstitutionalInvestorsBuySell', code, start).catch(() => []),
    fetchDataset('TaiwanStockMarginPurchaseShortSale', code, start).catch(() => []),
    fetchDataset('TaiwanStockDividendResult', code, start).catch(() => []),
  ]);
  const bars = Engine.buildBars({ prices, institutional, margin, dividends });
  if (bars.length < 160) return null;
  const bt = new Date();
  bt.setFullYear(bt.getFullYear() - HISTORY_YEARS);
  const bs = bt.toISOString().slice(0, 10);
  return Engine.analyze(bars, { startIdx: Math.max(0, bars.findIndex(b => b.date >= bs)) });
}

// 跨個股驗證（scripts/validate_strategy.mjs 產出）
let VALIDATION = null;
try { VALIDATION = JSON.parse(fs.readFileSync(path.join(ROOT, 'data/strategy_validation.json'), 'utf8')); } catch (e) { /* 無驗證檔 */ }

function crossText(s) {
  if (!VALIDATION) return '';
  if (s.side === 'buy') {
    const v = VALIDATION.buySignals[s.id]?.all;
    return v?.n ? `；跨 ${VALIDATION.stocks} 檔驗證 ${v.n} 次，勝率 ${(v.winRate * 100).toFixed(0)}%、PF ${v.profitFactor?.toFixed(2)}` : '';
  }
  const v = VALIDATION.sellSignals[s.id]?.h10;
  return v?.n ? `；跨 ${VALIDATION.stocks} 檔 10 日後下跌機率 ${(v.downRate * 100).toFixed(0)}%（基準 ${(VALIDATION.baseline.h10.downRate * 100).toFixed(0)}%）` : '';
}

// 可信度：觸發訊號在本股或跨個股驗證中是否具備統計優勢
function confidence(res, side) {
  const sigs = res.recent.filter(s => (side === 'strong' ? s.side === 'buy' : s.side === 'sell'));
  let level = 0;
  for (const s of sigs) {
    const ps = res.perSignal[s.id];
    if (side === 'strong') {
      const st = ps.stats, v = VALIDATION?.buySignals[s.id]?.all;
      if (st.n >= 5 && st.profitFactor >= 1.2) level = Math.max(level, 2);
      else if (v?.n >= 30 && v.profitFactor >= 1.5) level = Math.max(level, 2);
      else if (v?.n >= 30 && v.profitFactor >= 1.0) level = Math.max(level, 1);
    } else {
      const f = ps.fwd.h10, base = res.baseline.h10.downRate, v = VALIDATION?.sellSignals[s.id]?.h10;
      if (f.n >= 5 && f.downRate - base >= 0.05) level = Math.max(level, 2);
      else if (v?.n >= 30 && v.downRate - VALIDATION.baseline.h10.downRate >= 0.05) level = Math.max(level, 2);
      else if (f.n >= 5 && f.downRate > base) level = Math.max(level, 1);
    }
  }
  return level;
}
const CONF_LABEL = ['低', '中', '高'];

// 訊號在本股的歷史驗證摘要
function evidenceText(res, s) {
  const ps = res.perSignal[s.id];
  if (ps.side === 'buy') {
    const st = ps.stats;
    return st.n ? `本股歷史 ${st.n} 次，勝率 ${(st.winRate * 100).toFixed(0)}%、平均每筆 ${pct(st.avgRet, 2)}` : '本股歷史未曾觸發';
  }
  const f = ps.fwd.h10;
  return f.n ? `本股歷史 ${f.n} 次，10 日後下跌機率 ${(f.downRate * 100).toFixed(0)}%（基準 ${(res.baseline.h10.downRate * 100).toFixed(0)}%）` : '本股歷史未曾觸發';
}

const TREND_TXT = {
  RECOVERY: '回升架構：月線上揚且股價站上月線（多頭，回檔不破月線可續抱）',
  REBOUND: '反彈架構：月線下彎且股價在月線下（空頭，反彈是逃命點）',
  TRANSITION: '轉折過渡：月線方向與股價位置不一致',
};
const QUAD_TXT = { 1: '主力(法人)買＋月線上揚：順勢攻擊', 2: '主力(法人)賣＋月線上揚：利多出貨／假突破風險', 3: '主力(法人)買＋月線下彎：低檔抗跌', 4: '主力(法人)賣＋月線下彎：多殺多' };

function buildReasons(stock, res, side) {
  const r = [];
  r.push(`今日 ${stock.pct > 0 ? '+' : ''}${stock.pct.toFixed(2)}%，量 ${stock.volume.toLocaleString()} 張；所屬族群：${stock.themes.map(t => `${t.title}（平均 ${t.avg_pct > 0 ? '+' : ''}${t.avg_pct}%）`).join('、')}`);
  r.push(`策略綜合判斷「${res.verdict}」（分數 ${res.score > 0 ? '+' : ''}${res.score.toFixed(2)}）`);
  const sigs = res.recent.filter(s => (side === 'strong' ? s.side === 'buy' : s.side === 'sell'));
  for (const s of sigs.slice(0, 3)) r.push(`${s.daysAgo ? `${s.daysAgo} 日前` : '今日'}觸發「${s.name}」：${s.reasons[0]}｜${evidenceText(res, s)}${crossText(s)}`);
  const opp = res.recent.filter(s => (side === 'strong' ? s.side === 'sell' : s.side === 'buy'));
  if (opp.length) r.push(`⚠ 同時有反向訊號：${opp.map(s => s.name).join('、')}`);
  if (TREND_TXT[res.trend]) r.push(TREND_TXT[res.trend]);
  r.push(`${QUAD_TXT[res.quadrant]}（近 10 日 ${lots(res.mf.d10)}）`);
  if (res.bb.riding) r.push('股價沿布林上軌推升（山的左邊，空單禁入）');
  if (res.bb.strongTrend) r.push(`月線斜率 ${pct(res.bb.ma20Slope, 2)} ≥ 0.4%，屬強勢多頭推升`);
  if (res.margin.ratio != null && res.margin.ratio >= 0.3) r.push(`券資比 ${pct(res.margin.ratio)}，具軋空燃料`);
  const op = res.composite.openPos;
  if (op) r.push(op.pendingExit ? `策略部位今日觸發出場：${op.pendingExit}` : `策略部位持有中（${op.entryDate} 依「${op.name}」進場，停損 ${op.stop?.toFixed(2)}）`);
  return r;
}

const themes = JSON.parse(fs.readFileSync(path.join(ROOT, 'data/themes.json'), 'utf8'));

// 合併同一檔個股的所屬族群
const byCode = new Map();
for (const t of themes.themes) {
  for (const m of t.members) {
    if (m.pct == null) continue;
    const s = byCode.get(m.code) || { ...m, themes: [] };
    s.themes.push({ slug: t.slug, title: t.title, avg_pct: t.avg_pct });
    byCode.set(m.code, s);
  }
}
const liquid = [...byCode.values()].filter(s => s.volume >= MIN_VOLUME);
const strongPool = [...liquid].sort((a, b) => b.pct - a.pct).slice(0, STRONG_POOL);
const weakPool = [...liquid].sort((a, b) => a.pct - b.pct).slice(0, WEAK_POOL);

async function evaluate(pool, side) {
  const out = [];
  for (const s of pool) {
    try {
      const res = await analyzeStock(s.code);
      if (!res) { console.warn(`  ${s.code} 資料不足`); continue; }
      out.push({
        code: s.code, name: s.name, market: s.market, close: s.close, pct: s.pct, volume: s.volume,
        themes: s.themes.map(t => t.title), asOf: res.date, verdict: res.verdict, verdictKey: res.verdictKey,
        score: +res.score.toFixed(2), trend: res.trend, reasons: buildReasons(s, res, side),
        confidence: confidence(res, side), confidenceLabel: CONF_LABEL[confidence(res, side)],
      });
      console.log(`  ${s.code} ${s.name} ${s.pct}% → ${res.verdict} (${res.score.toFixed(2)})`);
    } catch (e) {
      console.warn(`  ${s.code} 失敗：${e.message}`);
    }
    await sleep(300);
  }
  return out;
}

console.log(`強勢候選 ${strongPool.length} 檔：`);
const strongEval = await evaluate(strongPool, 'strong');
console.log(`弱勢候選 ${weakPool.length} 檔：`);
const weakEval = await evaluate(weakPool, 'weak');

// 高機率強勢：策略判斷為買進以上，依分數排序；弱勢：分數最低前三
const strong = strongEval.filter(s => s.score >= 1.5).sort((a, b) => b.score - a.score || b.confidence - a.confidence || b.pct - a.pct).slice(0, STRONG_PICKS);
const weak = weakEval.sort((a, b) => a.score - b.score || b.confidence - a.confidence || a.pct - b.pct).slice(0, WEAK_PICKS);

const out = {
  generatedAt: new Date().toISOString(),
  quote_date: themes.quote_date,
  method: {
    strongPool: STRONG_POOL, weakPool: WEAK_POOL, minVolume: MIN_VOLUME, historyYears: HISTORY_YEARS,
    strongRule: '族群成分股當日漲幅前 15 名（量 ≥ 500 張）中，策略綜合分數 ≥ 1.5（買進以上），依分數取前 5',
    weakRule: '族群成分股當日跌幅前 12 名（量 ≥ 500 張）中，策略綜合分數最低的 3 檔',
    confidenceRule: '高：觸發訊號在本股（≥5 次）或跨個股驗證中具統計優勢（買訊 PF ≥1.2／1.5，賣訊下跌機率高於基準 5pp）；中：略優於基準；低：無統計優勢或樣本不足',
  },
  evaluated: { strong: strongEval.length, weak: weakEval.length },
  strong, weak,
  strongCandidates: strongEval.map(({ reasons, ...s }) => s),
};
fs.writeFileSync(path.join(ROOT, 'data/theme_picks.json'), JSON.stringify(out, null, 1));
console.log(`\n強勢 ${strong.length} 檔：${strong.map(s => `${s.code}${s.name}(${s.score})`).join('、') || '無'}`);
console.log(`弱勢 ${weak.length} 檔：${weak.map(s => `${s.code}${s.name}(${s.score})`).join('、')}`);
