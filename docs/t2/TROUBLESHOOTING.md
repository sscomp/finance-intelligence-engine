# Troubleshooting and receipts

| Observation | Meaning / action |
|---|---|
| `AUDIT_DIGEST_CHANGED` or audit coverage mismatch | Code/config differs from reviewed activation. Disable new jobs, inspect diff/tests and reseal deliberately; never remove the gate. |
| `YFINANCE_FETCH_FAILED_OR_OVERSIZED`, timeout, HTTP 429/auth failure | Bound the attempt, retain evidence and let slot backoff apply. Check CA/DNS/provider availability/version. Do not install a paid replacement, rotate identities or loop indefinitely as an automatic fix. |
| `DATA_DELAYED`, previous-session bar only | Provider has no usable current-session daily bar. Preserve old data as historical; do not relabel it as intraday. |
| Daily timestamp is midnight | Expected daily-index semantics. `source_as_of` is not latest trade. A new fetch cannot certify realtime freshness. |
| Provider bar on known closure | Record `provider_calendar_conflict_dates`; verified calendar wins. Never open a closed session because Yahoo returned a bar. |
| `CALENDAR_UNAVAILABLE_OR_EXPIRED` | Refresh original evidence and hashes/coverage/verification, rerun tests, reseal. A missing calendar cannot become a weekday fallback. |
| Source checkpoint expired or failed | Check original fetch time (cache preserves it), raw body hash, batch ID, receipt and current coverage. Failed collection cannot keep an old checkpoint usable. |
| DB/container/cluster refused | Verify private pins. Do not update them merely to suppress a refusal; confirm intended independent targets first. |
| Tail has no valid same-day opening | Remain DEGRADED; do not use yesterday's opening or a modified artifact. |
| Publisher returns DEGRADED | No valid same-day source/report or actual-day acceptance; no pointer update. Check report hashes and timestamp. |
| Completed slot is SKIPPED after restart | Correct idempotence. Do not delete SQLite rows to make another run appear successful. |
| Watchdog DEGRADED while report disabled | Expected on an open session until the staged producer is accepted; watchdog health does not authorize publication. |
| Core health PASS, yfinance failed | Core health covers the original pipeline. Check `state/research-yfinance_daily.json` and source-specific raw/receipt evidence separately. |

The accepted report path is source → normalized response hash → raw content batch → intelligence versions/heads/receipt → checkpoint → calendar/freshness/coverage → report hashes → local pointer → read-only Hermes. Separate generated time, source bar time, fetch time and first observation. A PASS fetch is not a PASS quote/report.

For support, record the Git commit, activation hash, calendar evidence hash/expiry, interpreter/yfinance versions, job ID/slot/attempt, original fetch time, bar dates, coverage, batch/receipt/report IDs and safe error category. Keep source bodies and host evidence private. Avoid API tokens, environment dumps, DB dumps, personal cards or delivery IDs in GitHub issues/PRs. The source is for personal research; no exchange-certified or guaranteed-live claim should appear in incident summaries.

The 50-row loopback latest query can exceed 8 seconds under load. Only that read has a bounded 20-second timeout; other API reads retain 8 seconds. Check measured API latency before restarting any service.
