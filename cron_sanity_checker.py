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

import argparse
import sys
import datetime
import calendar

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


def main():
    ap = argparse.ArgumentParser(
        description="Sanity-check cron schedule expressions for common footguns.")
    ap.add_argument("expression", nargs="?",
                     help="A single cron expression, quoted, e.g. '*/5 * * * *' "
                          "(command portion optional — dummy 'noop' is appended "
                          "if you only pass 5 fields)")
    ap.add_argument("--file", help="Path to a crontab-style file (one job per line)")
    ap.add_argument("--show-next", type=int, metavar="N", default=0,
                     help="Show the next N computed run times for each valid job "
                          "(searches up to 30 years ahead)")
    ap.add_argument("--from", dest="start", metavar="'YYYY-MM-DD HH:MM'",
                     help="Compute --show-next run times after this local time "
                          "instead of now")
    args = ap.parse_args()
    start = None
    if args.start:
        try:
            start = datetime.datetime.strptime(args.start.strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            ap.error(f"--from must look like '2026-01-31 23:59', got '{args.start}'")

    lines = []
    if args.file:
        try:
            with open(args.file) as f:
                lines = f.readlines()
        except (OSError, UnicodeDecodeError) as e:
            ap.error(f"can't read --file {args.file}: {e}")
    elif args.expression:
        parts = args.expression.split()
        if len(parts) == 1 and parts[0].startswith("@"):
            parts.append("noop")
        elif len(parts) == 5:
            parts.append("noop")
        elif len(parts) < 5 and not (parts and parts[0].startswith("@")):
            print(f"error: need at least 5 cron fields, got {len(parts)}",
                  file=sys.stderr)
            sys.exit(1)
        lines = [" ".join(parts)]
    else:
        ap.print_help(sys.stderr)
        sys.exit(2)

    results = {"errors": [], "warnings": []}
    jobs = []
    for i, line in enumerate(lines, start=1):
        job = analyze_line(line, i, results)
        if job:
            jobs.append(job)

    compute_overlaps(jobs, results)

    if not jobs:
        print("No valid cron job lines found to analyze.")
        for e in results["errors"]:
            print(f"  ✗ {e}")
        sys.exit(1)

    for job in jobs:
        status = "ERROR" if job["has_errors"] else "ok"
        print(f"[{status}] line {job['line_no']}: "
              f"{' '.join(job['fields'])}  ->  {job['command']}")
        if args.show_next and not job["has_errors"]:
            runs = next_n_runs(job["fields"], args.show_next, start=start)
            for r in runs:
                print(f"    next: {r.isoformat(sep=' ')}")
            if len(runs) < args.show_next:
                print(f"    (only {len(runs)} run(s) in the next {HORIZON_YEARS} years)")

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

    sys.exit(1 if results["errors"] else 0)


if __name__ == "__main__":
    main()
