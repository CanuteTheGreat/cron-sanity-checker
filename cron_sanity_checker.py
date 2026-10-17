#!/usr/bin/env python3
"""
cron_sanity_checker.py — catches broken/misleading cron schedules before they
bite you in production.

Zero dependencies (stdlib only), no network calls, works entirely offline.

What it catches that a plain crontab -e syntax check won't:
  - Impossible dates: day-of-month values that never occur in the given
    month(s) (e.g. "30" in February, "31" in April/June/Sept/Nov).
  - Field values out of range (minute 60, hour 24, month 13, dow 8, etc.)
  - Backwards ranges (e.g. "10-5" in a field where wraparound isn't valid).
  - Step-of-zero typos ("*/0") which some cron implementations silently
    treat as "*" (i.e. every unit) — almost never what was intended.
  - The classic "day-of-month AND day-of-week both restricted" gotcha:
    POSIX cron treats these fields as OR'd together when both are restricted,
    which surprises most people expecting AND semantics.
  - Two or more jobs in the same crontab that fire at literally the same
    minute (a common cause of resource contention / silent races) — flagged
    as an overlap warning, not an error.
  - Jobs that will *never* fire (contradictory day-of-month / month combo,
    e.g. "0 0 31 4 *" — April never has a 31st), and partly-dead combinations
    in an explicit month list (warning), e.g. "0 0 31 1,4 *".

Usage:
    ./cron_sanity_checker.py "*/5 * * * *"
    ./cron_sanity_checker.py --file /etc/crontab
    ./cron_sanity_checker.py --file mycrontab --show-next 5
    ./cron_sanity_checker.py "@weekly backup.sh" --show-next 3 --from "2026-01-01 00:00"

Schedule macros (@yearly/@annually/@monthly/@weekly/@daily/@midnight/@hourly) are
expanded and checked; @reboot lines and VAR=value lines are skipped.

Exit codes: 0 = no errors (warnings still possible), 1 = at least one error found
(or no job lines), 2 = usage error (no input, unreadable --file, bad --from).
"""

__version__ = "1.2.0"

import argparse
import calendar
import datetime
import json
import os
import re
import sys

FIELD_SPECS = [
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day-of-month", 1, 31),
    ("month", 1, 12),
    ("day-of-week", 0, 7),  # 0 and 7 both mean Sunday
]

MONTH_ALIASES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
DOW_ALIASES = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}

DAYS_IN_MONTH_MAX = {
    1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30,
    7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31,
}  # Feb uses 29 to allow for leap years; still flags 30/31 as impossible.


MACROS = {
    "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


class CronFieldError(ValueError):
    pass


def normalize_token(tok, aliases):
    low = tok.lower()
    for name, val in aliases.items():
        low = low.replace(name, str(val))
    return low


def parse_field(raw, field_name, lo, hi, warnings, errors):
    """Return a sorted set of ints this field expands to, plus flag issues."""
    aliases = MONTH_ALIASES if field_name == "month" else (
        DOW_ALIASES if field_name == "day-of-week" else {})
    raw_norm = normalize_token(raw, aliases)
    values = set()

    for part in raw_norm.split(","):
        part = part.strip()
        if not part:
            errors.append(f"{field_name}: empty sub-field in '{raw}'")
            continue

        step = 1
        if "/" in part:
            base, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                errors.append(f"{field_name}: non-numeric step '{step_s}' in '{part}'")
                continue
            if step == 0:
                errors.append(
                    f"{field_name}: step-of-zero '{part}' — likely a typo; "
                    f"many cron daemons treat '*/0' as '*' (every unit), silently "
                    f"changing your intended schedule")
                step = 1
            elif step < 0:
                errors.append(f"{field_name}: negative step '{step}' in '{part}'")
                step = abs(step)
        else:
            base = part

        if base == "*":
            rng = range(lo, hi + 1)
        elif "-" in base:
            try:
                a_s, b_s = base.split("-", 1)
                a, b = int(a_s), int(b_s)
            except ValueError:
                errors.append(f"{field_name}: malformed range '{base}'")
                continue
            if a > b:
                errors.append(
                    f"{field_name}: backwards range '{base}' (start {a} > end {b}) — "
                    f"cron does not support wraparound ranges here; did you mean "
                    f"'{b}-{a}'?")
                continue
            rng = range(a, b + 1)
        else:
            try:
                v = int(base)
            except ValueError:
                errors.append(f"{field_name}: non-numeric value '{base}'")
                continue
            rng = range(v, v + 1)

        for v in rng:
            vv = v
            if field_name == "day-of-week" and vv == 7:
                vv = 0  # normalize Sunday
            if vv < lo or vv > hi:
                errors.append(
                    f"{field_name}: value {v} out of range [{lo}-{hi}] in '{raw}'")
                continue
            values.add(vv)

    return values


def expand_field_with_step(raw, field_name, lo, hi, errors):
    """Full expansion honoring steps applied to ranges/wildcards/lists."""
    aliases = MONTH_ALIASES if field_name == "month" else (
        DOW_ALIASES if field_name == "day-of-week" else {})
    raw_norm = normalize_token(raw, aliases)
    result = set()

    for part in raw_norm.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        base = part
        if "/" in part:
            base, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError:
                continue
            if step <= 0:
                step = 1

        if base == "*":
            a, b = lo, hi
        elif "-" in base:
            try:
                a_s, b_s = base.split("-", 1)
                a, b = int(a_s), int(b_s)
            except ValueError:
                continue
            if a > b:
                continue
        else:
            try:
                a = b = int(base)
            except ValueError:
                continue
            if "/" in part:
                b = hi  # Vixie cron/cronie: "5/15" means "5-max/15"

        v = a
        while v <= b:
            vv = v
            if field_name == "day-of-week" and vv == 7:
                vv = 0
            if lo <= vv <= hi:
                result.add(vv)
            v += step

    return result


def analyze_line(raw_line, line_no, results):
    """Analyze a single cron schedule line. Returns dict with parsed info."""
    stripped = raw_line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    # Skip env var assignment lines (FOO=bar) and crontab-file user field
    # handling is intentionally left to the caller (see --file logic below).
    if "=" in stripped.split()[0] and not stripped.split()[0][0].isdigit() \
            and stripped.split()[0] not in ("*",):
        return None

    if stripped.startswith("@"):
        macro, _, command = stripped.partition(" ")
        macro = macro.lower()
        if macro == "@reboot":
            return None  # runs at boot, has no schedule to check
        if macro not in MACROS:
            results["errors"].append(
                f"line {line_no}: unknown schedule macro '{macro}' (valid: "
                f"{', '.join(sorted(MACROS))}, @reboot)")
            return None
        if not command.strip():
            results["errors"].append(f"line {line_no}: '{macro}' has no command")
            return None
        stripped = MACROS[macro] + " " + command.strip()

    tokens = stripped.split(None, 5)
    if len(tokens) < 6:
        results["errors"].append(
            f"line {line_no}: expected 5 schedule fields + command, got "
            f"{len(tokens)} token(s): '{stripped}'")
        return None

    fields = tokens[:5]
    command = tokens[5]
    errors = []
    warnings = []

    expanded = {}
    for (name, lo, hi), raw in zip(FIELD_SPECS, fields):
        # error-collection pass (range/step sanity)
        parse_field(raw, name, lo, hi, warnings, errors)
        expanded[name] = expand_field_with_step(raw, name, lo, hi, errors)

    dom_raw, month_raw, dow_raw = fields[2], fields[3], fields[4]
    dom_restricted = dom_raw.strip() != "*"
    dow_restricted = dow_raw.strip() != "*"

    if dom_restricted and dow_restricted:
        warnings.append(
            f"line {line_no}: both day-of-month ('{dom_raw}') and day-of-week "
            f"('{dow_raw}') are restricted — POSIX cron OR's these together "
            f"(fires if EITHER matches), not AND. If you expected AND "
            f"semantics, this job will fire more often than intended.")

    # Impossible dates. A day-of-month that doesn't exist in a month is only an
    # error when the job can then NEVER fire; "31 * *" (every month that has a
    # 31st) is a normal schedule. Partial dead combinations in an explicit month
    # list are warnings. With day-of-week also restricted cron ORs the two, so
    # the job still fires on matching weekdays.
    doms = expanded.get("day-of-month", set())
    months = expanded.get("month", set())
    month_restricted = month_raw.strip() != "*"
    if doms and months and dom_restricted:
        def name(m):
            return [k for k, v in MONTH_ALIASES.items() if v == m][0].capitalize()
        dead = {m: sorted(d for d in doms if d > DAYS_IN_MONTH_MAX.get(m, 31)) for m in sorted(months)}
        live_months = [m for m in months if len(dead[m]) < len(doms)]
        if not live_months:
            combos = ", ".join(name(m) for m in sorted(months))
            msg = (f"line {line_no}: day-of-month {sorted(doms)} can never occur in "
                   f"{combos} — this job will never fire on a day-of-month match")
            if dow_restricted:
                warnings.append(msg + " (it still fires on its day-of-week values, because "
                                      "cron ORs the two day fields)")
            else:
                errors.append(msg.replace("this job will never fire on a day-of-month match",
                                          "this job will never fire"))
        elif month_restricted:
            for m in sorted(months):
                if dead[m]:
                    warnings.append(
                        f"line {line_no}: day-of-month {dead[m]} can never occur in {name(m)} "
                        f"(month #{m}) — that month/day combination never fires")
        if not dow_restricted and doms == {29} and months <= {2}:
            warnings.append(f"line {line_no}: February 29th only exists in leap years — "
                            f"this job fires once every four years")

    if any(errors):
        results["errors"].extend(errors)
    if warnings:
        results["warnings"].extend(warnings)

    return {
        "line_no": line_no,
        "fields": fields,
        "command": command,
        "expanded": expanded,
        "has_errors": bool(errors),
    }


def compute_overlaps(jobs, results):
    """Warn if two+ distinct jobs share an identical (minute,hour,dom,month,dow)
    expansion intersection that is non-empty and non-trivial, indicating they
    could fire at the same wall-clock minute."""
    def could_fire_same_minute(a, b):
        for name, _, _ in FIELD_SPECS:
            sa, sb = a["expanded"].get(name, set()), b["expanded"].get(name, set())
            if not sa or not sb:
                return False
            if sa.isdisjoint(sb):
                return False
        return True

    n = len(jobs)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = jobs[i], jobs[j]
            if a["has_errors"] or b["has_errors"]:
                continue
            if a.get("tz") != b.get("tz"):
                continue  # same wall-clock fields in different zones are different instants
            if could_fire_same_minute(a, b):
                results["warnings"].append(
                    f"lines {a['line_no']} and {b['line_no']}: schedules overlap "
                    f"(can fire in the same minute) — '{a['command']}' vs "
                    f"'{b['command']}'. Consider staggering if they compete for "
                    f"the same resource.")


HORIZON_YEARS = 30  # > 28, the cycle after which dates fall on the same weekdays again


def next_n_runs(fields, n, start=None):
    """Next n run times after start (default: now), stdlib only. Walks day by day
    (minutes/hours only on matching days), so even a yearly or leap-day job is
    found quickly. Searches HORIZON_YEARS ahead."""
    expanded = {}
    for (name, lo, hi), raw in zip(FIELD_SPECS, fields):
        expanded[name] = expand_field_with_step(raw, name, lo, hi, [])

    dom_raw, dow_raw = fields[2], fields[4]
    dom_restricted = dom_raw.strip() != "*"
    dow_restricted = dow_raw.strip() != "*"
    minutes = sorted(expanded["minute"])
    hours = sorted(expanded["hour"])

    now = start or datetime.datetime.now().replace(second=0, microsecond=0)
    first = now + datetime.timedelta(minutes=1)
    day = first.date()
    last_day = day + datetime.timedelta(days=366 * HORIZON_YEARS)
    found = []

    while day < last_day and len(found) < n:
        dow = day.isoweekday() % 7  # Sunday=0 .. Saturday=6
        dom_match = day.day in expanded["day-of-month"]
        dow_match = dow in expanded["day-of-week"]
        if dom_restricted and dow_restricted:
            day_ok = dom_match or dow_match
        elif dom_restricted:
            day_ok = dom_match
        elif dow_restricted:
            day_ok = dow_match
        else:
            day_ok = True

        if day_ok and day.month in expanded["month"]:
            for hr in hours:
                for mi in minutes:
                    t = datetime.datetime(day.year, day.month, day.day, hr, mi)
                    if t >= first:
                        found.append(t)
                        if len(found) == n:
                            return found
        day += datetime.timedelta(days=1)

    return found


# --------------------------------------------------------------------------- plain-English description

DOW_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December"]


def _ordinal(n):
    return "%d%s" % (n, "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th"))


def _runs(values):
    """[1,2,3,5] -> [(1,3),(5,5)]"""
    out = []
    for v in sorted(values):
        if out and v == out[-1][1] + 1:
            out[-1] = (out[-1][0], v)
        else:
            out.append((v, v))
    return out


def _list(values, name=str, through=" through "):
    parts = []
    for a, b in _runs(values):
        if a == b:
            parts.append(name(a))
        elif b == a + 1:
            parts += [name(a), name(b)]
        else:
            parts.append(name(a) + through + name(b))
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _step_of(raw, lo):
    """'*/15' -> 15, '0-59/15' -> 15 when it starts at the field minimum; else None."""
    if "," in raw or "/" not in raw:
        return None
    base, step = raw.split("/", 1)
    if base in ("*", str(lo)) or base == "%d-%s" % (lo, base.split("-")[-1]):
        try:
            return int(step)
        except ValueError:
            return None
    return None


def describe(fields):
    """Plain-English description of a 5-field schedule, crontab.guru style."""
    exp = {name: expand_field_with_step(raw, name, lo, hi, []) for (name, lo, hi), raw in zip(FIELD_SPECS, fields)}
    if not all(exp.values()):
        return None  # unparseable field: nothing sensible to say
    m_raw, h_raw, dom_raw, mon_raw, dow_raw = [f.strip() for f in fields]
    minutes, hours = sorted(exp["minute"]), sorted(exp["hour"])

    if m_raw == "*" and h_raw == "*":
        when = "every minute"
    elif m_raw == "*":
        when = "every minute during hour " + _list(hours, lambda h: "%02d:00" % h)
    elif h_raw == "*":
        step = _step_of(m_raw, 0)
        if step:
            when = "every %d minutes" % step if step > 1 else "every minute"
        elif minutes == [0]:
            when = "every hour, on the hour"
        else:
            when = "at minute " + _list(minutes) + " past every hour"
    elif len(minutes) * len(hours) <= 6:
        when = "at " + _list([h * 60 + m for h in hours for m in minutes],
                             lambda t: "%02d:%02d" % divmod(t, 60), through=" through ")
    else:
        step = _step_of(m_raw, 0)
        mins = ("every %d minutes" % step) if step else ("at minute " + _list(minutes))
        hstep = _step_of(h_raw, 0)
        hrs = ("every %d hours" % hstep) if hstep else ("past hour " + _list(hours))
        when = "%s %s" % (mins, hrs)

    days = []
    dom_r, dow_r = dom_raw != "*", dow_raw != "*"
    if dom_r:
        days.append("on the " + _list(exp["day-of-month"], _ordinal) + " of the month")
    if dow_r:
        days.append("on " + _list(exp["day-of-week"], lambda d: DOW_NAMES[d]))
    text = when + (" " + " or ".join(days) if days else "")
    if mon_raw != "*":
        text += " in " + _list(exp["month"], lambda m: MONTH_NAMES[m])
    return text[0].upper() + text[1:]


# --------------------------------------------------------------------------- systemd OnCalendar

ONCALENDAR_SHORTHANDS = {
    "minutely": "*-*-* *:*:00", "hourly": "*-*-* *:00:00", "daily": "*-*-* 00:00:00",
    "monthly": "*-*-01 00:00:00", "weekly": "Mon *-*-* 00:00:00", "yearly": "*-01-01 00:00:00",
    "annually": "*-01-01 00:00:00", "quarterly": "*-01,04,07,10-01 00:00:00",
    "semiannually": "*-01,07-01 00:00:00",
}
SD_DOW = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 0}


SD_MAX = {"minute": 59, "hour": 23, "day": 31, "month": 12}


def _sd_component(part, what):
    """systemd list/range/step syntax ('1,3', '1..5', '*/15', '0/15') -> cron syntax."""
    out = []
    for item in part.split(","):
        if item.startswith("*") and "/" in item:
            out.append("*/" + item.split("/", 1)[1])
        elif "/" in item:
            base, step = item.split("/", 1)
            out.append("%d-%d/%d" % (int(base), SD_MAX[what], int(step)))
        elif ".." in item:
            a, b = item.split("..", 1)
            out.append("%d-%d" % (int(a), int(b)))
        elif item == "*":
            out.append("*")
        else:
            if "~" in item:
                raise CronFieldError("'~' (last day of month) in OnCalendar %s has no cron equivalent" % what)
            out.append(str(int(item)))
    return ",".join(out)


def oncalendar_to_cron(spec):
    """Translate a systemd OnCalendar= expression into the equivalent 5-field cron schedule.
    Raises CronFieldError for parts cron can't express (seconds other than 0, years, '~')."""
    s = " ".join(spec.strip().split())
    low = s.lower()
    if low in ONCALENDAR_SHORTHANDS:
        s = ONCALENDAR_SHORTHANDS[low]
    tokens = s.split(" ")
    dow = "*"
    if tokens and tokens[0][:1].isalpha():
        names = []
        for item in tokens.pop(0).split(","):
            ends = item.split("..")
            vals = [SD_DOW.get(e[:3].lower()) for e in ends]
            if None in vals:
                raise CronFieldError("unknown OnCalendar weekday '%s'" % item)
            names.append("%d-%d" % tuple(vals) if len(vals) == 2 else str(vals[0]))
        dow = ",".join(names)
    date, time_ = "*-*-*", "00:00:00"
    for t in tokens:
        if ":" in t:
            time_ = t
        elif "-" in t:
            date = t
        elif t:
            raise CronFieldError("can't parse OnCalendar part '%s'" % t)
    d = date.split("-")
    if len(d) == 2:
        d = ["*"] + d
    if len(d) != 3:
        raise CronFieldError("OnCalendar date must look like [YYYY-]MM-DD, got '%s'" % date)
    if d[0] != "*":
        raise CronFieldError("OnCalendar year '%s' has no cron equivalent" % d[0])
    tm = time_.split(":")
    if len(tm) == 2:
        tm.append("00")
    if len(tm) != 3:
        raise CronFieldError("OnCalendar time must look like HH:MM[:SS], got '%s'" % time_)
    if tm[2] not in ("00", "0"):
        raise CronFieldError("OnCalendar seconds '%s' can't be expressed in cron (minute resolution)" % tm[2])
    try:
        fields = [_sd_component(tm[1], "minute"), _sd_component(tm[0], "hour"), _sd_component(d[2], "day"),
                  _sd_component(d[1], "month"), dow]
    except ValueError as e:
        raise CronFieldError("can't parse OnCalendar '%s': %s" % (spec, e))
    return " ".join(fields)


# --------------------------------------------------------------------------- time zones / DST

def load_zone(name):
    """zoneinfo.ZoneInfo(name), or None when Python < 3.9 / tzdata missing."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return None
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def dst_issues(fields, zone, start=None, days=400):
    """Scheduled local times that fall into a DST gap (don't exist) or overlap (happen twice)
    in `zone` within `days` days. Returns a list of (kind, naive datetime)."""
    exp = {n: expand_field_with_step(raw, n, lo, hi, []) for (n, lo, hi), raw in zip(FIELD_SPECS, fields)}
    utc = datetime.timezone.utc
    day = (start or datetime.datetime.now()).date()
    issues = []

    def offset(dt, fold):
        return dt.replace(tzinfo=zone, fold=fold).utcoffset()

    prev = offset(datetime.datetime(day.year, day.month, day.day, 12), 0)
    for _ in range(days):
        day += datetime.timedelta(days=1)
        noon = offset(datetime.datetime(day.year, day.month, day.day, 12), 0)
        if noon != prev or offset(datetime.datetime(day.year, day.month, day.day, 0), 0) != noon:
            # a transition happened on this day (or the previous night): check the scheduled minutes
            # (dom/dow/month matching is the same as next_n_runs)
            sched = next_n_runs(fields, 1440, start=datetime.datetime(day.year, day.month, day.day) -
                                datetime.timedelta(minutes=1))
            for t in sched:
                if t.date() != day:
                    break
                aware0, aware1 = t.replace(tzinfo=zone, fold=0), t.replace(tzinfo=zone, fold=1)
                back = aware0.astimezone(utc).astimezone(zone).replace(tzinfo=None)
                if back != t:
                    issues.append(("gap", t))
                elif aware0.utcoffset() != aware1.utcoffset():
                    issues.append(("overlap", t))
        prev = noon
    return issues


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="cron-sanity-checker",
        description="Sanity-check cron schedule expressions for common footguns.",
        epilog="Exit codes: 0 no errors (warnings possible), 1 at least one error (or no job lines), 2 usage error.")
    ap.add_argument("--version", action="version", version="%(prog)s " + __version__)
    ap.add_argument("expression", nargs="?",
                    help="A single cron expression, quoted, e.g. '*/5 * * * *' "
                         "(command portion optional -- dummy 'noop' is appended "
                         "if you only pass 5 fields)")
    ap.add_argument("--file", help="Path to a crontab-style file (one job per line; CRON_TZ=/TZ= lines set the "
                                   "time zone for the lines after them)")
    ap.add_argument("--oncalendar", metavar="SPEC",
                    help="check a systemd timer OnCalendar= expression instead (e.g. 'Mon..Fri *-*-* 02:30')")
    ap.add_argument("--tz", metavar="ZONE",
                    help="IANA time zone the schedule runs in (e.g. America/New_York): enables DST gap/overlap "
                         "warnings (default: CRON_TZ/TZ lines in --file, else $TZ)")
    ap.add_argument("--show-next", type=int, metavar="N", default=0,
                    help="Show the next N computed run times for each valid job "
                         "(searches up to 30 years ahead)")
    ap.add_argument("--from", dest="start", metavar="'YYYY-MM-DD HH:MM'",
                    help="Compute --show-next run times (and DST checks) from this local time instead of now")
    ap.add_argument("--json", action="store_true", help="machine-readable JSON output")
    args = ap.parse_args(argv)
    start = None
    if args.start:
        try:
            start = datetime.datetime.strptime(args.start.strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            ap.error(f"--from must look like '2026-01-31 23:59', got '{args.start}'")
    if args.show_next < 0:
        ap.error("--show-next must be >= 0")

    default_tz = args.tz or os.environ.get("TZ") or None
    if default_tz and default_tz.startswith(":"):
        default_tz = default_tz[1:]
    if args.tz and load_zone(args.tz) is None:
        ap.error(f"unknown time zone '{args.tz}' (or this Python has no zoneinfo/tzdata; needs 3.9+)")

    lines = []
    source = None
    if sum(bool(x) for x in (args.file, args.expression, args.oncalendar)) > 1:
        ap.error("give one of: an expression, --file or --oncalendar")
    if args.file:
        try:
            with open(args.file) as f:
                lines = f.readlines()
        except (OSError, UnicodeDecodeError) as e:
            ap.error(f"can't read --file {args.file}: {e}")
    elif args.oncalendar:
        try:
            lines = [oncalendar_to_cron(args.oncalendar) + " noop"]
        except CronFieldError as e:
            return _emit(args, [], {"errors": [f"OnCalendar: {e}"], "warnings": []}, code=1)
        source = "OnCalendar=" + args.oncalendar
    elif args.expression:
        parts = args.expression.split()
        if len(parts) == 1 and parts[0].startswith("@"):
            parts.append("noop")
        elif len(parts) == 5:
            parts.append("noop")
        elif len(parts) < 5 and not (parts and parts[0].startswith("@")):
            return _emit(args, [], {"errors": [f"need at least 5 cron fields, got {len(parts)}"], "warnings": []},
                         code=1)
        lines = [" ".join(parts)]
    else:
        ap.print_help(sys.stderr)
        return 2

    results = {"errors": [], "warnings": []}
    jobs = []
    tz = default_tz
    for i, line in enumerate(lines, start=1):
        m = re.match(r"\s*(CRON_TZ|TZ)\s*=\s*['\"]?([^'\"\s]+)", line)
        if m:
            tz = m.group(2)
            if load_zone(tz) is None:
                results["warnings"].append(f"line {i}: {m.group(1)}={tz} is not a time zone this Python knows "
                                           f"(DST checks skipped; needs Python 3.9+ with tzdata)")
            continue
        job = analyze_line(line, i, results)
        if job:
            job["tz"] = tz
            jobs.append(job)

    compute_overlaps(jobs, results)

    for job in jobs:
        job["description"] = describe(job["fields"])
        if job["has_errors"]:
            continue
        zone = load_zone(job["tz"]) if job["tz"] else None
        if zone is not None:
            seen = set()
            for kind, t in dst_issues(job["fields"], zone, start):
                key = (kind, t.strftime("%H:%M"))
                if key in seen:
                    continue
                seen.add(key)
                if kind == "gap":
                    results["warnings"].append(
                        f"line {job['line_no']}: {t:%H:%M} does not exist on {t:%Y-%m-%d} in {job['tz']} "
                        f"(clocks jump forward for DST) -- depending on the cron daemon the run is skipped "
                        f"or moved; schedule outside the transition hour to be safe")
                else:
                    results["warnings"].append(
                        f"line {job['line_no']}: {t:%H:%M} happens twice on {t:%Y-%m-%d} in {job['tz']} "
                        f"(clocks go back for DST) -- some cron daemons run the job twice")
        if args.show_next:
            job["next"] = [r.isoformat(sep=" ") for r in next_n_runs(job["fields"], args.show_next, start=start)]

    code = 1 if (results["errors"] or not jobs) else 0
    return _emit(args, jobs, results, code, source)


def _emit(args, jobs, results, code, source=None):
    if args.json:
        print(json.dumps({
            "version": __version__, "exit_code": code, "source": source,
            "jobs": [{"line": j["line_no"], "schedule": " ".join(j["fields"]), "command": j["command"],
                      "description": j.get("description"), "time_zone": j.get("tz"), "ok": not j["has_errors"],
                      "next": j.get("next")} for j in jobs],
            "errors": results["errors"], "warnings": results["warnings"]}, indent=2))
        return code

    if not jobs:
        print("No valid cron job lines found to analyze.")
        for e in results["errors"]:
            print(f"  ✗ {e}")
        return code
    if source:
        print(f"{source}  =  cron '{' '.join(jobs[0]['fields'])}'")
    for job in jobs:
        status = "ERROR" if job["has_errors"] else "ok"
        tz = f"  [{job['tz']}]" if job.get("tz") else ""
        print(f"[{status}] line {job['line_no']}: {' '.join(job['fields'])}  ->  {job['command']}{tz}")
        if job.get("description"):
            print(f"    {job['description']}")
        if "next" in job:
            for r in job["next"]:
                print(f"    next: {r}")
            if len(job["next"]) < args.show_next:
                print(f"    (only {len(job['next'])} run(s) in the next {HORIZON_YEARS} years)")

    if results["errors"]:
        print("\nERRORS:")
        for e in results["errors"]:
            print(f"  ✗ {e}")
    if results["warnings"]:
        print("\nWARNINGS:")
        for w in results["warnings"]:
            print(f"  ! {w}")
    if not results["errors"] and not results["warnings"]:
        print("\nNo issues found.")
    return code


if __name__ == "__main__":
    sys.exit(main())
