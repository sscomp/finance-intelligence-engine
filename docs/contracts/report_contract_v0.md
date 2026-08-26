# 報告合約 v0 — Report Contract v0

狀態：**DRAFT / 非執行階段文件**（Phase 2A 增量產出）
作者：M2
日期：2026-07-08
對應章節：`ARCHITECTURE_REVIEW_PHASE2.md` §3、§4、§7

---

## 0. 適用範圍與限制

本合約是 Phase 2A 純增量文件，**沒有任何 production runtime 讀取它**。現有 4 個 cron
排程（總體經濟晨報 / 產業趨勢週報 / 公司研究月報 / 季度成分股更新提醒）仍 100% 走
`run*.sh` → `<report>.py` 的原路徑。Phase 2B 才會把 BaseReport plugin 介面對齊到本
合約。

- 凍結的東西（Phase 2 全程）：`macro_daily.py`、`industry_weekly.py`、
  `company_monthly.py`、`institutional.py`、`db.py`、`run*.sh`、
  `~/.hermes/cron/jobs.json`、`macro_history.db` schema、Telegram 派送邏輯。
- 本合約允許的事：定義 4 份 report 的「概念藍圖」、task_id / 狀態欄位名、約束條件，
  作為 Phase 2B 介面對齊的 reference。

---

## 1. 報告識別（Report Identity）

| 欄位 | 型別 | 必填 | 說明 |
|---|---|---|---|
| `report_id` | string | Y | 唯一識別。Phase 2 內由 dispatcher 產生；Phase 2A 之前以 `<report_type>_<yyyymmdd>` 推導。 |
| `report_type` | enum | Y | 4 選 1：`macro_daily` / `industry_weekly` / `company_monthly` / `constituents_quarterly_reminder`。 |
| `schedule_name` | string | Y | 對應 `~/.hermes/cron/jobs.json` 的 `name`（例：`總體經濟晨報`）。 |
| `cron_job_id` | string | Y | Hermes cronjob ID（例：`ed214c19c4ac`）。 |
| `cron_expr` | string | Y | 對應 cron 表達式（凍結值，列在此處僅供 audit）。 |
| `version` | semver | Y | 報告 schema 版本，初始 `1.0.0`（Phase 2A 起算）。 |

範例（v0 草稿）：

```yaml
report_id: macro_daily_20260708_a3f9c2
report_type: macro_daily
schedule_name: 總體經濟晨報
cron_job_id: ed214c19c4ac
cron_expr: "30 8 * * 1-6"
version: 1.0.0
```

---

## 2. 輸入源（Input Sources）

每份 report 對應到現有 Python 模組的 fetcher 函式。Phase 2A 僅列名稱，不變更呼叫
路徑。

| report_type | fetcher 函式 | 資料源 |
|---|---|---|
| `macro_daily` | `macro_daily.fetch_indicators()` | yfinance × 6 ticker |
| `industry_weekly` | `industry_weekly.collect_news()` | DIGITIMES / 鉅亨網 RSS × 8 |
| `company_monthly` | `company_monthly.fetch_all_stocks()` + `institutional.accumulate_institutional_data()` | yfinance × 50 + TWSE T86 × 35 天 |
| `constituents_quarterly_reminder` | (cron agent 開瀏覽器手動確認) | 元大 ETF 公告頁 |

合約欄位：

```yaml
input_sources:
  - name: yfinance_macro_indicators
    kind: external_api
    module: macro_daily
    function: fetch_indicators
    refresh: daily
    expected_size_kb: 2
  - name: digitimes_rss
    kind: rss
    module: industry_weekly
    function: collect_news
    feeds_count: 8
    refresh: weekly
```

---

## 3. 輸出路徑（Output Paths）

Phase 2A 沿用既有 stdout 派送，**不引入新檔案路徑**。Phase 2B 之後可選
`metadata/reports/<report_type>/<yyyymmdd>/<task_id>/` 目錄結構（design only）。

```yaml
output_paths:
  stdout: true                     # Phase 2A 仍走 stdout → cron agent → telegram
  log_archive: logs/<job>-<date>.json     # 既有路徑，例：logs/2026-07-08.json
  archive_dir_phase2b: metadata/reports/<report_type>/<yyyymmdd>/<task_id>/
```

---

## 4. 渲染器 / 模板 ID（Renderer / Template ID）

Phase 2A 不強制走 Jinja2。模板欄位先預留：

| 欄位 | Phase 2A 預設值 | 備註 |
|---|---|---|
| `renderer` | `python:<module>.format_*` | 既有 inline 函式 |
| `template_id` | `inline:v1` | Phase 2B 末可改 `jinja:macro_daily_v1.0.0` |
| `template_version` | `n/a` | renderer 仍 inline，無 template 概念 |
| `format_function` | `macro_daily.format_report` 等 | 既有 Python 函式名 |

Phase 2A template 草案位置：`templates/reports/<report_type>_v0.md.j2`
（**目前 production 不讀**，純 placeholder）。

---

## 5. 派送合約（Publisher Contract）

Phase 2A 派送 = 「現有 stdout → cron agent → Telegram」，**不變動**。

```yaml
publisher:
  type: telegram
  channel: stdout
  target_chat_id: "5132341473"          # 鼎鼎
  secondary_targets: ["8029464467"]     # 彪彪（季報/部分月報）
  retry_policy: none                    # 凍結：失敗交給 cron agent / Hermes 自己
  failure_action: cron_agent_reports_error
  max_message_length: 4096              # Telegram 限制；>4096 由 cron agent split
```

Phase 2C 規劃（design only，**Phase 2A 不實作**）：`config/publishers/telegram.yaml`
可定義 multi-channel、retry 規則、queue。

---

## 6. 歸檔 / Metadata 欄位（Archive / Metadata Fields）

既有歸檔格式（`logs/<job>-<date>.json`）Phase 2A 凍結。Phase 2B 計畫引入
`metadata/reports/<report_type>/<task_id>.json` 結構化歸檔，schema 如下（design only）：

```yaml
metadata:
  task_id: macro_daily_20260708_a3f9c2
  report_type: macro_daily
  triggered_by: cron | manual
  cron_job_id: ed214c19c4ac
  started_at: 2026-07-08T08:30:00+08:00
  finished_at: 2026-07-08T08:30:14+08:00
  duration_ms: 14000
  exit_code: 0
  status: completed | failed | partial
  plugin_version: 1.0.0
  template_version: inline:v1
  prompt_version: 1.0.0
  data_freshness_days: 1
  artifact_paths:
    - logs/2026-07-08.json
  error: null
```

---

## 7. 成功 / 失敗準則（Success / Failure Criteria）

| 條件 | 視為 |
|---|---|
| exit code = 0 且 stdout 非空 | `success` |
| exit code = 0 但 stdout 為空（例如 RSS 當天沒抓到） | `partial` |
| exit code ≠ 0 但 fetcher 部分成功 | `partial` |
| exit code ≠ 0 且 fetcher 全失敗 | `failed` |
| dispatcher / worker 層 timeout | `failed` |

Phase 2A 行為：4 個 cron 仍以「exit 0 = 成功、exit 1 = 失敗」決定是否送 Telegram
成功訊息。**現有行為凍結**。

---

## 8. 冪等鍵（Idempotency Key）

| 用途 | 鍵格式 | 範例 |
|---|---|---|
| 同一天重複觸發 | `<report_type>_<yyyymmdd>` | `macro_daily_20260708` |
| 同一天多次執行（idempotency check） | `<report_type>_<yyyymmdd>_<short_uuid>` | `macro_daily_20260708_a3f9c2` |
| 同份報告 rerun 帶 dedupe | `rerun_of:<task_id>` | `rerun_of:macro_daily_20260708_a3f9c2` |

短 uuid 採 `secrets.token_hex(3)`（6 hex chars），不依賴 LLM provider 給的 run_id。

---

## 9. 對應 BaseReport 的介面（Phase 2B 預留）

Phase 2B 將實作 `reports/base.py` 的 3 個 abstract method：

```python
class BaseReport(ABC):
    report_type: str
    version: str

    @abstractmethod
    def fetch(self, ctx) -> dict: ...      # 對應 §2 fetcher

    @abstractmethod
    def analyze(self, raw, ctx) -> dict:  # 對應 assess_*
        # 回傳 {verdict, signals, score}
        ...

    @abstractmethod
    def render(self, analyzed, ctx) -> str:  # 對應 format_*
        # 回傳最終 stdout 字串
        ...
```

Phase 2A 純合約，**不寫程式碼**。Plugin 實作在 Phase 2B（`reports/macro_daily_plugin.py`
等薄殼）。

---

## 10. 4 份既有 Report 的合約對照（Mapping Table）

| 欄位 | MacroDaily | CompanyMonthly | IndustryWeekly | ConstituentsQuarterlyReminder |
|---|---|---|---|---|
| `report_type` | `macro_daily` | `company_monthly` | `industry_weekly` | `constituents_quarterly_reminder` |
| `schedule_name` | 總體經濟晨報 | 公司研究月報 | 產業趨勢週報 | 季度成分股更新提醒 |
| `cron_job_id` | `ed214c19c4ac` | `5eaa5fa9a50d` | `60d92c57b826` | `af64556bc8e9` |
| `cron_expr` | `30 8 * * 1-6` | `0 8 12 * *` | `0 8 * * 1` | `0 9 1 1,4,7,10 *` |
| 觸發日 | Mon–Sat 08:30 | 每月 12 號 08:00 | 每週一 08:00 | 1/4/7/10 月 1 號 09:00 |
| fetcher 模組 | `macro_daily` | `company_monthly` + `institutional` | `industry_weekly` | (cron agent 手動) |
| analyzer | `assess_liquidity` | `assess_stock` | `analyze_supply_chain_impact` + 產業分類 | 無 |
| renderer | `format_report` | `format_monthly_report` | `format_weekly_report` | 純文字 emoji 模板 |
| 寫 SQLite | `macro_daily` table | `stock_monthly` + `institutional_daily` | 無 | 無 |
| 預估 stdout 長度 | ~3 KB | ~10 KB | ~5 KB | ~1 KB |
| 預期完成時間 | <30 秒 | <90 秒（含 T86 × 35 天） | <60 秒（RSS × 8） | <5 秒（人工確認） |
| 成功 / 失敗標準 | exit 0/1 | exit 0/1 | exit 0/1 | exit 0/1 |
| plugin 薄殼路徑（Phase 2B） | `reports/macro_daily_plugin.py` | `reports/company_monthly_plugin.py` | `reports/industry_weekly_plugin.py` | `reports/constituents_quarterly_plugin.py` |
| manifest draft | `config/reports/macro_daily.yaml` | `config/reports/company_monthly.yaml` | `config/reports/industry_weekly.yaml` | `config/reports/constituents_quarterly.yaml` |
| template placeholder | `templates/reports/macro_daily_v0.md.j2` | `templates/reports/company_monthly_v0.md.j2` | `templates/reports/industry_weekly_v0.md.j2` | `templates/reports/constituents_quarterly_v0.md.j2` |

---

## 11. 變更歷史

| 版本 | 日期 | 變更 |
|---|---|---|
| 0 | 2026-07-08 | Phase 2A 初始 draft。10 個小節、4 份 report 對照表。**未來 Phase 2B 對齊 BaseReport 介面時再升至 v1。** |
