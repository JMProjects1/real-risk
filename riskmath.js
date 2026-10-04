/*
 * REAL Risk: the maths behind the what-if calculator and the market order cost table.
 * Used by dashboard.html in the browser and by the automated tests in Node, so the
 * numbers on screen are the numbers that are tested.
 *
 * Liquidation model (standard for perpetuals):
 *   a position is liquidated when the collateral backing it, plus its PnL from the
 *   current mark, falls to its maintenance margin plus whatever else that collateral
 *   has to cover (for cross margin, mainly the maintenance margin of other positions):
 *
 *     C − K + σ·s·(P − M) = m·s·P      so      P = (C − K − σ·s·M) / (s·(m − σ))
 *
 *   σ  +1 for a long, −1 for a short
 *   s  position size (base units)        M  current mark price
 *   C  collateral: account equity for cross margin, position margin + uPnL for isolated
 *   m  the market's maintenance margin rate (from REAL)
 *   K  everything else counted against C. REAL doesn't publish it per position, so it is
 *      calibrated from REAL's own liquidation estimate L for the unchanged position:
 *        K = C + σ·s·(L − M) − m·s·L
 *      With nothing changed the model therefore reproduces REAL's price exactly, and when
 *      size or margin changes, the other positions' requirement stays fixed (as it should).
 */
(function (root) {
  "use strict";
  const finite = v => typeof v === "number" && isFinite(v);

  // Build the model for one position. p: { direction, size, mark, liq, margin_mode,
  // account_equity, margin, upnl }. mmr: the market's maintenance margin rate.
  function model(p, mmr) {
    const sig = p.direction === "Long" ? 1 : -1;
    const s = p.size, M = p.mark, m = finite(mmr) && mmr > 0 ? mmr : 0.0125;
    const C = p.margin_mode === "Isolated" ? (p.margin || 0) + (p.upnl || 0) : p.account_equity;
    let K = 0, calibrated = false;
    if (finite(p.liq) && p.liq > 0 && finite(C) && s > 0 && finite(M)) {
      K = C + sig * s * (p.liq - M) - m * s * p.liq;
      calibrated = true;
    }
    return { sig, s, M, C, m, K, calibrated, liq0: finite(p.liq) && p.liq > 0 ? p.liq : null };
  }

  // Liquidation price for collateral C and size s (fills assumed at the mark). null = none.
  function liqPrice(md, C, s) {
    if (!(s > 0) || !finite(C)) return null;
    const P = (C - md.K - md.sig * s * md.M) / (s * (md.m - md.sig));
    return finite(P) && P > 0 ? P : null;
  }

  // Distance (%) from price P to liquidation price L, in the direction that hurts.
  function distance(md, P, L) {
    return finite(L) && finite(P) && P > 0 ? md.sig * (P - L) / P * 100 : null;
  }

  // A scenario: price P, new size s2, margin added (negative = removed).
  function scenario(md, P, s2, add) {
    const C2 = md.C + (add || 0);
    const liq = liqPrice(md, C2, s2);
    const equity = C2 + md.sig * s2 * (P - md.M);
    const liquidated = s2 > 0 && equity - md.K <= md.m * s2 * P;
    return {
      liq,
      distance: s2 > 0 && !liquidated ? distance(md, P, liq) : null,
      liquidated,
      pnl: md.sig * s2 * (P - md.M),
      equity,
      leverage: equity > 0 ? s2 * P / equity : null,
    };
  }

  // The unchanged position (uses REAL's own liquidation price when there is one).
  function baseline(md) {
    const liq = md.liq0 != null ? md.liq0 : liqPrice(md, md.C, md.s);
    return {
      liq, distance: distance(md, md.M, liq), liquidated: false, pnl: 0, equity: md.C,
      leverage: md.C > 0 ? md.s * md.M / md.C : null,
    };
  }

  // Margin to add (total, relative to now) so that size s2 is liquidated d% away from the mark.
  function marginFor(md, s2, d) {
    const target = md.M * (1 - md.sig * d / 100);
    if (!(target > 0) || !(s2 > 0)) return { target: null, add: null };
    const Cneed = md.K + md.sig * s2 * md.M + s2 * target * (md.m - md.sig);
    return { target, add: Cneed - md.C };
  }

  // Walk an order book (levels sorted best first, [price, size]) with a market order worth
  // `quote` in the quote currency. Returns { avg, base } or null if the book runs out.
  function walkQuote(levels, quote) {
    let left = quote, base = 0;
    for (const [price, size] of levels) {
      if (!(price > 0) || !(size > 0)) continue;
      const take = Math.min(size, left / price);
      base += take; left -= take * price;
      if (left <= quote * 1e-12) return { avg: quote / base, base };
    }
    return null;
  }

  const api = { model, liqPrice, distance, scenario, baseline, marginFor, walkQuote };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.RiskMath = api;
})(typeof window !== "undefined" ? window : globalThis);
