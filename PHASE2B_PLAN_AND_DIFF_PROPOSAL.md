# Phase 2B Plan and Diff Proposal

Status: PROPOSAL ONLY — not yet implemented
Author: M2 (planning artifact)
Date: 2026-07-08
Parent design doc: `/home/ubuntu/macro-report/ARCHITECTURE_REVIEW_PHASE2.md`
Depends on: Phase 2A additive foundation (already on disk, no production changes)
Target: macro-report v1.x production stack — `macro_daily.py`, `industry_weekly.py`, `company_monthly.py`, `institutional.py`, `db.py`

---

## 1. Executive Recommendation

**Recommended first implementation step for Phase 2B: extract duplicated formatting helpers into a new `reports/common/format.py` module, behind non-publishing tests, before introducing any abstraction layer.**

Order of operations (strictly):

1. Add `.gitkeep` markers to any Phase 2A scaffolded empty directories (housekeeping only, no logic).
2. Add non-publishing tests that lock in the current observable behavior of the three duplicated formatting helpers:
   - `format_shares` in `institutional.py` (~lines 102–115)
   - `format_net` in `institutional.py` (~lines 117–125)
   - `format_net_shares` in `company_monthly.py` (~lines 47–63)
3. Run those tests against the *current* (un-extracted) helpers and confirm they pass byte-for-byte on the existing output.
4. **Only then** extract the helpers into `reports/common/format.py` and re-run the same tests against the extracted versions.
5. Switch the call sites in `institutional.py` and `company_monthly.py` to import from the new module.
6. Defer `BaseReport` ABC, thin plugin wrappers, dispatcher, and dashboard work until helper extraction is validated in production for at least one full cron cycle.

**Why this order, and not BaseReport first:**

The Phase 2 architecture review explicitly recommends a conservative rollout, and the duplication in `institutional.py` + `company_monthly.py` is the *lowest-risk* extraction target in the codebase — pure functions, no I/O, no state, no DB calls, no side effects, no scheduling awareness. Pulling these into a shared module is mechanical, testable in isolation, and produces no observable behavior change.

In contrast, `BaseReport` is an abstraction that sits *above* the data layer. Introducing it before helper behavior is test-protected risks:

- Defining the wrong method surface and having to migrate every subclass when reality disagrees.
- Coupling `run()` / `dispatch()` semantics to refactor churn inside the helpers themselves.
- Making Phase 2C (dispatcher) and Phase 2D (dashboard) dependent on an unstable interface.

The principle: **stabilize the leaves (helpers) before drawing the trunk (BaseReport).** This is the same reasoning behind the 7-day byte-identical shadow run that the architecture review mandates for any `.py` change — but at a smaller granularity we can ship and observe in a single cron cycle.

---

## 2. Current Duplicate / Helper Inventory

Three near-identical formatting helpers exist across two production files. All three implement the "net 買超/賣超 + 張" formatting pattern with subtle sign and threshold rules that must be preserved exactly.

### 2.1 `institutional.py::format_shares` (lines ~102–115)

- Signature: `format_shares(n: int | float) -> str`
- Behavior: Formats an absolute share count (no sign) into a human-readable 張 string with thousands separators and 萬 / 億 unit promotion.
- Input range observed: integer 張 counts from TWSE T86 dataset.
- Output shape: `"1,234"`, `"12.3 萬"`, `"1.23 億"` style.

### 2.2 `institutional.py::format_net` (lines ~117–125)

- Signature: `format_net(n: int | float) -> str`
- Behavior: Formats a *signed* net figure (買超 positive, 賣超 negative) with a leading sign and 萬/億 unit promotion.
- Output shape: `"+1,234"`, `"-12.3 萬"`, `"+1.23 億"`.

### 2.3 `company_monthly.py::format_net_shares` (lines ~47–63)

- Signature: `format_net_shares(n: int | float) -> str`
- Behavior: Structurally identical to `format_net` above. Slight differences suspected in edge-case handling (zero, very small positive, very large magnitude) — the test layer in step 2 of §1 will pin these down empirically rather than by reading source.

### 2.4 Duplication assessment

- `format_shares` vs `format_net`: differ in **sign handling only** — `format_net` prepends `+`/`-` and uses an absolute-value floor; `format_shares` is unsigned.
- `format_net` vs `format_net_shares`: near-clone. The Phase 2B first step assumes byte-for-byte equivalence for the current input distribution but **the test layer must empirically confirm this before extraction**, not assume it from source reading. If they differ on any real input, the extraction splits into two helpers (`format_net` and `format_net_shares`) rather than collapsing them.

### 2.5 Out of scope for the first step

The following Phase 2A scaffolded but unimplemented pieces are explicitly **not** part of the first implementation step:

- `BaseReport` ABC (Phase 2B sub-step 2, deferred).
- Thin plugin wrappers per report type (Phase 2B sub-step 3, deferred).
- `Dispatcher` (Phase 2C, not in Phase 2B).
- Dashboard surface (Phase 2D, not in Phase 2B).
- Any SQLite schema change.
- Any cron schedule change.
- Any Telegram template change.

---

## 3. Proposed First Implementation Step

Concrete sequence, each step with a single verifiable success criterion:

1. **Step 3.1 — Directory housekeeping**
   - Add `.gitkeep` to any empty Phase 2A directory (likely empty sub-folders under `/home/ubuntu/macro-report/reports/` if they exist).
   - Success: every subdirectory of `reports/` contains at least one entry (file or `.gitkeep`).
   - Risk: zero. No Python touched.

2. **Step 3.2 — Add non-publishing tests**
   - Create `tests/test_format_helpers.py` (see §4.2 for content outline).
   - Tests import the *current* helpers from their existing modules and assert output strings across a curated input set:
     - `0`
     - `1`
     - `-1`
     - `999`
     - `1000`
     - `-1000`
     - `12345`
     - `-12345`
     - `1_0000` (萬 boundary)
     - `-1_0000`
     - `1_2345_6789` (億 magnitude)
     - `-1_2345_6789`
   - Success: all assertions pass on un-extracted code. This locks current behavior before refactor.
   - Risk: zero. Tests are additive; they don't run in any cron pipeline.

3. **Step 3.3 — Extract helpers into `reports/common/format.py`**
   - New module, see §4.1 for content outline.
   - Public API: `format_shares`, `format_net`, `format_net_shares` (all three preserved even if `format_net` ≡ `format_net_shares` after empirical confirmation — keeping names stable avoids extra import-site churn).
   - Success: tests in `tests/test_format_helpers.py` still pass when pointed at the new module.
   - Risk: very low. Pure function move, no caller change yet.

4. **Step 3.4 — Switch call sites**
   - `institutional.py`: replace the two local function definitions with `from reports.common.format import format_shares, format_net` (preserve names).
   - `company_monthly.py`: replace the local `format_net_shares` with `from reports.common.format import format_net_shares`.
   - Success: `python3 -m py_compile` passes on all five production files. Manual smoke run of one cron-equivalent invocation (see §7) produces byte-identical Telegram output (or byte-identical dry-run log if dry-run mode exists).
   - Risk: low. Import path is the only behavioral surface; function signatures unchanged.

5. **Step 3.5 — Observe one full cron cycle**
   - Wait for one real cron execution of each affected report (Layer 1 daily 08:30, Layer 3 monthly on the 12th) before declaring step 3 done.
   - Success: Telegram output for the affected reports is unchanged from pre-extraction; no new errors in `~/.hermes/cron/jobs.json` last_status fields.
   - Risk: low because §3.2 already pinned behavior.

**Defer until after step 3.5:**

- `BaseReport` ABC (Phase 2B sub-step 2).
- Plugin wrappers (Phase 2B sub-step 3).
- Dispatcher (Phase 2C).
- Dashboard (Phase 2D).

---

## 4. Proposed File Additions

This section describes the files that *will be added* in a future implementation. They are **not** created by this planning task.

### 4.1 `reports/common/format.py` (new file — outline only)

Purpose: single home for the three extracted formatting helpers.

Public API surface (preserved verbatim from current local definitions):

- `def format_shares(n: int | float) -> str` — unsigned 張 formatter.
- `def format_net(n: int | float) -> str` — signed net 買超/賣超 formatter.
- `def format_net_shares(n: int | float) -> str` — signed net 張 formatter (the `company_monthly.py` variant).

Internal design (proposed, not committed):

- One private `_promote_unit(value: int | float) -> tuple[float, str]` helper that returns the scaled magnitude and the unit suffix (`""`, `"萬"`, `"億"`). This collapses the duplicated threshold logic.
- `format_shares` calls `_promote_unit(abs(n))` and formats with thousands separator, no sign.
- `format_net` and `format_net_shares` call `_promote_unit` with sign preservation, prepend `+`/`-`, attach unit.
- The two signed variants are kept as separate exported functions to avoid forcing `company_monthly.py` and `institutional.py` to share semantics that may diverge in the future (e.g., if `institutional.py` later wants a different zero handling than `company_monthly.py`).

What this file does **not** do:

- No imports from `db.py`, no I/O, no logging, no Telegram awareness, no config reads.
- No class definitions, no ABC, no plugin interface.
- No external dependencies (stdlib only).

### 4.2 `tests/test_format_helpers.py` (new file — outline only)

Purpose: lock in current observable behavior before and after extraction.

Test cases (one assertion per row of the input matrix in §3.2):

- `test_format_shares_zero`
- `test_format_shares_small_positive`
- `test_format_shares_thousands`
- `test_format_shares_wan_boundary`
- `test_format_shares_yi_magnitude`
- `test_format_shares_negative_is_treated_as_positive` (or documents the actual behavior if it differs)
- `test_format_net_zero`
- `test_format_net_positive_small`
- `test_format_net_negative_small`
- `test_format_net_wan_boundary_signed`
- `test_format_net_yi_magnitude_signed`
- `test_format_net_shares_*` (mirrors the `format_net` matrix — the goal is to empirically confirm whether `format_net` and `format_net_shares` are byte-for-byte equivalent on this input set)

Test runner: try `python3 -m pytest tests/test_format_helpers.py`; fall back to `python3 tests/test_format_helpers.py` (a tiny `if __name__ == "__main__":` runner block) if pytest is not installed in `macro-venv`.

Out of scope for the test file:

- No DB fixtures.
- No cron mocking.
- No Telegram send assertions (this is a non-publishing test layer by design).

### 4.3 `.gitkeep` files (housekeeping)

- One in any empty subdirectory under `/home/ubuntu/macro-report/reports/` left empty by Phase 2A scaffolding. Zero or more files depending on actual scaffold state at implementation time.

---

## 5. Proposed Existing File Modifications — Diff Outline Only

These modifications are **proposed only** and must not be applied by this planning task. Each is a minimal, mechanical change.

### 5.1 `institutional.py`

- **Remove** the two local definitions of `format_shares` and `format_net` (lines ~102–125).
- **Add** at top of file (after existing imports): `from reports.common.format import format_shares, format_net`.
- No other lines change. All call sites within `institutional.py` continue to work because names are preserved.

### 5.2 `company_monthly.py`

- **Remove** the local definition of `format_net_shares` (lines ~47–63).
- **Add** at top of file (after existing imports): `from reports.common.format import format_net_shares`.
- No other lines change.

### 5.3 Files explicitly **not** modified in this first step

- `macro_daily.py` — untouched.
- `industry_weekly.py` — untouched.
- `db.py` — untouched.
- `~/.hermes/cron/jobs.json` — untouched.
- `safety.json` / `config/*.json` — untouched.
- Any Telegram send / template code — untouched.
- Any SQLite schema migration — untouched.

### 5.4 Future diffs (Phase 2B sub-step 2+, deferred)

Out of scope for this first implementation step. Listed only for roadmap completeness:

- `reports/__init__.py` — package marker (empty or with `__all__`).
- `reports/common/__init__.py` — package marker.
- Future `reports/base.py` — `BaseReport` ABC, added only after §3.5 passes.
- Future `reports/<layer>_report.py` thin plugin wrappers — added only after `BaseReport` is validated.

---

## 6. Backward Compatibility Plan

The first implementation step is designed to produce **byte-identical observable behavior**:

- All three public function names preserved (`format_shares`, `format_net`, `format_net_shares`).
- All three function signatures preserved.
- No new public surface added at the module level of `institutional.py` or `company_monthly.py`.
- The new `reports/common/format.py` is a new file; no existing import path breaks.

Risks to backward compatibility and their mitigations:

- **Risk: `format_net` and `format_net_shares` differ on at least one real input.** Mitigation: keep them as two separate functions in `reports/common/format.py` even if they look identical at the call site. Do not collapse them based on source reading alone — let the test matrix in §3.2 settle the question.
- **Risk: import resolution order changes (circular import between `institutional.py` and the new `reports.common.format`).** Mitigation: `reports/common/format.py` is stdlib-only and depends on nothing in the `macro-report` package, so circular import is structurally impossible. This is one of the reasons this is the safest extraction target.
- **Risk: any caller uses `institutional.format_net` via fully-qualified path.** Mitigation: grep `institutional.format_` and `company_monthly.format_` before applying §5.1 / §5.2; if any external fully-qualified use exists, either keep a re-export shim or update the caller in the same change.

What "byte-identical" means here:

- Same string output for every input the test matrix covers.
- Same string output in the produced Telegram message for the cron-fired reports (Layer 1 daily, Layer 3 monthly) — verified by visual diff against a known-good prior message.
- Same `last_status` in `~/.hermes/cron/jobs.json` for all affected jobs.

What "byte-identical" does **not** require:

- Identical Python bytecode in the `.pyc` cache (irrelevant).
- Identical import order in `sys.modules` (irrelevant).
- Identical log lines from Python's import machinery (irrelevant).

---

## 7. Test / Validation Plan

Validation runs in three tiers, all **non-publishing**.

### 7.1 Tier 1 — Static syntax check

Run on the implementation branch after §5.1 and §5.2 are applied:

```
cd /home/ubuntu/macro-report
python3 -m py_compile macro_daily.py industry_weekly.py company_monthly.py institutional.py db.py
```

Success: zero errors, zero warnings. Exit code 0.

### 7.2 Tier 2 — Behavior tests

```
cd /home/ubuntu/macro-report
python3 -m pytest tests/test_format_helpers.py
```

If pytest is not available in `macro-venv`, fall back to:

```
cd /home/ubuntu/macro-report
python3 tests/test_format_helpers.py
```

Success: all assertions pass. Exit code 0.

### 7.3 Tier 3 — Non-publishing dry validation

The macro-report stack does not currently expose a `--dry-run` flag in every entry point, so this tier is conditional. If a dry-run mode exists in any of the affected scripts, invoke it once and confirm:

- No Telegram message dispatched.
- Output is captured to stdout / log only.
- All three formatting helpers appear in the output with the expected strings (search the captured output for the test inputs from §3.2).

If no dry-run mode exists, this tier is skipped; Tier 1 + Tier 2 + the one-cron-cycle observation in §3.5 cover the validation surface.

### 7.4 Tier 4 — One full cron cycle observation

- Wait for the next scheduled run of the affected cron jobs after deployment.
- Compare Telegram output against a prior known-good message.
- Confirm `last_status` remains `ok` in `~/.hermes/cron/jobs.json` for the affected job IDs.

### 7.5 Validation that this planning task does **not** run

This document is a proposal. The validation commands above are described for the *future* implementation, not for the act of writing this `.md` file. Writing this plan runs zero Python, sends zero Telegram messages, and modifies zero production files.

---

## 8. Rollback Plan

Because the first implementation step is purely a function move with no behavior change, rollback is mechanical and safe.

### 8.1 Pre-conditions for safe rollback

- The original three helper definitions must still be recoverable. Mitigation: capture the pre-change content of `institutional.py` and `company_monthly.py` (a `git diff` or a backup copy) before applying §5.1 / §5.2.

### 8.2 Rollback steps (in order)

1. In `institutional.py`, remove the `from reports.common.format import format_shares, format_net` line and re-paste the original local function definitions.
2. In `company_monthly.py`, remove the `from reports.common.format import format_net_shares` line and re-paste the original local function definition.
3. (Optional) Delete `/home/ubuntu/macro-report/reports/common/format.py` if no other module imports it.
4. (Optional) Delete `/home/ubuntu/macro-report/tests/test_format_helpers.py` if no CI references it.
5. Run Tier 1 (`py_compile`) and Tier 2 (`pytest` or direct run) — both must pass.
6. Observe next cron cycle — `last_status` must remain `ok`.

### 8.3 Rollback triggers

Roll back if any of the following is observed post-deployment:

- Any of the validation tiers in §7 fails.
- Telegram output for the affected reports differs visibly from pre-deployment.
- Any new exception class appears in cron logs.
- A new `last_status=failed` appears in `~/.hermes/cron/jobs.json` for an affected job within 48 hours of deployment.

### 8.4 Why rollback is cheap

- The change is two lines added (imports) and ~30 lines removed (helper bodies).
- No schema, no config, no cron, no Telegram behavior changes.
- The extraction is logically reversible by re-pasting the original function bodies and removing the imports.

---

## 9. Risk Assessment

Risk matrix for the first implementation step. Severity is post-mitigation; likelihood is post-mitigation.

| # | Risk | Severity | Likelihood | Mitigation |
|---|------|----------|------------|------------|
| R1 | `format_net` ≠ `format_net_shares` on real inputs, breaking a collapse assumption | Low | Medium | Test matrix in §3.2 pins the actual behavior; both functions kept as separate exports regardless |
| R2 | Circular import between `institutional.py` and `reports.common.format` | Low | Very low | `reports.common.format` is stdlib-only, no intra-package deps |
| R3 | Fully-qualified external import (`institutional.format_net`) breaks | Low | Low | Pre-flight grep; re-export shim if needed |
| R4 | Telegram output diff in production despite test pass | Medium | Low | §3.5 one-cron-cycle observation; §8.3 rollback triggers |
| R5 | Test runner unavailable (`pytest` missing) | Low | Medium | Fallback to direct `python3 tests/test_format_helpers.py` with `if __name__ == "__main__"` runner |
| R6 | `.gitkeep` accidentally clobbers a real file | Very low | Very low | `.gitkeep` only added to confirmed-empty directories |
| R7 | Premature `BaseReport` introduction creates churn | Medium | High *if attempted now* | Explicitly deferred per §1 and §3 — this is the central reason for the conservative order |

**Overall residual risk for the first step: LOW.** The change is pure function relocation, test-protected, observation-gated, and reversible in under 5 minutes.

Risks explicitly **out of scope** for this assessment:

- Risks of Phase 2C (Dispatcher) — not yet designed in detail.
- Risks of Phase 2D (Dashboard) — not yet designed in detail.
- Risks of the `BaseReport` ABC design itself — deferred until §3.5 passes.

---

## 10. Phase 2B First-Step No-Go List

The following actions are **explicitly forbidden** during the first implementation step, to keep the change set minimal and reversible.

1. ❌ Do not introduce `BaseReport` ABC. No `reports/base.py`. No `abc.ABC`, no `@abstractmethod`. The first step is helper extraction only.
2. ❌ Do not introduce thin plugin wrappers per report type (no `macro_daily_report.py`, `industry_weekly_report.py`, etc. as classes).
3. ❌ Do not modify `macro_daily.py` or `industry_weekly.py` at all. They do not use any of the three extracted helpers.
4. ❌ Do not modify `db.py`. No schema, no new query helpers, no connection-pool changes.
5. ❌ Do not modify `~/.hermes/cron/jobs.json`. No cron schedule changes, no provider swaps, no enabled_toolsets changes.
6. ❌ Do not modify any Telegram template / send code. The Telegram message format must remain byte-identical.
7. ❌ Do not modify `safety.json` or any `config/*.json`.
8. ❌ Do not modify the SQLite database (`macro_history.db`) schema or contents. The `_init_schema` idempotent column-add pattern from Phase 4 must not be invoked.
9. ❌ Do not install any new Python package. The new `reports/common/format.py` is stdlib-only.
10. ❌ Do not invoke any of the production reports (`macro_daily.py`, `industry_weekly.py`, `company_monthly.py`, `institutional.py`) in a mode that publishes to Telegram. Validation runs must be local-only.
11. ❌ Do not run the scheduler with `--replace` or any flag that touches cron state.
12. ❌ Do not edit the Phase 2A scaffolded files beyond adding `.gitkeep` markers.
13. ❌ Do not create this implementation in the same commit / change set as any other refactor. The first step must be shippable, reviewable, and rollback-able in isolation.

**Permitted actions during the first step:**

- ✅ Create `reports/common/format.py`.
- ✅ Create `tests/test_format_helpers.py`.
- ✅ Add `.gitkeep` to confirmed-empty directories.
- ✅ Edit `institutional.py` (import + remove local definitions only).
- ✅ Edit `company_monthly.py` (import + remove local definition only).
- ✅ Run Tier 1, Tier 2, and (if available) Tier 3 validation locally.
- ✅ Read any file under `/home/ubuntu/macro-report/` for context.

---

## 11. Acceptance Criteria for Future Implementation

The first implementation step is considered complete when **all** of the following are true. Any single failure triggers the rollback in §8.

### 11.1 File-level

- [ ] `/home/ubuntu/macro-report/reports/common/format.py` exists, contains `format_shares`, `format_net`, `format_net_shares`, and is stdlib-only.
- [ ] `/home/ubuntu/macro-report/tests/test_format_helpers.py` exists and contains at minimum the test cases listed in §4.2.
- [ ] `/home/ubuntu/macro-report/institutional.py` no longer contains the local definitions of `format_shares` and `format_net`; it contains exactly one `from reports.common.format import format_shares, format_net` line.
- [ ] `/home/ubuntu/macro-report/company_monthly.py` no longer contains the local definition of `format_net_shares`; it contains exactly one `from reports.common.format import format_net_shares` line.
- [ ] `macro_daily.py`, `industry_weekly.py`, `db.py` are byte-identical to their pre-step state (modulo trailing newline normalization from the editor).
- [ ] `~/.hermes/cron/jobs.json` is byte-identical to its pre-step state.
- [ ] All `config/*.json` and `safety.json` files are byte-identical to their pre-step state.

### 11.2 Validation-level

- [ ] `python3 -m py_compile macro_daily.py industry_weekly.py company_monthly.py institutional.py db.py` exits 0.
- [ ] `python3 -m pytest tests/test_format_helpers.py` (or the `python3 tests/test_format_helpers.py` fallback) exits 0 with all assertions passing.
- [ ] At least one cron cycle of each affected report has completed post-deployment with `last_status=ok` in `~/.hermes/cron/jobs.json`.
- [ ] Visual diff of the affected Telegram outputs against a known-good prior message shows no formatting change.

### 11.3 Behavior-level

- [ ] For every input in the test matrix in §3.2, the extracted function produces the same string as the pre-extraction local function.
- [ ] No new exception class appears in any cron log.
- [ ] No new `WARNING` or `ERROR` line appears in any cron log that wasn't there pre-step.

### 11.4 Process-level

- [ ] The change set is reviewable in a single diff.
- [ ] The rollback procedure in §8 has been rehearsed mentally and the pre-change content of `institutional.py` and `company_monthly.py` is recoverable.
- [ ] The Phase 2A scaffolded empty directories are either populated or carry `.gitkeep`.

### 11.5 What this acceptance list does **not** require

- It does not require any new test for `macro_daily.py`, `industry_weekly.py`, or `db.py` (out of scope).
- It does not require any new cron job.
- It does not require any new Telegram template or send path.
- It does not require any benchmark or performance test (the helpers are pure functions; perf is not a concern).
- It does not require `BaseReport`, the dispatcher, or the dashboard to exist.

---

## 12. Draft M2 Implementation Prompt — Not Executed

The following is a *draft* prompt that a future implementation session could be given to actually execute §3. The prompt is included here for review only. **It is not executed by this planning task.**

---

> You are M2. Execute Phase 2B first step exactly as described in `/home/ubuntu/macro-report/PHASE2B_PLAN_AND_DIFF_PROPOSAL.md` §3.
>
> Hard constraints (from §10 No-Go List):
> - Do not create `BaseReport`, do not create plugin wrappers, do not touch `db.py`, `macro_daily.py`, `industry_weekly.py`, `~/.hermes/cron/jobs.json`, any `*.json` config, any Telegram send code, or any SQLite schema/content.
> - Do not install packages.
> - Do not invoke any production report in a mode that publishes externally.
> - Do not run `hermes cron tick` or any scheduler command.
>
> Steps:
> 1. Read the current `institutional.py` and `company_monthly.py` and confirm the helper line ranges in §2 still match. If they don't, stop and report.
> 2. Snapshot the current contents of those two files (write to `/tmp/phase2b_rollback_<timestamp>/`).
> 3. Identify any empty directories left by Phase 2A and add `.gitkeep` to each.
> 4. Create `reports/common/format.py` per §4.1. Stdlib only. Public API: `format_shares`, `format_net`, `format_net_shares`.
> 5. Create `tests/test_format_helpers.py` per §4.2. Include the `if __name__ == "__main__":` fallback runner.
> 6. Run the test file against the *current un-extracted* helpers first (import them directly from `institutional` and `company_monthly`). All tests must pass.
> 7. Edit `institutional.py` and `company_monthly.py` per §5.1 / §5.2.
> 8. Run `python3 -m py_compile macro_daily.py industry_weekly.py company_monthly.py institutional.py db.py`. Must exit 0.
> 9. Run `python3 -m pytest tests/test_format_helpers.py` (or the fallback). Must exit 0.
> 10. Report back: list of files created, list of files modified, exact diff for the two modified files, validation command output, and a one-paragraph summary of whether §11.1 / §11.2 acceptance criteria are met.
>
> If any step fails, halt and report. Do not proceed. Do not retry the failing step without first reporting what failed and why.

---

## 13. Delivery Verification Notes

This section records how the Phase 4 delivery verification contract (see Hermes Bridge P4 SOP, 2026-07-08) applies to **this planning task** and to the **future implementation task**.

### 13.1 This planning task (current)

- **Expected artifact:** `/home/ubuntu/macro-report/PHASE2B_PLAN_AND_DIFF_PROPOSAL.md`
- **Verification method:** `os.stat()` on the path returns `exists=true`, `size > 0`, `mtime` is the current session time.
- **No intent_mismatch risk:** the only expected artifact is a single `.md` planning file; the agent's final message is a verification report, not a declarative "let me write" preamble. The intent_mismatch pattern in Phase 4.1 is not triggered because there is no prose preamble promising a write — this is a single explicit write instruction.
- **Protected files not modified:** `institutional.py`, `company_monthly.py`, `macro_daily.py`, `industry_weekly.py`, `db.py`, `~/.hermes/cron/jobs.json`, all `config/*.json`, `safety.json`, and the SQLite database are all unchanged. The planning task only writes to the planning document path.

### 13.2 Future implementation task (out of scope here)

When the future M2 session executes §12, it should declare the following `expected_artifacts` to the bridge so Phase 4 delivery verification applies:

- `/home/ubuntu/macro-report/reports/common/format.py` (new)
- `/home/ubuntu/macro-report/tests/test_format_helpers.py` (new)
- Any `.gitkeep` files added (new, list each)
- `/home/ubuntu/macro-report/institutional.py` (modified)
- `/home/ubuntu/macro-report/company_monthly.py` (modified)

The modified files are still verifiable via `os.stat()` (existence + size + mtime). The bridge's delivery verification does not do a content diff; for that, the future implementation must include the diff in its final response (as required by step 10 of §12).

### 13.3 Known limitations of delivery verification that apply here

- Cannot detect 0-byte writes — the planning document is multi-KB, so this is not a concern for this task.
- Cannot verify content — the planning task's contract is "the file exists with the required sections", which is verified by reading the file back (not part of the bridge's automatic verification).
- Cannot verify that protected files were *not* modified beyond their `mtime` — the future implementation should snapshot the protected files (step 2 of §12) and diff them post-implementation to catch accidental edits.

### 13.4 What "delivery verified" means for this task

For the current task, delivery is verified when:

1. The file exists at the expected path.
2. The file size is > 0 and consistent with the content of §1–§13.
3. The file's top-level headings include every section listed in the task brief (Executive Recommendation, Current Duplicate / Helper Inventory, Proposed First Implementation Step, Proposed File Additions, Proposed Existing File Modifications — Diff Outline Only, Backward Compatibility Plan, Test / Validation Plan, Rollback Plan, Risk Assessment, Phase 2B First-Step No-Go List, Acceptance Criteria for Future Implementation, Draft M2 Implementation Prompt — Not Executed, Delivery Verification Notes).
4. The file's `mtime` is within the current session window (proof it was actually written this turn, not pre-existing).
5. The protected files listed in §10 are confirmed unchanged via `mtime` (they should reflect their pre-session state, not this session).

End of proposal.
