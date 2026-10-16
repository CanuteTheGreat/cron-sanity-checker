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
  silent races (flagged as a warning, not an error).

## Usage

```
./cron_sanity_checker.py "*/5 * * * *"
./cron_sanity_checker.py --file /etc/crontab
./cron_sanity_checker.py --file mycrontab --show-next 5
./cron_sanity_checker.py "@weekly backup.sh" --show-next 3 --from "2026-01-01 00:00"
```

`--show-next N` prints the next N run times (real cron semantics: day-of-month and
day-of-week OR'd when both are set, `5/15` = `5-59/15`); `--from` computes them from a
given local time instead of now. Schedule macros (`@yearly`, `@annually`, `@monthly`,
`@weekly`, `@daily`, `@midnight`, `@hourly`) are expanded and checked; `@reboot` and
`VAR=value` lines are skipped. Python 3.8+.

Exit codes: `0` = no errors (warnings still possible), `1` = at least one error found
(or no job lines), `2` = usage error (no input, unreadable `--file`, bad `--from`).

## Example

```
$ ./cron_sanity_checker.py "0 0 31 4 * some-job.sh"
[ERROR] line 1: 0 0 31 4 *  ->  some-job.sh

ERRORS:
  ✗ line 1: day-of-month [31] can never occur in Apr — this job will never fire
```

## License

Free to use. Pay-what-you-want if it saved you a debugging session:
see the Stripe link on https://errant.solutions

— Errant Solutions
