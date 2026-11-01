// ORB 當沖引擎單元測試：node --test scripts/test_orb_engine.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const O = require('../assets/orb-engine.js');

// 09:00～13:20 的 5 分 K；預設開盤區間高 101、低 99，之後在 100 附近小幅震盪
const TIMES = [];
for (let m = 9 * 60; m <= 13 * 60 + 20; m += 5) TIMES.push(`${String(Math.floor(m / 60)).padStart(2, '0')}:${String(m % 60).padStart(2, '0')}`);
function session(over = {}, date = '2026-10-01') {
  const bars = TIMES.map((time, i) => {
    if (i < 6) return { time, o: 100, h: i === 2 ? 101 : 100.5, l: i === 4 ? 99 : 99.5, c: 100, v: i === 0 ? 0 : 1000 };
    return { time, o: 100, h: 100.3, l: 99.7, c: 100, v: 1000 };
  });
  for (const [t, b] of Object.entries(over)) { const i = TIMES.indexOf(t); bars[i] = { ...bars[i], ...b }; }
  return { date, bars };
}
const near = (a, b) => assert.ok(Math.abs(a - b) < 1e-9, `${a} ≠ ${b}`);
const run = (s, opts = {}, ctx = {}) => O.simulateDay(s, { narrowFilter: false, marketFilter: false, ...opts }, ctx);

test('開盤區間：09:00～09:30 高低點、觸發價 = 高/低 ± 0.2 × Range，平均量排除 0 量 K 棒', () => {
  const d = run(session());
  near(d.or.high, 101); near(d.or.low, 99); near(d.or.range, 2);
  near(d.or.longTrig, 101.4); near(d.or.shortTrig, 98.6); near(d.or.mid, 100);
  near(d.or.avgVol, 1000); // 09:00 的 0 量不計
  assert.equal(d.trades.length, 0);
});

test('多單：收盤帶量突破觸發價進場，跌破中軸／−1% 停損', () => {
  const d = run(session({ '09:45': { h: 101.6, c: 101.5, v: 2000 }, '09:50': { o: 101.4, h: 101.5, l: 100.2, c: 100.4 } }));
  assert.equal(d.trades.length, 1);
  const t = d.trades[0];
  assert.equal(t.side, 'long'); assert.equal(t.entryTime, '09:45'); assert.equal(t.entryPx, 101.5);
  near(t.exitPx, 101.5 * 0.99); // max(中軸 100, 進場 −1%)
  assert.equal(t.reason, '停損');
  near(t.ret, (101.5 * 0.99 * (1 - 0.001425 - 0.0015) - 101.5 * 1.001425) / 101.5);
});

test('量能不足（≤ 1.5 倍開盤區間均量）不進場', () => {
  assert.equal(run(session({ '09:45': { h: 101.6, c: 101.5, v: 1500 } })).trades.length, 0);
});

test('移動停利：浮盈達 1.5 × Range 後以最近 3 根低點與保本價防守', () => {
  const d = run(session({
    '09:45': { h: 101.6, c: 101.5, v: 2000 },
    '09:50': { o: 101.5, h: 104.6, l: 101.4, c: 104.4 }, // 浮盈 3.1 ≥ 3 → 啟動；防守 = max(保本 101.5, 近 3 根低點)
    '09:55': { o: 104.4, h: 105, l: 104, c: 104.8 },
    '10:00': { o: 104.8, h: 105.2, l: 104.5, c: 105 },
    '10:05': { o: 104.9, h: 105, l: 103.5, c: 103.8 }, // 收盤後防守價 = 09:55～10:05 最低 103.5
    '10:10': { o: 103.8, h: 103.9, l: 103.2, c: 103.3 },
  }));
  const t = d.trades[0];
  near(t.exitPx, 103.5);
  assert.equal(t.exitTime, '10:10');
  assert.equal(t.reason, '移動停利');
});

test('13:20 強制平倉：以 13:15 收盤價出場', () => {
  const d = run(session({ '13:05': { h: 101.6, c: 101.5, v: 2000 }, '13:10': { o: 101.5, h: 101.8, l: 101.2, c: 101.6 }, '13:15': { o: 101.6, h: 101.9, l: 101.3, c: 101.7 } }));
  assert.equal(d.trades[0].exitTime, '13:15');
  assert.equal(d.trades[0].exitPx, 101.7);
  assert.equal(d.trades[0].reason, '13:20 強制平倉');
});

test('13:15 那根缺資料時，以之後第一根 K 棒平倉', () => {
  const s = session({ '13:05': { h: 101.6, c: 101.5, v: 2000 }, '13:10': { o: 101.5, h: 101.8, l: 101.2, c: 101.6 }, '13:20': { o: 101.6, h: 101.9, l: 101.3, c: 101.8 } });
  s.bars = s.bars.filter(b => b.time !== '13:15');
  const t = run(s).trades[0];
  assert.equal(t.exitTime, '13:20');
  assert.equal(t.exitPx, 101.8);
});

test('空單：帶量跌破空方觸發價，報酬 = 先賣後買扣成本', () => {
  const s = session({ '10:00': { l: 98.4, c: 98.5, v: 2000 } });
  s.bars = s.bars.map(b => (b.time > '10:00' ? { ...b, o: 98, h: 98.2, l: 97.8, c: 98 } : b)); // 停損 99.485 未觸及
  const d = run(s);
  const t = d.trades[0];
  assert.equal(t.side, 'short'); assert.equal(t.exitPx, 98);
  near(t.ret, (98.5 * (1 - 0.001425 - 0.0015) - 98 * 1.001425) / 98.5);
});

test('避坑濾網：開盤窄幅暫停進場、指數翻黑不做多', () => {
  const s = session({ '09:45': { h: 101.6, c: 101.5, v: 2000 } });
  const narrow = O.simulateDay(s, { marketFilter: false }, { atrPrev: 20 }); // Range 2 < 20 × 15%
  assert.match(narrow.skip, /開盤窄幅/);
  assert.equal(O.simulateDay(s, { marketFilter: false }, { atrPrev: 10 }).trades.length, 1);
  assert.equal(O.simulateDay(s, { narrowFilter: false }, { tone: { '09:45': -1 } }).trades.length, 0);
  assert.equal(O.simulateDay(s, { narrowFilter: false }, { tone: { '09:45': 1 } }).trades.length, 1);
});

test('盤中資料（未到 13:20）：部位標示為持有中且不計入回測', () => {
  const s = session({ '09:45': { h: 101.6, c: 101.5, v: 2000 } });
  s.bars = s.bars.filter(b => b.time <= '10:30').map(b => (b.time > '09:45' ? { ...b, o: 101.5, h: 101.7, l: 101.3, c: 101.5 } : b));
  const r = O.backtest([s], { narrowFilter: false, marketFilter: false });
  assert.equal(r.days[0].complete, false);
  assert.equal(r.days[0].trades[0].open, true);
  assert.equal(r.stats.n, 0);
});

test('Yahoo 資料：轉台灣時間、剔除空值 K 棒與休市日', () => {
  const t0 = Date.UTC(2026, 9, 22, 1, 0) / 1000; // 台灣 09:00
  const t1 = Date.UTC(2026, 9, 23, 1, 0) / 1000; // 休市日（全為空值）
  const s = O.sessionsFromYahoo({
    meta: { gmtoffset: 28800 }, timestamp: [t0, t0 + 300, t1],
    indicators: { quote: [{ open: [1, null, null], high: [2, null, null], low: [0.5, null, null], close: [1.5, null, null], volume: [0, null, null] }] },
  });
  assert.equal(s.length, 1);
  assert.equal(s[0].date, '2026-10-22');
  assert.equal(s[0].bars[0].time, '09:00');
});
