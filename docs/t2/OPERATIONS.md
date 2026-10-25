# Independent FIE instances (2026-10-25)

This deployment is personal research/reporting. It does not execute trades or certify exchange quotes. The free, unofficial yfinance adapter is an explicitly limited daily-bar source. See [sources](DATA_SOURCES.md), [deployment](DEPLOYMENT.md), [scheduling](SCHEDULING.md), and [recovery](TROUBLESHOOTING.md).

| Instance | Runtime | Authority and state | Deployment policy |
|---|---|---|---|
| M2 / Abacus-M2 | Existing Linux macro-report + strategy_reports + Hermes | Its PostgreSQL, Python environment, job registry, credentials and delivery settings | Permanent independent production. This change does not install, restart, overwrite or migrate it. |
| T2 / MacBookPro-T2 | Independent instance root, loopback FIE P3 HTTP service, dedicated raw/intelligence PostgreSQL DBs, local scheduler | Its own API token, DB roles, container/cluster pins, cache, report artifacts and durable slots | New adapter and reference runtime under `instances/t2`; external Telegram/email/website upload disabled. |
| T2 P2 | Existing separate DBs/files | Existing independent data | No writes, migrations or deployment from this extension. |

No shared runtime paths, mounted M2 volumes, copied cookies, credentials, scheduler state, reports, person watchlists or private datasets are required. Set a distinct root, database pair, container names, role, ports, secrets and scheduler identity for every deployment. Do not use an M2 production database as a T2 target. The repository default branch is `master`.

## Accepted scope and current release state

M2's inspected source SOP calls `Ticker("0050.TW").history(period="5d")`, whose yfinance 1.4.1 default interval is `1d`. It requests 17 market symbols and an extra `2382.TW` `1mo` tail structure. T2 reproduces those history calls explicitly, including adjusted OHLCV, aware exchange daily-index timestamps and the free provider. The source call mechanism is replicated, not the complete M2 discretionary strategy or private person cards. Existing T2 macro scoring remains partial; RSS and company reports keep their bounded factual contracts.

T2 changes: independent verified calendar, sequential network subprocess with 420-second outer timeout, explicit 900-second cache, immutable content identities, guarded PostgreSQL persistence, durable report/publish receipts and local-only delivery. It does not treat a missing daily bar as proof the exchange is closed. A current-date daily bar can support a clearly labelled LIMITED observation, never a 120-second realtime quote gate. Previous close cannot become today's intraday price. The report has `latest_trade_at=null` and `live_report_allowed=false`.

As of the Sunday implementation: the actual provider can be probed and persisted, and offline replay can test gates. Opening 09:30, pre-close 13:00 and publisher stay **staged/disabled** until actual trading-day acceptance is recorded in private `config/yfinance.json` and the activation seal is reviewed. `market_day_accepted=false` is a hard stop even if someone adds those jobs to `enabled_jobs`. No October 27 operational acceptance is claimed on October 25. Free-source limitations are not replaced with a requirement to buy another provider.

## Routine operation

Run the same Python interpreter and instance root as the service. The `src/fie_t2.py` CLI provides `status`, `schedules`, `ingestion`, `market`, `report`, `research sources`, `research industry_weekly`, `research company_monthly`, `research opening`, `research tail`, and `independent-health`. Reads do not fetch yfinance or send notifications. T2 Hermes `/fie-t2-read` wraps the read-only CLI; preserve its allowlist and existing token. No new Hermes jobs or external delivery targets are installed.

One `tick` runs under the instance flock. Durable SQLite `(instance,job,slot)` completion suppresses repeat execution after restart. At most three attempts per slot, 60/120-second backoff; no unbounded catchup. Source batches are raw-first, content-addressed, then intelligence versions/heads/receipt are committed together under an advisory lock. Crash recovery replays the same batch; it does not delete history. Local publication verifies hashes, source age, calendar and same-day report before changing a private local pointer. A content marker prevents repeated publication. There is no notification sender.

Inspect heartbeat (<180 seconds), monitor (<420 seconds), per-source attempt/success times, actual source dates, row coverage, body hash, raw batch, intelligence receipt, scheduler slot and report hashes. A fresh fetch is not a fresh market observation. Core independent health monitors the original pipeline; inspect yfinance component receipts separately. Keep logs and evidence private, mode 0600, directories 0700.
