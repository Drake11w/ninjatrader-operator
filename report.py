"""Daily / weekly markdown report built from journaled trades (and rule status when a rules file exists)."""
from datetime import date, datetime, timedelta, timezone

import rules as rules_mod
import trades as trades_mod


def _tz(name: str):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None


def _money(x) -> str:
    return "n/a" if x is None else f"{'-' if x < 0 else ''}${abs(x):,.2f}"


def period_bounds(period: str, day: str) -> tuple:
    """Returns (first_day, last_day) ISO strings. 'day' is one trading day; 'week' is the Mon-Sun week containing it."""
    d = date.fromisoformat(day)
    if period == "week":
        start = d - timedelta(days=d.weekday())
        return start.isoformat(), (start + timedelta(days=6)).isoformat()
    return day, day


def build_report(all_trades: list, open_positions: list, period: str = "day", day: str = "",
                 rules_cfg: dict = None, unrealized: float = 0.0, tz_name: str = "America/New_York",
                 start_hour: int = 18) -> str:
    tz = _tz(tz_name)
    if not day:
        day = rules_mod.trading_day(datetime.now(timezone.utc).isoformat(), tz, start_hour)
    first, last = period_bounds(period, day)
    labelled = [(rules_mod.trading_day(t["exit_time"], tz, start_hour), t) for t in all_trades]
    chosen = [t for d, t in labelled if first <= d <= last]
    perf = trades_mod.performance(chosen, tz_name)
    title = f"Trading report: {first}" if first == last else f"Trading report: {first} to {last}"
    lines = [f"# {title}", ""]

    if not chosen:
        lines += ["No closed trades in this period.", ""]
    else:
        lines += ["## Summary", "",
                  f"- Net P&L: **{_money(perf['net_pnl'])}** over {perf['trades']} trades "
                  f"({perf['win_rate']:.0%} win rate)",
                  f"- Expectancy {_money(perf['expectancy'])}/trade; avg win {_money(perf['avg_win'])}, "
                  f"avg loss {_money(perf['avg_loss'])}; profit factor {perf['profit_factor'] if perf['profit_factor'] is not None else 'n/a'}",
                  f"- Max drawdown in period: {_money(perf['max_drawdown'])}"]
        if period == "week":
            lines.append(f"- Best day {_money(perf['best_day'])}, worst day {_money(perf['worst_day'])} "
                         f"({perf['trading_days']} trading days)")
        lines.append("")
        if period == "week":
            by_day = {}
            for d, t in labelled:
                if first <= d <= last:
                    by_day.setdefault(d, []).append(t["net_pnl"])
            lines += ["## By day", "", "| Day | Trades | Net P&L |", "|---|---|---|"]
            lines += [f"| {d} | {len(v)} | {_money(sum(v))} |" for d, v in sorted(by_day.items())]
            lines.append("")
        lines += ["## Trades", "", "| Exit time (ET) | Instrument | Side | Qty | Entry | Exit | Net P&L | Strategy |",
                  "|---|---|---|---|---|---|---|---|"]
        for t in chosen:
            ex = datetime.fromisoformat(t["exit_time"].replace("Z", "+00:00"))
            ex = ex.astimezone(tz) if tz else ex
            lines.append(f"| {ex.strftime('%m-%d %H:%M')} | {t['instrument']} | {t['side']} | {t['quantity']} | "
                         f"{t['entry_price']} | {t['exit_price']} | {_money(t['net_pnl'])} | {t['strategy'] or '-'} |")
        lines.append("")

    if open_positions:
        lines += ["## Open positions", ""]
        lines += [f"- {p['side']} {p['quantity']} {p['instrument']} @ {p['entry_price']} since {p['entry_time']}"
                  for p in open_positions]
        lines.append("")

    if rules_cfg:
        upto = [t for d, t in labelled if d <= last]
        now = datetime.fromisoformat(last + "T12:00:00+00:00")
        rs = rules_mod.evaluate(upto, rules_cfg, now=now, unrealized=unrealized)
        lines += [f"## Prop rules (as of {last}): {rs['overall'].upper()}", "", "| Rule | Status | Detail |", "|---|---|---|"]
        for name, r in rs["rules"].items():
            if name == "daily_loss_limit":
                detail = f"lost {_money(r['loss_today'])} of {_money(r['limit'])}; {_money(r['distance'])} left"
            elif name == "trailing_drawdown":
                detail = f"balance {_money(r['balance'])}, floor {_money(r['floor'])}; {_money(r['distance'])} cushion"
            elif name == "profit_target":
                detail = f"{_money(r['progress'])} of {_money(r['target'])}; {_money(r['remaining'])} to go"
            else:
                share = "n/a" if r["share"] is None else f"{r['share']:.0%}"
                detail = f"best day is {share} of profit (limit {r['max_share_of_profit']:.0%})"
            lines.append(f"| {name} | {r.get('status', 'n/a')} | {detail} |")
        lines.append("")

    notes = [w for w in perf.get("warnings", [])]
    if notes:
        lines += ["## Notes", ""] + [f"- {w}" for w in notes] + [""]
    return "\n".join(lines)
