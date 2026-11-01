// 美股科技族群連動引擎單元測試：node --test scripts/test_us_tech_engine.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const E = require('../assets/us-tech-engine.js');

const near = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} ≠ ${b}`);

// 產生 n 個平日日期（跳過週末）
function weekdays(n, start = Date.UTC(2025, 0, 1)) {
  const out = [];
  const d = new Date(start);
  while (out.length < n) {
    if (d.getUTCDay() !== 0 && d.getUTCDay() !== 6) out.push(d.toISOString().slice(0, 10));
    d.setUTCDate(d.getUTCDate() + 1);
  }
  return out;
}

test('dailyReturns / horizonPct', () => {
  const bars = [100, 110, 99, 120].map((c, i) => ({ date: `2025-01-0${i + 1}`, close: c }));
  const r = E.dailyReturns(bars);
  assert.equal(r.length, 3);
  near(r[0].ret, 0.1);
  near(E.horizonPct(bars, 1), (120 / 99 - 1) * 100);
  near(E.horizonPct(bars, 3), 20);
  assert.equal(E.horizonPct(bars, 10), null);
});

test('equalWeightSeries 等權平均且遵守覆蓋率門檻', () => {
  const a = [{ date: 'd1', ret: 0.1 }, { date: 'd2', ret: 0.2 }];
  const b = [{ date: 'd1', ret: -0.1 }];
  const s = E.equalWeightSeries([a, b], 1); // 需全部成分股都有資料
  assert.deepEqual(s, [{ date: 'd1', ret: 0 }]);
  const s2 = E.equalWeightSeries([a, b], 0.5);
  assert.equal(s2.length, 2);
  near(s2[1].ret, 0.2);
});

test('pairSeries：美股 t → 之後第一個台股交易日；台股連假只配最近一個美股日', () => {
  const us = [
    { date: '2025-01-02', ret: 0.01 }, { date: '2025-01-03', ret: 0.02 }, // 週四、週五
    { date: '2025-01-06', ret: 0.03 }, { date: '2025-01-07', ret: 0.04 },
  ];
  // 台股 1/6 休市：1/7 應只對到美股 1/6，美股 1/3 不配對
  const tw = [{ date: '2025-01-03', ret: 0.1 }, { date: '2025-01-07', ret: 0.2 }, { date: '2025-01-08', ret: 0.3 }];
  const p = E.pairSeries(us, tw);
  assert.deepEqual(p.map(x => [x.usDate, x.twDate]), [['2025-01-02', '2025-01-03'], ['2025-01-06', '2025-01-07'], ['2025-01-07', '2025-01-08']]);
  near(p[1].us, 0.03); near(p[1].tw, 0.2);
});

test('pearson 與 corrWindow / betaWindow / hitRate', () => {
  const days = weekdays(80);
  const us = days.map((d, i) => ({ date: d, ret: Math.sin(i) * 0.01 }));
  const twSame = days.map((d, i) => ({ date: d, ret: Math.sin(i - 1) * 0.02 })); // 台股隔日 = 美股前一日 × 2
  const pairs = E.pairSeries(us, twSame);
  near(E.corrWindow(pairs, 60), 1, 1e-9);
  near(E.betaWindow(pairs, 60), 2, 1e-9);
  const hit = E.hitRate(pairs, 60);
  assert.equal(hit.rate, 1);
  const twInv = days.map((d, i) => ({ date: d, ret: -Math.sin(i - 1) * 0.02 }));
  near(E.corrWindow(E.pairSeries(us, twInv), 60), -1, 1e-9);
  assert.equal(E.corrWindow(pairs, 10), null, '不足 MIN_PAIRS 回傳 null');
});

test('conditional 只統計超過門檻的美股日', () => {
  const days = weekdays(50);
  const us = days.map((d, i) => ({ date: d, ret: i % 2 ? 0.02 : -0.02 }));
  const tw = days.map((d, i) => ({ date: d, ret: (i - 1) % 2 ? 0.01 : -0.01 })); // 隔日同向
  const c = E.conditional(E.pairSeries(us, tw), 250, 0.01);
  assert.ok(c.up.n > 0 && c.down.n > 0);
  near(c.up.mean, 0.01); near(c.up.sameDirRate, 1);
  near(c.down.mean, -0.01); near(c.down.sameDirRate, 1);
});

test('followStrategy：完全同向時策略 > 買進持有，且命中率 100%', () => {
  const days = weekdays(60);
  const us = days.map((d, i) => ({ date: d, ret: i % 2 ? 0.01 : -0.01 }));
  const tw = days.map((d, i) => ({ date: d, ret: (i - 1) % 2 ? 0.01 : -0.01 }));
  const s = E.followStrategy(E.pairSeries(us, tw), 250);
  assert.equal(s.hitRate, 1);
  assert.ok(s.stratPct > s.bhPct);
  assert.equal(s.curve.length, s.n);
  assert.ok(s.maxDDPct <= 0);
});

test('verdict 分級', () => {
  assert.equal(E.verdict({ corr: 0.5, hit: 0.65 }).label, '高度連動');
  assert.equal(E.verdict({ corr: 0.3, hit: 0.56 }).label, '中度連動');
  assert.equal(E.verdict({ corr: 0.15, hit: 0.5 }).label, '弱連動');
  assert.equal(E.verdict({ corr: 0.0, hit: 0.5 }).label, '無明顯連動');
  assert.equal(E.verdict({ corr: null, hit: null }).label, '資料不足');
});

test('rankTwByCorr 依相關係數排序取前 N', () => {
  const days = weekdays(80);
  const us = days.map((d, i) => ({ date: d, ret: Math.sin(i) * 0.01 }));
  const mk = (k, noise) => days.map((d, i) => ({ date: d, ret: k * Math.sin(i - 1) * 0.01 + noise * Math.cos(i * 7) * 0.01 }));
  const twMap = {
    A: { name: 'A', market: '上市', series: mk(1, 0) },
    B: { name: 'B', market: '上市', series: mk(1, 0.5) },
    C: { name: 'C', market: '上市', series: mk(-1, 0) },
    D: { name: 'D', market: '上市', series: mk(1, 0.2) },
  };
  const top = E.rankTwByCorr(us, twMap, { main: 60, alt: 30, topN: 3 });
  assert.deepEqual(top.map(t => t.code), ['A', 'D', 'B']);
});
