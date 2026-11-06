"""Market session and contract-roll info for CME equity-index futures (MNQ, NQ, MES, ES, ...).

Assumes the standard schedule in US Eastern time: Globex open Sun-Fri 18:00 ET, daily break 17:00-18:00,
Friday close 17:00, RTH Mon-Fri 09:30-16:00. Exchange holidays and early closes are NOT modelled; check the
exchange calendar on those days. Contract roll follows the usual convention (expiry is the third Friday of
Mar/Jun/Sep/Dec; liquidity moves to the next contract about 8 days earlier), which you can change.
"""
from datetime import date, datetime, time, timedelta, timezone

QUARTER_MONTHS = (3, 6, 9, 12)
MONTH_CODES = {3: "H", 6: "M", 9: "U", 12: "Z"}
RTH_OPEN, RTH_CLOSE = time(9, 30), time(16, 0)
GLOBEX_OPEN, GLOBEX_CLOSE = time(18, 0), time(17, 0)


def _et():
    from zoneinfo import ZoneInfo
    return ZoneInfo("America/New_York")


def third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    first_friday = first + timedelta(days=(4 - first.weekday()) % 7)
    return first_friday + timedelta(days=14)


def front_contract(today: date, roll_days_before: int = 8) -> dict:
    """The contract most traders should be in on `today`, with its expiry and roll date."""
    for year in (today.year, today.year + 1):
        for month in QUARTER_MONTHS:
            expiry = third_friday(year, month)
            roll = expiry - timedelta(days=roll_days_before)
            if today < roll:
                return {"contract": f"{month:02d}-{year % 100:02d}", "code": f"{MONTH_CODES[month]}{year % 10}",
                        "expiry": expiry.isoformat(), "roll_date": roll.isoformat(),
                        "days_to_roll": (roll - today).days}
    raise RuntimeError("unreachable")


def _at(d: date, t: time, tz) -> datetime:
    return datetime.combine(d, t, tzinfo=tz)


def _next_rth(now: datetime, tz) -> tuple:
    """(next open, next close) strictly after `now`, skipping weekends."""
    d = now.date()
    for _ in range(8):
        if d.weekday() < 5:
            o, c = _at(d, RTH_OPEN, tz), _at(d, RTH_CLOSE, tz)
            if now < c:
                return (o, c)
        d += timedelta(days=1)
    raise RuntimeError("unreachable")


def _globex_state(now: datetime) -> str:
    wd, t = now.weekday(), now.time()
    if wd == 5:
        return "weekend_closed"
    if wd == 6:
        return "weekend_closed" if t < GLOBEX_OPEN else "globex"
    if wd == 4 and t >= GLOBEX_CLOSE:
        return "weekend_closed"
    if GLOBEX_CLOSE <= t < GLOBEX_OPEN:
        return "maintenance_break"
    return "globex"


def _fmt(delta: timedelta) -> str:
    mins = int(delta.total_seconds() // 60)
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    return (f"{d}d " if d else "") + f"{h}h {m}m"


def session_info(now: datetime = None, instrument: str = "", roll_days_before: int = 8) -> dict:
    tz = _et()
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    state = _globex_state(now)
    in_rth = now.weekday() < 5 and RTH_OPEN <= now.time() < RTH_CLOSE
    if in_rth:
        state = "rth"
    elif state == "globex":
        state = "globex_extended_hours"
    nxt_open, nxt_close = _next_rth(now, tz)
    out = {"now_et": now.isoformat(), "state": state, "rth_open": state == "rth",
           "market_open": state in ("rth", "globex_extended_hours")}
    if in_rth:
        out["rth_closes_in"] = _fmt(nxt_close - now)
        out["rth_closes_at"] = nxt_close.isoformat()
    else:
        out["rth_opens_in"] = _fmt(nxt_open - now)
        out["rth_opens_at"] = nxt_open.isoformat()
    front = front_contract(now.date(), roll_days_before)
    out["front_month"] = front
    parts = instrument.split()
    if len(parts) == 2:
        out["instrument_check"] = {
            "instrument": instrument, "is_front_month": parts[1] == front["contract"],
            "note": "matches the front month" if parts[1] == front["contract"]
            else f"front month is {front['contract']}; {instrument} may be thin or expired"}
    out["warnings"] = ["exchange holidays and early closes are not modelled; check the CME calendar"]
    return out
