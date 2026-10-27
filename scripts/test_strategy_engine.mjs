// 策略引擎單元測試：node --test scripts/test_strategy_engine.mjs
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const E = require('../assets/strategy-engine.js');

// 產生人造 K 線
function mkBars(rows) {
  return rows.map((r, i) => ({
    date: `2024-${String(Math.floor(i / 28) + 1).padStart(2, '0')}-${String((i % 28) + 1).padStart(2, '0')}`,
    open: r.o ?? r.c, high: r.h ?? Math.max(r.o ?? r.c, r.c), low: r.l ?? Math.min(r.o ?? r.c, r.c), close: r.c,
    volume: r.v ?? 1000, mf: r.mf ?? 0, foreign: 0, trust: 0, dealer: 0,
    marginBal: r.mb ?? null, shortBal: r.sb ?? null,
  }));
}
const flat = (n, base = 100, amp = 0.3) => Array.from({ length: n }, (_, i) => {
  const c = base + amp * Math.sin(i * 1.7);
  return { o: c - 0.1, c, h: c + 0.3, l: c - 0.4 };
});
const hitsAt = (bars, i) => E.scanAll(E.makeCtx(bars))[i].map(h => h.id);

test('均線扣抵：今日收盤 > close[i-19] ⇔ 明日收平盤時 MA20 上揚', () => {
  let seed = 7;
  const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const closes = [100];
  for (let i = 1; i < 300; i++) closes.push(closes[i - 1] * (1 + (rnd() - 0.5) * 0.06));
  const bars = mkBars(closes.map(c => ({ c })));
  const ctx = E.makeCtx(bars);
  for (let i = 25; i < 299; i++) {
    const tomorrow = [...closes.slice(i - 18, i + 1), closes[i]];
    const maTomorrow = tomorrow.reduce((a, b) => a + b, 0) / 20;
    assert.equal(ctx.h.ma20TurningUp(i), maTomorrow > ctx.ind.ma20[i], `i=${i}`);
  }
});

test('布林通道：中軌=20MA、上下軌=±2σ、帶寬=上/下-1', () => {
  const bars = mkBars(flat(40).map((r, i) => ({ ...r, c: 100 + i })));
  const ind = E.computeIndicators(bars);
  const w = bars.slice(20, 40).map(b => b.close);
  const m = w.reduce((a, b) => a + b, 0) / 20;
  const sd = Math.sqrt(w.reduce((a, b) => a + (b - m) ** 2, 0) / 20);
  assert.ok(Math.abs(ind.ma20[39] - m) < 1e-9);
  assert.ok(Math.abs(ind.upper[39] - (m + 2 * sd)) < 1e-9);
  assert.ok(Math.abs(ind.bw[39] - ((m + 2 * sd) / (m - 2 * sd) - 1)) < 1e-9);
});

test('光頭大紅棒：橫盤＋糾結＋實體≥5%＋上影≤0.5%＋爆量 2 倍才觸發', () => {
  const base = flat(70);
  const ok = { o: 100, c: 106, h: 106.2, l: 99.8, v: 2500 };
  assert.ok(hitsAt(mkBars([...base, ok]), 70).includes('BALD_RED'));
  // 上影線過長
  assert.ok(!hitsAt(mkBars([...base, { ...ok, h: 107.5 }]), 70).includes('BALD_RED'));
  // 量不足 2 倍
  assert.ok(!hitsAt(mkBars([...base, { ...ok, v: 1500 }]), 70).includes('BALD_RED'));
  // 實體不足 5%（收盤漲幅也不足）
  assert.ok(!hitsAt(mkBars([...base, { ...ok, c: 103.5, h: 103.6 }]), 70).includes('BALD_RED'));
  // 先前不是橫盤（大漲一段）
  const trend = Array.from({ length: 70 }, (_, i) => ({ c: 60 + i * 0.6 }));
  const last = trend[69].c;
  assert.ok(!hitsAt(mkBars([...trend, { o: last, c: last * 1.06, h: last * 1.061, v: 3000 }]), 70).includes('BALD_RED'));
});

test('高檔吊首線：漲幅≥30%＋小實體長下影＋主力賣超', () => {
  const up = Array.from({ length: 45 }, (_, i) => ({ c: 100 * (1 + 0.012 * i) }));
  const c = up[44].c;
  const hang = { o: c, c: c * 1.003, h: c * 1.0035, l: c * 0.96, mf: -500 };
  assert.ok(hitsAt(mkBars([...up, hang]), 45).includes('HANGING_MAN'));
  // 籌碼未轉弱（主力買、無資券資料）→ 不觸發
  assert.ok(!hitsAt(mkBars([...up, { ...hang, mf: 500 }]), 45).includes('HANGING_MAN'));
  // 位階不夠高
  const low = flat(45);
  const c2 = low[44].c;
  assert.ok(!hitsAt(mkBars([...low, { o: c2, c: c2 * 1.003, h: c2 * 1.0035, l: c2 * 0.96, mf: -500 }]), 45).includes('HANGING_MAN'));
});

test('窄帶寬開布林：帶寬≤5%＋上軌斜率≥1%＋主力買＋站上上軌', () => {
  const base = flat(40, 100, 0.2);
  assert.ok(hitsAt(mkBars([...base, { o: 100, c: 104, h: 104.2, mf: 300 }]), 40).includes('BB_SQUEEZE_BREAK'));
  // 主力賣 → 不追
  assert.ok(!hitsAt(mkBars([...base, { o: 100, c: 104, h: 104.2, mf: -300 }]), 40).includes('BB_SQUEEZE_BREAK'));
});

test('葛蘭碧⑤：月線下彎＋收盤位於月線下（狀態首日）', () => {
  const rows = [...Array.from({ length: 30 }, (_, i) => ({ c: 100 + i })), ...Array.from({ length: 25 }, (_, i) => ({ c: 129 - i * 0.8 }))];
  const bars = mkBars(rows);
  const ctx = E.makeCtx(bars);
  const hits = E.scanAll(ctx);
  const idx = hits.findIndex(d => d.some(h => h.id === 'GRANVILLE_5'));
  assert.ok(idx > 0, 'should trigger');
  assert.ok(ctx.ind.ma20[idx] < ctx.ind.ma20[idx - 1] && bars[idx].close < ctx.ind.ma20[idx]);
  assert.ok(!(ctx.ind.ma20[idx - 1] < ctx.ind.ma20[idx - 2] && bars[idx - 1].close < ctx.ind.ma20[idx - 1]));
});

test('無未來資料：截斷序列後各日訊號不變', () => {
  let seed = 11;
  const rnd = () => (seed = (seed * 16807) % 2147483647) / 2147483647;
  const rows = []; let c = 50;
  for (let i = 0; i < 400; i++) {
    const o = c * (1 + (rnd() - 0.5) * 0.02); c = o * (1 + (rnd() - 0.48) * 0.08);
    rows.push({ o, c, h: Math.max(o, c) * (1 + rnd() * 0.02), l: Math.min(o, c) * (1 - rnd() * 0.03), v: 1000 * (0.5 + rnd() * 3), mf: (rnd() - 0.5) * 500, mb: 1000 + i, sb: 300 + (i % 50) });
  }
  const full = E.scanAll(E.makeCtx(mkBars(rows)));
  for (const k of [150, 222, 301, 399]) {
    const cut = E.scanAll(E.makeCtx(mkBars(rows.slice(0, k + 1))));
    assert.deepEqual(cut[k].map(h => h.id), full[k].map(h => h.id), `k=${k}`);
  }
});

test('回測：次日開盤成交並扣除手續費＋證交稅', () => {
  const base = flat(70);
  const rows = [...base, { o: 100, c: 106, h: 106.2, l: 99.8, v: 2500 }, { o: 107, c: 108 }, { o: 108, c: 109 }];
  for (let i = 0; i < 5; i++) rows.push({ o: 109, c: 109, h: 109.1, l: 108.9 });
  // 吊首線出場
  rows.push({ o: 110, c: 110.2, h: 110.25, l: 107.5 });
  rows.push({ o: 109, c: 109 });
  const bars = mkBars(rows);
  const ctx = E.makeCtx(bars);
  const trades = E.backtestSignal(ctx, E.scanAll(ctx), 'BALD_RED');
  assert.equal(trades.length, 1);
  const t = trades[0];
  assert.equal(t.entryPx, 107); // 訊號隔日開盤
  assert.equal(t.exitPx, 109); // 吊首線隔日開盤
  const expected = (109 * (1 - 0.001425 - 0.003)) / (107 * 1.001425) - 1;
  assert.ok(Math.abs(t.ret - expected) < 1e-12);
});

test('搶反彈 SOP：次日殺低才進場、有賺就跑', () => {
  const base = flat(60, 100, 0.5);
  const rows = [...base, { o: 99, c: 92, h: 99, l: 91.5, v: 3000 }, { o: 92.5, c: 91, h: 92.5, l: 90, v: 2000 }, { o: 92, c: 94, h: 94.5, l: 91.5 }, { o: 94, c: 95 }];
  const bars = mkBars(rows);
  const ctx = E.makeCtx(bars);
  const hits = E.scanAll(ctx);
  assert.ok(hits[60].some(h => h.id === 'FLOOR_REBOUND'));
  const trades = E.backtestSignal(ctx, hits, 'FLOOR_REBOUND');
  assert.equal(trades.length, 1);
  assert.equal(trades[0].entryPx, 92); // 掛昨收 92 承接（開盤 92.5 > 92）
  assert.equal(trades[0].exitPx, 92); // 第三天開高（92 > 91）→ 開盤了結
  assert.equal(trades[0].reason, '第三天開高即了結');
});

test('除權息還原：除息日前價格乘上 after/before', () => {
  const bars = E.buildBars({
    prices: [
      { date: '2024-01-02', open: 100, max: 101, min: 99, close: 100, Trading_Volume: 1e6 },
      { date: '2024-01-03', open: 95, max: 96, min: 94, close: 95, Trading_Volume: 1e6 },
    ],
    dividends: [{ date: '2024-01-03', before_price: 100, after_price: 95 }],
    institutional: [{ date: '2024-01-02', name: 'Foreign_Investor', buy: 5000, sell: 2000 }, { date: '2024-01-02', name: 'Investment_Trust', buy: 0, sell: 1000 }],
  });
  assert.equal(bars[0].close, 95);
  assert.equal(bars[0].rawClose, 100);
  assert.equal(bars[1].close, 95);
  assert.equal(bars[0].mf, 2); // (3000 - 1000) 股 / 1000 = 2 張
  assert.equal(bars[1].mf, null);
  assert.equal(bars[0].volume, 1000);
});

test('主力成本線：近 20 日買超日以買超張數加權成交均價，賣超日不計', () => {
  const rows = Array.from({ length: 25 }, (_, i) => ({ c: 100 + i, mf: 0 }));
  rows[3].mf = 300; // 視窗外（i=24 的 20 日視窗為 5~24）
  rows[20].mf = 100; rows[22].mf = 300; rows[23].mf = -500; // 賣超日不計入
  const bars = mkBars(rows);
  bars.forEach(b => { b.vwap = b.close + 0.5; });
  const ind = E.computeIndicators(bars);
  const expected = (100 * 120.5 + 300 * 122.5) / 400;
  assert.ok(Math.abs(ind.mfCost20[24] - expected) < 1e-9);
  assert.equal(ind.mfCost20[18], null); // 不足 20 日
  // 近 20 日皆無買超 → 無成本線
  const flatBars = mkBars(Array.from({ length: 25 }, () => ({ c: 100, mf: -10 })));
  assert.equal(E.computeIndicators(flatBars).mfCost20[24], null);
});

test('成交均價：成交金額 ÷ 成交股數並套用除權息還原', () => {
  const bars = E.buildBars({
    prices: [
      { date: '2024-01-02', open: 100, max: 101, min: 99, close: 100, Trading_Volume: 1000, Trading_money: 100500 },
      { date: '2024-01-03', open: 95, max: 96, min: 94, close: 95, Trading_Volume: 1000, Trading_money: 95200 },
    ],
    dividends: [{ date: '2024-01-03', before_price: 100, after_price: 95 }],
  });
  assert.ok(Math.abs(bars[0].vwap - 100.5 * 0.95) < 1e-9);
  assert.ok(Math.abs(bars[1].vwap - 95.2) < 1e-9);
});
