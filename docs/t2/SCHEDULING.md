# Scheduler design and cadence

Use one executor per instance. Prefer one 60-second launchd/systemd tick over many independent cron invocations; it centralizes flock, source ordering, durable slots, dependencies, retries and bounded recovery. Cron is a wake-up mechanism, not a market calendar. All TWSE decisions use `Asia/Taipei` (no DST). US FRED releases use the appropriate US timezone/calendar, including DST; never hardcode a Taiwan offset to a US release time.

| Function | Recommended cadence | Installed T2 policy in this release |
|---|---|---|
| yfinance daily bars | Hourly during market sessions; fetch before each report. No hourly 24x7 polling. | `20 9-13 * * 1-5`, additionally verified-calendar gated; source may be enabled after real source/persistence checks. 900s cache, 4500s checkpoint maximum. Report jobs remain staged until actual-day acceptance. |
| TWSE daily price/valuation | Once after publication; hourly market window with one postclose refresh if recovery is needed | Existing `0 * * * *` all-day schedule retained to preserve working collectors. This is collection cadence, not realtime data. A future cadence edit requires source-publication tests. |
| TWSE institutional | After official daily publication, bounded retry/recovery | Existing hourly schedule retained. |
| RSS | Daily collection sufficient for weekly report; 6-hour collection when earlier news visibility matters | Three feeds every 6h at minute 10; weekly report Monday 08:00. No aggressive per-minute scrape. |
| Fundamentals | Monthly after publication, with bounded daily checks around expected release; quarterly financial updates after availability | Existing daily 07:20 collector and monthly report on the 12th 08:00 retained. Missing required prior-month period is NOT_RELEASED, never old-period substitution. |
| FRED | Publication-based per series; bounded retry after US release | Existing hourly collector retained. Series freshness uses US calendars, not TWSE. |
| Monitor | Every 5 min | `*/5 * * * *`; local alert state changes only. |
| Opening | Trading day 09:30 | `30 9 * * 1-5`, disabled pending actual-day acceptance. |
| Pre-close | Trading day 13:00 | `0 13 * * 1-5`, disabled; requires valid same-day opening. This is not the final close. |
| Watchdogs | 09:45 / 13:15 | `45 9 * * 1-5`, `15 13 * * 1-5`, retained, local-only. Closed day returns CLOSED. |
| Local website/publication pointer | Only after a valid same-day report/source | `*/10 9-13 * * 1-5`, staged/disabled; no external upload. |

At 09:20/10:20/11:20/12:20/13:20 the hourly yfinance source is eligible on verified trading days. Opening uses the 09:20 observation (or accepted cache); pre-close uses 12:20, less than 4500 seconds old. That age limits fetch/checkpoint staleness; it is not the age of the last exchange trade. If current-session daily labels are missing or cached source is too old, fail with DATA_DELAYED/DEGRADED and no valid local publish. The scheduler orders source work before consumers. Do not introduce a second collector cron for this same instance.

Periodic jobs process the current bucket only. Fixed jobs have same-day catchup at most 60 minutes; no prior-day replay. A restart after the report window is not acceptance. Three slot attempts with 60/120-second backoff cap network retries; exhausted slots require inspecting the cause, not deleting the ledger. No duplicate external notifications can occur because this extension has no external sender.

## Portable service examples

Copy/render `instances/t2/templates/fie-t2.plist.example` or `fie-t2.service.example` + `fie-t2.timer.example`. Replace every placeholder with this instance's absolute root/interpreter. macOS LaunchAgents require the intended user login and Docker availability; reboot/login restoration still needs testing. Linux systemd uses its own service user and a distinct instance root. Docker socket access is privileged even when application SQL uses a restricted role.

For cron-only hosts, use exactly one entry (absolute paths are deliberate):

```cron
* * * * * /ABS/T2/.venv/bin/python -I -B /ABS/T2/src/fie_t2.py tick >> /ABS/T2/logs/scheduler.log 2>&1
```

The Python executor applies Taipei timezone regardless of cron host timezone. Do not run this together with launchd/systemd. Avoid shell `TZ=` assumptions for calendar correctness. Never install the T2 service under the M2 service identity.

See [isolated rehearsal remediation](DRY_RUN_REMEDIATION.md) for stale-data,
production publisher, bounded strategy, Hermes namespace, rollback and later
live-session activation requirements. Production report jobs remain disabled.
