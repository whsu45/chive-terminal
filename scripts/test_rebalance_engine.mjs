// 指數調整事件引擎單元測試：node --test scripts/test_rebalance_engine.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const R = require('../assets/rebalance-engine.js');

// 連續 60 個交易日（跳過週末），收盤 100 + i、開盤 = 收盤 − 0.5
function mkBars(n = 60) {
  const out = [];
  const d = new Date(Date.UTC(2024, 0, 1));
  while (out.length < n) {
    const wd = d.getUTCDay();
    if (wd !== 0 && wd !== 6) { const i = out.length; out.push({ date: d.toISOString().slice(0, 10), o: 99.5 + i, h: 101 + i, l: 99 + i, c: 100 + i, v: 1000 }); }
    d.setUTCDate(d.getUTCDate() + 1);
  }
  return out;
}
const near = (a, b) => assert.ok(Math.abs(a - b) < 1e-9, `${a} ≠ ${b}`);
const bars = mkBars();
// 公告日 = 第 30 根，生效交易日 = 第 40 根
const ev = { announceDate: bars[30].date, tradeDate: bars[40].date };

test('新增股：公告隔日開盤買進 → 生效日收盤賣出，扣成本', () => {
  const s = R.eventStages(bars, ev, 'add');
  assert.equal(s.stage23.from, bars[31].date);
  assert.equal(s.stage23.to, bars[40].date);
  near(s.stage23.entry, 99.5 + 31);
  near(s.stage23.exit, 140);
  near(s.stage23.ret, (140 * (1 - 0.001425 - 0.003) - 130.5 * 1.001425) / 130.5);
});

test('階段 1（事後上限）：公告前 15 個交易日開盤 → 公告日收盤', () => {
  const s = R.eventStages(bars, ev, 'add');
  assert.equal(s.stage1.from, bars[15].date);
  near(s.stage1.entry, 114.5); near(s.stage1.exit, 130);
});

test('階段 4：生效日收盤反手，新增股放空、刪除股做多，T+3 收盤回補', () => {
  const add = R.eventStages(bars, ev, 'add');
  near(add.stage4.entry, 140); near(add.stage4.exit, 143);
  assert.ok(add.stage4.ret < 0); // 股價續漲 → 放空虧損
  const del = R.eventStages(bars, ev, 'delete');
  assert.ok(del.stage4.ret > 0); // 刪除股反手做多
  assert.ok(del.stage23.ret < 0); // 刪除股公告後放空，股價上漲 → 虧損
  near(add.stage4_t1open.exit, 99.5 + 41);
});

test('超額報酬：個股漲跌扣除加權指數同期間漲跌，放空方向相反', () => {
  const mkt = Object.fromEntries(bars.map(b => [b.date, { o: 1000, c: 1010 }])); // 每段指數漲幅 = 1.01 / 1 − 1
  const s = R.eventStages(bars, ev, 'add', mkt);
  near(s.stage23.excess, (140 / 130.5 - 1) - 0.01);
  near(s.stage4.excess, -((143 / 140 - 1) - 0));
  assert.equal(R.eventStages(bars, ev, 'add').stage23.excess, undefined);
});

test('擁擠交易：公告前 20 日漲幅 > 30% 標記', () => {
  const b = mkBars().map((x, i) => (i >= 10 && i <= 30 ? { ...x, c: x.c * (1 + (i - 10) * 0.02) } : x));
  const s = R.eventStages(b, ev, 'add');
  assert.ok(s.preRun > 0.3);
  assert.equal(s.crowded, true);
  assert.equal(R.eventStages(bars, ev, 'add').crowded, false);
});

test('生效交易日不是交易日或價格不足時回傳錯誤', () => {
  assert.ok(R.eventStages(bars, { announceDate: bars[30].date, tradeDate: '2024-01-06' }, 'add').error); // 週六
  assert.ok(R.eventStages(bars.slice(0, 35), ev, 'add').error);
});

test('盤前公告：公告當天開盤進場，同日生效者為「開盤 → 收盤」', () => {
  const pre = R.eventStages(bars, { announceDate: bars[40].date, tradeDate: bars[40].date, announceTiming: 'preOpen' }, 'add');
  assert.equal(pre.stage23.from, bars[40].date);
  near(pre.stage23.entry, 139.5); near(pre.stage23.exit, 140);
  assert.equal(pre.stage1.to, bars[39].date); // 階段 1 到公告前最後一個收盤
});

test('名單在生效後才公開：不計公告後進場的階段，保留生效後的階段 4', () => {
  const late = R.eventStages(bars, { announceDate: bars[42].date, tradeDate: bars[40].date }, 'add');
  assert.equal(late.lateAnnounce, true);
  assert.equal(late.stage23, undefined);
  assert.equal(late.stage1, undefined);
  assert.ok(late.stage4);
  assert.ok(R.eventStages(bars, { announceDate: null, tradeDate: bars[40].date }, 'add').stage4);
});

test('交易計畫：以歷史中位數推估出場點位，已進場者計算目前損益', () => {
  const hist = { add: { stage23: { n: 10, median: 0.05, p25: 0.01, p75: 0.09 }, stage4: { n: 10, median: 0.02 } } };
  const p = R.plan(bars.slice(0, 35), ev, 'add', hist, bars[34].date);
  assert.equal(p.entered, true);
  near(p.entryRef, 130.5);
  near(p.exitMedian, 130.5 * 1.05);
  near(p.pnlNow, R.ret(1, 130.5, 134));
  const before = R.plan(bars.slice(0, 31), ev, 'add', hist, bars[30].date);
  assert.equal(before.entered, false);
  near(before.entryRef, 130); // 尚未進場 → 以最新收盤估算
});

test('Yahoo 日 K：套用還原權息係數並剔除零成交量 K 棒', () => {
  const b = R.barsFromYahoo({
    meta: { gmtoffset: 28800 }, timestamp: [Date.UTC(2024, 5, 3, 1) / 1000, Date.UTC(2024, 5, 4, 1) / 1000],
    indicators: { quote: [{ open: [100, 90], high: [101, 91], low: [99, 89], close: [100, 90], volume: [1000, 0] }], adjclose: [{ adjclose: [95, 90] }] },
  });
  assert.equal(b.length, 1);
  assert.equal(b[0].date, '2024-06-03');
  near(b[0].o, 95); near(b[0].c, 95); assert.equal(b[0].raw, 100);
});
