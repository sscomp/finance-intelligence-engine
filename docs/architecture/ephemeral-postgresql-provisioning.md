# Ephemeral Test-PostgreSQL Provisioning Contract (Phase 6.9A-R3)

> **Status**: `REPOSITORY_READY — CODEX_CLOUD_EXECUTION NOT VALIDATED (see cloud-execution-contract.md §10)`
> 本文件定義 `scripts/provision-test-postgres.sh` — **ONE canonical, repository-owned**
> ephemeral PostgreSQL provisioning boundary。任何 fresh clone / ephemeral cloud
> environment 在**無本機 PostgreSQL tooling**的情況下依然取得真實的 ephemeral
> PostgreSQL server（R2 的根因：fresh cloud clone 只能 bootstrap,沒有
> `initdb`/`pg_ctl`,且 repository 沒有 server provisioning path →
> `CLOUD_POSTGRESQL_PROVISIONING_BLOCKED`）。

---

## 1. Two shapes, one implementation

```bash
# standalone (canonical cloud validation entrypoint scripts/test-cloud.sh 內部使用)
scripts/provision-test-postgres.sh --ensure-cache
scripts/provision-test-postgres.sh --start [--force-portable]
scripts/provision-test-postgres.sh --stop <job-dir>
scripts/provision-test-postgres.sh --run <child…>

# sourceable library
. scripts/provision-test-postgres.sh && fie_test_pg_start && fie_test_pg_stop
```

`scripts/test-cloud.sh` **source** 這同一個實作；沒有第二個 provisioning 路徑。

## 2. Acquisition order（§6.4 — portable server acquisition）

1. `FIE_TEST_PG_BIN`（documented test-only override;必須含 `initdb`+`pg_ctl`）
2. system tooling:`/usr/lib/postgresql/<N>/bin`（版本高者優先）→ `initdb` on PATH
3. **portable distribution**（無系統 tooling 時的 deterministic path）:

| 項目 | 契約 |
|---|---|
| Artifact | `io.zonky.test.postgres:embedded-postgres-binaries-linux-<arch>` jar(內含單一 flat `.txz`:`bin/{initdb,pg_ctl,postgres}` + `lib/` + `share/`;**無 psql** — SQL 步驟一律走 psycopg) |
| Version | PINNED `PORTABLE_PG_VERSION=18.4.0`（== Production major.minor 18.4；改 pin 只改 script 內一處常數） |
| Source | Maven Central(reachable、documented、`https://repo1.maven.org/maven2/io/zonky/test/postgres`) |
| Integrity | 先取 published `.jar.sha256` sidecar(64-hex 形式驗證)→ 下載 jar → `sha256sum -c` → 安裝後 `initdb --version` / `pg_ctl --version` 驗證;**cache 每次 HIT 重新驗證 stored jar sha256** |
| Arch/OS detect | `uname -s`==Linux、`uname -m` ∈ {x86_64→amd64, aarch64→arm64v8};任一不符 → `POSTGRESQL_PLATFORM_UNSUPPORTED`(exit 90),fail-closed,無 fallback |
| Cache | `$HOME/.cache/fie/test-postgres`(可 `FIE_TEST_PG_CACHE_DIR` 覆寫);單一 install root(cold path 下載 jar → txz → 移除中間物) |
| 禁止 | sudo / interactive prompt / global daemon / system PostgreSQL configuration mutation / operator-HOME(帳號)依賴 |

## 3. Job-owned provisioning identity（§6.2 — MUST-holds）

| 項目 | 契約 |
|---|---|
| Data dir | per-run `mktemp -d /tmp/fie-pgtest.XXXXXX`,內含 `pgdata/` |
| Ownership marker | `pgdata/fie_ephemeral_pg.owner`(format `fie-6-9a-ephemeral-v1`;**start 前建立**;teardown 與 cleanup 的所有權證明) |
| User / DB | per-run 隨機 `fie_<hex6>` / `fie_test_<hex6>`;密碼 per-run 隨機,**只寫 0600 `auth.password`(job TMPDIR 內)**,僅作 readiness TCP-auth proof 的 child env value,永不列印、永不超過 child scope 持久 |
| Auth | socket 保持 job-owned trust;`pg_hba.conf` 的 host 行 pin `scram-sha-256`(僅改 job-owned pgdata 內的檔案,系統 PG config 永不觸碰) |
| Bind | `listen_addresses=127.0.0.1` server-side pin + start 後 per-line bind proof(僅 `127.0.0.1:<port>` 記錄算數;0.0.0.0 → start 失敗 stop+removed) |
| Port | per-run 隨機 free port(loopback bind 探測得出;非固定埠,不與系統服務衝突) |
| Encoding | `--encoding=UTF8 --locale=C.UTF-8`(pinned,不繼承 ambient locale;6.8C finding)+ readiness 驗證 `server_encoding==UTF8` |
| Guard | caller env 的任何 PG DSN candidate(`FIE_TEST_PG_DSN`/`FIE_DB_TARGET_*`/`FIE_INTELLIGENCE_DB`/`FIE_DB_PATH`/`FIE_DATABASE_URL`)→ **refuse fail-closed (exit 78)**,分類:非 loopback → `PRODUCTION_DSN_REJECTED`;protected identity(fie_prod*)或任意 caller PG DSN → `POSTGRESQL_IDENTITY_GUARD_REJECTED`;**value 永不列印**。自 provision 的 job 的非 Production 性由 construction 證明(cloud 上 production contract files 缺失) |

## 4. Readiness(§6.5 — deterministic)

全部通過才 export DSN:process 建立且 bind proof 通過 → socket 鑑權(TCP
scram proof + socket 鑑權一致) → DB identity(`current_database()`==、
`current_user()`==synthetic) → UTF-8(`server_encoding==UTF8`) → PG major
與 pin 一致 → guard approval(provision start 本身在 guard 之後)。產出
`readiness.json`(job dir 內,非 secret)。失敗 → `POSTGRESQL_*_FAILED` 分類
(93/94/95)、**stop + rm job-owned state**、永不靜默改認 SQLite。

## 5. Teardown(§6.6 — deterministic,idempotent)

`fie_test_pg_stop [<dir>]`(預設 `FIE_TEST_PG_JOB_DIR`):

1. dir 不存在 → 明確的成功(idempotent;re-entry 安全)
2. **無 ownership marker → 拒絕(exit 96),state 保留供診斷**(所有權不可證明時
   不動任何東西)
3. 運行中的 server:`postmaster.pid` 的 `data_dir` 與本 job dir 比對;不符 →
   拒絕(不會 kill 無關 PG)
4. `pg_ctl -m fast stop` 只以本 job 的 bin dir/pgdata + `rm -rf` job dir 驗證

契約:**永不 kill 無關 PostgreSQL、永不刪 operator cluster、永不刪
production data**。雙 cluster NC(tests/test_cloud_pg_provisioning.py)證明
stop A 之後 B 依然服務。

## 6. Failure classes(§6.7 — never generic)

| Exit | 類別 |
|---|---|
| 78 | `POSTGRESQL_IDENTITY_GUARD_REJECTED`(fail-closed guard;含 `PRODUCTION_DSN_REJECTED`) |
| 90 | `POSTGRESQL_PLATFORM_UNSUPPORTED` |
| 91 | `POSTGRESQL_PORTABLE_ARTIFACT_UNAVAILABLE` |
| 92 | `POSTGRESQL_PORTABLE_ARTIFACT_INTEGRITY_FAILED`(含 `POSTGRESQL_TOOLING_DISCOVERY_FAILED` 形式) |
| 93 | `POSTGRESQL_INITDB_FAILED` |
| 94 | `POSTGRESQL_START_FAILED` |
| 95 | `POSTGRESQL_READINESS_FAILED` |
| 96 | `POSTGRESQL_TEARDOWN_FAILED` |
| 2 | usage(unknown/missing action) |

`test-cloud.sh` 對 provisioning 失敗的下游語意:PG legs **skip-with-reason**
(classification 明示,永不靜默 SQLite fallback);test 本身不得因 provisioning
不可得而「改寫成 PASS」。

## 7. Security boundary(§9 — invariants)

```text
LISTEN                   = 127.0.0.1 ONLY(bind proof 強制；0.0.0.0 → start 失敗)
CALLER DB TARGET         = 禁止（任何 caller PG DSN → exit 78；value 永不列入）
PRODUCTION FALLBACK      = 禁止（artifact/platform 失敗 → 91/90，無 5432/fie_prod fallback）
SYNTHETIC CREDENTIALS    = ONLY（密碼 0600 job-scoped temp material；永不 REPORT/RECEIPT/evidence）
DSN IN COMMAND LINE      = 禁止（blob 以 env value 傳遞；DSN 只 export 給 child scope）
PGPASSWORD 持久          = 禁止（僅 child validation scope）
.SYSTEM PG CONFIG        = 永不觸碰（job-owned pgdata 內的檔案才可改）
```

## 8. Bootstrap 銜接(§7)

`bash scripts/bootstrap.sh`（zero-arg 契約,R1 修復)不受本變更影響;
`--with-postgres`(及 `--all`)安裝 psycopg。未知選項 fail-closed(exit 2)。
`test-cloud.sh` 在 provisioning 前不讀/不寫任何 env 檔;provisioner 亦然。

## 9. Test 證明(§10/§11)

* `tests/test_cloud_pg_provisioning.py` — NC4–NC15(offline-safe;portable
  legs 只走 pre-warmed cache,由 `test-cloud.sh` 自動 warm;cache-cold →
  附原因 skip,非 silent pass)。fake-tooling probes 證明 fail-closed
  (stub initdb → exit 93 + owned state removed;fake arch → exit 90;
  下載失敗 → exit 91;篡改 sha → exit 92;feign marker-less dir → exit 96)
* network-true acquisition(--ensure-cache 的真實 cold path)在 Evidence 層,
  即 `force_portable_start.log` / `portable_acquisition_cold_cache.log`
* 雙 cluster NC 證明 teardown 隔離
* 全 regression(focused + full)見 work order REPORT

## 10. 文件詞彙紀律

Remediation 是 **repository-ready / re-entry-ready**。在未來全新的 Codex Cloud
job 獨立 rerun 全套 validation 之前,Codex Cloud **NOT VALIDATED**。本文與
cloud-execution-contract.md 都**不**使用「Codex Cloud supported」或等效詞彙。

---

*Change record: Phase 6.9A-R3(2026-10-05)— one canonical repository-owned
ephemeral PostgreSQL provisioning boundary;replaces the R2 inline/best-effort
provisioning;R2 verdict `CLOUD_POSTGRESQL_PROVISIONING_BLOCKED` closed at
repository level, cloud level remains NOT VALIDATED until fresh re-entry.*