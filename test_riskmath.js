// Tests for riskmath.js. Run with: node test_riskmath.js  (also run by `python3 -m unittest`).
"use strict";
const assert = require("node:assert/strict");
const R = require("./riskmath.js");

const near = (a, b, tol, msg) => assert.ok(Math.abs(a - b) <= tol, `${msg}: expected ${b}, got ${a}`);
let passed = 0;
function test(name, fn) { fn(); passed++; console.log("ok -", name); }

// The "true" liquidation price of a position when the rest of the account needs `other`
// in maintenance margin and its PnL doesn't move.
function truth(E, other, sig, s, M, m) { return (E - other - sig * s * M) / (s * (m - sig)); }

test("single cross short reproduces REAL's liquidation price", () => {
  // Example single cross short: equity 44.95, short 0.002 BTC at mark 86,062.83, REAL says 107,188.26.
  const p = { direction: "Short", size: 0.002, mark: 86062.83, liq: 107188.26, margin_mode: "Cross", account_equity: 44.95 };
  const md = R.model(p, 0.0125);
  assert.ok(md.calibrated);
  near(R.scenario(md, p.mark, p.size, 0).liq, 107188.26, 1e-6, "unchanged scenario");
  near(R.baseline(md).liq, 107188.26, 1e-9, "baseline");
  near(R.baseline(md).distance, 24.5466, 1e-3, "distance");
  near(R.scenario(md, p.mark, p.size, 20).liq, 117064.8, 1, "+20 margin");
  assert.ok(R.scenario(md, p.mark, 0.001, 0).liq > 107188.26, "smaller short moves liquidation further away");
  assert.ok(R.scenario(md, 110000, p.size, 0).liquidated, "price past liquidation");
  assert.ok(!R.scenario(md, 100000, p.size, 0).liquidated, "price before liquidation");
});

test("margin needed matches hand calculation", () => {
  const p = { direction: "Short", size: 0.002, mark: 86062.83, liq: 107188.26, margin_mode: "Cross", account_equity: 44.95 };
  const md = R.model(p, 0.0125);
  const r30 = R.marginFor(md, p.size, 30), r50 = R.marginFor(md, p.size, 50);
  near(r30.target, 111881.68, 0.01, "30% target");
  near(r50.target, 129094.25, 0.01, "50% target");
  near(r30.add, 9.51, 0.05, "30% margin");
  near(r50.add, 44.36, 0.05, "50% margin");
  near(R.marginFor(md, p.size, 24.5466).add, 0, 0.01, "current distance needs nothing");
});

test("cross account with another large position (review bug #1)", () => {
  // Small BTC long next to a large position elsewhere on the same cross account.
  const m = 0.0125, s = 0.01, M = 86000, other = 250;           // other position needs 250 maintenance margin
  const L = M * 0.9975;                                          // REAL says liquidation is 0.25% away
  const E = other + s * M + s * L * (m - 1);                     // equity consistent with that
  const md = R.model({ direction: "Long", size: s, mark: M, liq: L, margin_mode: "Cross", account_equity: E }, m);
  near(md.K, other, 1e-6, "calibrated offset equals the other position's margin");
  near(R.scenario(md, M, s, 0).liq, L, 1e-6, "unchanged: reproduces REAL");
  assert.notEqual(R.scenario(md, M, s, 0).liq, null, "unchanged: liquidation price is not None");
  for (const s2 of [0.005, 0.002, 0.02]) {
    near(R.scenario(md, M, s2, 0).liq, truth(E, other, 1, s2, M, m), 1e-6, `size ${s2}`);
  }
  for (const add of [5, 50, 200]) {
    near(R.scenario(md, M, s, add).liq, truth(E + add, other, 1, s, M, m), 1e-6, `+${add} margin`);
  }
  const fifty = R.marginFor(md, s, 50);
  assert.ok(fifty.add > 0, "50% away needs more margin, not 'Already there'");
  near(R.scenario(md, M, s, fifty.add).distance, 50, 1e-6, "adding that margin lands at 50%");
});

test("isolated position uses its own margin", () => {
  const p = { direction: "Long", size: 0.01, mark: 50000, liq: 46000, margin_mode: "Isolated", margin: 25, upnl: 3, account_equity: 9999 };
  const md = R.model(p, 0.0125);
  assert.equal(md.C, 28);
  near(R.scenario(md, 50000, 0.01, 0).liq, 46000, 1e-6, "reproduces REAL");
  assert.ok(R.scenario(md, 50000, 0.01, 10).liq < 46000, "more margin moves a long's liquidation down");
});

test("no liquidation price from REAL", () => {
  const md = R.model({ direction: "Long", size: 0.001, mark: 50000, liq: null, margin_mode: "Cross", account_equity: 1000 }, 0.0125);
  assert.equal(md.calibrated, false);
  assert.equal(R.liqPrice(md, 1000, 0.001), null, "over-collateralised long has no liquidation price");
  assert.equal(R.baseline(md).liq, null);
});

test("market order walks the book in quote currency", () => {
  const asks = [[100, 1], [101, 1], [102, 10]];
  const r = R.walkQuote(asks, 150);                       // 100 at 100, then 50 at 101
  near(r.base, 1 + 50 / 101, 1e-12, "base filled");
  near(r.avg, 150 / (1 + 50 / 101), 1e-9, "average price");
  near(R.walkQuote(asks, 50).avg, 100, 1e-12, "fits in the first level");
  assert.equal(R.walkQuote(asks, 5000), null, "book too thin");
  near(R.walkQuote([[0.2, 1e6]], 1000).base, 5000, 1e-6, "low-priced market");
});

console.log(`\n${passed} riskmath tests passed`);
