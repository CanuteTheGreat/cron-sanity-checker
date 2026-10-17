"""End-to-end tests for cron_sanity_checker.py (run unmodified as a subprocess).

Next-run times from --show-next are cross-checked against croniter, an
independent, widely used cron implementation (installed by ci/setup.sh), so
the schedule math is compared with real cron semantics rather than with
expectations written by hand.
"""
import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest

from croniter import croniter

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "cron_sanity_checker.py")
START = "2026-01-30 22:17"


def run(*args):
    return subprocess.run([sys.executable, TOOL] + list(args), capture_output=True, text=True, timeout=300)


def crontab(text):
    fd, path = tempfile.mkstemp(suffix=".crontab")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


def next_runs(stdout):
    return [l.split("next:", 1)[1].strip() for l in stdout.splitlines() if "next:" in l]


class TestErrors(unittest.TestCase):
    def assert_error(self, expr, needle):
        p = run(expr)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("[ERROR] line 1", p.stdout)
        self.assertIn(needle, p.stdout)

    def test_out_of_range_values(self):
        for expr, needle in (("60 * * * *", "minute: value 60 out of range [0-59]"),
                             ("0 24 * * *", "hour: value 24 out of range [0-23]"),
                             ("0 0 32 * *", "day-of-month: value 32 out of range [1-31]"),
                             ("0 0 0 * *", "day-of-month: value 0 out of range"),
                             ("0 0 * 13 *", "month: value 13 out of range [1-12]"),
                             ("0 0 * * 8", "day-of-week: value 8 out of range")):
            self.assert_error(expr, needle)

    def test_backwards_range(self):
        self.assert_error("0 10-5 * * *", "backwards range '10-5'")

    def test_step_of_zero_and_bad_steps(self):
        self.assert_error("*/0 * * * *", "step-of-zero '*/0'")
        self.assert_error("*/x * * * *", "non-numeric step 'x'")

    def test_garbage_values(self):
        self.assert_error("0 noon * * *", "hour: non-numeric value 'noon'")
        self.assert_error("0 1-x * * *", "malformed range '1-x'")
        self.assert_error("0 1,,2 * * *", "empty sub-field")

    def test_never_fires(self):
        self.assert_error("0 0 31 4 * some-job.sh", "day-of-month [31] can never occur in Apr")
        self.assert_error("0 0 30,31 2 *", "this job will never fire")

    def test_wrong_field_count(self):
        p = run("0 0 * *")
        self.assertEqual(p.returncode, 1)
        self.assertIn("need at least 5 cron fields", p.stdout)
        path = crontab("0 0 * *\n")
        try:
            p = run("--file", path)
        finally:
            os.unlink(path)
        self.assertEqual(p.returncode, 1)
        self.assertIn("expected 5 schedule fields + command", p.stdout)


class TestWarnings(unittest.TestCase):
    def test_valid_but_skips_short_months_is_clean(self):
        p = run("0 0 31 * *")  # every month that has a 31st: a normal schedule
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("No issues found.", p.stdout)

    def test_partly_dead_month_list_warns(self):
        p = run("0 0 31 1,4 *")
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("day-of-month [31] can never occur in Apr (month #4)", p.stdout)
        self.assertNotIn("Jan (month #1)", p.stdout)

    def test_dom_and_dow_or_gotcha(self):
        p = run("0 9 1 * mon")
        self.assertEqual(p.returncode, 0)
        self.assertIn("POSIX cron OR's these together", p.stdout)

    def test_dead_dom_with_dow_still_fires(self):
        p = run("0 0 30 2 1", "--show-next", "2", "--from", START)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("still fires on its day-of-week values", p.stdout)
        self.assertEqual(next_runs(p.stdout), ["2026-02-02 00:00:00", "2026-02-09 00:00:00"])

    def test_leap_day(self):
        p = run("0 0 29 2 *", "--show-next", "1", "--from", START)
        self.assertEqual(p.returncode, 0)
        self.assertIn("only exists in leap years", p.stdout)
        self.assertEqual(next_runs(p.stdout), ["2028-02-29 00:00:00"])

    def test_overlapping_jobs_in_file(self):
        path = crontab("SHELL=/bin/sh\nMAILTO=ops@errant.solutions\n# nightly\n"
                       "0 2 * * * /usr/local/bin/backup.sh\n"
                       "*/30 * * * * /usr/local/bin/sync.sh\n"
                       "15 2 * * * /usr/local/bin/report.sh\n"
                       "@reboot /usr/local/bin/on-boot.sh\n")
        try:
            p = run("--file", path)
        finally:
            os.unlink(path)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("lines 4 and 5: schedules overlap", p.stdout)
        self.assertNotIn("lines 4 and 6", p.stdout)
        self.assertNotIn("lines 5 and 6", p.stdout)
        self.assertNotIn("on-boot", p.stdout)        # @reboot has no schedule
        self.assertNotIn("SHELL", p.stdout)          # env lines skipped
        self.assertEqual(p.stdout.count("[ok]"), 3)


class TestShowNextAgainstCroniter(unittest.TestCase):
    EXPRESSIONS = ["*/5 * * * *", "0 0 * * *", "30 8 * * mon-fri", "0 */6 1,15 * *", "0 0 31 * *",
                   "15 14 1 * *", "0 22 * * 1-5", "23 0-20/2 * * *", "0 4 8-14 * 6", "0 0 1 jan,jul *",
                   "5 4 * * sun", "0 9 1 * mon", "0 0 * * 7", "*/20 9-17 * 3-5 *", "59 23 31 12 *"]

    def test_next_runs_match_croniter(self):
        base = datetime.datetime.strptime(START, "%Y-%m-%d %H:%M")
        for expr in self.EXPRESSIONS:
            p = run(expr, "--show-next", "6", "--from", START)
            self.assertEqual(p.returncode, 0, (expr, p.stdout))
            it = croniter(expr, base)
            want = [it.get_next(datetime.datetime).strftime("%Y-%m-%d %H:%M:%S") for _ in range(6)]
            self.assertEqual(next_runs(p.stdout), want, expr)

    def test_single_value_with_step(self):
        p = run("5/15 * * * *", "--show-next", "4", "--from", "2026-01-01 00:00")
        self.assertEqual(next_runs(p.stdout), ["2026-01-01 00:05:00", "2026-01-01 00:20:00",
                                               "2026-01-01 00:35:00", "2026-01-01 00:50:00"])

    def test_macros(self):
        base = datetime.datetime.strptime(START, "%Y-%m-%d %H:%M")
        for macro, expr in (("@yearly", "0 0 1 1 *"), ("@annually", "0 0 1 1 *"), ("@monthly", "0 0 1 * *"),
                            ("@weekly", "0 0 * * 0"), ("@daily", "0 0 * * *"), ("@midnight", "0 0 * * *"),
                            ("@hourly", "0 * * * *")):
            p = run(macro + " job.sh", "--show-next", "3", "--from", START)
            self.assertEqual(p.returncode, 0, (macro, p.stdout))
            self.assertIn(expr + "  ->  job.sh", p.stdout)
            it = croniter(expr, base)
            want = [it.get_next(datetime.datetime).strftime("%Y-%m-%d %H:%M:%S") for _ in range(3)]
            self.assertEqual(next_runs(p.stdout), want, macro)

    def test_show_next_without_from_starts_now(self):
        p = run("* * * * *", "--show-next", "1")
        got = datetime.datetime.strptime(next_runs(p.stdout)[0], "%Y-%m-%d %H:%M:%S")
        self.assertLess(abs((got - datetime.datetime.now()).total_seconds()), 180)

    def test_rare_schedule_reports_short_list(self):
        p = run("0 0 29 2 *", "--show-next", "9", "--from", START)  # leap days: 7 within 30 years
        self.assertEqual(len(next_runs(p.stdout)), 7, p.stdout)
        self.assertIn("(only 7 run(s) in the next 30 years)", p.stdout)

    def test_no_next_runs_for_jobs_with_errors(self):
        p = run("0 0 31 4 *", "--show-next", "3")
        self.assertEqual(next_runs(p.stdout), [])


class TestCli(unittest.TestCase):
    def test_unknown_macro(self):
        p = run("@fortnightly job.sh")
        self.assertEqual(p.returncode, 1)
        self.assertIn("unknown schedule macro '@fortnightly'", p.stdout)

    def test_usage_errors_exit_2(self):
        for args in ((), ("--file", "/nonexistent/crontab"), ("* * * * *", "--from", "tomorrow")):
            p = run(*args)
            self.assertEqual(p.returncode, 2, (args, p.stdout + p.stderr))
            self.assertNotIn("Traceback", p.stderr)

    def test_file_with_only_comments(self):
        path = crontab("# nothing here\n\n")
        try:
            p = run("--file", path)
        finally:
            os.unlink(path)
        self.assertEqual(p.returncode, 1)
        self.assertIn("No valid cron job lines found", p.stdout)

    def test_version(self):
        p = run("--version")
        self.assertRegex(p.stdout, r"^cron-sanity-checker \d+\.\d+\.\d+$")

    def test_help(self):
        p = run("--help")
        self.assertEqual(p.returncode, 0)
        for opt in ("--file", "--show-next", "--from", "--json", "--tz", "--oncalendar"):
            self.assertIn(opt, p.stdout)


HAS_ZONEINFO = sys.version_info >= (3, 9)


class TestDescribe(unittest.TestCase):
    CASES = {
        "* * * * *": "Every minute",
        "*/5 * * * *": "Every 5 minutes",
        "0 * * * *": "Every hour, on the hour",
        "15,45 * * * *": "At minute 15 and 45 past every hour",
        "30 2 * * 1-5": "At 02:30 on Monday through Friday",
        "0 0 1 1 *": "At 00:00 on the 1st of the month in January",
        "0 */6 * * *": "At 00:00, 06:00, 12:00 and 18:00",
        "15,45 9-17 1,15 * *": "At minute 15 and 45 past hour 9 through 17 on the 1st and 15th of the month",
        "0 9 13 * 5": "At 09:00 on the 13th of the month or on Friday",
        "*/10 8-18 * * 1-5": "Every 10 minutes past hour 8 through 18 on Monday through Friday",
        "0 0 22 * 0,6": "At 00:00 on the 22nd of the month or on Sunday and Saturday",
    }

    def test_descriptions(self):
        for expr, want in self.CASES.items():
            with self.subTest(expr=expr):
                p = run(expr)
                self.assertEqual(p.returncode, 0, p.stdout)
                self.assertEqual(p.stdout.splitlines()[1].strip(), want, p.stdout)

    def test_no_description_for_unparseable(self):
        p = run("0 noon * * *")
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("Traceback", p.stderr)


class TestJson(unittest.TestCase):
    def test_json_output(self):
        p = run("*/15 * * * *", "--json", "--show-next", "3", "--from", START)
        self.assertEqual(p.returncode, 0, p.stdout)
        d = json.loads(p.stdout)
        self.assertEqual(d["exit_code"], 0)
        job = d["jobs"][0]
        self.assertEqual(job["schedule"], "*/15 * * * *")
        self.assertEqual(job["description"], "Every 15 minutes")
        want = croniter("*/15 * * * *", datetime.datetime.strptime(START, "%Y-%m-%d %H:%M"))
        self.assertEqual(job["next"], [want.get_next(datetime.datetime).isoformat(sep=" ") for _ in range(3)])
        p = run("0 0 31 4 *", "--json")
        self.assertEqual(p.returncode, 1)
        d = json.loads(p.stdout)
        self.assertFalse(d["jobs"][0]["ok"])
        self.assertTrue(any("can never occur" in e for e in d["errors"]))
        p = run("0 0 * *", "--json")
        self.assertEqual(json.loads(p.stdout)["jobs"], [])


class TestOnCalendar(unittest.TestCase):
    def test_translations_match_cron(self):
        for spec, cron in (("daily", "0 0 * * *"), ("hourly", "0 * * * *"), ("weekly", "0 0 * * 1"),
                           ("monthly", "0 0 1 * *"), ("quarterly", "0 0 1 1,4,7,10 *"),
                           ("Mon..Fri *-*-* 02:30", "30 2 * * 1-5"), ("Sat,Sun 10:00", "0 10 * * 6,0"),
                           ("*-*-* *:0/15", "0-59/15 * * * *"), ("*-02-29 12:00:00", "0 12 29 2 *"),
                           ("*-*-01..07 03:00", "0 3 1-7 * *")):
            with self.subTest(spec=spec):
                p = run("--oncalendar", spec, "--json", "--show-next", "4", "--from", START)
                d = json.loads(p.stdout)
                self.assertEqual(d["jobs"][0]["schedule"], cron)
                it = croniter(cron, datetime.datetime.strptime(START, "%Y-%m-%d %H:%M"))
                self.assertEqual(d["jobs"][0]["next"], [it.get_next(datetime.datetime).isoformat(sep=" ") for _ in range(4)])
        p = run("--oncalendar", "Mon..Fri *-*-* 02:30")
        self.assertIn("OnCalendar=Mon..Fri *-*-* 02:30  =  cron '30 2 * * 1-5'", p.stdout)

    def test_untranslatable_specs_are_errors(self):
        for spec, needle in (("*-*-* 00:00:30", "seconds"), ("2027-01-01 00:00", "year"),
                             ("*-*~01 00:00", "can't parse"), ("Funday 00:00", "unknown OnCalendar weekday")):
            with self.subTest(spec=spec):
                p = run("--oncalendar", spec)
                self.assertEqual(p.returncode, 1, p.stdout)
                self.assertIn(needle, p.stdout)
        self.assertEqual(run("--oncalendar", "daily", "0 0 * * *").returncode, 2)


class TestTimeZones(unittest.TestCase):
    def test_dst_gap_and_overlap(self):
        p = run("30 2 * * *", "--tz", "America/New_York", "--from", "2026-01-01 00:00")
        if not HAS_ZONEINFO:
            self.assertEqual(p.returncode, 2)
            self.assertIn("needs 3.9+", p.stderr)
            return
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("02:30 does not exist on 2026-03-08 in America/New_York", p.stdout)
        p = run("30 1 * * *", "--tz", "America/New_York", "--from", "2026-01-01 00:00")
        self.assertIn("01:30 happens twice on 2026-11-01 in America/New_York", p.stdout)
        # weekdays only: the US transitions are on Sundays, so nothing to warn about
        p = run("30 2 * * 1-5", "--tz", "America/New_York", "--from", "2026-01-01 00:00")
        self.assertIn("No issues found.", p.stdout)
        # UTC has no DST; outside the transition hour is fine
        self.assertIn("No issues found.", run("30 2 * * *", "--tz", "UTC").stdout)
        self.assertIn("No issues found.", run("0 12 * * *", "--tz", "Europe/Berlin").stdout)

    def test_cron_tz_lines_in_file(self):
        path = crontab("CRON_TZ=Europe/London\n30 1 * * * job-a\nTZ=UTC\n30 1 * * * job-b\n")
        try:
            p = run("--file", path, "--from", "2026-01-01 00:00", "--json")
        finally:
            os.unlink(path)
        d = json.loads(p.stdout)
        if not HAS_ZONEINFO:
            self.assertTrue(any("not a time zone this Python knows" in w for w in d["warnings"]))
            return
        self.assertEqual([j["time_zone"] for j in d["jobs"]], ["Europe/London", "UTC"])
        self.assertEqual(sorted(w.split(":", 1)[0] for w in d["warnings"]), ["line 2", "line 2"])
        self.assertTrue(any("does not exist on 2026-03-29 in Europe/London" in w for w in d["warnings"]))
        self.assertTrue(any("happens twice on 2026-10-25 in Europe/London" in w for w in d["warnings"]))

    def test_bad_zone_is_usage_error(self):
        p = run("* * * * *", "--tz", "Mars/Olympus_Mons")
        self.assertEqual(p.returncode, 2)


if __name__ == "__main__":
    unittest.main()
