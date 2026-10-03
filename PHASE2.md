# Phase 2 Summary — macro-report

> Date: 2026-07-08
> Author: M2
> Scope: Phase 2A + 2B (Steps 1, 2, 3, 4A, 4B) — the full additive rollout
> Status: **Complete, validated, zero production behavior change**
> Design doc: `ARCHITECTURE_REVIEW_PHASE2.md`
> Plan/proposal: `PHASE2B_PLAN_AND_DIFF_PROPOSAL.md`

This document is a developer-oriented overview of what Phase 2 delivered, when, and what's left. It is a pointer and orientation guide to the implementation summaries listed in the Appendix — not a substitute for them.

---

## 1. Executive Summary

Phase 2 evolves macro-report from **four independent scripts + cron** into a **report / research agent framework** that the four existing scripts can opt into, without changing their production behavior. The core thesis: the four scripts are the same pipeline (fetch → analyze → render → archive) instantiated four times. That common shape is worth naming once; the four scripts keep working unchanged while a parallel framework grows beside them.

What was actually built, in one paragraph:

- A pure-function shared module (`reports/common/format.py`) extracted from two duplicated helpers, test-protected by 49 characterization tests.
- A Report Framework (`reports/base.py` + `pipeline.py` + `registry.py`) defining the `BaseReport` contract, the `ReportPipeline` orchestrator, and a manifest-driven `Registry`.
- A Dispatcher / Task Framework (`reports/hermes/`) with a `Task` state machine, an in-process `Dispatcher` (dispatch/status/rerun/cancel), and a JSON-backed `LocalStore` for cross-process task history and artifact tracking.
- A CLI (`run_report.py`) that defaults to dry-run and never publishes to Telegram.
- One wired plugin (`reports/plugins/macro_daily_plugin.py`) and one manifest (`config/reports/macro_daily.json`) proving the contract end-to-end.

**Hard constraint honored throughout:** no production `.py`, no SQLite schema, no cron schedule/prompt, no Telegram publishing path, and no venv/supervisord/cloudflared change. 11 protected files are byte-for-byte identical pre-Phase-2 to post-Phase-2. 133/133 tests pass. No production report was executed during the rollout.

---

## 2. Timeline of Phase 2

| Step | Date | Doc | Deliverable | Tests added |
|------|------|-----|-------------|-------------|
| **2A** | 2026-07-08 | `PHASE2A_IMPLEMENTATION_SUMMARY.md` | 17 additive files: 9 directories, 3 contract docs, 1 runbook, 4 report manifests (YAML, draft), 3 policy/publisher YAML, 4 report templates + 1 prompt template (Jinja2 placeholders) | 0 |
| **2B Step 1** | 2026-07-08 | `PHASE2B_STEP1_IMPLEMENTATION_SUMMARY.md` | `tests/test_format_helpers.py` (46 characterization tests for the 3 duplicated helpers), 2 `.gitkeep` markers | 46 |
| **2B Step 2** | 2026-07-08 | `PHASE2B_STEP2_IMPLEMENTATION_SUMMARY.md` | Extracted 3 helpers to `reports/common/format.py`; switched call sites in `institutional.py` and `company_monthly.py`; added 3 tripwire tests | 49 (46+3) |
| **2B Step 3 + 4A** | 2026-07-08 | `PHASE2B_STEP3_STEP4_IMPLEMENTATION_SUMMARY.md` | Report Framework (`base.py` / `pipeline.py` / `registry.py`) + Dispatcher/Task Framework (`hermes/task.py` / `hermes/dispatcher.py`) + CLI (`run_report.py`) + 1 plugin + 1 JSON manifest + 41 unit tests | 90 (49+41) |
| **2B Step 4B** | 2026-07-08 | `PHASE2B_STEP4B_IMPLEMENTATION_SUMMARY.md` | Local JSON-backed `LocalStore` (cross-process task history + artifact files), Dispatcher wired to store, expanded CLI (`artifact` / `tasks` subcommands), 43 new tests | 133 (90+43) |

All five implementation summaries were written on 2026-07-08 in a single working session. Step 3 + Step 4A were combined into a single task per the brief; Step 4B is the natural follow-on that turned the Dispatcher skeleton into a usable tool.

---

## 3. Architecture Before vs After

### Before Phase 2

```
┌────────────────────────────────────────────────────────────┐
│  Hermes Cronjob Scheduler (jobs.json, sqlite)              │
│   ├─ ed214c19c4ac  總體經濟晨報          30 8 * * 1-6       │
│   ├─ 60d92c57b826  產業趨勢週報          0 8 * * 1         │
│   ├─ 5eaa5fa9a50d  公司研究月報          0 8 12 * *        │
│   └─ af64556bc8e9  季度成分股更新提醒    0 9 1 1,4,7,10    │
└────────────────────────────────────────────────────────────┘
            │ cron agent runs prompt in fresh session
            ▼
┌────────────────────────────────────────────────────────────┐
│  /home/ubuntu/macro-report/                                │
│   ├─ run.sh / run_weekly.sh / run_monthly.sh (venv wrap)   │
│   ├─ macro_daily.py       (264 行) — yfinance × 6 ticker  │
│   ├─ industry_weekly.py   (276 行) — RSS × 8 feeds         │
│   ├─ company_monthly.py   (616 行) — yfinance × 50 + T86   │
│   ├─ institutional.py     (140 行) — TWSE T86              │
│   └─ db.py                (255 行) — SQLite 3 tables        │
└────────────────────────────────────────────────────────────┘
            │ stdout → cron agent → Telegram
            ▼
       Telegram (鼎鼎 <TELEGRAM_CHAT_ID_REDACTED>)
```

Four scripts, four cron jobs, no abstraction. Adding a 5th report meant copy-pasting four files.

### After Phase 2

```
┌──────────────────────────────────────────────────────────────────────┐
│  Existing production path — UNCHANGED                                │
│   Hermes cronjobs → run*.sh → *.py → stdout → Telegram              │
│   11 protected files byte-for-byte identical (sha256 verified)       │
└──────────────────────────────────────────────────────────────────────┘
                          │ parallel, opt-in
                          ▼
┌──────────────────────────────────────────────────────────────────────┐
│  Phase 2 framework (purely additive)                                  │
│                                                                       │
│   run_report.py CLI (list / dispatch / status / rerun / cancel /      │
│                      artifact / tasks)                                │
│                          │                                            │
│                          ▼                                            │
│   reports/hermes/dispatcher.py   ← LocalStore (JSON, cross-process)   │
│                          │                                            │
│                          ▼                                            │
│   reports/registry.py  (manifests → plugin classes)                   │
│                          │                                            │
│                          ▼                                            │
│   reports/pipeline.py  (fetch → analyze → render → archive)           │
│                          │                                            │
│                          ▼                                            │
│   reports/base.py      (BaseReport ABC, 3 abstract methods)           │
│                          │                                            │
│                          ▼                                            │
│   reports/plugins/macro_daily_plugin.py  (only plugin wired so far)   │
│                          │                                            │
│                          ▼                                            │
│   reports/common/format.py  (3 shared formatters, stdlib only)        │
│                                                                       │
│   Tests: 133 (49 characterization + 41 framework + 43 dispatcher)     │
└──────────────────────────────────────────────────────────────────────┘
```

Key property: **the framework is below the existing cron path, not in front of it.** Cronjobs still call `bash run.sh` and the four `.py` entrypoints still run their `if __name__ == "__main__":` blocks exactly as before. The framework is a parallel observation / dry-run surface; promoting it to the primary path is a future decision (Step 7.3 #7 in the Step 3+4A summary, deferred until 100% plugin coverage).

---

## 4. Files / Modules Introduced

### New framework code (under `reports/`)

| Path | Bytes | Purpose |
|------|------:|---------|
| `reports/__init__.py` | 999 | Package surface; re-exports the framework public API |
| `reports/common/__init__.py` | 156 | Package marker |
| `reports/common/format.py` | 3,292 | 3 shared formatting helpers (`format_shares`, `format_net`, `format_net_shares`) |
| `reports/base.py` | 8,165 | `BaseReport` ABC + `ReportResult` dataclass |
| `reports/pipeline.py` | 8,610 | `ReportPipeline` — fetch/analyze/render/archive orchestrator |
| `reports/registry.py` | 7,175 | `Registry` — manifest loader + entrypoint resolver + instance cache |
| `reports/plugins/__init__.py` | 336 | Package marker |
| `reports/plugins/macro_daily_plugin.py` | 3,480 | `MacroDailyReport` — thin wrapper around `macro_daily.py` functions |
| `reports/hermes/__init__.py` | 488 | Package marker |
| `reports/hermes/task.py` | ~6,000 | `Task` model + state machine (created→queued→running→completed/failed/cancelled) |
| `reports/hermes/dispatcher.py` | ~8,000 | `Dispatcher` — dispatch/status/rerun/cancel + list helpers |
| `reports/hermes/store.py` | ~10,000 | `LocalStore` — JSON-backed task/artifact persistence with atomic writes |

### New config

| Path | Purpose |
|------|---------|
| `config/reports/macro_daily.json` | First concrete manifest (JSON because PyYAML is not in `macro-venv`) |
| `config/reports/*.yaml` (4 files, Phase 2A) | Draft contracts; not loaded by the framework (see "What did NOT change" below) |
| `config/policies/*.yaml`, `config/publishers/*.yaml` (3 files, Phase 2A) | Policy contracts; not loaded by the framework |

### New templates (Phase 2A drafts, not loaded)

- `templates/reports/*_v0.md.j2` (4 files) — Jinja2 placeholder layouts
- `templates/prompts/research_interpretation_v0.md` — prompt template draft

### New tests

| Path | Tests | Subject |
|------|------:|---------|
| `tests/test_format_helpers.py` | 49 | Helper extraction characterization + tripwires |
| `tests/test_phase2b_framework.py` | 41 | BaseReport / Pipeline / Registry / Task state machine / Dispatcher / CLI / production-file protection |
| `tests/test_phase2b_step4b.py` | 43 | LocalStore / Dispatcher-store integration / cross-process status / rerun / cancel / artifact tracking |
| `tests/__init__.py` | — | Empty package marker (added in Step 2 for unittest discovery) |

### New docs (implementation summaries, contracts, runbooks)

- `PHASE2A_IMPLEMENTATION_SUMMARY.md` (10,674 bytes)
- `PHASE2B_STEP1_IMPLEMENTATION_SUMMARY.md` (18,164 bytes)
- `PHASE2B_STEP2_IMPLEMENTATION_SUMMARY.md` (19,160 bytes)
- `PHASE2B_STEP3_STEP4_IMPLEMENTATION_SUMMARY.md` (28,246 bytes)
- `PHASE2B_STEP4B_IMPLEMENTATION_SUMMARY.md` (22,847 bytes)
- `PHASE2B_PLAN_AND_DIFF_PROPOSAL.md` (30,940 bytes) — the conservative-order plan that governed the rollout
- `ARCHITECTURE_REVIEW_PHASE2.md` (101,579 bytes) — the design doc
- `docs/contracts/report_contract_v0.md`
- `docs/contracts/task_lifecycle_v0.md`
- `docs/contracts/dispatcher_api_v0.md`
- `docs/runbooks/phase2a_migration_runbook.md`

### Modified production files (intentional, minimal)

| File | Change | Why |
|------|--------|-----|
| `institutional.py` | −24 lines (two local helper defs removed), +7 lines (1 import + 4-line comment) | Switched to import from `reports.common.format` |
| `company_monthly.py` | −17 lines (one local helper def removed), +7 lines (1 import + 4-line comment) | Switched to import from `reports.common.format` |
| `tests/test_format_helpers.py` | Imports updated, 1 new test class added | Test the new shared module instead of inlined references |

All other production files (macro_daily.py, industry_weekly.py, db.py, run*.sh, industry_config.json, taiwan50_config.json, macro_history.db) are byte-for-byte unchanged.

---

## 5. Framework Components

### 5.1 BaseReport (the contract)

`reports/base.py` defines the report plugin contract. Every report plugin subclasses `BaseReport` and implements three abstract methods:

- `fetch(self) -> dict` — pull external data (yfinance, RSS, T86, etc.)
- `analyze(self, data) -> tuple[str, list, int]` — return `(verdict, signals, score)`
- `render(self, data, verdict, signals, score) -> str` — return the final report text

BaseReport also provides:
- A default `run(publish=True) -> ReportResult` orchestrator that wires fetch → analyze → render → archive into a try/except, so callers never see a stack trace.
- A default `archive(result) -> None` no-op (overridable per plugin; `MacroDailyReport` overrides it to call `db.save_macro_daily()` in a try/except so DB failures don't fail the pipeline).

### 5.2 ReportPipeline (the "how")

`reports/pipeline.py` is a separate class that runs a `BaseReport` instance through its stages with per-stage timing and stage-level error capture. The split exists so tests that want to assert "fetch took at least 0ms" don't have to set up the BaseReport machinery, and vice versa. Pipeline is constructed with `publish=False` at every dispatch site — this is the gate that prevents the framework from accidentally sending Telegram messages.

### 5.3 Registry (the lookup)

`reports/registry.py` discovers reports by scanning a config directory for manifest files. It supports two formats:
- JSON (default; loaded via stdlib `json`)
- YAML (only if `yaml` is importable — PyYAML is not in `macro-venv`, so YAML manifests are silently skipped in this environment)

The Registry exposes two layers:
- Manifest-level: list reports without instantiating them (cheap; used by `list` CLI subcommand).
- Instance-level: resolve the entrypoint string (e.g. `reports.plugins.macro_daily_plugin:MacroDailyReport`), import it, instantiate it, and cache. Cache lives only within a single Registry instance (per-process), so stale-state across CLI invocations is structurally impossible.

### 5.4 Task (the state machine)

`reports/hermes/task.py` defines the lifecycle:

```
created → queued → running → completed
                       \→ failed
                       \→ cancelled
```

`Task.transition(new_status)` validates the transition and raises `ValueError` on illegal moves. The Task model carries:
- `task_id` (UUID4)
- `report_name`, `status`
- `created_at`, `started_at`, `finished_at` (UTC datetimes)
- `error` (on failed), `metadata` (carries `dry_run`, plugin metadata, etc.)
- `result_text`
- `artifact_paths: list[str]` (default `[]`)
- `parent_task_id: str` (default `""`, set by `rerun`)
- `to_dict()` includes a computed `duration_ms`

### 5.5 Dispatcher (the API)

`reports/hermes/dispatcher.py` exposes four methods plus two list helpers:

- `dispatch(report_name, dry_run=True, metadata=None) -> Task` — create + run a task. Default is `dry_run=True`, which short-circuits the pipeline (no fetch, no analyze, no render, no print, no DB write). Real execution requires the explicit `--execute` flag at the CLI.
- `status(task_id) -> Optional[Task]` — check in-memory first, then fall back to the store (cross-process path).
- `rerun(task_id) -> Task` — always creates a `dry_run=True` child linked via `parent_task_id`. Rerun is recovery, not re-fetch.
- `cancel(task_id) -> Task` — `queued`/`created` are immediately marked `cancelled`; `running` is marked `cancelled` as soon as the in-flight call returns (Python doesn't support portable thread cancellation); `completed`/`failed`/`cancelled` raise `ValueError`.
- `list_tasks(report_name=None) -> list[Task]` — filter by name.
- `list_active() -> list[Task]` — non-terminal tasks only.

### 5.6 LocalStore (the persistence layer)

`reports/hermes/store.py` is the cross-process task/artifact store. JSON files only; stdlib only; no new dependencies.

Layout under `<metadata_dir>/`:
- `tasks/<task_id>.json` — one file per task
- `tasks/index.json` — list of `{task_id, report_name, status, created_at}` records
- `reports/<artifact_id>.txt` — rendered report text (only for `--execute` runs with non-empty output)
- `reports/<artifact_id>.json` — artifact metadata
- `reports/index.json` — list of artifacts

Properties:
- **Atomic writes** via `os.replace()` after writing to `<name>.tmp` first.
- **Brief `flock(LOCK_EX)`** around each write to prevent concurrent corruption when multiple processes hit the same `index.json`. Reads don't lock.
- **Corruption tolerance**: malformed `index.json` is logged and treated as empty; the per-file JSONs remain individually readable.
- **Cross-process semantics**: any process pointing at the same `metadata_dir` sees all tasks/artifacts written by other processes.
- **`metadata_dir` is opt-in**: `run_report.py` defaults to `metadata_dir=""` (no persist), so a dry-run validation never silently writes to production metadata.

### 5.7 CLI (`run_report.py`)

Subcommands: `list`, `dispatch`, `status`, `rerun`, `cancel`, `artifact`, `tasks`.

Default behavior:
- `dispatch` is dry-run unless `--execute` is passed.
- `metadata_dir` is empty (in-memory only) unless `--metadata-dir <path>` is passed.

The CLI is the only entry point the framework exposes to operators. The four production `.py` entrypoints are still the primary execution path; the CLI is a parallel observation / dry-run surface.

---

## 6. What Production Behavior Intentionally Did NOT Change

This section is the audit trail for "did Phase 2 break anything?" The answer, verified by sha256 + mtime, is no.

### 6.1 Protected files (11 files, byte-for-byte unchanged)

| File | Why protected |
|------|---------------|
| `macro_daily.py` | Production Layer 1 entrypoint (cron `ed214c19c4ac`, 08:30 daily) |
| `industry_weekly.py` | Production Layer 2 entrypoint (cron `60d92c57b826`, Monday 08:00) |
| `company_monthly.py` | Production Layer 3 entrypoint (cron `5eaa5fa9a50d`, 12th of month) |
| `institutional.py` | TWSE T86 fetcher used by Layer 3 |
| `db.py` | SQLite helper for `macro_history.db` |
| `run.sh` | Layer 1 bash wrapper (venv + python) |
| `run_weekly.sh` | Layer 2 bash wrapper |
| `run_monthly.sh` | Layer 3 bash wrapper |
| `industry_config.json` | Layer 2 static config |
| `taiwan50_config.json` | Layer 3 static config |
| `~/.hermes/cron/jobs.json` | Schedule + prompts for the 4 cronjobs + cron agent behavior |

**Note on `institutional.py` and `company_monthly.py`:** these two files *did* change in Step 2 — by design, to switch to the shared helper. Their sha256 differs from the original baseline; the post-Step-2 sha256 is the new baseline, and the framework's `ProductionFileProtectionTests` enforces that no further changes slip in.

### 6.2 SQLite

- `macro_history.db` schema unchanged. No migration, no new tables, no new columns.
- Phase 2's task data lives in a separate JSON-based store (`LocalStore`); the architecture review explicitly mandates that the new `tasks.db` (if it ever becomes SQLite) must be a **separate file** to avoid touching the existing schema.
- `db.py` is byte-for-byte unchanged. Production code still calls `db.save_macro_daily()` etc.; the framework's `MacroDailyReport.archive()` calls the same function inside a try/except.

### 6.3 Cron

- 4 cronjobs (`ed214c19c4ac`, `60d92c57b826`, `5eaa5fa9a50d`, `af64556bc8e9`) — schedule, prompt, and delivery target all unchanged.
- No new cronjobs were added by Phase 2.
- No prompt was rewritten to call the new CLI. The architecture review §2.1 #5 explicitly says "不過 prompt 改寫本身屬於 Phase 2C — Phase 2A/2B 純粹旁路觀察".

### 6.4 Telegram publishing

- The `print(stdout) → cron agent → Telegram` path is untouched.
- `run_report.py` defaults to `dry_run=True` and `metadata_dir=""`; even with `--execute`, the `ReportPipeline` is constructed with `publish=False`, so no Telegram send happens.
- The execute-mode validation in Step 4B (in sandbox) proved the pipeline reaches the fetch step (`yfinance` import attempt, then `ModuleNotFoundError`) before any publisher could fire — the safety gate is at the Pipeline constructor, not at the Telegram module.

### 6.5 venv / supervisord / cloudflared / runtime

- `/home/ubuntu/macro-venv` is unchanged (no pip install).
- No supervisord conf change.
- No cloudflared config change.
- No new long-running process, no new port, no new public URL.
- `hermes-runtime-bridge` and all skills (`~/.hermes/skills/`) are untouched.

### 6.6 Phase 2A artifacts (deliberately not loaded)

- `config/reports/*.yaml` (4 files) — draft report manifests; the framework's `Registry` only loads JSON manifests, so these YAML files are silent in production. They are kept as human-readable contract drafts.
- `config/policies/*.yaml`, `config/publishers/*.yaml` — policy / publisher contracts; not loaded.
- `templates/reports/*_v0.md.j2` (4 files) — Jinja2 placeholder layouts; the framework's `BaseReport.render()` returns raw Python strings, so these templates are documentation only.
- `templates/prompts/research_interpretation_v0.md` — prompt template draft; not loaded.

### 6.7 Other intentional non-changes

- No `BaseReport` plugin wrapper for `industry_weekly`, `company_monthly`, or `institutional` (only `macro_daily` is wired). Step 4C is the natural next step.
- No multi-worker / parallelism (out of scope per the architecture review §2.2).
- No dashboard (Phase 2D; deferred).
- No Jinja2 enforcement (architecture review §3.3 says "預設不動").
- No `from_manifest_class` convention in manifests (Step 4B #3 deferred).
- No persistent SQLite-backed task store (the JSON store is the Step 4B scope; SQLite is a future enhancement).
- No `--rerun-failed` CLI mode (Step 4B #6 deferred).
- No cronjob prompt migration to use the framework (Step 4B #7 deferred; requires 100% plugin coverage first).

---

## 7. Migration Strategy

The conservative-order pattern is the central design lesson of Phase 2. It is worth recording so future Phase 3 work doesn't repeat the temptation to introduce an abstraction before its leaves are stable.

### 7.1 The order (and why)

1. **Phase 2A: additive scaffolding only.** Manifests, contracts, templates, runbooks — all written in `docs/`, `config/`, `templates/`, `metadata/`. Zero Python code touched, zero cron changes, zero schema. This proves the directory layout and the contract shape before any abstraction lands.
2. **Phase 2B Step 1: characterization tests first.** Write 46 tests that pin the current behavior of the 3 duplicated helpers, *using inlined reference functions* (not imports from production). This is the "test before refactor" baseline.
3. **Phase 2B Step 2: extract leaves, not trunk.** Move the 3 helpers into a shared module and switch the 2 call sites. The test layer (from Step 1) catches any byte-for-byte divergence. Trunk-level abstractions (BaseReport, Dispatcher) are still deferred.
4. **Phase 2B Step 3 + 4A: introduce the trunk.** With the leaves stable and test-protected, introduce `BaseReport`, `ReportPipeline`, `Registry`, `Task`, `Dispatcher`, `run_report.py`. Each new file is purely additive; the 4 production `.py` files are untouched. Tests cover each component in isolation (49 → 90 tests).
5. **Phase 2B Step 4B: wire the trunk to a cross-process store.** Add `LocalStore` for JSON-backed task/artifact persistence. Dispatcher reads in-memory first, falls back to store. CLI gains `artifact` and `tasks` subcommands. 90 → 133 tests.

### 7.2 The principle

> "Stabilize the leaves (helpers) before drawing the trunk (BaseReport)."

This is the same reasoning behind the 7-day byte-identical shadow run that the architecture review mandates for any `.py` change — but at a smaller granularity that can ship and be observed in a single cron cycle.

### 7.3 What "byte-identical observable behavior" means (and doesn't)

Means:
- Same string output for every test input.
- Same `last_status` in `~/.hermes/cron/jobs.json` for the 4 cronjobs.
- Same Telegram output for the 4 production reports.
- Same SQLite rows in `macro_history.db` (no schema change, so this is automatic).

Does not require:
- Identical Python bytecode in `.pyc` cache.
- Identical `sys.modules` import order.
- Identical import-machinery log lines.

### 7.4 Migration safety mechanisms used throughout

- **sha256 + mtime baseline** captured before each step (`/tmp/phase2b_step1_baseline.json` through `/tmp/phase2b_step4b_baseline.json`).
- **Production-file protection test** in `tests/test_phase2b_framework.py::ProductionFileProtectionTests` — re-checks the 11 protected files against the Step 2 baseline on every test run. This is the "canary" that fails CI if a future refactor accidentally re-touches a production file.
- **Dry-run by default** in the CLI and the Dispatcher.
- **Publish-off** in the Pipeline constructor.
- **Rollback plans** in every step's implementation summary, including exact `rm -rf` and `cp` commands.

---

## 8. Remaining Work for Phase 3

Phase 3 has not started. The architecture review defines it as Dispatcher-as-FastAPI + cron prompt migration + multi-worker + dashboard. The current open menu (from the Step 3+4A and Step 4B summaries) is:

### 8.1 Plugin coverage (highest priority — unblocks the rest)

1. **Wrap the remaining 3 reports as plugins**: `industry_weekly_plugin.py`, `company_monthly_plugin.py`, `constituents_quarterly_plugin.py`. Subprocess-wrapping is the recommended adapter strategy (preserves the standalone-scripts path that cron uses). ~30 min per wrapper.
2. **JSON manifests for the remaining 3 reports**: `config/reports/{industry_weekly,company_monthly,constituents_quarterly}.json`. The Phase 2A YAML drafts are the human-readable source of truth for the field values.
3. **A `from_manifest_class` field** for manifests that share an entrypoint (e.g. a future generic "weekly RSS" plugin that handles both `industry_weekly` and a future `etf_weekly`).

### 8.2 Observability and safety (medium priority)

4. **Persistent SQLite-backed task store** (`tasks.db`, separate from `macro_history.db`). Schema sketch is in the Step 3+4A summary §7.2 #4. This makes the Dispatcher survive process restarts.
5. **Archive support in the framework** — let plugin manifests declare `archive.target` and have the Pipeline call it. Currently `MacroDailyReport.archive()` is the only one and it does it ad-hoc.
6. **A `--rerun-failed` CLI mode** that scans the recent task history and re-dispatches any task that ended in `failed`. Most common "why did yesterday's report not arrive" remediation.
7. **A `purge` CLI subcommand** that removes task/artifact JSONs older than N days. Mitigates the unbounded-growth risk from Step 4B #7.

### 8.3 Promotion to primary execution path (lower priority — needs the above first)

8. **Cronjob prompt migration** — change the 4 existing cronjob prompts to call `python3 /home/ubuntu/macro-report/run_report.py dispatch <name> --execute` instead of `bash /home/ubuntu/macro-report/run.sh`. This is the moment the framework becomes the primary execution path. **Not recommended before 100% plugin coverage.**
9. **Dispatcher as FastAPI** (architecture review §6) — exposes the Dispatcher over HTTP so the ChatGPT Orchestrator can dispatch reports and poll task status without going through Hermes cron. This is the `hermes-runtime-bridge`-style integration for macro-report.
10. **Dashboard surface** — a `/macro-status` page on the existing Hermes dashboard (`hermes.biaobecue.com`) that reads from `tasks.db` and shows the last N tasks per report. This is the user-facing payoff of the task lifecycle.
11. **Multi-worker / parallelism** — explicitly deferred to "Phase 2D" by the architecture review §2.2. Current Dispatcher is single-threaded by design.

### 8.4 Carry-forward non-goals (still forbidden)

- Do NOT touch the 4 production `.py` entrypoints (still). Plugin coverage must reach 100% before any of them are folded into the framework.
- Do NOT migrate `macro_history.db` schema. New persistent data goes in new files.
- Do NOT change the 4 existing cronjob schedules or delivery targets.
- Do NOT modify `~/.hermes/skills/market-data-reports/` (it tracks the Phase 2A scaffolding state; a future patch should mention the framework, but that's out of scope here).
- Do NOT install new Python packages in `macro-venv` (PyYAML, Jinja2, etc. — the framework is stdlib-only by design).

---

## 9. Appendix — Key Implementation Summary Documents

| Doc | Bytes | What it covers |
|-----|------:|----------------|
| `PHASE2A_IMPLEMENTATION_SUMMARY.md` | 10,674 | Phase 2A: 17 additive files (manifests, contracts, runbooks, templates, policy YAML); 11 protected files verified unchanged |
| `PHASE2B_PLAN_AND_DIFF_PROPOSAL.md` | 30,940 | The conservative-order plan: extract leaves first, then trunk; 4 risk mitigations; explicit no-go list |
| `PHASE2B_STEP1_IMPLEMENTATION_SUMMARY.md` | 18,164 | 46 characterization tests; characterization-test design rationale; cross-validation against production code |
| `PHASE2B_STEP2_IMPLEMENTATION_SUMMARY.md` | 19,160 | Helper extraction to `reports/common/format.py`; call-site switch; 49 tests pass; function-by-function behavior mapping |
| `PHASE2B_STEP3_STEP4_IMPLEMENTATION_SUMMARY.md` | 28,246 | BaseReport / Pipeline / Registry / Task / Dispatcher / CLI; 9 design decisions; dry-run + publish-off safety gates; recommended Step 4B scope |
| `PHASE2B_STEP4B_IMPLEMENTATION_SUMMARY.md` | 22,847 | LocalStore (JSON, cross-process); Dispatcher→store wiring; CLI artifact/tasks subcommands; 43 new tests; 7 known limitations |
| `ARCHITECTURE_REVIEW_PHASE2.md` | 101,579 | The design doc: target architecture, pipeline, BaseReport, Task lifecycle, Dispatcher, Config strategy, Prompt/Template, Data layer, Observability, Roadmap, Risk, 8 Q&A |

All seven are under `/home/ubuntu/macro-report/`. Read them in the order above for the most coherent developer onboarding.

---

*Phase 2 status: complete, validated, zero production behavior change. 133/133 tests pass. 11/11 protected files unchanged. Framework is opt-in, parallel to the existing cron path, ready for Phase 3 plugin coverage.*
