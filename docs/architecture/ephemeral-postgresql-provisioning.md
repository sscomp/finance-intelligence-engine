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
scripts/provision-test-postgres.sh --run [--force-portable] <child…>

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
| Cache | R4-R1 canonical resolver(見 §2.5):explicit `FIE_TEST_PG_CACHE_DIR` override(不可用 → fail-closed 97,無 fallback)> XDG > HOME > job-local temp fallback |
| 禁止 | sudo / interactive prompt / global daemon / system PostgreSQL configuration mutation / operator-HOME(帳號)依賴 |

## 2.5 R4-R1 cache-runtime resolution contract

§2.1 observed R4 blocker: Codex Cloud managed filesystem 限制寫入,`$HOME`
存在但 `$HOME/.cache` 不可寫 → 舊的 cache 收縮契約(`mkdir` 失敗 →
`POSTGRESQL_PLATFORM_UNSUPPORTED` exit 90)在最終可用 temp root 存在時仍然
fail — 分類錯誤(見 §11 distinctions)且無 fallback。R4-R1 修復:
**ONE canonical resolver**(`_fie_test_pg_cache_resolve`,存在於本
provisioner,bootstrap/test wrappers 皆不得複製 path-selection 邏輯):

| 優先序 | 候選 | 契約 |
|---|---|---|
| 1 | `FIE_TEST_PG_CACHE_DIR` explicit override | **hard contract**:probe 不可用 → `POSTGRESQL_PORTABLE_CACHE_PATH_UNWRITABLE`(exit 97)fail-closed,**無 fallback**;value 永不列印 |
| 2 | `XDG_CACHE_HOME` (定義時) | `<XDG>/fie/test-postgres`;probe-proven |
| 3 | `${HOME}/.cache/fie/test-postgres` | `$HOME` 被設定**不是**可寫證明;probe-proven |
| 4 | job-local temp fallback | `${TMPDIR:-/tmp}` 下 `mktemp -d` 配置的 FIE-owned dir(0700 + `fie_cache.owner` marker `format=fie-6-9a-cache-v1`);**每 invocation 唯一 → 並行 job 不碰撞;禁止 static `/tmp/fie`** |

解析 = **probe 證明實際可用性**(parent-creatable + write+unlink probe),
不是路徑存在性。temp-mode cache 只在 scope 內 memo reuse(test-cloud / `--run`
每 scope 一次 acquisition)、marker-proven 由建立者 cleanup
(`fie_test_pg_cache_cleanup`;測於 ownership unprovable 時 refuse exit 96)。

Cold acquisition(R4-R1 强化,R3 完整性模型不變):下載到**永不見終態的
`.partial.*` 名**→ 已驗 sha256 → extract/validate 於 `.stage.*` →
promotion 順序 bin/lib/share 先、verified jar+sidecar 後(完整性 marker)→
**中斷的 promotion 永遠不會成為可信 cache entry**。PERSISTENT cache 的 cold
acquisition 以 flock serialize;cache root 含**非 artifact-class 內容 →
exit 97 fail-closed,永不修改/清除他人內容(ownership unprovable)**。
Cache HIT 每次重驗 stored jar sha256(無網路)。

## 2.7 R4-R2-R1 additions（lock ownership / cleanup / acquisition robustness）

`.acquire.lock` 契約(R4-R2-R1 起;取代「路徑存在性」形狀的裸 lock):

| 項目 | 契約 |
|---|---|
| Ownership metadata | `format=fie-6-9a-acquire-lock-v1` + `invocation` / `pid` / `created_utc` / `cache_mode`,**在持有 flock 之下寫入**(LK1) |
| Classification(非變更性) | `ABSENT` / `STALE_JOB_OWNED`(fie metadata + flock free)/ `ACTIVE`(任何 live flock holder)/ `UNKNOWN`(不可解析內容,含 legacy 0-byte lock);`fie_test_pg_cache_lock_classify` 只讀 |
| Stale 處理 | fie-owned stale lock 的移除要求 fd 開啟、flock 取得、marker 判讀、readlink 活性確認與 `unlink` **全部在同一 shell**(子 shell 持有的 fd 對 caller 不可見 — 6.9A-R4-R2-R1 修復) |

Cleanup(R4-R2-R1 修復:cleanup-only 呼叫也會先解析 active cache):

```text
job-owned stale  → 安全移除(僅 flock-proven-dead + metadata-proven)
ACTIVE           → 原樣保留(絕不從 live holder 下抽走)
UNKNOWN          → exit 96,保留 + 明示回報(ambiguous residue 永不自動刪除)
temp-mode(scope 內)→ cleanup 解析後僅移除本 invocation 配置的 dir
```

併發/健狀契約:

- cold acquisition 以 flock `-w 300` serialize;fd 9 指到的 lock 被並行
  維護替換時,以 `readlink /proc/self/fd/N` 的活路徑/`(deleted)` marker
  偵測並重試(**不可**以 inode-stat 比較 — 在部份 kernel(本 host 實測
  6.17)/proc fd 解析不自跟 inode 而致偽差異);
- POSIX null-`exec` 規則:`exec 9>>file 2>/dev/null`(無命令)對 shell
  的重導是**永久**的 — 開鎖探測必須保持 `{ exec 9>>f; } 2>/dev/null`
  群組形狀,診斷輸出(含 fail-closed 訊息)不得被靜默(回歸測試 pin)。

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
| 97 | `POSTGRESQL_PORTABLE_CACHE_PATH_UNWRITABLE`(R4-R1:cache-runtime 路徑契約失敗 — override 不可用 / 無可寫候選 / cache root 含非 artifact 內容;**不**折疊進 90) |
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

## 11. R4 blocker classification discipline (R4-R1)

舊分類 `POSTGRESQL_PLATFORM_UNSUPPORTED` 只指 **OS/arch/tooling** 不符
(uname、tar/xz 缺失)。R4 觀察到的 `mkdir` EROFS/EACCES 是 **cache-runtime
路徑契約**問題,不得折疊進平台分類。獨立審查者必須能區分五類failure:
(1) PostgreSQL runtime/platform 不相容;(2) portable artifact
acquisition/cache-path 不相容(R4 實際類型,exit 97);
(3) download/network 失敗(91);(4) checksum/artifact-integrity 失敗(92);
(5) PostgreSQL startup/readiness 失敗(93/94/95)。

---

*Change record: Phase 6.9A-R3(2026-10-05)— one canonical repository-owned
ephemeral PostgreSQL provisioning boundary;replaces the R2 inline/best-effort
provisioning;R2 verdict `CLOUD_POSTGRESQL_PROVISIONING_BLOCKED` closed at
repository level, cloud level remains NOT VALIDATED until fresh re-entry.*

*Change record: Phase 6.9A-R4-R1(2026-10-05)— canonical writable-runtime
cache resolver(§2.5);exit 97 `POSTGRESQL_PORTABLE_CACHE_PATH_UNWRITABLE`;
atomic promotion + flock serialization + ownership-proven cleanup;remediates
the R4 Codex Cloud cache-path blocker at repository level.*

---

*Change record: (R4 blocker reference) 6.9A-R4 fresh Codex Cloud acceptance
job stopped at portable-acquisition cache mkdir (`/home/agent/.cache/fie`
read-only managed filesystem) with the pre-R4-R1 exit-90 contract;the R4
failure class is the cache-path contract (§11 distinctions), NOT a
PostgreSQL platform incompatibility;remediated here; authoritative cloud
acceptance is a NEW fresh job from the published remediation HEAD.*

## 12. R4-R4-R1 additions — canonical portable SQL client + exception-safe fixture lifecycle

兩個 fresh-Codex-Cloud R4-R4 缺口的 repository-level 修復:

1. **Canonical portable SQL client(WO §5)。** Repository validation 的 SQL
   需求(建/拆測試物件、查值、zero-write invariants、fixture 準備)一律走
   `scripts/sql_exec.py`(psycopg — pyproject 既有宣告依賴,亦是 provisioner
   readiness 檢查的同一 client)。**hermetic validation 永不執行 host `psql`**:
   fresh Codex Cloud 無 host psql,portable artifact 只含 server binaries
   (initdb/pg_ctl/postgres)。契約:target 永遠是**明確 DSN 參數**(無 env
   fallback、無 Production 選擇;ambiguous → `POSTGRESQL_SQL_TARGET_UNRESOLVED`
   fail-closed);driver 缺失 → `POSTGRESQL_SQL_CLIENT_UNAVAILABLE`(精確合約
   錯誤,非 generic FileNotFoundError);語句失敗 → `POSTGRESQL_SQL_*` 保留原始
   server 錯誤;DSN 進任何訊息前一律 redact `password=`。host `psql` 僅允許
   於「主題即是 host-tool 行為」的模組(如 phase3/service/pg_runtime_preflight.py)。

2. **Exception-safe fixture lifecycle(R4-R4 RESOURCE_OWNERSHIP_FAILURE)。**
   zero-write fixture(hermetic Production-SHAPED)改為 transactional:
   snapshot process-visible state → teardown 責任在任何 fallible 步驟前註冊
   (addClassCleanup + fixture 內 try/except)→ 任一失敗點執行 ownership-aware
   teardown(stop 本 fixture instance、還原 provisioner globals/cache/contract
   env、移除 self-owned workspace)→ **re-raise 原始例外**(teardown 以
   `strict=False` 不遮蔽)。teardown idempotent(FI-08)。另:每個 provision
   subshell 環境**scrub 掉所有 per-job provisioner globals**(僅保留
   `FIE_TEST_PG_CACHE_DIR` policy)— 先前 fixture 失敗後洩漏的
   `_FIE_CACHE_RESOLVED_DIR` memo 等狀態(讓下一個 lifecycle fixture 的
   registry 帶上前一個 fixture 的 cache path)— 此洩漏向量整體消除。

   失敗注入矩陣(FI-01..FI-10)與測試順序獨立性(歷史失敗序列、反序、單獨、
   交替重複)見 `tests/test_fixture_lifecycle_exception_safety.py`。

---

*Change record: Phase 6.9A-R4-R4-R1(2026-10-06)— §12 新增;remediates the
R4-R4 Codex Cloud portable-SQL-client contract defect(FileNotFoundError 'psql')
與 fixture lifecycle/state-isolation defect at repository level;
acceptance NOT VALIDATED until a fresh Codex Cloud job from the published
remediation HEAD.*
