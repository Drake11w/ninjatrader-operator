"""Market session and contract-roll info for CME equity-index futures (MNQ, NQ, MES, ES, ...).

Assumes the standard schedule in US Eastern time: Globex open Sun-Fri 18:00 ET, daily break 17:00-18:00,
Friday close 17:00, RTH Mon-Fri 09:30-16:00. Holidays and early closes are known only by date (special_session, a
rule-based approximation of the CME equity-index calendar); session times are not adjusted on those days, so check
the exchange calendar. Contract roll follows the usual convention (expiry is the third Friday of
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


def easter(year: int) -> date:
    """Western Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    m = (32 + 2 * e + 2 * i - h - k) % 7
    n = (a + 11 * h + 19 * m) // 433
    month = (h + m - 7 * n + 90) // 25
    return date(year, month, (h + m - 7 * n + 33 * month + 19) % 32)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_monday(year: int, month: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=d.weekday())


def _observed(d: date) -> date:
    """Saturday holidays are observed on Friday, Sunday holidays on Monday."""
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


def special_sessions(year: int) -> dict:
    """{date: description} for the weekdays in `year` that are not a normal session for CME equity-index futures:
    full closures, midday trading halts and early closes. Computed from the usual US holiday rules, not read from
    the exchange: it does not know one-off closures, and the exchange can change a day's hours. Meant for "do not
    start an intraday strategy today", where a false positive only costs a day."""
    halt, early = "trading halts 12:00 CT", "early close 12:15 CT"
    out = {}
    new_year = date(year, 1, 1)
    if new_year.weekday() != 5:  # a Saturday New Year is not observed on the Friday before
        out[_observed(new_year)] = "New Year's Day (closed)"
    out[_nth_weekday(year, 1, 0, 3)] = f"Martin Luther King Jr. Day ({halt})"
    out[_nth_weekday(year, 2, 0, 3)] = f"Presidents' Day ({halt})"
    out[easter(year) - timedelta(days=2)] = "Good Friday (closed)"
    out[_last_monday(year, 5)] = f"Memorial Day ({halt})"
    out[_observed(date(year, 6, 19))] = f"Juneteenth ({halt})"
    july4 = _observed(date(year, 7, 4))
    out[july4] = f"Independence Day ({halt})"
    out[_nth_weekday(year, 9, 0, 1)] = f"Labor Day ({halt})"
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    out[thanksgiving] = f"Thanksgiving ({halt})"
    out[thanksgiving + timedelta(days=1)] = f"day after Thanksgiving ({early})"
    christmas = _observed(date(year, 12, 25))
    out[christmas] = "Christmas Day (closed)"
    for eve, name in ((date(year, 7, 3), "day before Independence Day"), (date(year, 12, 24), "Christmas Eve")):
        if eve.weekday() < 5 and eve not in out:
            out[eve] = f"{name} ({early})"
    return out


def special_session(d: date, extra=()) -> str:
    """Why `d` is not a normal session ('' if it is one). `extra` is a list of ISO dates to treat as closed as well
    (one-off closures the rules cannot know)."""
    if d.isoformat() in extra:
        return "listed in extra_holidays"
    return special_sessions(d.year).get(d, "")


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
    special = special_session(now.date())
    out["special_session"] = special or None
    out["warnings"] = ([f"today is {special}: the times above assume a normal session"] if special else []) + [
        "holidays and early closes come from date rules, and session times are not adjusted for them; "
        "check the CME calendar"]
    return out


def weekend_closed(now: datetime, tz: str = "America/Chicago", close: time = time(16, 0),
                   reopen: time = time(16, 45)) -> bool:
    """True from Friday `close` until Sunday `reopen`, local time in `tz` (default: the CME weekend in Central time,
    with 15 minutes of slack before the 17:00 CT Sunday open). Shared by the launcher (which goes quiet) and the
    supervisor (which stops reporting a closed NinjaTrader as an incident). Holidays: see special_session."""
    from zoneinfo import ZoneInfo
    local = now.astimezone(ZoneInfo(tz))
    wd, t = local.weekday(), local.time()
    return wd == 5 or (wd == 4 and t >= close) or (wd == 6 and t < reopen)
