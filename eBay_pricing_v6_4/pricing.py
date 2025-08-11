
from __future__ import annotations
import math, statistics as stats, re
from typing import List, Dict, Optional, Tuple, Set

# --- token utilities exposed for app.py ---
STOP = set("for with the and of to by from in on a an new pack filter filters water size sizes large small medium".split())

def tokens(s: str) -> Set[str]:
    if not s:
        return set()
    t = re.sub(r"[^A-Za-z0-9]+", " ", s).lower().split()
    return {w for w in t if len(w) > 2 and w not in STOP}

def jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))

# --- helper for mode-style cluster ---
def _densest_window(xs: List[float], width: float = 10.0) -> Tuple[float, float]:
    xs = sorted(xs)
    i = j = 0
    best_i = best_j = 0
    n = len(xs)
    while i < n:
        while j < n and xs[j] - xs[i] <= width:
            j += 1
        if (j - i) > (best_j - best_i):
            best_i, best_j = i, j
        i += 1
    return xs[best_i], xs[best_j - 1]

# --- robust suggestion set selection used upstream ---
def compute_suggestion(rows: List[Dict],
                       iqr_mult: float = 1.5,
                       min_price: Optional[float] = None,
                       max_price: Optional[float] = None,
                       method: str = "mode",
                       window: float = 10.0,
                       mad_k: float = 3.5):
    if not rows:
        return None, None, []
    totals = [r.get("total") for r in rows if r.get("total") is not None]
    if not totals:
        return None, None, []
    used: List[Dict] = []

    if method == "mode":
        lo, hi = _densest_window(totals, width=window)
        for r in rows:
            t = r.get("total")
            if t is None:
                continue
            if (min_price is not None and t < min_price) or (max_price is not None and t > max_price):
                continue
            if lo <= t <= hi:
                used.append(r)
    elif method == "mad":
        m = stats.median(totals)
        mad = stats.median([abs(x - m) for x in totals]) or 0.01
        thresh = 1.4826 * mad * mad_k
        for r in rows:
            t = r.get("total")
            if t is None:
                continue
            if (min_price is not None and t < min_price) or (max_price is not None and t > max_price):
                continue
            if abs(t - m) <= thresh:
                used.append(r)
    else:
        xs = sorted(totals)
        n = len(xs)
        def q(p):
            k = (n - 1) * p
            f = int(k); c = min(f + 1, n - 1)
            return xs[f] if f == c else xs[f] + (xs[c] - xs[f]) * (k - f)
        q1, q3 = q(0.25), q(0.75)
        iqr = max(0.0, q3 - q1)
        lo = q1 - iqr_mult * iqr
        hi = q3 + iqr_mult * iqr
        for r in rows:
            t = r.get("total")
            if t is None:
                continue
            if (min_price is not None and t < min_price) or (max_price is not None and t > max_price):
                continue
            if lo <= t <= hi:
                used.append(r)

    if not used:
        return None, None, []
    used.sort(key=lambda r: r["total"])
    best = used[0]
    return best["total"], best, used

# --- pricing policy helpers ---
def round_same_dollar_to_99(x: float) -> float:
    """
    Snap to floor(x) + 0.99 without crossing down a dollar band.
    Example: 56.56 -> (56.56 - 1) = 55.56 -> 55.99
             61.10 -> (61.10 - 1) = 60.10 -> 60.99
    """
    if x <= 0.99:
        return 0.99
    return math.floor(x) + 0.99

# --- final chooser: undercut the lower of Amazon/eBay ---
def choose_and_suggest(amazon_total: Optional[float], ebay_total: Optional[float]) -> Dict:
    """
    Policy:
      - If both available: undercut the lower one (eBay if eBay<Amazon, else Amazon).
      - If only one available: undercut that one.
      - Suggestion is $1 lower than the chosen comp, rounded to same-dollar .99.
    Returns dict: {"source": "Amazon"|"eBay", "competitor_price": float, "suggested": float}
    """
    candidates = []
    if amazon_total is not None:
        candidates.append(("Amazon", float(amazon_total)))
    if ebay_total is not None:
        candidates.append(("eBay", float(ebay_total)))
    if not candidates:
        return {"source": None, "competitor_price": None, "suggested": None}

    # Choose the lower competitor
    source, comp = min(candidates, key=lambda t: t[1])
    # Undercut by $1, then snap to D+.99
    target = comp - 1.0
    suggested = round_same_dollar_to_99(target)
    return {"source": source, "competitor_price": comp, "suggested": suggested}
