# cron-sanity-checker

Catches broken/misleading cron schedules *before* they bite you in production.
Zero dependencies (Python stdlib only), no network calls, works entirely offline.

A plain `crontab -e` syntax check won't catch any of these — this tool does:

- **Impossible dates** — a job that can never fire because its day-of-month never
  occurs in its month(s), e.g. `0 0 31 4 *` or `0 0 30 2 *` (error), plus dead
  combinations inside an explicit month list such as `31` with `1,4` (warning) and
  Feb 29 schedules that only fire in leap years (warning). `0 0 31 * *` (every month
  that has a 31st) is a normal schedule and is not flagged.
- **Out-of-range field values** — minute 60, hour 24, month 13, day-of-week 8, etc.
- **Backwards ranges** — `10-5` in a field where wraparound isn't valid.
- **Step-of-zero typos** — `*/0`, which some cron implementations silently
  treat as `*` (every unit), silently changing your intended schedule.
- **The classic day-of-month + day-of-week gotcha** — POSIX cron OR's these
  two fields together when both are restricted (fires if *either* matches),
  which surprises almost everyone expecting AND semantics.
- **Overlapping jobs** — two or more entries in the same crontab file that
  can fire in the same minute, a common cause of resource contention or
  silent races (flagged as a warning, not an error). Jobs in different time zones
  aren't compared.
- **DST traps** — with a time zone (`--tz`, a `CRON_TZ=`/`TZ=` line in the file, or
  `$TZ`) it warns about run times that don't exist on the spring-forward day (skipped
  or moved, depending on the daemon) or happen twice on the fall-back day. Needs
  Python 3.9+ (zoneinfo).
- **Plain-English description** of every schedule, e.g. `30 2 * * 1-5` ->
  "At 02:30 on Monday through Friday".
- **systemd timers** — `--oncalendar 'Mon..Fri *-*-* 02:30'` translates an
  `OnCalendar=` expression to cron and runs the same checks (seconds other than 0,
  years and `~` can't be expressed in cron and are reported as errors).

## Install

```sh
pipx install git+https://git.canutethegreat.com/CanuteTheGreat/cron-sanity-checker.git        # latest release branch; puts `cron-sanity-checker` on PATH
pipx install git+https://git.canutethegreat.com/CanuteTheGreat/cron-sanity-checker.git@v1.2.0  # a pinned release
brew install canutethegreat/errant-tools/cron-sanity-checker   # after: brew tap canutethegreat/errant-tools https://git.canutethegreat.com/CanuteTheGreat/homebrew-errant-tools
scoop install errant-tools/cron-sanity-checker                 # after: scoop bucket add errant-tools https://git.canutethegreat.com/CanuteTheGreat/scoop-errant-tools
```

Or download the single file (stdlib-only Python 3.8+) and run it directly:
`curl -fsSLO https://errant.solutions/downloads/cron_sanity_checker.py && python3 cron_sanity_checker.py --help`.
Every [release](https://git.canutethegreat.com/CanuteTheGreat/cron-sanity-checker/releases) has a changelog, the wheel, the sdist and a `SHA256SUMS` file.
Not on PyPI yet, so plain `pipx install cron-sanity-checker` doesn't work until it's published there.

## Usage

```
cron-sanity-checker "*/5 * * * *"
cron-sanity-checker --file /etc/crontab
cron-sanity-checker --file mycrontab --show-next 5 --json
cron-sanity-checker "30 2 * * *" --tz America/New_York        # DST gap warning
cron-sanity-checker --oncalendar "Mon..Fri *-*-* 02:30"
cron-sanity-checker "@weekly backup.sh" --show-next 3 --from "2026-01-01 00:00"
```

`--show-next N` prints the next N run times (real cron semantics: day-of-month and
day-of-week OR'd when both are set, `5/15` = `5-59/15`); `--from` computes them from a
given local time instead of now. Schedule macros (`@yearly`, `@annually`, `@monthly`,
`@weekly`, `@daily`, `@midnight`, `@hourly`) are expanded and checked; `@reboot` and
`VAR=value` lines are skipped (`CRON_TZ=`/`TZ=` set the zone for the lines after them).
`--json` prints `{"version", "exit_code", "jobs": [{"line", "schedule", "command",
"description", "time_zone", "ok", "next"}], "errors", "warnings"}`. Python 3.8+.

Exit codes: `0` = no errors (warnings still possible), `1` = at least one error found
(or no job lines), `2` = usage error (no input, unreadable `--file`, bad `--from`/`--tz`).

## Example

```
$ cron-sanity-checker "0 0 31 4 * some-job.sh"
[ERROR] line 1: 0 0 31 4 *  ->  some-job.sh
    At 00:00 on the 31st of the month in April

ERRORS:
  ✗ line 1: day-of-month [31] can never occur in Apr — this job will never fire
```

## License

MIT — see LICENSE. Free to use; pay-what-you-want if it saved you a debugging session:
see the Stripe link on https://errant.solutions

## Tests

`tests/` runs every option end to end in Forgejo CI on Python 3.8 and current Python,
cross-checking next-run times against croniter, then installs the package with pip and
checks `--version`/`--help`; `sh ci/run-tests.sh 3.13` reproduces it locally (needs docker).

— Errant Solutions
