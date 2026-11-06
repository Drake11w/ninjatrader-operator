"""Strategy Supervisor: behavior comparisons against a backtest reference (pure functions, no I/O).

These flag *unusual* behavior, not malfunction: an anomaly can be ordinary market variation. They return evidence
dicts, require a minimum sample, and are always reported at warning level by the callers. Nothing here acts.
"""
import math
from datetime import datetime, timedelta

MIN_EXPECTED_TRADES = 5      # do not test pace until the backtest rate predicts at least this many trades
PACE_ALPHA = 0.01            # two-sided tail probability that triggers a flag
HOLD_MIN_SIM = 15            # sim trades needed before holding time is judged
HOLD_MIN_REF = 30            # backtest trades needed to define a normal range
HOLD_BASE_RATE = 0.02        # share of backtest trades outside the 1st-99th percentile band, by construction
HOLD_ALPHA = 0.001


def trading_days(since: datetime, now: datetime) -> int:
    """Weekdays from `since` (inclusive) up to `now` (exclusive of the current, unfinished day). No holidays."""
    d, n = since.date(), 0
    while d < now.date():
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def poisson_cdf(k: int, lam: float) -> float:
    """P(X <= k) for X ~ Poisson(lam)."""
    if k < 0:
        return 0.0
    term = total = math.exp(-lam)
    for i in range(1, k + 1):
        term *= lam / i
        total += term
    return min(1.0, total)


def pace_finding(observed: int, days: int, per_day: float):
    """Is the number of trades since activation unusually low or high vs the backtest rate? None if fine or too early."""
    expected = per_day * days
    if days <= 0 or per_day <= 0 or expected < MIN_EXPECTED_TRADES:
        return None
    p_low = poisson_cdf(observed, expected)
    p_high = 1.0 - poisson_cdf(observed - 1, expected)
    if p_low < PACE_ALPHA / 2:
        direction, p = "fewer", p_low
    elif p_high < PACE_ALPHA / 2:
        direction, p = "more", p_high
    else:
        return None
    return {"direction": direction, "observed": observed, "expected": round(expected, 1), "trading_days": days,
            "p_value": round(p, 5),
            "note": f"{observed} trades in {days} trading days vs about {expected:.1f} expected from the backtest "
                    f"({direction} than expected, p={p:.4f}); may be market conditions or day filters, not a fault"}


def _percentile(sorted_vals: list, q: float) -> float:
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _binom_upper(n: int, m: int, p: float) -> float:
    """P(X >= m) for X ~ Binomial(n, p)."""
    return sum(math.comb(n, k) * p ** k * (1 - p) ** (n - k) for k in range(m, n + 1))


def holding_finding(sim_secs: list, ref_secs: list):
    """Are sim holding times outside the backtest's normal band more often than chance allows?"""
    sim = [s for s in sim_secs if isinstance(s, (int, float))]
    ref = sorted(s for s in ref_secs if isinstance(s, (int, float)))
    if len(sim) < HOLD_MIN_SIM or len(ref) < HOLD_MIN_REF:
        return None
    lo, hi = _percentile(ref, 0.01), _percentile(ref, 0.99)
    outside = sum(1 for s in sim if s < lo or s > hi)
    p = _binom_upper(len(sim), outside, HOLD_BASE_RATE)
    if outside == 0 or p >= HOLD_ALPHA:
        return None
    med = lambda v: sorted(v)[len(v) // 2]
    return {"outside": outside, "sim_trades": len(sim), "band_seconds": [round(lo), round(hi)],
            "sim_median_seconds": round(med(sim)), "backtest_median_seconds": round(med(ref)),
            "p_value": round(p, 6),
            "note": f"{outside} of {len(sim)} trades held outside the backtest's normal {round(lo)}-{round(hi)}s band "
                    f"(sim median {round(med(sim))}s vs backtest {round(med(ref))}s, p={p:.5f})"}
