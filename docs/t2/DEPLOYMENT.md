# Deploying an independent T2 instance

This guide applies to `instances/t2`, an isolated execution plane layered on the existing FIE PostgreSQL/API deployment. It is not a replacement for M2. Follow the repository [production database contract](../production-database-contract.md) and existing deploy/runbooks to provision a separate FIE database/API pair first. Do not point a fresh install at existing M2/P2 volumes. The host installation and existing services are not provisioned automatically by this extension.

## Prerequisites and private configuration

Use macOS or Linux, Python 3.11+ (the T2 evidence run used 3.14), Docker/psql, and the existing FIE API/schema version 6.5. Ensure enough disk, TLS CA trust, DNS, network and service-user access. A user with Docker socket access has host-level power; the SQL role/cluster guards are application safeguards. No secrets should appear in shell arguments, Git, report artifacts, logs or scheduler plists.

Required shell variables in the examples are `FIE_INSTANCE_ROOT` (absolute independent root), `FIE_PYTHON` (Python executable), and `FIE_REPO` (checkout). These are path settings without credential values. The extension does not consume a Yahoo API key or FRED key; FRED uses existing public CSV. Private files hold the API token (`secrets/api-token`, mode 0600) and existing DB/API deployment secrets outside Git. Existing core deployments may additionally require `FIE_*`/PostgreSQL variables documented in their own `.env.example`; never copy M2's `.env` or delivery configuration. This extension never reads Telegram/email credentials.

```sh
export FIE_INSTANCE_ROOT=/ABS/INDEPENDENT/T2
export FIE_REPO=/ABS/finance-intelligence-engine
export FIE_PYTHON=/ABS/python3
umask 077
mkdir -p "$FIE_INSTANCE_ROOT"
cp -R "$FIE_REPO/instances/t2/". "$FIE_INSTANCE_ROOT/"
"$FIE_PYTHON" -m venv "$FIE_INSTANCE_ROOT/.venv"
"$FIE_INSTANCE_ROOT/.venv/bin/python" -m pip install -r "$FIE_INSTANCE_ROOT/requirements-yfinance.txt"
"$FIE_INSTANCE_ROOT/.venv/bin/python" "$FIE_INSTANCE_ROOT/bin/prepare.py" --initialize
```

Edit the private generated `config/deployment.json`: absolute Docker executable, dedicated DB/app container names, dedicated raw/intelligence DB names, admin/writer roles, actual PostgreSQL `system_identifier`, loopback API URL and read principal. Values in the example are nonsecret synthetic defaults; `cluster=0` is deliberately unusable. `src/runtime_config.py` validates identifiers before SQL interpolation. This is a portable export of the T2 snapshot: existing host-pinned constants become private deployment pins; the source adapter and report contract are otherwise the same. Existing T2 was not repinned or moved by this release.

Set `config/ingestion.json.container_identities` using actual `docker inspect` container ID **and image ID**, not mutable tags. Put the yfinance interpreter's absolute path in `config/yfinance.json.python`; keep the pinned version and daily interval. Set unique universe identifiers in `config/universe.json` after verifying code/name mappings. Configure source allowlists in `config/research.json`; never silently replace a failed feed. Keep `external_writes=false` and every side-effect `external_writes=[]`. `bin/prepare.py --seal` synchronizes instance/ledger database names with deployment pins; review the rest of the ledger before sealing.

Build a verified calendar using the schema in `templates/calendar.json.example`: coverage dates, Taipei closures, extraordinary closures, US Treasury/equity holidays, per-series FRED publication rules, verification time/expiry and private evidence paths with SHA256. The placeholder calendar is intentionally invalid. Preserve the existing verified T2 calendar/evidence on upgrade; never replace it with this template. October 23 closure and October 26 holiday override a Yahoo bar or weekday cron. Missing/changed/expired evidence fails closed. Refresh before expiry, test and reseal.

## Schema and additive migration contracts

Core FIE public tables, API views and existing migration ledger must already match the repository's normal PostgreSQL deployment. `migrations/ingestion-{raw,intelligence}.sql` describes the existing T2 ingestion additions; `research-{raw,intelligence}.sql` describes the additive research schema reused by yfinance. Apply each only to its named layer, in a transaction, with the role and exact target independently verified. Default writer is `fie_t2_ingestor NOLOGIN`; if changing it, render validated role identifiers in the SQL. No DELETE/TRUNCATE or public schema DDL is required for yfinance parity.

The research raw layer has content-keyed `batches`. Intelligence has append-only `versions`, latest `heads`, committed `receipts`, and source-backed `reports`; `heads` permits only version/observed_at update. The source adapter reuses these tables; no new production migration was needed for the October 25 T2 install. Scheduler SQLite is local execution state, not a substitute for PostgreSQL market data. Never edit core migration checksums to make a deployment appear current.

For an existing accepted T2 deployment: preserve configuration, token, DBs, report history, cache, ledger and calendar. Back up only changed code/config for rollback, install reviewed source changes, add `config/yfinance.json` and its side-effect entry, and include both in the activation manifest. Do not copy public template defaults over working runtime configuration.

## Validate, seal, start and restart

Run the offline adapter tests from the source tree before production enablement:

```sh
"$FIE_PYTHON" -B -m unittest discover -s "$FIE_REPO/instances/t2/tests" -v
"$FIE_PYTHON" -I "$FIE_REPO/scripts/scan_secrets.py" --selftest
```

The existing host's 74 regression tests and Hermes 20 read/refusal checks use private evidence and remain a host acceptance requirement; the public 19 adapter tests use synthetic temp roots and need no credentials/network. Existing repository CI also remains required. Never run tests against production DBs.

Start with `enabled_jobs=[]`. After API/source/DB guard validation, review the ledger and enable only intended collectors/monitors/research jobs. Obtain real source/persistence/replay receipts before enabling `yfinance`. Keep opening/tail/publisher absent until the actual-day checklist below passes. `market_day_accepted=false` remains a second hard gate.

```sh
"$FIE_INSTANCE_ROOT/.venv/bin/python" "$FIE_INSTANCE_ROOT/bin/prepare.py" --seal
"$FIE_INSTANCE_ROOT/.venv/bin/python" -I -B "$FIE_INSTANCE_ROOT/src/fie_t2.py" status
"$FIE_INSTANCE_ROOT/.venv/bin/python" -I -B "$FIE_INSTANCE_ROOT/src/fie_t2.py" schedules
```

Render one service template from `templates/`, using this root/interpreter and a unique T2 service identity. For Linux install the rendered service/timer and use `systemctl --user daemon-reload` / `enable --now fie-t2.timer` (omit `User=` for a user unit). For macOS use `launchctl bootstrap gui/$(id -u) /ABS/RENDERED.plist`. Restart only that T2 service: systemd stop/start its timer, or launchctl bootout/bootstrap its plist. Do not restart Docker or shared PostgreSQL/AEE/Hermes services to deploy this adapter. Reboots/login restoration require their own acceptance.

## Actual trading-day acceptance (not completed on Sunday)

1. Confirm current verified calendar and expected next session. October 27 2026 is expected, subject to updated official closures.
2. At 09:30 obtain T2's actual yfinance batch, exact source labels, coverage, volumes, distinct fetch times, normalized-body hashes, raw commit and intelligence receipt. Verify same-session labels; previous close must return DATA_DELAYED. No assertion of realtime trade time is permitted.
3. Replay the exact real batch: immutable batch/version/receipt counts must not grow. Test a source outage, stale checkpoint and clean recovery; restart must skip completed slots.
4. At 13:00 verify a real pre-close sample and valid same-day opening artifact hashes. Report must say LIMITED and pre-close, include unknown latest-trade time, explicit missing strategy/scoring inputs, and make no new trading recommendation.
5. Verify the local publication pointer once, replay suppression, watchdog receipts and `/fie-t2-read`. No Telegram/email/external website dispatch. Record actual artifacts and checks, then deliberately set `market_day_accepted=true`, enable only the approved local report jobs and reseal. A historical fixture never supplies this evidence.

If these checks fail, keep staged jobs disabled and record the exact source/time/coverage/lineage blocker. Free/unofficial source operation and complete M2 strategy-score parity are separate acceptance boundaries.

## Rollback

Stop only the T2 timer/LaunchAgent; take the instance lock and preserve current config, activation manifest, source/report receipts and new data. Restore the prechange code/config copies, remove new jobs from `enabled_jobs`/side-effect ledger, and reseal to the restored audited files. Leave raw/intelligence history and additive schemas intact. Validate original collector/RSS/company/read behavior before starting the T2 service again. Do not drop tables, reset scheduler state, force-push Git or roll back M2/P2. Rollback is code/config only; full backup expansion remains deferred.

See [isolated rehearsal remediation](DRY_RUN_REMEDIATION.md) for stale-data,
production publisher, bounded strategy, Hermes namespace, rollback and later
live-session activation requirements. Production report jobs remain disabled.
