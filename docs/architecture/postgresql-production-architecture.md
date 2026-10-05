# PostgreSQL Production Architecture（FIE Production Authority）

> **Document class**：Architecture — current-state production narrative（Phase 6.8B publication）
> **Status**：CURRENT — 描述 2026-10-05 PostgreSQL controlled cutover 之後的 accepted production state。
> **取代**：任何將 SQLite 描述為 production primary / default / transparent fallback 的舊文件段落（歷史文件保留原狀並以 amendment／banner 標註，見 §13）。
> **本文件不授權**：任何 deployment、schema migration、data mutation、codex cloud integration、SQLite retirement。Phase 6.8B 是 documentation publication only。

---

## 1. Current production topology

FIE Production（Abacus host 變體；containerized 變體見
[`docs/production/FIE_6_7B_PRODUCTION_BASELINE.md`](../production/FIE_6_7B_PRODUCTION_BASELINE.md)）：

```text
Client / Caller（Hermes cron、HTTP client、CLI operator）
      |
      v
FIE Runtime / API（phase3.transport.http + wrappers run.sh / run_weekly.sh / run_monthly.sh）
      |
      v
Runtime DB Contract（phase3/runtime_contract.py — 單一 canonical resolver，fail-closed）
      |
      +------> PostgreSQL   （Production Authority — authoritative datastore）
      |
      +------> SQLite       （Explicit Rollback / Recovery Source ONLY — 永遠不是 production default）
```

| Production property | Accepted value |
|---|---|
| Authoritative production datastore | **PostgreSQL**（2026-10-05 SQLite→PostgreSQL controlled cutover accepted） |
| Service | supervisord program `fie-api-67b`，runtime `/home/ubuntu/fie-venv-67b/bin/python3 -m phase3.transport.http` |
| Bind | `127.0.0.1:8790`（loopback only；public probe 為 `/healthz`） |
| Production directory | `/home/ubuntu/macro-report`（canonical host path；wrapper literals 也以 rollback 語意保留此路徑） |
| Job runtime (wrappers) | `/home/ubuntu/macro-venv/bin/python3` |
| Schema version | `1`（`phase3b_initial_schema`；migration registry checksum 依 backend build 不同，兩者皆 by design） |
| Raw-layer store | PostgreSQL（cutover 後 raw 擷取寫入 PG）；SQLite `macro_history.db` 僅為 rollback source |

## 2. PostgreSQL authority

- Production 唯一 authoritative datastore 是 PostgreSQL。Cutover 採 controlled migration（preflight →
  rehearsal → controlled production cutover → final acceptance / incident closure，
  2026-10-05 FINAL acceptance 為 `FULL_PASS` closure state）。
- Raw layer（`db.py`）與 intelligence/store layer（`phase3.persistence.*`）在 production
  都以 PostgreSQL DSN 為 target。
- `automatic PostgreSQL→SQLite degradation` 從未存在且由 tests 斷言（`test_backend_contract.py`
  等）；SQLite 只可能經由 **explicit rollback contract** 被選用。

## 3. Runtime DB contract（Phase 6.8A normalization）

Canonical contract 文件：repo 外 WO 產物
`ABACUS_FIE_6_8A_RUNTIME_CONTRACT.md`（§1–§8）。核心規則：

### 3.1 Backend classes（explicit，never inferred）

- `postgres`：`postgres://` / `postgresql://` DSN（URL 或 libpq keyword 形式）。
- `sqlite`：explicit target — `sqlite:///abs/path`、absolute path、literal `:memory:`（test-only）、
  `sqlite:relative`（writable production-capable 用途中 REJECTED）。
- 從 CWD、import failure、missing env、implicit default file 推導 target **已廢除**；
  relative path / unknown scheme 一律 fail-closed 拒絕。

### 3.2 Role-scoped targets

| Role | Canonical env var | Legacy aliases（compat） | Consumer |
|---|---|---|---|
| raw | `FIE_DB_TARGET_RAW` | `FIE_DB_PATH` | `db.py` raw layer、wrapper `--source-db` |
| intelligence/store | `FIE_DB_TARGET_INTELLIGENCE` | `FIE_DATABASE_URL`（service contract）、`FIE_INTELLIGENCE_DB`（wrapper seed alias） | `phase3.persistence.backend.resolve_spec`、seed bridge |
| test/rehearsal | `FIE_DB_TARGET_TEST` | — | test-only explicit pin |
| rollback | `FIE_DB_TARGET_ROLLBACK` | — | explicit rollback operation only |

### 3.3 Resolution precedence（never silent default）

```text
explicit argument  >  canonical env var  >  legacy alias  >  FAIL CLOSED（exit 78 / exception；無 default）
```

### 3.4 Fail-closed refusal classes（shell guard exit 78；Python 同類 exception）

| Condition | Class |
|---|---|
| role 缺 target contract | `FAIL_CLOSED_DB_TARGET_REQUIRED` / `DATABASE_URL_MISSING` |
| relative path / unresolvable path | `FAIL_CLOSED_MALFORMED_DB_TARGET` |
| unsupported URL scheme | `FAIL_CLOSED_MALFORMED_DB_TARGET` |
| unparseable PostgreSQL DSN | `FAIL_CLOSED_MALFORMED_DB_TARGET` |
| contradictory aliases | `FAIL_CLOSED_CONTRADICTORY_DB_TARGETS`（transport refusal code `CONTRADICTORY_DB_TARGETS`） |
| rehearsal/test target ≡ production identity | `FAIL_CLOSED_REHEARSAL_DB_TARGET_REQUIRED` |
| production contract missing/unreadable/DSN-free | `FAIL_CLOSED_PRODUCTION_IDENTITY_UNAVAILABLE` |
| indeterminate execution mode + DB overrides present | `FAIL_CLOSED_PRODUCTION_MODE_DECLARATION_REQUIRED` |

歷史的「production fallback default in use while overrides are present」WARNING 已廢除：
未宣告 mode 且有 DB overrides 是 fatal。Production wrappers 在 wrapper env file
明確宣告 `FIE_SERVICE_ENV=production`。

### 3.5 Authoritative production identity

Production identity **只**從 production contract files 讀取
（`FIE_PRODUCTION_DB_CONTRACT` 指定檔案清單，未指定則使用 default contract set
`/home/ubuntu/fie-67b-upgrade/fie-service.env` + `/home/ubuntu/fie-67b-upgrade/fie-wrapper-pg.env`）。
Shell guard（`scripts/rehearsal_db_guard.sh`）、Python guard、tests、tooling 共用同一 identity set，
且 identity 比較不含任何 credential。Test/rehearsal wrapper 無法重新定義 production identity。

## 4. Configuration ownership

| Layer | Owner | Location | Git? |
|---|---|---|---|
| Production DB contract（service） | operator-owned 0600 env file | `/home/ubuntu/fie-67b-upgrade/fie-service.env`（`FIE_DATABASE_URL`=PG DSN、`FIE_SERVICE_ENV=production` 等） | **NO — outside git** |
| Production DB contract (wrapper) | operator-owned 0600 env file | `/home/ubuntu/fie-67b-upgrade/fie-wrapper-pg.env`（`FIE_WRAPPER_ENV` stanza 於每個 wrapper 最前） | **NO — outside git** |
| Repository config | tracked `config/**` | `config/phase3/*.yaml`、`config/policies/` 等 | tracked（僅 `${FIE_*}` placeholder；機敏值走 `.local.yaml` 並被 ignore） |
| Code-side resolver | `phase3/runtime_contract.py` | repo | tracked（純邏輯；無 secret echo） |

Variable 語意分類：

| Variable | Status | Notes |
|---|---|---|
| `FIE_DB_TARGET_RAW` / `FIE_DB_TARGET_INTELLIGENCE` / `FIE_DB_TARGET_TEST` / `FIE_DB_TARGET_ROLLBACK` | **canonical**（6.8A） | role-scoped，唯一優先權標準 |
| `FIE_SERVICE_ENV` | canonical（execution mode；ADR-017） | `production\|local\|""`=production class、`test\|staging`=rehearsal class；其他值拒絕 |
| `FIE_DATABASE_URL` | legacy alias — **compatibility-only** | 6.3 service contract；仍有效（resolved as intelligence role），不得設為 SQLite path 描述 production |
| `FIE_DB_PATH` | legacy alias — **compatibility-only**（raw role） | SQLite 時代路徑或 PG DSN（cutover stanza 以 DSN 值使用中） |
| `FIE_INTELLIGENCE_DB` | legacy alias — **wrapper seed alias**（intelligence role） | 與 `FIE_DATABASE_URL` contradiction 時 fail-closed |
| `FIE_DB_TARGET_ROLLBACK` | rollback-only | 只在 explicit rollback operation 使用 |
| 任何 embedded-credential DSN 出現在 repo / docs / evidence | **FORBIDDEN** | secrets 只存在 0600 contract files |

Forbidden configuration patterns（documented contract）：

- Silent SQLite fallback（任意形式）— 6.8A 已移除並回歸測試。
- Ambiguous DB target（canonical 與 alias 互相矛盾）— fail-closed。
- Production DSN embedded in repository / documentation / evidence — 禁止。
- Secret-bearing committed env file — 禁止。

## 5. Credential boundary & authentication boundary

- DSN credentials（含 password / `PGPASSWORD`）只存在 operator-owned 0600 檔案，
  位於 git repository 之外。Key 必須是 exported（child process 繼承 backend selection）。
- 服務 → PostgreSQL 認證：containerized 變體走 SCRAM-SHA-256（該變體 baseline §3.2）；
  Abacus production 的 PG 為 local supervisord 管理的叢集（見 6.7B cutover acceptance）。
- `scripts/rehearsal_db_guard.sh` + `phase3/runtime_contract.py` 的 identity 比較
  永遠 normalized、永遠不含 credentials。
- Never print/store：`FIE_AUTH_TOKEN` value、database/backup credentials、passphrase、
  bot credentials、embedded-credential DSN。可記錄：variable 名稱、路徑、fingerprint。
- PostgreSQL local trust boundary（OBS-006）與 application role least privilege（OBS-007）
  是 registered non-blocking hardening residuals（owner decisions pending；NOT remediated）。

## 6. Startup / preflight validation

- `phase3/service/pg_runtime_preflight.py`（WO C3 preflight，machine-testable JSON invariant report）：
  - `--mode production`：READ-ONLY。stat-based invariants（runtime root traversable = 0755、
    PGDATA path/owner/mode、`pg_control` 可被 service account 讀取、bounded `pg_isready`）。
    不 restart、不 checkpoint live cluster。
  - `--mode disposable`：完整 invariant chain 只針對 /tmp 下 disposable cluster。
  - 背景：2026-10-04 `pg_control Permission denied` PANIC 事故（home chmod 0700）；
    PGDATA-under-home 是 accepted constraint（owner decision AD-2），preflight 保證其
    survival conditions 持續可驗證。
- Transport config 的 DB-target resolution 在啟動時 fail-closed（§3.4），
  生產服務不會帶著有問題的 DB contract 起動。

## 7. Health vs readiness

| Probe | Auth | Semantics |
|---|---|---|
| `GET /healthz` | public（401 fail-closed 僅 `/readyz` 端） | **Liveness** — 固定 minimal envelope（`status: alive`），**independent of database**；the ONE public probe surface（ADR-018） |
| `GET /readyz` | **authenticated** | **Readiness** — 實際 exercise persistence seam；healthy 回 `status=ok` + `backend_kind`（`sqlite` \| `postgres`，no DSN/details）+ schema version；dependency/schema 條件走 503-family 分類（ADR-016/019） |

production cutover 後 `/readyz` 之 `backend_kind=postgres` 即為 production authority 的
operational evidence surface。Restart-loop signal：err log 中 `server_started` events 超過
deliberate restarts。

## 8. Schema ownership / migration assumptions

- Schema 由 in-repo persistence layer（`phase3/persistence/schema_v1.py`、`schema_pg.py`、
  `migrations.py`＋ migration registry）擁有；schema version `1`。
- 兩個 schema registry checksum 依 backend build（SQLite / PostgreSQL）不同屬 by design。
- Production schema change 需要獨立 work order + verified preflight backup；本 WO 無 schema 變更。
- `init-db` 是唯一 accepted store 建立機制；production init 需要 explicit target 且受全 guard boundary。

## 9. Data lifecycle / protection policy

- Raw 擷取：cutover 後寫入 PG raw tables；每日 in-band refresh 為 idempotent。
- Intelligence：seed 經 idempotent upsert（換日 bucket 合法重寫 `ingested_at` 為
  已鑑識確認之 legitimate attribution，非 residue）。
- Raw layer 保護（6.8A §4）：rehearsal/test 模式下每個 writable raw-layer open 比對
  authoritative production identities（PG DSN identity / file inode equivalence），
  production-equivalent write於 writer 產出 output **之前** 拒絕；intelligence target
  disposable 不使 raw layer disposable（兩層皆須隔離）。
- Zero-write invariant：負面 guard 測試證明 writer 未被呼叫／disposable fingerprint 不變。

## 10. Failure behavior（production）

- Missing/malformed/contradictory DB target → fail-closed refusal（不寫任何資料）。
- PG unreachable → transport/service 依 503-family readiness 分類回報；
  **不會** degrade 到 SQLite（degradation 不存在且被斷言）。
- Preflight invariant FAIL → exit 1（JSON report），供 gate 判斷。

## 11. Rollback boundary — SQLite role

SQLite 的正式角色：

```text
SQLite = ROLLBACK / RECOVERY SOURCE
```

- Rollback sources 保留不動：SQLite-era `macro_history.db`（含 repo-root tracked fixture
  `Macro_History.db` 為 stale legacy reference）、SQLite-era `intelligence_store.db`、
  gpg-encrypted backups（`/home/ubuntu/fie-67b-upgrade/backups/`，0600，digest 在外部記錄）。
- 操作任何 rollback source 需要 **explicit rollback contract**（`FIE_DB_TARGET_ROLLBACK`
  或 wrapper env 移除路徑）。
- Accepted rollback 模型（6.7B baseline §12）：stop service → git checkout
  `legacy-pre-6-7b-cutover` tag（fast-forwardable lineage，無 history rewrite）→
  還原 raw-layer DB（verified logical-content hash）→ revert wrapper rewires／
  移除 wrapper-pg env file（SQLite-era literal defaults 重新生效）→ relaunch + validate。
- Rollback 是 **lifecycle-level** 事件（cutover-level），需要 owner authorization，
  不是 runtime 的不會 silent fallback。

## 12. Testing isolation

- Test runners MUST NOT inherit production DSN variables（scrubbed env full regression;
  6.8A: `PYTHONPATH=. FIE_SERVICE_ENV=test FIE_HTTP_PORT=18720 python -m unittest
  discover -s tests --top-level-dir=.` → 2568 tests OK）。
- PG parity tests 使用 disposable PG target（`FIE_TEST_PG_DSN`）；declared but unreachable
  → FAIL（不 silent skip）；skip 僅限 explicit opt-out marker 且須記錄。
- Raw-layer rehearsal boundary：`tests/phase3/persistence/test_rehearsal_db_target_guard.py`、
  `tests/test_rehearsal_wrapper_guard.py`、`tests/test_rehearsal_guard_zero_write_invariant.py`、
  `tests/test_wrapper_guard_68a.py`。
- Production 永遠不是 mutation target。

## 13. Document precedence / known residuals

- 本文件為 **current-state** production narrative。SQLite/SQLite-fallback 語意若出現於
  其他文件，以日期分級判讀：
  - `docs/production/ABACUS_FIE_6_7B_PRODUCTION_BASELINE.md`（§9 D3 SQLite production）=
    2026-10-04 cutover 前的歷史記錄，已由其 §18 amendment 標註 supersession。
  - `docs/phase3/*`、root-level `PHASE2/3/4_*.md` = 該 phase 的歷史設計記錄。
  - README entrypoint section（Task F）指向本文件。
- Known residuals / accepted deviations：
  - OBS-006（PG local trust boundary）／OBS-007（role least privilege）— non-blocking
    hardening，owner decisions pending。
  - repo-root tracked SQLite fixtures（`Macro_History.db` 等）— stale legacy reference；
    清理屬 SQLite retirement 鄰接工作，未授權。
  - Wrapper 內 preserved SQLite-era literal defaults — 僅作 rollback path 生效
    （移除 wrapper-pg env file 時），絕非 production 預設。
- Observability：structured JSON logs（`/var/log/fie_api_67b.log`）、pipeline logs
  `logs/`、artifacts `metadata/reports/`；log destination/level 變更不在任何文件化授權範圍。