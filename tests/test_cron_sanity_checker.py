"""End-to-end tests for cron_sanity_checker.py (run unmodified as a subprocess).

Next-run times from --show-next are cross-checked against croniter, an
independent, widely used cron implementation (installed by ci/setup.sh), so
the schedule math is compared with real cron semantics rather than with
expectations written by hand.
"""
import datetime
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
        self.assertIn("need at least 5 cron fields", p.stderr)
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

    def test_help(self):
        p = run("--help")
        self.assertEqual(p.returncode, 0)
        for opt in ("--file", "--show-next", "--from"):
            self.assertIn(opt, p.stdout)


if __name__ == "__main__":
    unittest.main()
