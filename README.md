# cron-sanity-checker

Catches broken/misleading cron schedules *before* they bite you in production.
Zero dependencies (Python stdlib only), no network calls, works entirely offline.

A plain `crontab -e` syntax check won't catch any of these — this tool does:

- **Impossible dates** — day-of-month values that never occur in the given
  month(s), e.g. `30` in February, `31` in April/June/September/November.
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
```

Exit codes: `0` = no errors (warnings still possible), `1` = at least one error found.

## Example

```
$ ./cron_sanity_checker.py "0 0 31 4 * some-job.sh"
[ERROR] line 1: 0 0 31 4 *  ->  some-job.sh

ERRORS:
  ✗ day-of-month [31] can never occur in Apr (month #4) — this job segment
    will never fire for that month/day combination
```

## License

Free to use. Pay-what-you-want if it saved you a debugging session:
see the Stripe link on https://errant.solutions

— Errant Solutions
