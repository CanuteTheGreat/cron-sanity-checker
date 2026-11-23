"""Shared CLI contract (docs/CLI-CONTRACT.md): exit codes, --json schema, --quiet, stderr for errors."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

import contract_support as cs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "cron_sanity_checker.py")
sys.path.insert(0, ROOT)
import cron_sanity_checker as tool  # noqa: E402

SCHEMA = cs.load_schema(ROOT, "cron-sanity-checker")
CLEAN = "30 2 * * *"
BAD = "60 * * * *"


def cli(*args):
    return cs.run_cli(TOOL, args)


def crontab(text):
    fd, path = tempfile.mkstemp(suffix=".crontab")
    with os.fdopen(fd, "w") as f:
        f.write(text)
    return path


class TestExitCodes(unittest.TestCase):
    def test_clean_is_0(self):
        self.assertEqual(cli(CLEAN)[0], 0)

    def test_findings_is_1(self):
        self.assertEqual(cli(BAD)[0], 1)

    def test_bad_args_is_2_with_message_on_stderr(self):
        for args in (("--no-such-flag", CLEAN), (CLEAN, "--show-next", "-1"), (CLEAN, "--from", "yesterday"),
                     (CLEAN, "--tz", "No/Such_Zone"), (CLEAN, "--file", "x")):
            rc, out, err = cli(*args)
            self.assertEqual(rc, 2, args)
            self.assertEqual(out, "", args)
            self.assertIn("usage:", err)

    def test_no_input_is_2(self):
        rc, out, err = cli()
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("usage:", err)

    def test_missing_file_is_2(self):
        rc, out, err = cli("--file", os.path.join(tempfile.gettempdir(), "no-such-crontab-file.xyz"))
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("can't read --file", err)

    def test_unexpected_error_is_3_on_stderr_not_a_traceback_exit_1(self):
        buf = io.StringIO()
        with mock.patch.object(tool, "_main", side_effect=RuntimeError("boom")), redirect_stderr(buf):
            self.assertEqual(tool.main([CLEAN]), 3)
        self.assertIn("RuntimeError: boom", buf.getvalue())


class TestJson(unittest.TestCase):
    def check(self, args, code):
        rc, out, err = cli("--json", *args)
        self.assertEqual(rc, code, err)
        doc = json.loads(out)  # exactly one JSON document, nothing else on stdout
        self.assertEqual(cs.validate(doc, SCHEMA), [])
        self.assertEqual(doc["exit_code"], rc)
        self.assertEqual(doc["schema_version"], 1)
        self.assertEqual(doc["tool"], "cron-sanity-checker")
        self.assertEqual(doc["version"], tool.__version__)
        return doc

    def test_clean(self):
        doc = self.check((CLEAN,), 0)
        self.assertEqual(doc["errors"], [])

    def test_findings(self):
        doc = self.check((BAD,), 1)
        self.assertTrue(doc["errors"])

    def test_file_with_next_runs(self):
        path = crontab("30 2 * * * backup\n")
        try:
            doc = self.check(("--file", path, "--show-next", "2", "--from", "2026-01-30 22:17"), 0)
        finally:
            os.unlink(path)
        self.assertEqual(len(doc["jobs"][0]["next"]), 2)

    def test_oncalendar_error_and_short_expression_keep_the_envelope(self):
        self.check(("--oncalendar", "not a calendar"), 1)
        self.check(("* * *",), 1)

    def test_usage_error_leaves_stdout_empty(self):
        rc, out, err = cli("--json")
        self.assertEqual((rc, out), (2, ""))
        self.assertIn("usage:", err)

    def test_schema_rejects_a_document_without_the_contract_keys(self):
        self.assertNotEqual(cs.validate({"jobs": []}, SCHEMA), [])
        self.assertNotEqual(cs.validate({"schema_version": 1, "tool": "x", "version": "1", "exit_code": 9,
                                         "jobs": [], "errors": [], "warnings": []}, SCHEMA), [])


class TestQuiet(unittest.TestCase):
    def test_quiet_prints_nothing_but_keeps_exit_code(self):
        for expr, code in ((CLEAN, 0), (BAD, 1)):
            for extra in ((), ("--json",)):
                for flag in ("--quiet", "-q"):
                    rc, out, err = cli(flag, *(extra + (expr,)))
                    self.assertEqual((rc, out), (code, ""), (flag, extra, expr))

    def test_quiet_with_unparseable_oncalendar_and_short_expression(self):
        for args in (("--oncalendar", "not a calendar"), ("* * *",)):
            self.assertEqual(cli("--quiet", *args)[:2], (1, ""))

    def test_without_quiet_the_report_is_printed(self):
        self.assertIn("No issues found", cli(CLEAN)[1])


if __name__ == "__main__":
    unittest.main()
