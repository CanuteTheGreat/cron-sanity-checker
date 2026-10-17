"""Detection accuracy: false negatives (known-bad schedules must be flagged) and false
positives (known-good, real-world schedules must come back clean), plus a differential
check of a large seeded random corpus of valid expressions against a reference.

Cases are judged against independent references, not against this tool's own output:
next run times against croniter (plain schedules) or croniter's parser + Vixie cron's day
rule (vixie_next), never-firing dates against Python's calendar, and the known-bad list
against the crontab grammar (with Debian's real `crontab` behaviour noted below).
"""
import datetime
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest

from croniter import croniter

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "cron_sanity_checker.py")
START = "2026-01-30 22:17"
BASE = datetime.datetime.strptime(START, "%Y-%m-%d %H:%M")


def run(*args):
    return subprocess.run([sys.executable, TOOL] + list(args), capture_output=True, text=True, timeout=300)


def run_file(text, *args):
    fd, path = tempfile.mkstemp(suffix=".crontab")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    try:
        return run("--file", path, *args)
    finally:
        os.unlink(path)


def vixie_next(expr, n):
    """Reference next-run computation: croniter parses and expands the fields; the day rule is
    Vixie cron's own (cron.c, Debian cron 3.0pl1): if the day-of-month or day-of-week field
    *starts with* '*', both must match, otherwise either may. croniter itself differs here
    (it treats '*/3' as a restriction and '*/1' as '*'), so it can't be the day-rule oracle."""
    exp = croniter(expr).expanded
    full = [range(60), range(24), range(1, 32), range(1, 13), range(7)]
    sets = [set(full[i]) if e == ["*"] else {v % 7 if i == 4 else v for v in e} for i, e in enumerate(exp)]
    f = expr.split()
    star = f[2].startswith("*") or f[4].startswith("*")
    out, day = [], BASE.date()
    while len(out) < n and day.year < BASE.year + 30:
        dom_ok, dow_ok = day.day in sets[2], day.isoweekday() % 7 in sets[4]
        if day.month in sets[3] and ((dom_ok and dow_ok) if star else (dom_ok or dow_ok)):
            for h in sorted(sets[1]):
                for m in sorted(sets[0]):
                    t = datetime.datetime(day.year, day.month, day.day, h, m)
                    if t > BASE and len(out) < n:
                        out.append(t.isoformat(sep=" "))
        day += datetime.timedelta(days=1)
    return out


def croniter_next(expr, n):
    it = croniter(expr, BASE)
    return [it.get_next(datetime.datetime).isoformat(sep=" ") for _ in range(n)]


# Known-bad: (expression, substring the tool's error must contain). These are bad by the POSIX
# crontab grammar (values outside a field's range, zero/non-numeric steps, start > end ranges,
# empty list items, unknown macros). Checked 2026-10-16 against Debian bookworm's own `crontab`
# (Vixie cron 3.0pl1): it rejects some of these, but silently installs `30-10 * * * *`,
# `* * * * 8`, `0 0 1,,15 * *` and `0 0 * 12-1 *`, which then never match -- exactly the
# mistakes this tool exists to catch. (croniter isn't a reference here: it accepts wraparound
# ranges as an extension.)
KNOWN_BAD = [
    ("60 * * * *", "minute: value 60 out of range"),
    ("* 24 * * *", "hour: value 24 out of range"),
    ("* * 0 * *", "day-of-month: value 0 out of range"),
    ("* * 32 * *", "day-of-month: value 32 out of range"),
    ("* * * 0 *", "month: value 0 out of range"),
    ("* * * 13 *", "month: value 13 out of range"),
    ("* * * * 8", "day-of-week: value 8 out of range"),
    ("0-60 * * * *", "minute: value 60 out of range"),
    ("0 20-24 * * *", "hour: value 24 out of range"),
    ("*/0 * * * *", "step-of-zero"),
    ("0 */0 * * *", "step-of-zero"),
    ("0 0 1-31/0 * *", "step-of-zero"),
    ("*/x * * * *", "non-numeric step"),
    ("30-10 * * * *", "backwards range"),
    ("0 18-6 * * *", "backwards range"),
    ("0 0 * 12-1 *", "backwards range"),
    ("0 0 * * 5-1", "backwards range"),
    ("0 noon * * *", "non-numeric value"),
    ("0 0 * * funday", "non-numeric value"),
    ("0 1-x * * *", "malformed range"),
    ("0 0 1,,15 * *", "empty sub-field"),
    ("@every_day job", "unknown schedule macro"),
    ("@daily", None),  # macro with nothing to run: fine from the CLI (a dummy command is added)
]
NEVER_FIRES = [
    ("0 0 30 2 *", "can never occur in Feb"),
    ("0 0 31 2 *", "can never occur in Feb"),
    ("0 0 30,31 2 *", "this job will never fire"),
    ("0 0 31 4 *", "can never occur in Apr"),
    ("0 0 31 6 *", "can never occur in Jun"),
    ("0 0 31 9 *", "can never occur in Sep"),
    ("0 0 31 11 *", "can never occur in Nov"),
    ("0 0 31 4,6,9,11 *", "can never occur in Apr, Jun, Sep, Nov"),
    ("0 0 31 feb,apr *", "can never occur in Feb, Apr"),
]

# Known-good, taken from real distribution/package crontabs and common runbooks. Each must be
# completely clean: exit 0, no errors, no warnings.
KNOWN_GOOD = [
    "17 * * * *",            # Debian /etc/crontab cron.hourly
    "25 6 * * *",            # Debian cron.daily
    "47 6 * * 7",            # Debian cron.weekly
    "52 6 1 * *",            # Debian cron.monthly
    "0 */12 * * *",          # certbot renew (Debian cron.d/certbot)
    "09,39 * * * *",         # php sessionclean (Debian cron.d/php)
    "5-55/10 * * * *",       # sysstat debian-sa1
    "59 23 * * *",           # sysstat daily summary
    "30 7 * * *",            # anacron
    "*/5 * * * *",
    "0 0 * * 0",
    "0 3 * * 1-5",
    "30 2 1 * *",
    "0 0 1 1 *",
    "0 9-17 * * mon-fri",
    "15 4 * * sat,sun",
    "0 0 31 * *",            # every month with a 31st is a normal, documented schedule
    "0 0 30 * *",
    "0 0 31 1,3,5,7,8,10,12 *",
    "0 12 28 2 *",
    "*/15 8-18 * * 1-5",
    "0 0 1 */3 *",
    "0 22 * * 1-5",
    "@daily",
    "@hourly",
    "@weekly",
    "@monthly",
    "@yearly",
]


def random_field(rng, lo, hi, names=None):
    def atom():
        kind = rng.choice(["star", "value", "range", "star-step", "range-step"])
        if kind == "star":
            return "*"
        if kind == "value":
            v = rng.randint(lo, hi)
            return names[v] if names and rng.random() < 0.3 else str(v)
        a = rng.randint(lo, hi - 1)
        b = rng.randint(a + 1, hi)
        if kind == "range":
            return "%d-%d" % (a, b)
        if kind == "star-step":
            return "*/%d" % rng.randint(1, max(1, (hi - lo) // 2))
        return "%d-%d/%d" % (a, b, rng.randint(1, max(1, b - a)))
    if rng.random() < 0.2:
        return ",".join(sorted({atom() for _ in range(rng.randint(2, 3))} - {"*"}) or ["*"])
    return atom()


MONTHS = {i + 1: n for i, n in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
DOWS = {i: n for i, n in enumerate("sun mon tue wed thu fri sat".split())}


def random_corpus(seed, n):
    rng = random.Random(seed)
    out = []
    while len(out) < n:
        expr = " ".join([random_field(rng, 0, 59), random_field(rng, 0, 23),
                         random_field(rng, 1, 28), random_field(rng, 1, 12, MONTHS),
                         random_field(rng, 0, 6, DOWS)])
        if expr not in out:
            out.append(expr)
    return out


class TestFalseNegatives(unittest.TestCase):
    def test_known_bad_schedules_are_errors(self):
        for expr, needle in KNOWN_BAD:
            with self.subTest(expr=expr):
                p = run(expr)
                if needle is None:
                    self.assertEqual(p.returncode, 0, p.stdout)
                    continue
                self.assertEqual(p.returncode, 1, p.stdout)
                d = json.loads(run(expr, "--json").stdout)
                self.assertTrue(any(needle in e for e in d["errors"]), (needle, d["errors"]))

    def test_never_firing_schedules_are_errors(self):
        for expr, needle in NEVER_FIRES:
            with self.subTest(expr=expr):
                d = json.loads(run(expr, "--json").stdout)
                self.assertEqual(d["exit_code"], 1)
                self.assertFalse(d["jobs"][0]["ok"])
                self.assertTrue(any(needle in e for e in d["errors"]), (needle, d["errors"]))
                # independent confirmation that no such date exists in a 30-year window
                fields = expr.split()
                doms, months = [int(x) for x in fields[2].split(",")], fields[3]
                mons = [MONTH_NUM.get(m, None) or int(m) for m in months.split(",")]
                for y in range(2026, 2056):
                    for m in mons:
                        for d_ in doms:
                            with self.assertRaises(ValueError):
                                datetime.date(y, m, d_)

    def test_bad_lines_in_a_crontab_are_each_reported(self):
        text = "".join("%s job%d\n" % (e, i) for i, (e, n) in enumerate(KNOWN_BAD) if n and not e.startswith("@"))
        d = json.loads(run_file(text, "--json").stdout)
        bad = [n for e, n in KNOWN_BAD if n and not e.startswith("@")]
        self.assertEqual(d["exit_code"], 1)
        self.assertEqual(len(d["jobs"]), len(bad))
        self.assertTrue(all(not j["ok"] for j in d["jobs"]), d["jobs"])
        for i, needle in enumerate(bad, start=1):
            self.assertTrue(any(e.startswith("line %d: " % i) and needle in e for e in d["errors"]),
                            (i, needle, d["errors"]))

MONTH_NUM = {v: k for k, v in MONTHS.items()}


class TestFalsePositives(unittest.TestCase):
    def test_real_world_schedules_are_clean(self):
        for expr in KNOWN_GOOD:
            with self.subTest(expr=expr):
                p = run(expr)
                self.assertEqual(p.returncode, 0, p.stdout)
                self.assertIn("No issues found.", p.stdout)
                d = json.loads(run(expr, "--json", "--show-next", "5", "--from", START).stdout)
                self.assertEqual((d["errors"], d["warnings"]), ([], []))
                sched = d["jobs"][0]["schedule"]
                self.assertEqual(d["jobs"][0]["next"], croniter_next(sched, 5), expr)

    def test_real_debian_crontab_is_clean(self):
        text = ("SHELL=/bin/sh\nPATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin\n"
                "# m h dom mon dow user\tcommand\n"
                "17 *\t* * *\troot\tcd / && run-parts --report /etc/cron.hourly\n"
                "25 6\t* * *\troot\ttest -x /usr/sbin/anacron || { cd / && run-parts --report /etc/cron.daily; }\n"
                "47 6\t* * 7\troot\ttest -x /usr/sbin/anacron || { cd / && run-parts --report /etc/cron.weekly; }\n"
                "52 6\t1 * *\troot\ttest -x /usr/sbin/anacron || { cd / && run-parts --report /etc/cron.monthly; }\n"
                "#\n")
        p = run_file(text)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("No issues found.", p.stdout)
        self.assertEqual(p.stdout.count("[ok]"), 4)


class TestDifferentialAgainstReference(unittest.TestCase):
    """300 seeded random valid expressions (lists, ranges, steps, names, both day fields):
    none may be reported as an error, the DOM/DOW warning must appear exactly when cron ORs the
    day fields (neither starts with '*'), and the next 5 run times must equal the reference's
    (croniter's field expansion + Vixie cron's day rule, see vixie_next)."""

    def test_random_corpus(self):
        corpus = random_corpus(20261016, 300)
        for chunk in range(0, len(corpus), 60):
            exprs = corpus[chunk:chunk + 60]
            for i, expr in enumerate(exprs):
                with self.subTest(expr=expr):
                    d = json.loads(run(expr, "--json", "--show-next", "5", "--from", START).stdout)
                    self.assertEqual(d["errors"], [], expr)
                    self.assertTrue(d["jobs"][0]["ok"])
                    f = expr.split()
                    both = not f[2].startswith("*") and not f[4].startswith("*")  # Vixie DOM_STAR/DOW_STAR
                    self.assertEqual(any("POSIX cron OR's" in w for w in d["warnings"]), both, (expr, d["warnings"]))
                    self.assertEqual(d["jobs"][0]["next"], vixie_next(expr, 5), expr)


class TestRegressions(unittest.TestCase):
    """Bugs the accuracy suite found (2026-10-16)."""

    def test_star_prefixed_day_field_means_and(self):
        # Vixie cron/cronie: '*/10' sets DOM_STAR, so the day fields are ANDed, not ORed
        for expr in ("51 * */10 */2 */1", "18-55 * */4,4 * */1", "0 0 */2 * 1", "0 12 1 * */2"):
            with self.subTest(expr=expr):
                d = json.loads(run(expr, "--json", "--show-next", "5", "--from", START).stdout)
                self.assertEqual(d["jobs"][0]["next"], vixie_next(expr, 5))
                self.assertFalse(any("POSIX cron OR's" in w for w in d["warnings"]), d["warnings"])
        d = json.loads(run("0 0 */2 * 1", "--json").stdout)
        self.assertIn(" and only on Monday", d["jobs"][0]["description"])

    def test_dead_dom_with_star_step_dow_never_fires(self):
        d = json.loads(run("0 0 30 2 */1", "--json").stdout)
        self.assertEqual(d["exit_code"], 1)
        self.assertTrue(any("this job will never fire" in e for e in d["errors"]), d["errors"])

    def test_overlap_through_or_semantics(self):
        # line 1 fires on the 1st OR any Monday; the 2nd is a Monday in some months
        d = json.loads(run_file("0 0 1 * 1 a\n0 0 2 * * b\n0 0 */2 * 1 c\n0 0 2 * 2 d\n", "--json").stdout)
        overlaps = sorted(w.split(":")[0] for w in d["warnings"] if "schedules overlap" in w)
        self.assertEqual(overlaps, ["lines 1 and 2", "lines 1 and 3", "lines 1 and 4", "lines 2 and 4"])

    def test_partial_step_is_not_every_n_minutes(self):
        for expr, want in (("0-30/15 * * * *", "At minute 0, 15 and 30 past every hour"),
                           ("0-59/15 * * * *", "Every 15 minutes"),
                           ("0/20 * * * *", "Every 20 minutes"),
                           ("*/30 0-11/3 * * *", "Every 30 minutes past hour 0, 3, 6 and 9"),
                           ("*/30 0-23/3 * * *", "Every 30 minutes every 3 hours")):
            with self.subTest(expr=expr):
                self.assertEqual(json.loads(run(expr, "--json").stdout)["jobs"][0]["description"], want)

    def test_field_errors_name_their_line(self):
        d = json.loads(run_file("0 0 * * *  ok\n60 0 * * * bad\n", "--json").stdout)
        self.assertEqual(d["errors"], ["line 2: minute: value 60 out of range [0-59] in '60'"])


if __name__ == "__main__":
    unittest.main()
