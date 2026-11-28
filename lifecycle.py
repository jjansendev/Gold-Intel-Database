"""What happened to each auction between two snapshots of a house: the demand classifier.

Shared, byte for byte, with the data repo (gold-intel-database/lifecycle.py): standard library only.

Forever's auctions last 2, 8 or 24 h, and the API only says how long each has left as a bucket:
SHORT (<30 min), MEDIUM (30 min-2 h), LONG (2-12 h), VERY_LONG (12-24 h here). An auction that is
gone at the next snapshot was bought, cancelled or expired. This module separates them as far as
the data allows, per item, for one pair of snapshots `gap` apart:

  partial    units bought out of a stack that's still listed (commodities): certain sales
  early      units of auctions gone sooner than they could have expired (bucket minimum > gap):
             bought or cancelled
  cancels    early units that were certainly cancelled: a commodity listed above a cheaper unit of the
             same item that was still there (buyers always take the cheapest first)
  ambiguous  units gone that could also have expired in the gap
  sales_est  expected units sold: partial + weighted early (0.8 near the cheapest price, 0.3 when 20%+
             above it: cancels and reposts are common) + ambiguous units weighted by the chance they
             were removed before expiring rather than expired (from how fast similar listings, near
             the cheapest price or above it, were removed in the same gap). Commodities: anything that
             went priced below a stack that was partly bought counts as bought (buyers take the
             cheapest first), and nearly so below the dearest early removal
             + listings posted and bought between the two snapshots (never seen: estimated from the
             new listings that were seen and how fast listings like them went)
  sales_lo   partial (certain)            sales_hi  partial + early - cancels + ambiguous

The weights are assumptions until calibrated against your own auction mail (sold vs cancelled).
Pairs more than 12 h apart are not used; pairs more than 2 h apart are counted as "rough".
"""
from __future__ import annotations

import math

FIELDS = ["sales_est", "sales_lo", "sales_hi", "partial", "early", "cancels", "ambiguous", "events",
          "span_ms", "pairs", "rough_pairs", "max_gap_ms", "sold_value"]
VERSION = 2
MAX_GAP_MS = 12 * 3_600_000
ROUGH_GAP_MS = 2 * 3_600_000
# Hours left, by bucket (Forever: auctions last 2, 8 or 24 h)
BUCKET_H = {"SHORT": (0.0, 0.5), "MEDIUM": (0.5, 2.0), "LONG": (2.0, 12.0), "VERY_LONG": (12.0, 24.0)}
W_NEAR, W_HIGH, HIGH_RATIO = 0.8, 0.3, 1.2
CLOSE_GAP_H = 3.0            # the buy-frontier rule and the never-seen estimate need snapshots this close


def _exposure_before_expiry(lo: float, hi: float, g: float) -> float:
    """E[min(r, g)] for r uniform on [lo, hi]: how long an auction could be bought before it expired."""
    if g <= lo:
        return g
    if g >= hi or hi <= lo:
        return (lo + hi) / 2
    return ((g - lo) * (lo + g) / 2 + (hi - g) * g) / (hi - lo)


def _id(ref) -> int:
    try:
        return int(str(ref).rsplit(":", 1)[-1])
    except ValueError:
        return 0


def _p_removed_early(lam: float, lo: float, hi: float, g: float, n: int = 12) -> float:
    """An auction with lo..hi hours left is gone `g` hours later. Chance it was bought/cancelled
    rather than expired, if listings like it are removed at `lam` per hour: over its possible
    remaining times r, early = 1 - e^(-lam*min(r, g)), expired = [r < g] * e^(-lam*r)."""
    if lam <= 0:
        return 0.0
    pe = px = 0.0
    for i in range(n):
        r = lo + (hi - lo) * (i + 0.5) / n
        pe += 1 - math.exp(-lam * min(r, g))
        if r < g:
            px += math.exp(-lam * r)
    return pe / (pe + px) if pe + px > 0 else 0.0


def classify_pair(before: dict, after: dict, gap_ms: int, commodity: bool,
                  w_near: float = W_NEAR, w_high: float = W_HIGH) -> dict:
    """before/after: {auction_ref: (item_key, qty, unit_price or None, time_left)}.
    Returns {item_key: [FIELDS...]} for every item listed in either snapshot (None if the gap is unusable).
    w_near / w_high: the chance an early removal near / 20%+ above the cheapest price was a sale
    (defaults W_NEAR / W_HIGH; the app passes weights measured on your own auctions when it has enough)."""
    if gap_ms <= 0 or gap_ms > MAX_GAP_MS:
        return {}
    g = gap_ms / 3_600_000
    by_key: dict = {}
    for ref, (key, qty, unit, tl) in before.items():
        by_key.setdefault(key, ([], []))[0].append((ref, qty, unit, tl))
    for ref, (key, qty, unit, tl) in after.items():
        by_key.setdefault(key, ([], []))[1].append((ref, qty, unit, tl))
    out = {}
    rough = 1 if gap_ms > ROUGH_GAP_MS else 0
    seen_before = set(before)
    max_id_before = max((_id(r) for r in before), default=0)
    for key, (b, a) in by_key.items():
        now = {ref: (qty, unit) for ref, qty, unit, _ in a}
        units = [u for _, _, u, _ in b if u]
        floor = min(units) if units else None
        surv_units = [u for ref, _, u, _ in b if ref in now and u]
        surv_floor = min(surv_units) if surv_units else None
        partial = early = cancels = amb_units = events = 0
        sold_value = 0.0
        early_rows, amb_rows = [], []
        for ref, qty, unit, tl in b:
            if ref in now:
                q2 = now[ref][0]
                if q2 < qty:
                    d = qty - q2
                    partial += d
                    events += 1
                    sold_value += d * (unit or 0)
                continue
            lo, hi = BUCKET_H.get(tl, (0.0, 0.5))
            if lo >= g:
                if commodity and surv_floor is not None and unit and unit > surv_floor:
                    cancels += qty
                    continue
                early += qty
                events += 1
                early_rows.append((qty, unit))
            else:
                amb_units += qty
                amb_rows.append((qty, unit, lo, hi))

        def near(unit):
            return not (floor and unit and unit > HIGH_RATIO * floor)

        def w(unit):
            return w_near if near(unit) else w_high
        # removal rate per unit-hour, separately near the cheapest price and above it (buyers take
        # the cheap end, so cheap listings go much faster)
        exp_n = exp_h = rem_n = rem_h = 0.0
        for ref, qty, unit, tl in b:
            gone = ref not in now
            if gone and BUCKET_H.get(tl, (0.0, 0.5))[0] < g:
                continue                                     # ambiguous: not used for the rate
            if gone and commodity and surv_floor is not None and unit and unit > surv_floor:
                continue                                     # proven cancel
            t = g / 2 if gone else g
            if near(unit):
                exp_n += qty * t
                rem_n += qty if gone else 0
            else:
                exp_h += qty * t
                rem_h += qty if gone else 0
        lam_n = rem_n / exp_n if exp_n > 0 else 0.0
        lam_h = rem_h / exp_h if exp_h > 0 else 0.0
        # Commodities: buyers take the cheapest units first, so once a stack priced P was partly bought,
        # everything cheaper that disappeared was bought too ("buy frontier"); the same, a little
        # less surely, for the dearest early removal.
        frontier = frontier_likely = None
        if commodity and g <= CLOSE_GAP_H:          # over longer gaps a cheap listing may have expired first
            bought = [u for ref, qty, u, _ in b if ref in now and now[ref][0] < qty and u]
            frontier = max(bought) if bought else None
            gone_early = [u for _, u in early_rows if u]
            frontier_likely = max(gone_early + bought) if (gone_early or bought) else None
        est = float(partial)
        for qty, unit in early_rows:
            wt = (1.0 if frontier and unit and unit < frontier
                  else 0.9 if frontier_likely and unit and unit < frontier_likely else w(unit))
            est += wt * qty
            sold_value += wt * qty * (unit or 0)
        for qty, unit, lo, hi in amb_rows:
            p = _p_removed_early(lam_n if near(unit) else lam_h, lo, hi, g)
            if frontier and unit and unit <= frontier:
                p = max(p, 0.9)
            elif frontier_likely and unit and unit <= frontier_likely:
                p = max(p, 0.7)
            wt = 1.0 if frontier and unit and unit <= frontier else w(unit)
            est += wt * qty * p
            sold_value += wt * qty * p * (unit or 0)
        # Listings posted after the first snapshot that were bought before the second are never seen.
        # From the new listings that were seen and how fast listings like them go, estimate how many
        # weren't (a listing up for u hours of the gap survives with chance e^(-lam*u)).
        prev_max = max_id_before
        invisible = 0.0
        for ref, qty, unit, _ in (a if g <= CLOSE_GAP_H else ()):
            if ref in seen_before or _id(ref) <= prev_max:
                continue
            lam = lam_n if near(unit) else lam_h
            if lam <= 0:
                continue
            surv = (1 - math.exp(-lam * g)) / (lam * g)          # mean of e^(-lam*u), u uniform in [0, g]
            invisible += qty * (1 / max(surv, 0.2) - 1) * (1.0 if near(unit) else w_high)
        invisible = min(invisible, 2.0 * sum(q for r, q, _, _ in a if r not in seen_before))
        est += invisible
        out[key] = [round(est, 2), partial, partial + early + amb_units, partial, early, cancels, amb_units,
                    events, gap_ms, 1, rough, gap_ms, round(sold_value)]
    return out


def add(acc: list | None, row: list) -> list:
    """Accumulate two FIELDS rows (sums, except max_gap_ms which is a max)."""
    if acc is None:
        return list(row)
    out = [x + y for x, y in zip(acc, row)]
    out[FIELDS.index("max_gap_ms")] = max(acc[FIELDS.index("max_gap_ms")], row[FIELDS.index("max_gap_ms")])
    out[0] = round(out[0], 2)
    return out
