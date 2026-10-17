# Changelog

All notable changes. Versions are git tags; each tag has a Forgejo release with checksums.

## 1.2.1 - 2026-10-17

- Day-of-month/day-of-week matching now follows Vixie cron/cronie exactly: a day field that *starts with* `*` (e.g. `*/2`) makes cron AND the two fields, so `0 0 */2 * 1` runs only on odd-dated Mondays. Next-run times, the OR warning, never-fires detection and descriptions were wrong for these schedules.
- Overlap detection follows the same day rule, so `0 0 1 * 1` (the 1st OR any Monday) now overlaps `0 0 2 * *`; it used to be missed.
- `0-30/15` is described as "At minute 0, 15 and 30", not "Every 15 minutes".
- Field errors from `--file` name their crontab line (`line 7: minute: value 60 out of range ...`).
- Tests: accuracy suite (known-bad and real-world known-good schedules, 300-expression differential corpus against croniter's parser + Vixie's day rule), mutation testing (nightly), claims ledger.

## 1.2.0 - 2026-10-16

- **MIT LICENSE file** (the README said "free to use" while Homebrew/Scoop declared MIT).
- `--json` output.
- Plain-English description of every schedule ("At 02:30 on Monday through Friday").
- Time zones: `--tz`, `CRON_TZ=`/`TZ=` lines in a crontab, or `$TZ`; warns when a run time doesn't exist
  (spring forward) or happens twice (fall back) in the next ~13 months. Needs Python 3.9+ for zoneinfo.
- `--oncalendar` checks a systemd timer `OnCalendar=` expression (translated to cron; untranslatable parts
  such as seconds or years are reported as errors).
- Overlap warnings no longer pair jobs that run in different time zones.
- Installable as a package: `pipx install git+https://git.canutethegreat.com/CanuteTheGreat/cron-sanity-checker.git`
  puts a `cron-sanity-checker` command on PATH (pyproject.toml, MIT license metadata).
- `--version` flag.
- Exit codes follow the shared convention: 0 = clean, 1 = findings at/above the gate, 2 = error or bad usage.
- Every tag now publishes a Forgejo release with this changelog, the wheel, the sdist and SHA256SUMS.
- CI installs the package with pip in a clean venv and checks `--version`/`--help` on every push.

## 1.1.0 - 2026-10-16

- Stop flagging '31 * *' as never-firing, support @macros, find yearly/leap-day runs (30y horizon), --from, show errors when no job parses, usage errors exit 2; add CI tests checked against croniter

## 1.0.0 - 2026-09-16

- First public release.
