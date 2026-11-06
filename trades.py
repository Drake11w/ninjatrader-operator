"""Round-trip trade builder and performance metrics, computed from journaled executions.

Fills are netted per (account, instrument) FIFO, so scale-ins, partial exits and reversals all work.
Each trade is one entry fill matched against one exit fill (or part of them). MAE/MFE and slippage vs signal
need bar/signal data the add-on does not expose yet, so they are not computed here.
"""
import math
from collections import defaultdict
from datetime import datetime

# Dollars per full point. Override per call; unknown roots fall back to 1.0 and are flagged.
POINT_VALUE = {"MNQ": 2.0, "NQ": 20.0, "MES": 5.0, "ES": 50.0, "M2K": 5.0, "RTY": 50.0,
               "MYM": 0.5, "YM": 5.0, "MGC": 10.0, "GC": 100.0, "MCL": 100.0, "CL": 1000.0}

BUY_ACTIONS = {"buy", "buytocover"}
SELL_ACTIONS = {"sell", "sellshort"}


def root(instrument: str) -> str:
    return (instrument or "").split()[0].upper() if (instrument or "").split() else ""


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def build_trades(executions: list, point_values: dict = None) -> dict:
    """Pair fills into closed trades. Returns {"trades": [...], "open": [...], "warnings": [...]}."""
    pv = {**POINT_VALUE, **(point_values or {})}
    warnings, trades, books = [], [], defaultdict(list)  # books: (account, instrument) -> open lots
    unknown = set()
    fills = sorted((e for e in executions if e.get("time") and e.get("quantity")), key=lambda e: e["time"])
    for e in fills:
        a = str(e.get("action", "")).lower()
        if a in BUY_ACTIONS:
            sign = 1
        elif a in SELL_ACTIONS:
            sign = -1
        else:
            warnings.append(f"skipped execution {e.get('execution_id')}: unknown action {e.get('action')!r}")
            continue
        key = (e.get("account", ""), e.get("instrument", ""))
        lots = books[key]
        remaining = orig = int(e["quantity"])
        comm = e.get("commission") or 0.0
        value = pv.get(root(key[1]))
        if value is None:
            unknown.add(root(key[1]))
            value = 1.0
        while remaining and lots and lots[0]["sign"] != sign:
            lot = lots[0]
            q = min(remaining, lot["qty"])
            entry_fee = lot["commission"] * q / lot["qty"]
            fee = entry_fee + comm * q / orig
            points = (e["price"] - lot["price"]) * lot["sign"] * q
            trades.append({
                "account": key[0], "instrument": key[1], "side": "long" if lot["sign"] > 0 else "short",
                "quantity": q, "entry_time": lot["time"], "exit_time": e["time"],
                "entry_price": lot["price"], "exit_price": e["price"],
                "entry_name": lot["name"], "exit_name": e.get("order_name"),
                "strategy": lot["strategy"], "strategy_version": lot["version"],
                "duration_sec": int((_parse(e["time"]) - _parse(lot["time"])).total_seconds()),
                "points": round(points, 4),
                "gross_pnl": round(points * value, 2), "commission": round(fee, 2),
                "net_pnl": round(points * value - fee, 2),
            })
            lot["commission"] -= entry_fee
            lot["qty"] -= q
            remaining -= q
            if lot["qty"] == 0:
                lots.pop(0)
        if remaining:
            lots.append({"sign": sign, "qty": remaining, "price": e["price"], "time": e["time"],
                         "name": e.get("order_name"), "commission": comm * remaining / orig,
                         "strategy": e.get("strategy", ""), "version": e.get("strategy_version", "")})
    if unknown:
        warnings.append("no point value for " + ", ".join(sorted(unknown)) + "; P&L assumes $1/point (pass point_values)")
    open_lots = [{"account": k[0], "instrument": k[1], "side": "long" if lot["sign"] > 0 else "short",
                  "quantity": lot["qty"], "entry_time": lot["time"], "entry_price": lot["price"]}
                 for k, ls in books.items() for lot in ls]
    trades.sort(key=lambda t: t["exit_time"])
    return {"trades": trades, "open": open_lots, "warnings": warnings}


def _summary(pnls: list) -> dict:
    n = len(pnls)
    if not n:
        return {"trades": 0}
    wins, losses = [p for p in pnls if p > 0], [p for p in pnls if p < 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {
        "trades": n, "net_pnl": round(sum(pnls), 2), "win_rate": round(len(wins) / n, 4),
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "expectancy": round(sum(pnls) / n, 2),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else None,
    }


def _max_drawdown(pnls: list) -> float:
    peak = equity = dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    return round(dd, 2)


def _ratios(daily: list) -> dict:
    n = len(daily)
    if n < 2:
        return {"sharpe": None, "sortino": None}
    mean = sum(daily) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in daily) / (n - 1))
    down = math.sqrt(sum(min(x, 0) ** 2 for x in daily) / n)
    ann = math.sqrt(252)
    return {"sharpe": round(mean / sd * ann, 3) if sd else None,
            "sortino": round(mean / down * ann, 3) if down else None}


def performance(trades: list, tz_name: str = "America/New_York") -> dict:
    """Summary stats plus breakdowns by weekday, hour of entry, and month (in tz_name, falling back to UTC)."""
    tz, tz_used, warnings = None, "UTC", []
    try:
        from zoneinfo import ZoneInfo
        tz, tz_used = ZoneInfo(tz_name), tz_name
    except Exception:
        warnings.append(f"timezone {tz_name!r} unavailable (pip install tzdata); using UTC for breakdowns")

    def local(ts):
        d = _parse(ts)
        return d.astimezone(tz) if tz else d

    groups = {"by_weekday": defaultdict(list), "by_entry_hour": defaultdict(list), "by_month": defaultdict(list)}
    daily = defaultdict(float)
    for t in trades:
        entry, exit_ = local(t["entry_time"]), local(t["exit_time"])
        groups["by_weekday"][entry.strftime("%a")].append(t["net_pnl"])
        groups["by_entry_hour"][f"{entry.hour:02d}"].append(t["net_pnl"])
        groups["by_month"][entry.strftime("%Y-%m")].append(t["net_pnl"])
        daily[exit_.date().isoformat()] += t["net_pnl"]
    pnls = [t["net_pnl"] for t in trades]
    if len(pnls) < 30:
        warnings.append(f"only {len(pnls)} trades: too few for the statistics to be reliable")
    return {
        **_summary(pnls), "max_drawdown": _max_drawdown(pnls),
        "avg_duration_sec": round(sum(t["duration_sec"] for t in trades) / len(trades)) if trades else None,
        "trading_days": len(daily), "worst_day": round(min(daily.values()), 2) if daily else None,
        "best_day": round(max(daily.values()), 2) if daily else None,
        **_ratios(list(daily.values())),
        **{name: {k: _summary(v) for k, v in sorted(g.items())} for name, g in groups.items()},
        "timezone": tz_used, "warnings": warnings,
    }
