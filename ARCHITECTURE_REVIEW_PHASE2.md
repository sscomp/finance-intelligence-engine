# 投資情報系統 — Phase 2 架構審查報告

分析日期：2026-07-08
分析對象：宏觀晨報 / 週報 / 月報 / 季度成分股更新提醒（四個排程 + 對應 Python 模組 + Hermes Cronjob 編排）
視角：Hermes Agent Platform — 從「四支腳本 + cron」進化到「可擴展的 Report / Research Agent Framework」
作者：M2（純研究，無程式碼變更、無 cron 修改、無 schema 變更）

---

## 0. 摘要（Executive Summary）

Phase 1 報告（`OPTIMIZATION_REPORT.md`，2026-07-07）已把現況盤點清楚：四個排程跑得動、SQLite 有 11 筆 macro + 50 筆 stock + 50 筆 institutional，但屬於「功能優先、品質債累積」狀態。本文不重複 bug hunting，而是把鏡頭拉遠，回答一個更根本的問題：

> 「四個獨立腳本 + Hermes cron」要如何進化，才能在不破壞現有排程的前提下，撐起未來 5–10 個報告、撐起多 agent / 多 worker、撐起 Orchestrator GPT 可隨時查任務狀態、撐起可移植到其他 Hermes 節點？

核心論點：

1. **現有四個腳本其實是同一條管線的四個實例** — 抓資料 → 評分 → 渲染 → 寫庫 → 派送。差異只在「抓什麼、評什麼、怎麼排」。這個共同骨架值得抽出。
2. **Hermes Cronjob 已經是「半個 Dispatcher」** — 已有 schedule、prompt、deliver、enabled_toolsets、state、last_run_at、last_status 概念。我們不需要從零打造排程引擎，而是**在 Cronjob 之上疊一層「Task Lifecycle」**（task_id、metadata schema、status transitions、retry policy、query API）。
3. **Phase 2 的成敗不在「寫多少新 code」，而在「能不能把現有程式當作 plugin 載入」**。如果 BaseReport 介面要強迫重寫 `macro_daily.py` 整支，那 BaseReport 就是 over-engineering。如果介面只要求「實作 4 個方法 + 1 個 manifest」，就能在零修改現有 main() 的情況下掛進去，那就是對的抽象。
4. **MVP 範圍建議縮成 2A + 2B 的 80%**：先做 metadata / template / config 結構化（純檔案層、不動 Python、不動 cron、不動 DB schema），再把 BaseReport 當作薄包裝（不改既有 main() 行為，只多 expose 一個 `run()` 介面 + 一個 `manifest.yaml`）。後面的 2C（Dispatcher + task_id）、2D（多 worker + dashboard）等 Phase 2 後半再做。
5. **不要碰的事**：cron schedule、SQLite schema、Python 源碼、venv、supervisord、telegram publishing、已上線 prompt — 全部 freeze。Phase 2 應該是「純增量」。

整份報告分 13 章，覆蓋：當前地圖 → 目標架構 → Pipeline → BaseReport / Plugin → Task Lifecycle → Dispatcher → Config 策略 → Prompt / Template → Data Layer → Observability → Roadmap → 風險 → 結論 + 8 個明確問題的回答。

---

## 1. 目前架構地圖（Current Architecture Map）

### 1.1 實體拓樸

```
┌──────────────────────────────────────────────────────────────────────────────┐
│ Abacus Container (abacusclaw-120fa02af8)                                     │
│                                                                              │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │ Hermes Gateway (LLM 路由)                                              │  │
│  │   └─ Cronjob Scheduler (sqlite + jobs.json)                            │  │
│  │        ├─ 381d62ce7f5e  morning-brief-dreaming      0 22 * * *         │  │
│  │        ├─ 4d8197ba6dab  morning-brief-delivery      0 8 * * *          │  │
│  │        ├─ 50d257f12a18  zo-computer-keepalive       */20 * * * *       │  │
│  │        ├─ ed214c19c4ac  總體經濟晨報                 30 8 * * 1-6      │  │
│  │        ├─ 60d92c57b826  產業趨勢週報                 0 8 * * 1          │  │
│  │        ├─ 5eaa5fa9a50d  公司研究月報                 0 8 12 * *         │  │
│  │        └─ af64556bc8e9  季度成分股更新提醒           0 9 1 1,4,7,10 *   │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
│                              │ 觸發 (每 tick scan jobs.json)                  │
│                              ▼                                               │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │ Cron Agent (獨立 hermes session, enabled_toolsets=terminal)            │  │
│  │   讀 prompt → terminal: bash run.sh → 把 stdout 整段送 telegram       │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
│                              │                                               │
│                              ▼                                               │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │ /home/ubuntu/macro-report/                                             │  │
│  │  ├─ run.sh / run_weekly.sh / run_monthly.sh   (venv + python wrapper)  │  │
│  │  ├─ macro_daily.py      (264 行)  →  yfinance × 6 ticker              │  │
│  │  ├─ industry_weekly.py  (276 行)  →  RSS × 8 feeds                    │  │
│  │  ├─ company_monthly.py  (616 行)  →  yfinance × 50 + T86 × 35 天      │  │
│  │  ├─ institutional.py    (140 行)  →  TWSE T86                          │  │
│  │  ├─ db.py               (255 行)  →  SQLite 3 張表                    │  │
│  │  ├─ industry_config.json / taiwan50_config.json                       │  │
│  │  └─ logs/YYYY-MM-DD.json (per-run archive)                            │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
│                              │                                               │
│                              ▼                                               │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │ SQLite: /home/ubuntu/macro-report/macro_history.db                     │  │
│  │   ├─ macro_daily          (date PK)        11 筆                       │  │
│  │   ├─ stock_monthly        (date, code PK)  50 筆                       │  │
│  │   └─ institutional_daily   (date, code PK)  50 筆                       │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
│                              │                                               │
│                              ▼                                               │
│  ┌────────────────────────────────────────────────────────────────────────┐  │
│  │ Telegram (鼎鼎 <TELEGRAM_CHAT_ID_REDACTED>) — stdout 原文轉發                            │  │
│  └────────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 共用 vs 差異矩陣

| 階段 | 共同 | 差異 |
|---|---|---|
| Scheduler | Hermes cron expr | `30 8 * * 1-6` vs `0 8 * * 1` vs `0 8 12 * *` vs `0 9 1 1,4,7,10 *` |
| Prompt | 「執行腳本 + 將 stdout 原樣送 telegram」 | 細節：timeout、月報 12 號、季度 ping 不一樣 |
| Script | `bash run*.sh` → source venv → `python3 *.py` | 三個不同 .sh + 一個 cron agent 自己印文字 |
| Fetcher | yfinance / urllib / xml.etree | 6 ticker vs 8 RSS vs 50 stock + 35-day T86 |
| Analyzer | 算 score + verdict + signals | assess_liquidity vs assess_stock vs industry classify |
| Renderer | 純 Python 字串拼接 + emoji + markdown-like | format_report / format_weekly_report / format_monthly_report |
| Publisher | `print(report)` → cron agent 收 stdout | 一致（都靠 cron agent 轉發） |
| Archive | 寫 `logs/<job>-YYYY-MM-DD.json` + SQLite | 一致 |
| Metadata | 沒有 | 全部都沒有 |

**觀察**：4 個 stage（Fetcher / Analyzer / Renderer / Archive）在所有 4 支腳本都是「存在但各寫各的」。這就是 BaseReport 抽象的甜蜜點。

### 1.3 已知缺口（與本架構最相關的，Phase 1 已列，這裡只標架構面）

- **沒有 task_id**：每次執行沒有 UUID/hash 識別，crash 後無法 dedupe / 重試。
- **沒有 status machine**：跑了就是「ok / failed」二選一，沒有 queued / running / waiting_external / rendering / publishing 等過渡狀態。
- **沒有 manifest**：新加一個 report 要「複製 4 個檔案 + 改 cron」，沒有「一行註冊」。
- **沒有結構化日誌**：全部 `print(..., file=sys.stderr)`，沒有 trace_id、沒有 metric、沒有結構化欄位。
- **沒有 retry / dedupe policy**：TWSE 503 → continue 丟整天 / yfinance throttle → None 沿用前值。
- **沒有 dispatcher / queue**：所有都在 cron agent 同一個 session 同步跑，沒有平行化、沒有優先權、沒有「下一批可排程」。
- **沒有 dispatcher → orchestrator 的回饋通道**：cron agent 送完 telegram 就「消失」，鼎鼎或 GPT 想知道「上次月報是 6/22 跑的、不是 7/12」只能去查 jobs.json。

---

## 2. 目標架構（Target Architecture）

Phase 2 結束時，目標是「**Hermes Report / Research Agent Framework**」：

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       Hermes Agent Platform (Phase 2)                        │
│                                                                             │
│  ┌────────────────────┐    ┌──────────────────┐    ┌────────────────────┐  │
│  │  Orchestrator GPT  │◀──▶│  Dispatcher API  │◀──▶│  Hermes Cronjob     │  │
│  │  (鼎鼎的 ChatGPT)  │    │  (FastAPI thin)  │    │  (現有 scheduler)   │  │
│  └────────────────────┘    └────────┬─────────┘    └────────────────────┘  │
│                                     │                                      │
│                                     ▼                                      │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │ Task Lifecycle Layer (新增, 純 SQLite + Python)                     │  │
│  │   task_id (UUID v7)  status: created→queued→running→rendering→       │  │
│  │                       publishing→completed | failed | cancelled     │  │
│  │   metadata: {report_type, schedule, attempt, triggered_by, ...}    │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                     │                                      │
│                                     ▼                                      │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │ Worker Pool (Phase 2D 才展開；Phase 2B 只要「單一 worker 同步」）   │  │
│  │   Worker-1: MacroDailyReport    (plugin)                             │  │
│  │   Worker-1: IndustryWeeklyReport (plugin)                            │  │
│  │   Worker-1: CompanyMonthlyReport (plugin)                            │  │
│  │   Worker-1: ConstituentsQuarterlyReminder (plugin)                   │  │
│  │   (未來) Worker-2: ETFWeeklyReport / CryptoDailyReport / ...         │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                     │                                      │
│                                     ▼                                      │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │ BaseReport 抽象層（plugin contract）                                │  │
│  │   interface: manifest.yaml + BaseReport.run()                       │  │
│  │   內含: Fetcher → Analyzer → Renderer → Archiver → Publisher         │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                     │                                      │
│                                     ▼                                      │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │ 現有 Python 模組（**不修改** main() 行為，只新增 plugin wrapper）   │  │
│  │   macro_daily.py / industry_weekly.py / company_monthly.py / ...   │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                     │                                      │
│                                     ▼                                      │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │ 資料層                                                              │  │
│  │   SQLite (現有 macro_history.db — schema 不變)                      │  │
│  │   + 新增: tasks.db (task lifecycle, **獨立 DB 檔** 避免動現有)      │  │
│  │   + 新增: reports.db (報告快照 metadata，獨立 DB 檔)                │  │
│  │   + config/reports/*.yaml / config/policies/*.yaml (純檔案層)        │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
│  ┌──────────────────────────────────────────────────────────────────────┐  │
│  │ Observability                                                        │  │
│  │   L1: 結構化 logs/<report>-<task_id>.jsonl (新增)                    │  │
│  │   L2: SQLite `task_metrics` 表 (計數器、histogram)                   │  │
│  │   L3: Hermes Dashboard `/macro-health` (Phase 2D)                    │  │
│  └──────────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 設計原則（5 條）

1. **Freeze the core**：現有 4 支 .py 檔的 main() 不動；現有 SQLite schema 不動；現有 cron 不動；venv 不重建；supervisord 不動；telegram publishing 邏輯不動。所有 Phase 2 改動都是「旁路 + 包裝」。
2. **Plugin over framework**：與其寫「macro-report framework」逼所有 report 繼承，不如寫「report plugin contract」讓既有 main() 變成 plugin（一行 import 就掛進去）。
3. **Append-only DB**：新資料（task lifecycle、metrics、templates）放新檔案（`tasks.db`、`reports.db`），不碰 `macro_history.db` 既有 3 張表。
4. **Config-driven report definition**：新加 Layer 5 / 6 不需要寫 Python，只要寫 `config/reports/<name>.yaml` + 一個 `tasks/<name>.py`（薄殼）。
5. **Hermes-native scheduling**：Phase 2 不自己造 scheduler，而是把 Hermes cronjob 當 scheduler，每個 cron job 的 prompt 改為「呼叫 dispatcher `POST /tasks`，不要直接執行 .py」。**不過 prompt 改寫本身屬於 Phase 2C — Phase 2A/2B 純粹旁路觀察，現有 prompt 一字不改。**

### 2.2 不在 Phase 2 範圍內（明確 non-goals）

- ❌ 改寫 `macro_daily.py` / `company_monthly.py` / `industry_weekly.py` 任何一行（除了「新增 1 個 function 給 plugin wrapper 用」這種純增量）
- ❌ 改 SQLite schema
- ❌ 改 cron schedule 或 prompt
- ❌ 改 telegram publishing 邏輯
- ❌ 改 venv / requirements
- ❌ 寫 multi-worker 真正平行（Phase 2D 才做，2B 只要單一 worker 同步）
- ❌ 部署新 dashboard / 新 cloudflared tunnel
- ❌ 從 OpenAI / Anthropic 改成其他 LLM provider（除非 cron agent 自己換）

---

## 3. 報告管線設計（Report Pipeline Design）

### 3.1 共同管線（七段式）

```
┌─────────┐   ┌─────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌────────┐
│Scheduler│──▶│  Task   │──▶│ Fetcher  │──▶│ Analyzer │──▶│ Renderer │──▶│Publisher │──▶│Archive │
│ (cron)  │   │ (新建)  │   │ (既有)   │   │ (既有)   │   │ (既有)   │   │ (既有)   │   │(新增)  │
└─────────┘   └─────────┘   └──────────┘   └──────────┘   └──────────┘   └──────────┘   └────────┘
     │              │             │              │              │              │            │
     ▼              ▼             ▼              ▼              ▼              ▼            ▼
  cron expr    task_id=        HTTP / yfinance  score/verdict  text+emoji    stdout→     log JSON
  + metadata  uuid v7         / RSS / T86      / signals      template      telegram    + SQLite
```

| 階段 | 角色 | 現況對應 | Phase 2 增強 |
|---|---|---|---|
| Scheduler | 何時觸發 | Hermes cron expr | 不動（凍結） |
| Task | 唯一識別 + 狀態追蹤 | 無 | 新增 task_id + status machine（§5） |
| Fetcher | 抓外部資料 | `fetch_indicators` / `fetch_rss` / `fetch_all_stocks` / `accumulate_institutional_data` | 不動；plugin 介面只要求「回傳 dict / list」 |
| Analyzer | 算分、評等、判讀 | `assess_liquidity` / `assess_stock` / `match_industry` | 不動；plugin 介面只要求「回傳 (verdict, signals, score)」 |
| Renderer | 文字 + 格式 | `format_report` / `format_weekly_report` / `format_monthly_report` | Phase 2B 可選：把硬編碼格式改 Jinja2，**但預設不動** |
| Publisher | 派送 | `print(report)` → cron agent 收 stdout → telegram | 不動（凍結） |
| Archive | 寫 log + 寫 DB | 寫 `logs/<job>.json` + 寫 SQLite | 不動；**新增** `tasks.db` 與 `reports.db` 作為 metadata 層 |

### 3.2 各 report 對應到七段管線

| 階段 | MacroDaily | IndustryWeekly | CompanyMonthly | ConstituentsQuarterly |
|---|---|---|---|---|
| Scheduler | `30 8 * * 1-6` | `0 8 * * 1` | `0 8 12 * *` | `0 9 1 1,4,7,10 *` |
| Task | ❌ 無 | ❌ 無 | ❌ 無 | ❌ 無 |
| Fetcher | `fetch_indicators()` yfinance ×6 | `collect_news()` RSS ×8 | `fetch_all_stocks()` yfinance ×50 + `accumulate_institutional_data()` T86 ×35 | (cron agent 自己打開瀏覽器) |
| Analyzer | `assess_liquidity()` | `analyze_supply_chain_impact()` + industry 分類 | `assess_stock()` | 無 |
| Renderer | `format_report()` | `format_weekly_report()` | `format_monthly_report()` | 純文字 emoji 模板 |
| Publisher | stdout → telegram | stdout → telegram | stdout → telegram | stdout → telegram |
| Archive | logs + SQLite macro_daily | logs + 無 DB | logs + SQLite stock/inst | logs + 無 DB |

### 3.3 共用抽出 vs 各自保留

**應該往上抽（到 BaseReport）**：
- `get_taipei_now()` / TZ_TAIPEI — 三個檔案重複
- 寫 `logs/<job>-<date>.json` 的 IO 樣板
- exit code 0/1 的 try/except 樣板
- Telegram-friendly 長度截斷（>4096 字 split）
- 「抓資料失敗但不讓整份報告 fail」的 partial-success 模式

**應該各自保留（不要硬抽）**：
- 各 report 的 fetcher 邏輯（yfinance 跟 RSS 完全是兩回事）
- 各 report 的 analyzer 邏輯（VIX 評分 vs stock 評分是不同的 domain knowledge）
- 各 report 的 renderer 邏輯（版面差太多，硬抽會變成 `if report_type == ...` 的巨型函式）

**可選抽（Phase 2B 之後再決定）**：
- Renderer 抽 Jinja2 — 見 §8
- Magic number 抽 config — 見 §8

---

## 4. BaseReport / Plugin 設計（BaseReport / Report Plugin Design）

### 4.1 為什麼要 BaseReport

**為了新增 Layer 5 / 6 不再複製 4 個檔案**。現況痛點：要把 macro_daily 整套（264 行 + 1 個 bash wrapper + 1 個 cron job）複製一份才能加新 report。BaseReport 提供「只要寫 plugin class + manifest.yaml，scheduler 自動撈到」的介面。

### 4.2 介面設計（最小可用集合）

```python
# reports/base.py — 不修改既有 .py；純新增
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

@dataclass
class ReportResult:
    report_text: str          # 最終要送 telegram 的字串
    verdict: str              # 「寬鬆」「中性」「緊縮」或 stock rank
    score: int                # 0–100 或 domain-specific
    signals: list             # 結構化信號
    raw_data: dict = field(default_factory=dict)  # 供 archive / dashboard 用
    artifacts: dict = field(default_factory=dict)  # 報告快照路徑、log 路徑

class BaseReport(ABC):
    # === 必要 metadata（從 manifest.yaml 載入）===
    name: str           # "macro_daily"
    display_name: str   # "總體經濟晨報"
    schedule: str       # "30 8 * * 1-6"
    version: str        # "1.0.0"

    # === 介面方法 ===
    @abstractmethod
    def fetch(self) -> dict: ...           # 抓外部資料
    @abstractmethod
    def analyze(self, data: dict) -> tuple[str, list, int]:
        """回傳 (verdict, signals, score)"""
        ...
    @abstractmethod
    def render(self, data: dict, verdict: str, signals: list, score: int) -> str:
        """回傳最終文字（要送 telegram 的字串）"""
        ...

    # === 內建輔助（plugin 不用自己寫）===
    def archive(self, result: ReportResult) -> None:
        """寫 logs/<name>-<task_id>.jsonl + SQLite reports.db"""
        ...

    def run(self) -> int:
        """單一 entrypoint：給 dispatcher / cron 呼叫。
        永遠 try/except，exit code 0/1 語意統一。"""
        try:
            data = self.fetch()
            verdict, signals, score = self.analyze(data)
            text = self.render(data, verdict, signals, score)
            result = ReportResult(
                report_text=text, verdict=verdict, score=score,
                signals=signals, raw_data=data,
                artifacts={"log_path": ...},
            )
            self.archive(result)
            print(text)  # 維持現有 stdout → cron agent 派送
            return 0
        except Exception as e:
            print(f"❌ {self.name} 失敗: {e}", file=sys.stderr)
            return 1
```

### 4.3 Plugin 載入機制

```
config/reports/
  macro_daily.yaml
  industry_weekly.yaml
  company_monthly.yaml
  constituents_quarterly.yaml
  (未來) etf_weekly.yaml
  (未來) crypto_daily.yaml
```

每個 manifest 範例：

```yaml
# config/reports/macro_daily.yaml
name: macro_daily
display_name: 總體經濟晨報
schedule: "30 8 * * 1-6"
timezone: Asia/Taipei
version: "1.0.0"
entrypoint: "reports.macro_daily_plugin:MacroDailyReport"
timeout_seconds: 120
retry_policy:
  max_attempts: 2
  backoff_seconds: 60
publisher:
  type: telegram
  chat_id: <TELEGRAM_CHAT_ID_REDACTED>
archive:
  sqlite_table: macro_daily
  log_dir: /home/ubuntu/macro-report/logs
  retain_days: 90
```

**Plugin loader 邏輯**（新增，不動現有 .py）：

```python
# reports/loader.py — 新增
import importlib
import yaml
from pathlib import Path

def load_all_plugins(config_dir="/home/ubuntu/macro-report/config/reports"):
    plugins = {}
    for yaml_path in Path(config_dir).glob("*.yaml"):
        manifest = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        module_path, attr = manifest["entrypoint"].split(":")
        cls = getattr(importlib.import_module(module_path), attr)
        instance = cls(manifest=manifest)
        plugins[manifest["name"]] = instance
    return plugins
```

### 4.4 各 plugin 對應檔案

| Plugin class | 對應既有檔案 | 對應 manifest | 實作方式 |
|---|---|---|---|
| `MacroDailyReport(BaseReport)` | `macro_daily.py` | `macro_daily.yaml` | **薄殼**：`fetch()` 直接 call `fetch_indicators()`、`analyze()` call `assess_liquidity()`、`render()` call `format_report()`。既有 main() **不刪**。 |
| `IndustryWeeklyReport` | `industry_weekly.py` | `industry_weekly.yaml` | 同上 |
| `CompanyMonthlyReport` | `company_monthly.py` | `company_monthly.yaml` | 同上 |
| `ConstituentsQuarterlyReminder` | (cron agent 印文字) | `constituents_quarterly.yaml` | 純文字模板，不抽 fetcher |

### 4.5 Layer / Module 切分

```
macro-report/                   (現有 root, 不動)
├── macro_daily.py              (不動)
├── industry_weekly.py          (不動)
├── company_monthly.py          (不動)
├── institutional.py            (不動)
├── db.py                       (不動 schema；只新增 1 個 task_log 表的 write helper 給 plugin 用)
├── *.json                      (不動)
├── run*.sh                     (不動)
├── logs/                       (不動)
│
├── reports/                    (新增, Phase 2B)
│   ├── __init__.py
│   ├── base.py                 (BaseReport abstract class)
│   ├── loader.py               (load_all_plugins)
│   ├── macro_daily_plugin.py   (薄殼，import 既有函式)
│   ├── industry_weekly_plugin.py
│   ├── company_monthly_plugin.py
│   └── constituents_quarterly_plugin.py
│
├── config/                     (新增, Phase 2A)
│   ├── reports/
│   │   ├── macro_daily.yaml
│   │   ├── industry_weekly.yaml
│   │   ├── company_monthly.yaml
│   │   └── constituents_quarterly.yaml
│   ├── policies/               (Phase 2A 末或 2B 開)
│   │   ├── scoring.yaml        (ROE 25/15/8 等)
│   │   ├── liquidity.yaml      (VIX 15/20/30)
│   │   └── retries.yaml
│   ├── publishers/             (Phase 2B)
│   │   └── telegram_default.yaml
│   └── templates/              (Phase 2B 末或 2C, 可選 Jinja2)
│       ├── macro_daily.md.j2
│       ├── industry_weekly.md.j2
│       └── company_monthly.md.j2
│
├── tasks/                      (新增, Phase 2C)
│   ├── __init__.py
│   ├── schema.py               (Task, TaskStatus enum)
│   ├── store.py                (SQLite 寫讀 task lifecycle)
│   ├── runner.py               (單一 worker：dequeue → call plugin → update status)
│   └── dispatcher.py           (FastAPI POST /tasks, GET /tasks/{id}, /tasks?status=...)
│
├── data/                       (新增, Phase 2C)
│   ├── tasks.db                (task lifecycle, 獨立檔, 不碰 macro_history.db)
│   └── reports.db              (報告快照 metadata)
│
├── observability/              (新增, Phase 2A 末)
│   ├── logger.py               (結構化 logging 設定)
│   └── metrics.py              (寫 task_metrics 表)
│
└── hermes_runtime/             (新增, Phase 2C, optional)
    └── bridge_hooks.py         (Hermes cron agent 透過 shell hook 跟 dispatcher 通訊)
```

### 4.6 介面是不是 over-engineering

判斷標準（3 個，全部 Yes 才算合適）：

- [x] 至少 3 個現有 / 規劃中的 report 對應 — ✅ 4 個現有 + 已規劃 ETF / Crypto
- [x] 介面只要求 3 個 abstract method，不要求 plugin 作者重寫 main() — ✅ 薄殼
- [x] plugin 跟現有 main() 可以並存（先開 plugin 路徑驗證，再決定是否 deprecate main()）— ✅ 不刪 main()

任何 1 個 No → 砍掉 BaseReport，改用「每個 report 一個獨立 .py + 共享 helper module」的扁平結構。

### 4.7 應該往上移的共用函式

| 函式 | 現況位置 | 應該到 |
|---|---|---|
| `get_taipei_now()` / `TZ_TAIPEI` | 4 個檔案各自定義 | `reports/common/time.py` |
| 寫 `logs/<job>-<date>.json` 樣板 | 4 個 main() 各自 | `reports/common/io.py` |
| `safe_num()` | company_monthly.py 私有 | `reports/common/format.py`（company_monthly 用，未來其他 plugin 也用） |
| `format_net_shares()` / `format_shares()` / `format_net()` | company + institutional 重複 | `reports/common/format.py` |
| try/except → exit 0/1 樣板 | 4 個 main() | `BaseReport.run()` 內建 |
| Telegram 長度截斷 (>4096 split) | 無 | `reports/common/telegram.py` |
| partial-success 模式 | 各 main 自行處理 | `BaseReport.run()` 提供 `continue_on_fetch_error=True` 旗標 |

### 4.8 應保持各 report 私有的

- `fetch_*` 函式本身（domain 不同，硬抽會變 god module）
- `assess_*` 函式（VIX 評分 vs stock 評分是不同的領域知識）
- `format_*` 函式本體（**Renderer 函式本體留在 plugin；只有「format 用的字串模板」可考慮抽 Jinja2**）

---

## 5. 任務生命週期設計（Task Lifecycle Design）

### 5.1 為什麼需要獨立的 task_id 系統（不依賴 OpenAI run_id）

現況：每次 cron 觸發，cron agent 開一個 hermes session，session 結束後 stdout 送出 telegram，整個生命週期就消失。沒有「這份報告是 task X 那一批的」「X 失敗過幾次」「上次成功是什麼時候」的概念。

OpenAI / Anthropic 雖然有 run_id / message_id，但：
- cron agent session 是 hermes 自己開的，跟上游 LLM provider 的 ID 不綁
- 一個 task 可能呼叫多個 LLM 步驟（fetcher 用 LLM？analyzer 用 LLM？），要分層
- Orchestrator GPT 想知道的是「macro-report task X 跑到哪」，不是「OpenAI run Y」

**因此 Phase 2 應該定義自己的 task_id，跟任何 LLM provider 的 ID 解耦。**

### 5.2 狀態機

```
                    ┌──────────┐
                    │ created  │  (Dispatcher 收到 trigger, 寫入 tasks.db)
                    └─────┬────┘
                          │
                          ▼
                    ┌──────────┐
                    │  queued  │  (等待 worker 取走)
                    └─────┬────┘
                          │
                          ▼
                    ┌──────────┐
                    │ running  │  (worker 開始 plugin.run())
                    └─────┬────┘
                          │
              ┌───────────┼───────────┐
              ▼           ▼           ▼
       ┌─────────────┐ ┌────────────┐ ┌──────────────┐
       │waiting_     │ │ rendering  │ │  publishing  │
       │ external    │ │            │ │              │
       │(API 等回應) │ │(format 階段)│ │(送 telegram) │
       └──────┬──────┘ └─────┬──────┘ └──────┬───────┘
              │              │              │
              └──────────────┼──────────────┘
                             ▼
                  ┌─────────────────────┐
                  │   completed |       │
                  │   failed    |       │
                  │   cancelled         │
                  └─────────────────────┘
```

### 5.3 狀態定義

| 狀態 | 進入條件 | 離開條件 | 重試政策 |
|---|---|---|---|
| `created` | Dispatcher `POST /tasks` 收到 | 立刻 → `queued` | N/A |
| `queued` | 寫入 tasks.db，等待 worker | worker 拿走 → `running` | 排隊逾時（>10min）→ `failed` |
| `running` | worker 開始 plugin.run() | 進入 waiting / rendering / publishing | watchdog 逾時（per-task timeout）→ `failed` |
| `waiting_external` | fetcher 等 yfinance / T86 / RSS 回應 | 收到回應 → `running` | HTTP timeout → retry, max_attempts 內 |
| `rendering` | renderer 開始（`format_*`） | 完成 → `publishing` | 通常很快失敗直接 → `failed` |
| `publishing` | 開始送 telegram | 送達 → `completed`；失敗 → `failed` | 1 次重試，失敗不重試（避免 spam） |
| `completed` | telegram 收到 ACK | 終態 | N/A |
| `failed` | 任何階段 raise | 終態（除非手動 rerun） | 累積 attempt > max_attempts → 不再 auto-retry |
| `cancelled` | Dispatcher `DELETE /tasks/{id}` | 終態 | N/A |

### 5.4 task_id 命名慣例

格式：`<report_type>_<yyyymmdd>_<short_uuid>`

範例：
- `macro_daily_20260708_a3f9c2`
- `company_monthly_20260712_7b4e1d`
- `macro_daily_20260708_b9c4f1`（同日第二次，rerun）

規則：
- `report_type` 對應 manifest.yaml 的 `name`
- `yyyymmdd` 是 Asia/Taipei 的觸發日
- `short_uuid` 是 uuid7 短碼（8 字元 base32），collision 機率 < 1e-9
- 同 report 同日可有多個 task_id（rerun / manual trigger / retry 都算）
- task_id 進 SQLite 主鍵，FK 到 `task_events` / `task_artifacts` / `task_metrics`

### 5.5 metadata schema（SQLite tasks.db）

```sql
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    report_type TEXT NOT NULL,         -- 'macro_daily' | 'industry_weekly' | 'company_monthly' | 'constituents_quarterly'
    schedule TEXT,                     -- cron expr
    triggered_at TEXT NOT NULL,        -- ISO8601 Asia/Taipei
    triggered_by TEXT,                 -- 'cron' | 'manual' | 'retry' | 'orchestrator'
    attempt INTEGER DEFAULT 1,         -- 1, 2, 3 ...
    status TEXT NOT NULL,              -- enum above
    status_updated_at TEXT,
    parent_task_id TEXT,               -- rerun 時指向原 task
    manifest_version TEXT,             -- 對應 manifest.yaml 的 version (e.g. '1.0.0')
    timeout_seconds INTEGER,
    metadata_json TEXT,                -- 自由欄位：trigger source, env vars, git sha
    created_at TEXT,
    finished_at TEXT
);
CREATE INDEX idx_tasks_status ON tasks(status, triggered_at);
CREATE INDEX idx_tasks_type ON tasks(report_type, triggered_at);

CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    event TEXT NOT NULL,               -- 'created' | 'fetch_started' | 'fetch_ok' | 'fetch_error' | ...
    payload_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES tasks(task_id)
);
CREATE INDEX idx_events_task ON task_events(task_id, created_at);

CREATE TABLE task_artifacts (
    task_id TEXT,
    kind TEXT,                         -- 'log' | 'snapshot' | 'report_text' | 'sqlite_row'
    path TEXT,                         -- /home/ubuntu/macro-report/logs/macro_daily-20260708-a3f9c2.json
    size_bytes INTEGER,
    created_at TEXT,
    PRIMARY KEY(task_id, kind, path)
);
```

**重點**：`tasks.db` 跟 `macro_history.db` 是**完全獨立的兩個檔案**。前者管 lifecycle，後者管 domain data。前者新增失敗絕不影響後者；後者 schema 不變。

### 5.6 狀態轉換範例（macro_daily 一次成功執行）

```
T+00:00  POST /tasks 觸發               → created → queued
T+00:00.1 worker 拿走                  → running
T+00:00.2 fetch_indicators 開始         → waiting_external
T+00:05.0 yfinance 6 ticker 完成        → running
T+00:05.1 assess_liquidity 完成         → running
T+00:05.2 format_report 開始           → rendering
T+00:05.3 format_report 完成           → publishing
T+00:05.4 print(stdout) → cron agent → telegram
T+00:08.0 telegram ACK                 → completed
```

### 5.7 失敗 + 自動 retry 範例（company_monthly 第一次失敗）

```
T+00:00  cron 觸發                       → created → queued → running → waiting_external
T+00:30  yfinance throttle → HTTPError   → failed (attempt 1)
T+00:31  retry policy: max_attempts=2, backoff=60s
T+00:32  task 重新 queued                → queued → running → waiting_external
T+00:90  第二次 fetch ok                  → running → rendering → publishing
T+00:95  telegram ok                     → completed (attempt 2)
```

### 5.8 Rerun 設計

- `POST /tasks` 帶 `parent_task_id` 欄位：Dispatcher 視為 rerun，建立新 task_id 但 `parent_task_id` 指回原 task
- 同一 `parent_task_id` 可有多個 child task
- 顯示時 `GET /tasks/{id}` 回傳本 task + 鏈上所有 ancestor/descendant

### 5.9 Orchestrator GPT 怎麼查 task 狀態

Dispatcher API（Phase 2C 實作）：

```
POST   /tasks                       觸發新 task
GET    /tasks/{task_id}             查單一 task 詳細（status, events, artifacts）
GET    /tasks?report_type=&status=  列表查詢（status=running 找出目前在跑的）
POST   /tasks/{task_id}/rerun       觸發 rerun
POST   /tasks/{task_id}/cancel      取消（running 設 cancelled）
GET    /health                      Dispatcher + Worker 健康（7 天成功率）
```

Hermes cron agent 跟 Dispatcher 通訊方式（不破壞現有 prompt）：
- Phase 2A/2B：純旁路觀察。Cron agent 跑完後由 dispatcher 從 `~/.hermes/cron/jobs.json` 同步 job_id 跟 task_id 的對應（讀 last_run_at → 推 task_id）
- Phase 2C：cron agent 的 prompt 改為「`POST /tasks` 拿 task_id，polling `/tasks/{id}` 到 completed，把 task_id 跟 report 一起送 telegram」。**這是唯一 prompt 改動的時間點。**

### 5.10 邊界情況設計

| 情況 | 處理 |
|---|---|
| Worker crash 中途 | watchdog 30s 沒更新 → 設 failed |
| Dispatcher 跟 worker 一起掛 | tasks.db 有 status=created/queued，啟動時 worker 撿回 |
| 同 task 連續 2 次以上 failed | 在 metadata_json 加 alert_required=true，下次 manual trigger 時通知 |
| task_id collision | 機率 < 1e-9，發生時 tasks.db UNIQUE 拒絕寫入，Dispatcher 重新生成 |
| Manual trigger 跟 cron trigger 撞 | 兩個 task 並存，兩份報告都送（手動觸發通常有「我現在要看」需求） |
| Rerun 跟原 task 同時 in-flight | 兩個 worker 各自跑，後完成的為「winner」；client 看 completed_at 取新的 |

---

## 6. Dispatcher / Worker / Queue 設計（Dispatcher / Worker / Queue Design）

### 6.1 三個角色

- **Dispatcher**：HTTP API server。負責「接 trigger、寫 tasks.db、回 task_id、查狀態」。無狀態，可水平擴展。
- **Worker**：執行 plugin 的 process。負責「dequeue → 跑 plugin.run() → 更新 status → 寫 artifacts」。**Phase 2B 跟 2C 都只要 1 個 worker**；Phase 2D 才展開 multi-worker。
- **Queue**：用 SQLite `tasks` 表的 `status` 當 queue。`status='queued'` 的 row 就是「待跑」。Worker `SELECT ... WHERE status='queued' LIMIT 1` 拿，UPDATE 成 `running`。

### 6.2 Phase 2B 範圍：單一 worker + SQLite queue

- **為什麼 SQLite 夠**：1 個 writer（worker），4 個 report / day = 4 tasks/day。SQLite WAL 模式撐得住每秒數十次 transaction。引入 Redis / RabbitMQ 是 over-engineering。
- **什麼時候升級**：當 worker 數 > 1，或 task 量 > 100/day，或需要優先權 queue → 換 Redis list 或 PostgreSQL `SELECT FOR UPDATE SKIP LOCKED`。
- **lock 機制**：worker 拿 task 時 `UPDATE tasks SET status='running', worker_id=?, status_updated_at=? WHERE task_id=? AND status='queued'`。CAS 風格，搶失敗的 worker 不重試。
- **watchdog**：每 30s scan `status='running' AND status_updated_at < now - timeout` → 設 failed。

### 6.3 Dispatcher API 草案

```python
# tasks/dispatcher.py — FastAPI（用 hermes-runtime-bridge 同套 uvicorn 風格）
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional, List

app = FastAPI(title="macro-report-dispatcher", version="2.0.0")

class CreateTaskRequest(BaseModel):
    report_type: str                          # 'macro_daily' etc
    triggered_by: str = "manual"              # 'manual' | 'orchestrator'
    parent_task_id: Optional[str] = None
    metadata: dict = {}

class TaskResponse(BaseModel):
    task_id: str
    status: str
    attempt: int
    triggered_at: str
    report_type: str
    finished_at: Optional[str]
    duration_seconds: Optional[float]
    events: List[dict] = []
    artifacts: List[dict] = []
    parent_task_id: Optional[str]

@app.post("/tasks", response_model=TaskResponse)
def create_task(req: CreateTaskRequest):
    # 1. validate report_type in plugins
    # 2. generate task_id
    # 3. INSERT INTO tasks (...) VALUES (...)
    # 4. return task_id
    ...

@app.get("/tasks/{task_id}", response_model=TaskResponse)
def get_task(task_id: str):
    # 1. SELECT * FROM tasks WHERE task_id=?
    # 2. SELECT * FROM task_events WHERE task_id=? ORDER BY created_at
    # 3. SELECT * FROM task_artifacts WHERE task_id=?
    # 4. compute duration_seconds
    # 5. return aggregated
    ...

@app.get("/tasks")
def list_tasks(report_type: Optional[str] = None,
               status: Optional[str] = None,
               limit: int = 20):
    ...

@app.post("/tasks/{task_id}/rerun")
def rerun_task(task_id: str):
    # 1. SELECT parent = task
    # 2. POST /tasks 帶 parent_task_id
    # 3. return new task_id
    ...

@app.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str):
    # 1. UPDATE status='cancelled' WHERE status IN ('queued', 'running')
    # 2. (running 要通知 worker 中止；Phase 2B 用 cooperative cancel — worker 輪詢 DB)
    ...

@app.get("/health")
def health():
    return {
        "dispatcher": "ok",
        "worker_count": 1,
        "queue_depth": count_queued(),
        "last_24h_success_rate": compute_success_rate(hours=24),
        "last_7d_success_rate": compute_success_rate(hours=24*7),
    }
```

### 6.4 Worker 執行模型

```python
# tasks/runner.py — 單一 process, 1 個 worker
import sqlite3, time, signal, sys
from reports.loader import load_all_plugins

PLUGINS = load_all_plugins()
WORKER_ID = f"worker-{os.getpid()}"
TICK_SECONDS = 2
WATCHDOG_SECONDS = 30

def main_loop():
    while True:
        task = claim_next_queued_task()
        if not task:
            time.sleep(TICK_SECONDS)
            continue
        run_task(task)
        watchdog_sweep()  # 順便掃 stale running

def claim_next_queued_task():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE tasks
        SET status='running', worker_id=?, status_updated_at=?
        WHERE task_id = (
            SELECT task_id FROM tasks
            WHERE status='queued'
            ORDER BY triggered_at ASC
            LIMIT 1
        )
        AND status='queued'
        RETURNING task_id, report_type, attempt, manifest_version, metadata_json
    """, (WORKER_ID, now_iso()))
    return cur.fetchone()

def run_task(task):
    plugin = PLUGINS[task["report_type"]]
    log_event(task["task_id"], "plugin_started", {"worker_id": WORKER_ID})
    try:
        rc = plugin.run()
        if rc == 0:
            update_status(task["task_id"], "completed")
        else:
            schedule_retry_or_fail(task)
    except Exception as e:
        log_event(task["task_id"], "plugin_error", {"error": str(e)})
        schedule_retry_or_fail(task)

def schedule_retry_or_fail(task):
    manifest = load_manifest(task["report_type"])
    max_attempts = manifest.get("retry_policy", {}).get("max_attempts", 1)
    if task["attempt"] < max_attempts:
        # queued again with attempt+1
        new_task = create_retry_task(task)
        log_event(new_task, "retry_scheduled", {...})
    else:
        update_status(task["task_id"], "failed", reason="max_attempts_exceeded")
```

### 6.5 重複預防

- **同 task_id 不會重複執行**：CAS UPDATE 只允許 `status='queued'` 變 `running`，搶到的人跑。
- **同 report_type 同分鐘不會重複 enqueue**：Dispatcher 在 `POST /tasks` 收到 `report_type` 時先查 `SELECT 1 FROM tasks WHERE report_type=? AND status IN ('queued','running') AND triggered_at > now-1min`。如有 → 回 409 conflict。
- **同 report_type 同 day 不限制**：rerun 跟 manual trigger 是合理情境。
- **Cron 觸發去重**：如果 cron 漏跑後又補跑（同 date 多個 trigger），Dispatcher 用 `report_type+date+triggered_by` 去重（同 source 同 date 第二次 enqueue 自動忽略）。

### 6.6 長任務與 timeout

- Per-task `timeout_seconds` 從 manifest 載入（macro_daily=120, industry_weekly=180, company_monthly=300, quarterly=10）
- Worker 用 `signal.alarm(timeout_seconds)`，timeout 觸發 → plugin.run() 拋 TimeoutError → 設 failed
- Phase 2D 平行化後：long task 跟 short task 應該分 queue，但 Phase 2B/2C 先 1 queue 1 worker 不用分

### 6.7 Multi-worker 擴展路徑（Phase 2D）

**情境**：3 個 worker 平行跑不同 report_type。瓶頸是 SQLite write contention。

**M2 路徑（單機多 process）**：
- 3 個 worker process，每個掛 supervisord
- Dispatcher 仍單一（uiautostart）
- Queue 仍 SQLite，CAS 拿 task 沒問題
- 寫 macro_history.db 仍可能 lock conflict — 加 `PRAGMA journal_mode=WAL` + `PRAGMA busy_timeout=5000`

**B2 路徑（多機）**：
- 兩台 Abacus 跑 worker（透過 cloudflared + dispatcher API）
- 風險：兩台同時搶 task → 仍 CAS 解決，不會 double-run
- 風險：兩台同時寫 macro_history.db → **不要這麼做**，DB 應集中在一台，另一台只跑 worker，DB 寫入走 API（Phase 2D 末或 Phase 3 處理）

**Cloud Worker 路徑（Vercel / Fly.io / Cloud Run）**：
- Worker 跑在 serverless
- 風險：cloud 環境不一定能連到 127.0.0.1:11434 (Ollama) / 127.0.0.1:8080 (web)
- 風險：cron agent 跟 worker 不同 network
- 結論：Cloud Worker 只適合**純 stateless** 報告（無 local data），macro-report 目前**不適合**（依賴 yfinance + T86 + 本地 SQLite）

### 6.8 Dispatcher 部署選項

| 選項 | 優點 | 缺點 |
|---|---|---|
| 跟現有 hermes-runtime-bridge 同 supervisord | 已有 SOP、不用新服務 | 跟 bridge 同 port range (8787)，可開 8788 |
| 獨立 supervisord program | 完全解耦 | 多一個 service 要管 |
| 嵌入 macro-report 內 (in-process) | 簡單，無 port | 沒辦法被其他 client 呼叫 |
| Cloudflare Tunnel public (hermes-runtime.biaobecue.com 已上線) | 外部可達 | 需 auth、需配 basic_auth（已 SOP） |

**建議**：Phase 2B 採 in-process（macro-report 內部 import dispatcher 跑在 main thread，port 8788）。Phase 2C 改獨立 supervisord program，bind 127.0.0.1:8788，未來可選 expose。

---

## 7. Config / Prompt / Template 策略（Config / Prompt / Template Strategy）

### 7.1 現況盤點

散落的設定有 6 類：

| 類型 | 現況 | 位置 | 問題 |
|---|---|---|---|
| 排程時間 | cron expr 字串 | `~/.hermes/cron/jobs.json` | 散落 4 處 + dream/brief 共 7 個 job |
| Magic number (threshold) | 散在 code 裡 | `macro_daily.py:assess_liquidity()` / `company_monthly.py:assess_stock()` | 4 個函式 12 處重複 |
| Ticker 對照 | JSON 檔 | `industry_config.json` (176 行) / `taiwan50_config.json` (258 行) | 兩個檔案，無法 schema validate |
| RSS feed URL | JSON 檔 | `industry_config.json.digitimes_feeds` 等 | 同上 |
| Cron prompt | 純文字 | `~/.hermes/cron/jobs.json[*].prompt` | 不可版本化 |
| Report format | Python string template 散在 code | 各 `format_*()` 函式 | 改格式要改 code |

### 7.2 Phase 2 統一策略

**目標目錄**：

```
config/
├── reports/                # 每個 report 一個 manifest
│   ├── macro_daily.yaml
│   ├── industry_weekly.yaml
│   ├── company_monthly.yaml
│   └── constituents_quarterly.yaml
├── policies/               # 跨 report 的 threshold 政策
│   ├── scoring.yaml        # ROE 25/15/8 等 stock scoring
│   ├── liquidity.yaml      # VIX 15/20/30 等 macro liquidity
│   ├── date_filter.yaml    # 週末 staleness threshold
│   └── retries.yaml        # 預設 retry policy
├── publishers/             # 派送目標
│   ├── telegram_dingde.yaml
│   ├── telegram_biaobio.yaml
│   └── (未來) email_dingde.yaml
├── data_sources/           # 外部 API 連線資訊（不含 secret）
│   ├── yfinance.yaml       # 6 ticker 對照
│   ├── rss_feeds.yaml      # 8 RSS feed 對照
│   └── twse.yaml           # T86 端點
├── templates/              # (Phase 2B 末) Jinja2
│   ├── macro_daily.md.j2
│   ├── industry_weekly.md.j2
│   └── company_monthly.md.j2
└── schedule.yaml           # (Phase 2C) 排程總表（取代散在 jobs.json 的 4 個 macro cron）
```

### 7.3 JSON vs YAML vs SQLite 取捨

| 設定類型 | 格式 | 理由 |
|---|---|---|
| `reports/*.yaml` | YAML | 人讀可寫、支援 comment、不需要 binary parse、git diff 友善 |
| `policies/*.yaml` | YAML | 同上，且常需要 inline comment 解釋「為什麼 ROE 25 是好公司」 |
| `data_sources/*.yaml` | YAML | 同上 |
| `templates/*.md.j2` | Jinja2 純文字 | template 本質就是 text |
| Tickers / 成分股清單 | **保留 JSON** | 既有 `taiwan50_config.json` / `industry_config.json` 已是 JSON 格式，且 `company_monthly.load_config()` 直接讀，動它會動現有 code。Phase 2 純粹旁路新增 YAML 對照表，JSON 留著當「真相」 |
| Task lifecycle runtime state | SQLite | 需 query、需 index、需 transaction |
| 報告 archive (報告全文) | SQLite (新檔 reports.db) 或 JSONL | 取 SQLite — 可 query「上個月所有 macro_daily 的 verdict 分布」 |
| 排程時間 (cron expr) | YAML (`config/schedule.yaml`) + jobs.json 雙寫 | jobs.json 是 hermes 內部 schema，**不要直接改**。Phase 2C 提供 `config/schedule.yaml` 為 single source of truth，再用 `hermes cron edit` 同步 jobs.json |

### 7.4 移轉策略（重要 — 不能一步到位）

**Phase 2A**：純新增 YAML。`macro_daily.py` 仍讀原 ticker JSON，**不從 YAML 讀**。驗證 YAML 結構正確（用 jsonschema 驗），但實際讀取路徑不變。

**Phase 2B**：plugin 從 YAML 讀 manifest，但 ticker 仍從原 JSON 讀（plugin 不去動既有 `load_config()` 函式）。

**Phase 2C 之後**（選擇性）：如果 YAML 維護明顯比 JSON 方便，且 `taiwan50_config.json` 跟 `industry_config.json` 都要改時，才考慮把 JSON 內容搬到 YAML 對照表。**這個轉移是「雙寫」不是「取代」**，以免 Phase 2 之後回不去。

### 7.5 Cron prompt 改寫（最後才做）

**現況 prompt 範例**（`ed214c19c4ac`）：
> 執行總體經濟晨報腳本，將結果發送給鼎鼎。
> 1. 用 terminal 執行 `bash /home/ubuntu/macro-report/run.sh`
> 2. 將輸出的完整報告文字原樣發送給使用者（不要修改格式，不要加額外說明）
> 3. 如果腳本執行失敗，發送簡短錯誤通知

**Phase 2C 之後改寫為**：
> 執行總體經濟晨報排程。
> 1. `POST http://127.0.0.1:8788/tasks` 帶 `{"report_type": "macro_daily", "triggered_by": "cron"}` 拿 task_id
> 2. 每 5 秒 `GET /tasks/{task_id}` 直到 status=completed/failed
> 3. 從 `GET /tasks/{task_id}` 取 `report_text` 欄位，原樣送 telegram
> 4. 把 `task_id` 加在訊息 footer（例如「📋 task_id: macro_daily_20260708_a3f9c2」）
> 5. 如果 status=failed，從 task_events 取最後一個 error event 發簡短通知

**為什麼這是 Phase 2C 才做**：Phase 2A/2B 期間 dispatcher / worker 還沒驗證過，prompt 改寫如果撞 dispatcher bug 會 debug 困難。讓 prompt 改寫對應到 dispatcher 「已上線且穩定 1 週以上」這個 milestone。

### 7.6 LLM 角色切分（防止 reformat）

現況：cron agent 收到 stdout 還會自己「加額外說明」、「改 emoji」造成報告漂移。Phase 2 prompt 明確要求「**不要修改格式，不要加額外說明**」但這要靠 LLM 自律 — 不穩。

**Phase 2 強化**：
- cron agent 收到 task response **只 render `report_text` 欄位**，不接觸整個 response JSON 的其他部分
- report_text 已經是 plugin 產出的最終字串，**LLM 不再加工**
- 如果未來 LLM 想加分析 / 評論，必須在 plugin 內部加（讓 plugin 多一個 `analyst_commentary` 欄位），不能由 cron agent 自由加
- 訊息 footer 加 `📋 task_id: ...` 跟 `🤖 generated by: macro_daily_plugin v1.0.0`，讓人（跟 LLM）知道這是程式產出、不是 LLM 即興

---

## 8. Prompt / Template 版本化（Prompt / Template Versioning）

### 8.1 現況問題

- cron agent prompt 散在 `~/.hermes/cron/jobs.json`，改 prompt 要 `hermes cron edit`，無 git diff
- 4 個 prompt 各自微調，沒有「prompt family」概念
- 報告格式（emoji 區塊、footer）散在 `format_*` 函式，改一個版面要重新 deploy
- 沒人知道「鼎鼎看到的 7/1 月報是用哪個 prompt + 哪個 template 跑的」

### 8.2 Prompt Versioning

- 每個 cron job 在 manifest.yaml 帶 `prompt_version` 欄位（e.g. `v1.0.0`）
- prompt 實際內容放在 `config/prompts/<job_id>/<version>.md`
- 範例：

```
config/prompts/
├── macro_daily/
│   ├── v1.0.0.md    # 現行 prompt（執行 run.sh, 轉發 stdout）
│   └── v2.0.0.md    # Phase 2C 後的 prompt（呼叫 dispatcher API）
├── industry_weekly/
├── company_monthly/
└── constituents_quarterly/
```

- 改 prompt = 新增 `v1.0.1.md` 檔 + 改 manifest.yaml 指向新版本
- jobs.json 的 prompt 欄位由 dispatcher 或 setup script 從 `config/prompts/...` 同步進去
- 任務完成時在 tasks.db 寫 `prompt_version=v1.0.0`，供日後 audit「那天用的是哪個 prompt」

### 8.3 Template Versioning

- `config/templates/<report_type>/<version>.md.j2`
- 範例：

```
config/templates/macro_daily/
├── v1.0.0.md.j2    # 現行版面
└── v1.1.0.md.j2    # 加 staleness warning
```

- plugin 啟動時 `template_version` 從 manifest 載入，渲染時讀對應檔
- 改 template 不需要改 plugin code
- 報告 footer 加 `template_version: v1.1.0`，audit 用

### 8.4 Jinja2 合適嗎

**合適，但要條件**：
- ✅ 當 template 是「把資料填進固定框」：例如「Top 5 列表」「產業新聞彙整」這種純資料→文字
- ✅ 當版面規則穩定、不常大改：例如 macro_daily 的「VIX 區塊 + 利差區塊 + footer」三段式
- ⚠️ 當邏輯複雜、有 conditional branches：Jinja2 `{% if %}` 會跟 Python if 混亂，這種情況用 Python `format_*` 函式更清楚
- ❌ 當 template 需要呼叫外部 API / 算衍生指標：Jinja2 沒辦法（也不應該），應在 plugin 的 `analyze()` 階段處理

**Phase 2B 末建議**：先把 `macro_daily.md.j2` 試一個，驗證 Jinja2 在 cron agent 自動加料的對抗下仍能維持「template 控格式，LLM 只解釋」。如果 1 個月穩定沒漂移，再推 `industry_weekly` 跟 `company_monthly`。**不強迫統一**。

### 8.5 「LLM 只解釋，template 控格式」實作

三層切分：

1. **plugin 層（Python）**：決定「**有什麼資料**」（fetcher 抓 + analyzer 算）
2. **template 層（Jinja2）**：決定「**資料怎麼排**」（標題、emoji、縮排、footer）
3. **LLM 層（cron agent）**：決定「**訊息外殼**」（要不要加摘要、要不要加 ⚠️ 圖示）

Phase 2 prompt 明確：

```
你只負責「把 report_text 整段丟給使用者，不修改、不加 emoji、不加評論」。
如果 report_text 開頭是 "❌"，表示 task failed，發簡短失敗通知，不要重發 report_text。
footer 加上 "📋 task_id: {task_id}\n🤖 generated by: {plugin_name} {version}\n🕐 data_freshness: {staleness_note}"
```

LLM 唯一允許的加工是「加 task_id footer」。其他都禁止。

### 8.6 防止 cron agent reformat 的 5 條機制

1. **prompt 明文禁止**：「不要修改格式，不要加 emoji」
2. **訊息 footer 加 task_id**：人跟 LLM 看到 task_id 就知道這是程式產出
3. **加 prompt version**：人跟 LLM 知道這是 v1.0.0，不是 LLM 即興
4. **task_events 記 LLM modify 行為**：Phase 2D 可加 LLM input/output log，audit 漂移
5. **報告長度超過 4096 不送**（Telegram 限制），由 plugin 自己 split，LLM 不要再加

### 8.7 退場條款

如果 LLM 仍漂移（例如擅自加「📈 看起來不錯喔」），退場方案：
- 把 prompt 改得更嚴格（明文要求「只 forward `report_text` 欄位的 bytes，不做任何字串處理」）
- 或在 dispatcher 端加一道「sanity check」：送 telegram 前比對「送出 bytes == report_text bytes + footer」，如果被 LLM 加料則 alarm

---

## 9. 資料層設計（Data Layer Design）

### 9.1 現況

單一 SQLite `macro_history.db` (48KB, 3 張表)：

| Table | PK | 用途 | 現有資料量 |
|---|---|---|---|
| `macro_daily` | date | 每日總體指標快照 | 11 筆 |
| `stock_monthly` | (date, code) | 每月個股基本面 | 50 筆（只有 1 個 snapshot） |
| `institutional_daily` | (date, code) | 法人動向（累積近月） | 50 筆（只有 1 個 snapshot） |

**現有 schema 限制**：
- `institutional_daily` 用「累積近月值」當 snapshot 邏輯反直覺（Phase 1 F7）
- 沒有 task_id 欄位
- 沒有 report_type 欄位（隱含從 table name 推）
- 沒有 prompt_version / template_version
- 沒有 staleness metadata
- 沒有 archive hash / size

### 9.2 Phase 2 資料層（6 層）

```
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 1: 原始資料（raw, immutable, append-only）                   │
│   = 從 fetcher 抓回來的原始 bytes / json                             │
│   存: /home/ubuntu/macro-report/data/raw/                          │
│       ├── yfinance/2026-07-08/US10Y.json                           │
│       ├── rss/2026-07-06/digitimes-semiconductor.xml               │
│       └── twse/2026-07-08/T86-20260708.json                        │
│   用 SQLite 也行, 但檔案較好 debug + 便宜                          │
│   不要在 Phase 2A/2B 做, 列入 Phase 2D                              │
└─────────────────────────────────────────────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 2: 正規化資料（normalized, 現有 macro_history.db）          │
│   = 現有 3 張表, **不動 schema**                                    │
│   唯讀的 analytic 查詢走這層                                        │
│   get_foreign_streak / get_pe_percentile 在這層                     │
└─────────────────────────────────────────────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 3: 衍生訊號（derived, 新增 tasks.db 或 reports.db）         │
│   = analyzer 算出的 score / verdict / signals                       │
│   應該跟 raw_data 分開存, 避免污染 source of truth                 │
│   Phase 2B: 在 reports.db 新增 `report_signals` 表                 │
│     task_id TEXT, kind TEXT, value_json TEXT, created_at           │
└─────────────────────────────────────────────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 4: 報告快照（snapshot, 新增 reports.db）                    │
│   = 每次 task 跑完, 把最終 report_text 存進 DB                     │
│   用途: 查「過去 30 天所有 macro_daily 報告全文」、「verdict 分布」│
│   schema:                                                         │
│     CREATE TABLE report_snapshots (                                │
│       task_id TEXT PRIMARY KEY,                                    │
│       report_type TEXT, version TEXT,                              │
│       verdict TEXT, score INTEGER,                                 │
│       report_text TEXT,         -- 完整字串                        │
│       signals_json TEXT,                                          │
│       data_freshness TEXT,       -- 距今 N 天                       │
│       created_at TEXT                                              │
│     );                                                            │
│   不要為了省空間只存 metadata, 報告全文的歷史價值高                │
└─────────────────────────────────────────────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 5: 任務 metadata（新增 tasks.db, 跟現有 DB 完全分離檔案）   │
│   = tasks / task_events / task_artifacts / task_metrics            │
│   見 §5.5 schema                                                  │
│   跟 macro_history.db 完全獨立, 不互相 FK                          │
│   這層是 dispatcher / worker 的 state                              │
└─────────────────────────────────────────────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 6: Outbox / Publish Queue（Phase 2C 新增 tasks.db）         │
│   = 如果 telegram 送失敗, 不丟訊息, 寫 outbox 表                  │
│   Worker 定期 retry outbox                                         │
│   schema:                                                         │
│     CREATE TABLE publish_outbox (                                 │
│       id INTEGER PRIMARY KEY AUTOINCREMENT,                       │
│       task_id TEXT, destination TEXT,                             │
│       payload_text TEXT,                                          │
│       attempt INTEGER DEFAULT 0,                                  │
│       next_retry_at TEXT,                                         │
│       last_error TEXT,                                            │
│       status TEXT  -- 'pending' | 'sent' | 'gave_up'              │
│     );                                                            │
│   **但 Phase 2 範圍內** 因為現有 telegram publishing 凍結,        │
│   這層只是設計, 不實作 (除非 dispatcher 自己直接送 telegram)        │
└─────────────────────────────────────────────────────────────────────┘
```

### 9.3 哪些表未來會加、哪些不動

| 表 | 動作 | 階段 |
|---|---|---|
| `macro_history.db.macro_daily` | **不動** | freeze |
| `macro_history.db.stock_monthly` | **不動**（F7 是 Phase 1 修的事）| freeze |
| `macro_history.db.institutional_daily` | **不動**（同 F7） | freeze |
| `tasks.db.tasks` | 新增 | Phase 2A |
| `tasks.db.task_events` | 新增 | Phase 2A |
| `tasks.db.task_artifacts` | 新增 | Phase 2B |
| `tasks.db.task_metrics` | 新增 | Phase 2B |
| `reports.db.report_snapshots` | 新增 | Phase 2B |
| `reports.db.report_signals` | 新增 | Phase 2B |
| `tasks.db.publish_outbox` | 新增（**不實作**） | Phase 2C design-only |
| `raw/yyyy-mm-dd/...` 檔案目錄 | 新增 | Phase 2D |

### 9.4 為什麼 append-only

- **可重現**：raw layer 完整保留，未來想改 analyzer 邏輯可重跑
- **可審計**：報告漂移時可對比「prompt v1.0.0 + template v1.0.0 + data 2026-07-08」是哪個環節出問題
- **不退化**：normalized layer 不被衍生計算污染
- **debug 友善**：失敗時可從 task_id → artifacts path → 對應 raw 檔

### 9.5 容量估算（不需擴容的證明）

- 4 tasks/day × 30 天 × 12 月 = 1440 tasks/year
- 每個 task ~ 5 events × 200 bytes = 1KB
- 每個 task 報告 ~ 10KB
- 一年總計：1.5MB tasks.db + 15MB reports.db + 1MB raw/
- SQLite 撐到 1GB 都不慢，**Phase 2 完全不用擔心容量**

### 9.6 什麼時候要升級到 PostgreSQL

- ✋ **不要在 Phase 2**。SQLite 撐得住。
- 升級 trigger（任何一個出現就升級）：
  - 平行 worker 數 > 5 寫同一 DB
  - task 量 > 1000/day
  - 需要 `SELECT FOR UPDATE SKIP LOCKED` 風格的 queue
  - 需要 multi-region 同步
  - tasks.db 檔案 > 100MB（雖然要很久才到）

### 9.7 為什麼 `tasks.db` 跟 `macro_history.db` 分開

- **風險隔離**：tasks.db schema migration 失敗不會影響 macro_history.db
- **lock 隔離**：worker 寫 tasks.db 不會跟 report 寫 macro_history.db 卡 WAL lock
- **備份策略不同**：macro_history.db 是「歷史資料」，每週備份；tasks.db 是「lifecycle 雜訊」，30 天 rotate
- **不同 owner / 不同權限**：tasks.db 可被 dispatcher 寫，macro_history.db 應只被 fetcher 寫

---

## 10. 觀測性設計（Observability Design）

### 10.1 三層觀測

| 層級 | 物件 | 形式 | 範圍 | 實作階段 |
|---|---|---|---|---|
| L1 | 結構化日誌 | JSONL 檔 | 每個 task 一次記錄 | Phase 2A |
| L2 | Metrics | SQLite 計數器 | aggregate 健康指標 | Phase 2B |
| L3 | Dashboard | Hermes Dashboard `/macro-health` 頁 | 7d / 30d 趨勢、drill-down | Phase 2D |

### 10.2 L1 — 結構化日誌

**新增** `observability/logger.py`：

```python
import json, os, time
from datetime import datetime, timezone, timedelta

TZ_TAIPEI = timezone(timedelta(hours=8))
LOG_DIR = "/home/ubuntu/macro-report/logs"

class StructuredLogger:
    def __init__(self, task_id: str, report_type: str):
        self.task_id = task_id
        self.report_type = report_type
        self.log_path = f"{LOG_DIR}/{report_type}-{task_id}.jsonl"
    
    def info(self, event: str, **fields):
        self._write("info", event, **fields)
    
    def error(self, event: str, **fields):
        self._write("error", event, **fields)
    
    def _write(self, level, event, **fields):
        rec = {
            "ts": datetime.now(TZ_TAIPEI).isoformat(),
            "level": level,
            "task_id": self.task_id,
            "report_type": self.report_type,
            "event": event,
            **fields,
        }
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
```

**用法**（plugin 內）：

```python
log = StructuredLogger(task_id, "macro_daily")
log.info("fetch_started", source="yfinance", tickers=6)
log.info("fetch_ok", ticker="US10Y", duration_ms=234)
log.error("fetch_failed", ticker="US2Y", error="HTTPError")
log.info("analyze_done", verdict="寬鬆", score=2, signals_count=4)
log.info("publish_done", telegram_msg_id=12345, duration_s=8.2)
```

**為什麼 JSONL**：append-only、grep 友善（`grep '"level":"error"' *.jsonl`）、可一次 `jq` 聚合。

**取代現況**：`print(..., file=sys.stderr)` 仍保留（給 cron agent 看），**平行**加 JSONL 結構化日誌給程式讀。**不取代**。

### 10.3 L2 — Metrics

**新增** `tasks.db.task_metrics` 表：

```sql
CREATE TABLE task_metrics (
    task_id TEXT,
    metric TEXT,             -- 'fetch_duration_ms' | 'fetch_count' | 'fetch_error_count' | 'publish_duration_ms' | 'report_bytes' | 'queue_wait_seconds'
    value REAL,
    labels_json TEXT,        -- e.g. {"source": "yfinance", "ticker": "US10Y"}
    created_at TEXT
);
CREATE INDEX idx_metrics_task ON task_metrics(task_id);
CREATE INDEX idx_metrics_name ON task_metrics(metric, created_at);
```

**範例 metrics**：
- `cron_run_exit_code{job="..."}` counter
- `cron_run_duration_seconds{job="..."}` histogram
- `fetch_duration_ms{source="yfinance",ticker="..."}` histogram
- `fetch_error_total{source="yfinance",error="HTTPError"}` counter
- `report_bytes{job="..."}` histogram
- `queue_wait_seconds{job="..."}` histogram
- `data_freshness_days{job="..."}` gauge

**Prometheus 暴露（Phase 2D 末）**：
- `127.0.0.1:8788/metrics` 提供 Prometheus text format
- 從 task_metrics 表 roll up

### 10.4 L3 — Dashboard（Phase 2D）

Hermes Dashboard 已上線（`https://hermes.biaobecue.com`，basic_auth 保護）。Phase 2D 在 Dashboard 加 `/macro-health` 頁：

**頁面 layout**：
```
┌─────────────────────────────────────────────────────────────┐
│ Macro Health                                                │
│                                                             │
│ ┌─────────────┬─────────────┬─────────────┬─────────────┐  │
│ │ Macro Daily │ Industry Wk │ Company Mth │ Quarterly   │  │
│ │ 7d: 100%    │ 7d: 100%    │ 7d: N/A     │ 7d: 100%    │  │
│ │ 30d: 95%    │ 30d: 92%    │ 30d: 80%    │ 30d: 100%   │  │
│ │ last: ok 2h │ last: ok 1d │ last: 16d ⚠│ last: 7d    │  │
│ └─────────────┴─────────────┴─────────────┴─────────────┘  │
│                                                             │
│ Failed Tasks (last 30 days)                                 │
│ ┌───────────────────────────────────────────────────────┐  │
│ │ task_id                  │ report  │ finished │ err  │  │
│ │ company_monthly_..._a1b  │ monthly │ 2026-07..│ yfi..│  │
│ └───────────────────────────────────────────────────────┘  │
│                                                             │
│ Report Archive (last 7 days)                                │
│ [expand list, click to view full report_text]              │
└─────────────────────────────────────────────────────────────┘
```

**後端**：用 dispatcher `/health` 跟 `/tasks?status=failed` 拿資料。**不要寫新 DB schema**。

### 10.5 健康檢查 / 告警

**L1 級別**（Phase 2A 就有）：每個 task 跑完寫一行 JSONL。失敗時可在 plugin 內 trigger 一行 `level=error, event=alert_required, reason=...`，dispatcher 看到就送 telegram 給鼎鼎。

**L2 級別**（Phase 2B）：cron job 加 healthcheck：「過去 24h 沒看到 macro_daily completed → 報 ⚠️」。`hermes cron add` 已有此概念（用 SQL query 驗證）。

**L3 級別**（Phase 2D）：Dashboard 視覺化，鼎鼎可以「主動查」而不是「被通知」。

### 10.6 鼎鼎的 3 條通知偏好（沿用既有）

從 MEMORY（2026-06-06 確認）：
1. 健康/正常/無變化 → **保持靜默**
2. 異常/事件/需要介入 → **通知**
3. 排程檢查正常 → 寫 log 但**不推送**

Phase 2 觀測性要遵守這 3 條：
- L1 結構化日誌永遠寫（不推送，給人查）
- L2 metrics 永遠累積（不推送，給 dashboard 算）
- L3 異常才推送 telegram（連續 2 次 failed 才推、單次失敗只 log）

---

## 11. 實作 Roadmap（Phase 2 Roadmap）

### 11.1 Phase 2A — 最小風險架構清理（1 週）

**目標**：建立 metadata / template / config 結構化骨架，**零修改現有 .py**、**零修改 cron**、**零修改 SQLite schema**。

**產出**：
- `tasks/` 模組雛形：`schema.py` (Task / TaskStatus enum) + `store.py` (SQLite CRUD)
- `tasks.db`（全新檔）含 `tasks` + `task_events` 兩張表
- `config/reports/macro_daily.yaml` 等 4 個 manifest
- `config/policies/scoring.yaml` 等政策檔（**純檔案，現有 .py 不讀**）
- `observability/logger.py`（**純新增**，plugin 還沒接，**現有 .py 不接**）
- `bash macro-report/setup.sh`：clone `tasks.db` schema from `db.py:init_db` 風格

**不在範圍**：
- 不改 `macro_daily.py` / `company_monthly.py` / `industry_weekly.py` 任何一行
- 不改 cron job
- 不改 `macro_history.db` schema
- 不改 telegram publishing 邏輯
- 不裝任何新套件（PyYAML 已內建、jsonschema 可選）

**風險**：極低。新增檔案 / 新增目錄，不影響任何現有 path。

**驗收標準**：
- `python -c "import tasks.store"` 不報錯
- `sqlite3 tasks.db ".tables"` 顯示 `tasks`, `task_events`
- `python -c "import yaml; yaml.safe_load(open('config/reports/macro_daily.yaml'))"` 解析 OK
- 4 個 cron 跑 1 週 0 失敗（因為完全沒動）

**rollback**：刪 `tasks/`、`config/`、`observability/`、`tasks.db` 即可。5 分鐘。

**預估工作量**：2-3 人天。

### 11.2 Phase 2B — 管線抽象（2 週）

**目標**：實作 BaseReport + 4 個薄殼 plugin，把 fetcher/analyzer/renderer 介面標準化，**仍不動現有 .py main()**。

**產出**：
- `reports/base.py` (BaseReport ABC)
- `reports/loader.py` (load_all_plugins)
- `reports/macro_daily_plugin.py` (薄殼：import `fetch_indicators`, `assess_liquidity`, `format_report` from `macro_daily.py`)
- `reports/industry_weekly_plugin.py` (同)
- `reports/company_monthly_plugin.py` (同)
- `reports/constituents_quarterly_plugin.py` (從 cron prompt 抽出的純文字模板)
- `reports/common/time.py` (`get_taipei_now`, `TZ_TAIPEI`)
- `reports/common/format.py` (`safe_num`, `format_net_shares`, `format_shares`, `format_net`)
- `reports/common/io.py` (寫 logs 樣板)
- `tasks/runner.py` (單一 worker)
- `tasks.db.task_metrics` 表
- `reports.db` (新檔) + `report_snapshots` + `report_signals` 表
- 1 個 integration test：mock yfinance 跑 `MacroDailyReport.run()` → 確認報告產出、tasks.db 寫入、log 寫入

**不在範圍**：
- 不刪既有 main()（並存）
- 不改 cron prompt
- 不改 cron schedule
- 不改 SQLite macro_history.db

**風險**：中。引入 plugin 抽象後可能撞既有 main() 的隱式依賴（例：`macro_daily.main()` 假設自己是 process entrypoint 印了特定 stderr）。**緩解**：plugin 跟 main() 並存 1 個月，確認 plugin 跑 100% 跟 main() 結果一致後，再考慮 deprecate main()。

**驗收標準**：
- `python -m reports.macro_daily_plugin` 跟 `python macro_daily.py` 跑出來的報告 byte-for-byte 一致
- `python -m tasks.runner` 跑 1 小時，吃 1 個 manual `POST /tasks`，status 從 queued → running → completed，task_events 5 筆以上
- 4 個 cron 仍正常跑（因為 main() 沒動），但 cron agent 的 stdout 內容完全相同

**rollback**：disable plugin 路徑（`config/reports/*.yaml` 改名 `.yaml.disabled`），回到 main() 流程。1 小時。

**預估工作量**：1-1.5 人週。

### 11.3 Phase 2C — Dispatcher + Task Tracking（2 週）

**目標**：把 Hermes cron job 從「直接跑 .py」升級為「呼叫 dispatcher /tasks」。**這是唯一允許改 cron prompt 的時間點**。

**產出**：
- `tasks/dispatcher.py` (FastAPI app, 6 個 endpoint)
- supervisord 新增 `macro-report-dispatcher` program（port 8788）
- `tasks/worker_pool.py` (1 worker, 支援 graceful shutdown)
- cron job prompt 改寫（4 個 prompt 各存 v2.0.0.md 至 `config/prompts/<job>/v2.0.0.md`）
- 4 個 prompt v1.0.0.md 保留為 fallback（rollback 用）
- 4 個 `hermes cron edit` 指令（同步 jobs.json）
- `bash macro-report/deploy_dispatcher.sh`
- `/tasks/{task_id}` 的 HTTP API 文件（給 Orchestrator GPT 看）

**不在範圍**：
- 不改 `macro_history.db`
- 不改 supervisord 既有 4 個 program（openclaw / api-server / syncthing / hermes-runtime-bridge / cloudflared）
- 不暴露 dispatcher 對外（仍 bind 127.0.0.1）

**風險**：中。改 cron prompt 是單點失敗（4 個排程同步停擺風險）。**緩解**：
- 改 1 個 prompt → 跑 1 週 → 沒事 → 改下一個
- 隨時可 `hermes cron edit` rollback 到 v1.0.0 prompt

**驗收標準**：
- `curl http://127.0.0.1:8788/health` 回 200
- `curl -X POST http://127.0.0.1:8788/tasks -d '{"report_type":"macro_daily"}'` 回 task_id
- `curl http://127.0.0.1:8788/tasks/{task_id}` 可看到 status
- 4 個 cron 跑 1 個月 0 失敗（提示：相當於現況 baseline）
- 連續 2 次 failed → 自動通知鼎鼎
- 1 次失敗 → 自動 retry 1 次

**rollback**：`hermes cron edit <job_id> 改 prompt v1.0.0` + 刪 supervisord program。30 分鐘。

**預估工作量**：1.5-2 人週。

### 11.4 Phase 2D — Dashboard / Multi-Agent / 未來擴展（1 季+）

**目標**：把系統從「4 個 report」進化成「Hermes Report / Research Agent Platform」。

**產出**：
- Hermes Dashboard `/macro-health` 頁（用 dispatcher `/health` + `/tasks?status=failed`）
- 3 個 worker 平行（macro_daily / industry_weekly / company_monthly 各自獨立 worker process）
- `data/raw/` 原始資料歸檔
- Layer 1 raw → Layer 2 normalized 重跑機制（reread raw → re-analyze → 對比新舊 verdict）
- Multi-region 預備（兩台 Abacus 跑 worker，DB 集中）
- Cloudflare tunnel 暴露 dispatcher API（basic_auth 保護）
- 對接 Orchestrator GPT（ChatGPT Custom GPT Action）：`POST /tasks`, `GET /tasks/{id}`, `GET /health`
- 第一個非 macro 報告：ETF 週報（驗證 plugin contract 對新 domain 適用）

**不在範圍**：
- 不取代 cron（Hermes cronjob 仍是最上層 scheduler）
- 不取代 LLM provider（仍走 cron agent）

**風險**：高。多 worker 平行會撞 SQLite WAL、會撞 macro_history.db 寫入。**緩解**：先單機多 process，再跨機，最後 cloud。

**驗收標準**：
- 3 個 worker process 並行跑 4 個 report 1 週，0 dead lock
- Dashboard `7d success rate` 顯示 4 個 report 各 1 個數字
- 新增第 5 個 report（ETF 週報）只要寫 1 個 `config/reports/etf_weekly.yaml` + 1 個 `reports/etf_weekly_plugin.py`，不需要改其他 4 個檔案

**rollback**：複雜。多 worker 各自 supervisord program，可逐個停。Dashboard 隱藏。**估 1 天。**

**預估工作量**：4-6 人週。

### 11.5 整體時程表

| 週 | Phase | 驗收 |
|---|---|---|
| W1 | 2A | tasks.db 4 個 manifest schema 驗證 |
| W2-W3 | 2B | 4 個 plugin 跑通 + integration test |
| W4-W5 | 2C | dispatcher 上線 + 1 個 cron 改 v2.0.0 prompt 跑 1 週 |
| W6-W7 | 2C | 剩下 3 個 cron 改 v2.0.0 prompt 跑 2 週 |
| W8-W12 | 2D | Dashboard + multi-worker + 第 5 個 report |

**保守估 Phase 2 全部 3 個月可完成，3 人月工作量。**

---

## 12. 風險與非目標（Risks and Non-goals）

### 12.1 風險表

| 風險 | 機率 | 影響 | 緩解 |
|---|---|---|---|
| BaseReport 介面跟現有 main() 行為偏離 | 中 | 高（plugin 跑出來跟 main() 不一致） | 並存 1 個月，byte-for-byte 對齊後才 deprecate main() |
| Dispatcher API 寫太複雜，cron prompt 改寫撞牆 | 中 | 高（4 個排程同步停擺） | 改 1 個 prompt 跑 1 週確認，1 個 1 個 rollout |
| tasks.db 跟 macro_history.db lock 衝突 | 低 | 中 | WAL mode + busy_timeout；不同 file 不會 lock 同一個 page |
| Cron agent LLM 仍漂移報告 | 中 | 中 | footer 加 task_id + prompt 改嚴格 + sanity check 退場方案 |
| Worker crash 中途狀態卡 running | 中 | 中 | watchdog 30s 掃 stale running → failed |
| YAML 改壞導致 plugin 啟動失敗 | 中 | 低 | jsonschema 驗證，fail-fast，dispatcher 啟動失敗時不接 request |
| 平行 worker 寫 macro_history.db 卡 | 高 | 中 | Phase 2D 才展開，先單機單 worker |
| Cloudflare tunnel 暴露 dispatcher 被外部攻擊 | 中 | 高（可被外部觸發報告） | basic_auth + IP allowlist + rate limit（沿用 hermes-runtime-bridge SOP） |
| Phase 2D 預算膨脹（從 3 月拉到 6 月） | 中 | 中 | 嚴守「每週有可演示的成果」原則，2A/2B 結束就 ship value |

### 12.2 明確 Non-goals

- ❌ 改寫既有 4 支 .py 的 main() 行為（除非「新增 1 個 function 給 plugin 用」這種純增量）
- ❌ 改 `macro_history.db` 既有 3 張表 schema
- ❌ 改 cron schedule
- ❌ 改 cron prompt（Phase 2A/2B 期間）— 只在 Phase 2C 改 1 次
- ❌ 改 telegram publishing 邏輯
- ❌ 從 OpenAI / Anthropic 換 LLM provider
- ❌ 從 Hermes cronjob 換 scheduler
- ❌ 部署新 cloudflared tunnel / 新 dashboard（Phase 2D 才做）
- ❌ 從 SQLite 換 PostgreSQL（Phase 2 完全不考慮）
- ❌ 從 yfinance 換 data source（單點風險，列入 Phase 3+ 評估）
- ❌ 從 `print(stdout)` 換 telegram publishing pipeline（除非 dispatcher 自己要送）

### 12.3 範圍擴張控制

如果 Phase 2 過程中發現新需求（例如「月報要加法人 Top 5」、「季度提醒要實際 fetch 元大公告」、「要加 dashboard 即時顯示 VIX 走勢」），**這些不是 Phase 2 的 scope**。應該：
1. 寫進 Phase 3 backlog
2. 評估是否需要修改 Phase 1 既有的 .py（如果需要 → 不要現在做）
3. 如果純 plugin 改寫可達 → 評估是否影響 dispatch contract

**原則**：Phase 2 結束時的「成功標準」是「能加第 5 個 report 不動既有 4 個檔案」。任何阻礙這個目標的改動都要再評估。

---

## 13. 最終建議與 8 個問題的回答（Final Recommendations）

### 13.1 三句話總結

1. **現有 4 個腳本是同一條管線的 4 個實例** — 共用骨架值得抽出，但要在不破壞現有 main() 的前提下做。
2. **Hermes Cronjob 已經是半個 Dispatcher** — 不要從零造排程引擎，而是在 Cronjob 之上疊 Task Lifecycle + Dispatcher API。
3. **Phase 2 成功的關鍵是「能加第 5 個 report 不動既有 4 個檔案」** — 任何阻礙這個目標的設計都是 over-engineering。

### 13.2 主要建議（5 條）

1. **先做 Phase 2A**：零修改現有 .py、零修改 cron、零修改 schema 的純增量骨架，1 週可完成。
2. **BaseReport 介面只要 3 個 abstract method**（fetch / analyze / render），內建 archive + run()，plugin 寫薄殼不重寫 main()。
3. **task_id 自有體系**：`<report_type>_<yyyymmdd>_<short_uuid>`，跟 LLM provider 的 run_id 完全解耦。
4. **tasks.db 跟 macro_history.db 分檔**：append-only 設計，6 層分層（raw / normalized / derived / snapshot / metadata / outbox）。
5. **LLM 只解釋、template 控格式**：報告內容由 plugin 產出、版型由 Jinja2 控、cron agent 只加 task_id footer。

### 13.3 8 個明確問題的回答

#### Q1. 哪些 Phase 1 Quick Wins 應該先做？哪些要等 Phase 2 架構就位？

**先做（不需架構）**：
- **Q1 cron prompt exit-code gate**（消除 F4）— 純改 prompt，1 小時
- **Q2 手動 trigger 月報驗證 next_run_at**（驗證 F1）— 10 分鐘
- **Q3 排程錯開**（消除 F6）— 5 分鐘
- **Q4 staleness 偵測**（消除 F5）— 30 分鐘

**等 Phase 2A 後做（要配合 metadata）**：
- **Q5 db.py 改 institutional schema**（修 F7）— **不要直接改**，等 Phase 2 用衍生表 `report_signals` 表達
- **Q6 quarterly reminder 自動 fetch**（修 F18）— 可在 Phase 2B 用 plugin 概念做
- **Q8 magic number 抽 config**（修 F10）— 等 Phase 2A 寫好 `config/policies/scoring.yaml` 才有地方放

**等 Phase 2C 之後做**：
- **M1 yfinance cache**（修 F2）— Phase 2C 的 worker 才有 task 級 cache
- **M2 T86 平行化**（修 F3、F8）— Phase 2C 的 worker pool 才適合
- **L5 Dashboard observability**（L2）— Phase 2D

#### Q2. `company_monthly.py` 應該立刻重構嗎？

**不應該立刻重構**。建議：

1. **Phase 2A 結束後**：只把 `safe_num` / `format_net_shares` / `format_shares` / `format_net` 抽到 `reports/common/format.py`，**新檔**。`company_monthly.py` 改 import 而已（純 refactor，不改行為）。
2. **Phase 2B**：`company_monthly_plugin.py` 是薄殼：`fetch()` call `fetch_all_stocks`、`analyze()` call `assess_stock`、`render()` call `format_monthly_report`。`company_monthly.py` 的 main() 仍可跑（並存）。
3. **Phase 2C 之後**：`format_monthly_report` 拆 5 個小函式（summary / rankings / sector / inst-rankings / footer），**這時才動 .py 本體**。需要 unit test 保護。

**立即重構的風險**：
- F1（月報漏跑）還沒修，重構後如果撞新 bug 會更難 debug
- 沒有 unit test，重構無法驗證
- 6/12 漏跑、7/1 manual 補跑的現狀，重構期間如果再漏一次會影響鼎鼎信任

**觸發重構的條件**（任何一個出現就動）：
- 已經有 ≥6 個 unit test 覆蓋 critical path
- 已經有 tasks.db 能記錄 task 結果（重構撞 bug 可 rollback）
- 鼎鼎明確要求（例：「月報想加 XX」）

#### Q3. BaseReport 會不會 over-engineering？

**不會，前提是 3 個判斷都 Yes**（見 §4.6）：

- [x] 4 個現有 + 已規劃 ETF / Crypto = 至少 5 個 report 對應
- [x] 介面只 3 個 abstract method，plugin 寫薄殼不重寫 main()
- [x] plugin 跟 main() 並存，先驗證再 deprecate

**如果 3 個條件有任何一個變 No，立即砍 BaseReport，改用「每個 report 一個 .py + 共享 helper module」的扁平結構**。

**過度設計的紅線**：
- ❌ BaseReport 超過 3 個 abstract method
- ❌ 要求 plugin 一定要寫 config.yaml 才跑（薄殼就應該 inline）
- ❌ BaseReport 內含 dispatcher / queue 邏輯（這些應該獨立）
- ❌ 要求所有 plugin 一定要用 Jinja2（應 optional）
- ❌ 要求所有 plugin 一定要寫 unit test 才接（先並存再要求）

#### Q4. SQLite 對 Phase 2 夠嗎？什麼時候要 PostgreSQL？

**Phase 2 完全用 SQLite**。升級 trigger（見 §9.6）：
- 平行 worker > 5
- task 量 > 1000/day
- 需要 `SELECT FOR UPDATE SKIP LOCKED`
- multi-region 同步
- tasks.db > 100MB

**為什麼 SQLite 對現在夠**：
- 4 reports/day × 365 = 1460 tasks/year，每個 task ~10 events，1.5MB/year
- WAL mode + busy_timeout 撐得住 1 個 writer + 多個 reader
- 4 個 report 都是「每天 1 次」，不是「每秒 100 次」
- 沒有需要 cross-region 同步

**升級風險**：
- PostgreSQL 需要長期 connection、TCP socket、不同備份策略
- 1 個 SQLite 檔可以 tar 走，PostgreSQL 是整個 service
- 對「Phase 2 還沒驗證」的系統，換 DB 等同換引擎

**建議**：
- Phase 2 全程 SQLite
- Phase 3+ 評估時先看「PostgreSQL 的什麼功能真的需要」
- 不要在 Phase 2 因為「將來可能要」就升級

#### Q5. Hermes Orchestrator GPT 怎麼拿到可靠的 task_id？

**Phase 2C 之後**（dispatcher 上線）：

```
1. Orchestrator GPT → POST http://hermes-runtime.biaobecue.com/dispatcher/tasks
   body: {"report_type": "macro_daily", "triggered_by": "orchestrator"}
   response: {"task_id": "macro_daily_20260708_a3f9c2", "status": "queued"}

2. Orchestrator GPT 收到 task_id → 存入自己的 short-term memory
3. 之後對話要查「月報跑完了沒」→ GET /tasks/{task_id}
4. 看到 status=completed → 取 report_text 渲染
```

**Phase 2A/2B（dispatcher 還沒上線）**：

- GPT 透過「查 ~/.hermes/cron/jobs.json」的 last_run_at 推 task_id
- 或：GPT 透過「`tail -n 50 /home/ubuntu/macro-report/logs/2026-07-08.json`」讀最新一份報告
- 或：GPT 透過 hermes-runtime-bridge 呼叫 hermes MCP 拿 cron job state

**可靠性設計**：
- task_id 是 primary key，永遠能從 tasks.db 查到
- 即使 dispatcher 掛了，tasks.db 還在 → 重啟 dispatcher 即可
- GPT 不用記 task_id 在自己 memory，隨時可從 `report_type + date` 推（用 `GET /tasks?report_type=macro_daily&date=2026-07-08`）

#### Q6. cron prompt 應該被替換還是標準化？

**標準化，但要分階段**：

1. **Phase 2A/2B**：**不動**現有 4 個 prompt。理由：dispatcher 還沒上線，prompt 改寫沒意義。
2. **Phase 2C**：4 個 prompt 改寫成統一 template：
   ```
   執行 {display_name} 排程。
   1. POST http://127.0.0.1:8788/tasks 帶 {"report_type": "{report_type}", "triggered_by": "cron"}
   2. 每 5 秒 GET /tasks/{task_id} 直到 status=completed/failed
   3. 取 report_text 欄位原樣送 telegram
   4. footer 加 "📋 task_id: {task_id}\n🤖 generated by: {plugin_name} v{version}\n🕐 data_freshness: {staleness_note}"
   5. status=failed 時發簡短失敗通知
   ```
3. **Phase 2C 末**：把 prompt 從 jobs.json 抽到 `config/prompts/<job>/v2.0.0.md`，jobs.json 由 setup script 同步。

**為什麼要標準化**：
- 4 個 prompt 各自微調會造成行為不一致（例：月報 prompt 寫「12 號」，其他沒寫）
- 統一 template 讓 Orchestrator GPT 可以預測每個 task 的呼叫方式
- 統一 prompt_version 讓 audit 可行

**為什麼不能一次到位**：
- 4 個 cron 同時改 prompt 是 single point of failure
- 改 1 個跑 1 週驗證、沒事再改下一個，是必要的 rollout 紀律
- v1.0.0 prompt 永遠保留為 fallback

#### Q7. 最好的 MVP 範圍是什麼？

**MVP = Phase 2A + Phase 2B 80%**：

**必做（2A + 2B 的必須項）**：
- ✅ tasks.db 跟 tasks 模組（2A）
- ✅ config/reports/*.yaml 4 個 manifest（2A）
- ✅ observability/logger.py 結構化日誌（2A）
- ✅ reports/base.py + reports/loader.py（2B）
- ✅ 4 個薄殼 plugin（2B）
- ✅ tasks/runner.py 單一 worker（2B）
- ✅ 1 個 integration test（2B）
- ✅ 並存 1 個月（2B 末）

**不做（推到 Phase 2C 之後）**：
- ❌ Dispatcher FastAPI（2C）
- ❌ cron prompt 改寫（2C）
- ❌ supervisord 整合（2C）
- ❌ Multi-worker（2D）
- ❌ Dashboard（2D）
- ❌ Jinja2 template（**2B 末可選**，但不強迫）

**MVP 結束時的「可演示成果」**：
- 寫 `config/reports/etf_weekly.yaml` + `reports/etf_weekly_plugin.py` 後，跑 `python -m tasks.runner` 看到 5 個 report 都能跑
- 4 個既有 .py 沒被改 1 行
- 4 個 cron 仍正常跑、報告跟以前一模一樣
- tasks.db 完整記錄所有 task 歷史

**MVP 時間**：3 週（2A 1 週 + 2B 2 週），1.5 人月。

#### Q8. 什麼絕對不能先碰？

**絕對不能先碰的（Phase 2 全程）**：

1. **現有 4 支 .py 檔的 main() 邏輯** — Phase 2 結束前一行都不能動
2. **`macro_history.db` 既有 3 張表 schema** — 連加欄位都不行（要加新表、新檔）
3. **cron schedule** — `30 8 * * 1-6` 等 4 個 expr 永遠不動
4. **cron prompt（Phase 2A/2B 期間）** — 只在 Phase 2C 統一改 1 次
5. **telegram publishing 邏輯** — `print(stdout)` → cron agent → telegram 這條路徑永遠不動
6. **venv / requirements** — 不裝新套件
7. **supervisord 既有 5 個 program**（openclaw / api-server / syncthing / hermes-runtime-bridge / cloudflared）— Phase 2C 才加 dispatcher program
8. **Hermes Cronjob scheduler 本身** — Phase 2 不自己造 scheduler
9. **cloudflared 設定** — 不加新 tunnel、不改現有 config
10. **LLM provider 切換** — 不換 OpenAI / Anthropic

**如果有人提議先碰以上任何一個，預設回答**：
> 這個東西很好，但應該排在 Phase 3。我們先讓 Phase 2A/2B 的純增量骨架上線，再評估要不要碰。

**為什麼**：Phase 1 已上線 4 個 cron 穩定跑 1 個月以上。Phase 2 任何「我覺得這個 .py 寫得不好順便改一下」的衝動，都可能造成 4 個 cron 同步停擺。**Freeze 是策略，不是偷懶。**

---

## 附錄 A：對照 Phase 1 報告的 18 項

| Phase 1 ID | Phase 2 對應 |
|---|---|
| F1 月報 6/12 漏跑 | **Phase 2C**：dispatcher 有 retry / health check，會主動通知 |
| F2 yfinance 沒 cache | **Phase 2C 末 / 2D**：worker 級 cache（先 in-memory，2D 用 Redis） |
| F3 T86 沒平行化 | **Phase 2D**：multi-worker 才有平行化價值 |
| F4 cron prompt 沒 exit-code gate | **Phase 2C**：prompt v2.0.0.md 內建 exit-code + status check |
| F5 週末 stale 沒標示 | **Phase 2A**：plugin metadata 加 `data_freshness` 欄位；**Phase 2B 末**：Jinja2 template 加 `⚠️ 資料落後 N 天` |
| F6 8:00 vs 8:30 撞期 | **Phase 2C 之後**：cron schedule 是 frozen，所以這個由「合併晨報內容」而非「改時間」處理 |
| F7 institutional_daily PK 邏輯反直覺 | **不在 Phase 2 範圍**（schema 凍結）— 用 `report_signals` 表達衍生信號 |
| F8 T86 沒 retry/backoff | **Phase 2C**：worker 內建 retry，manifest 的 `retry_policy` 設 max_attempts=2, backoff=60s |
| F9 format_monthly_report 太長 | **Phase 2C 末**：拆 5 個小函式（在 .py 內，需要 unit test 保護） |
| F10 magic number 散落 | **Phase 2A**：`config/policies/scoring.yaml` 結構化，**現有 .py 仍硬編碼讀**；**Phase 2B**：plugin 從 YAML 讀 |
| F11 industry 跨 feed 時間格式 | **Phase 2B**：plugin 加統一 pub_date 標準化 |
| F12 沒 unit test | **Phase 2B 末**：6 個 critical path test 為 plugin 標配 |
| F13 沒結構化日誌 | **Phase 2A**：`observability/logger.py` 上線，**現有 .py 不接**；**Phase 2B**：plugin 接入 |
| F14 config 沒 schema 驗證 | **Phase 2A**：jsonschema 驗 manifest，fail-fast |
| F15 沒去識別化 | **Phase 2A**：logs/*.jsonl 改成只存 metadata，report_text 改存 reports.db（分層） |
| F16 沒 type hint | **不在 Phase 2 範圍**（.py frozen）— 新寫的 .py（plugin / tasks / reports）全部 strict type hint |
| F17 沒 dependency lock | **不在 Phase 2 範圍**（venv frozen）— `uv pip freeze > macro-venv.lock.txt` 列為 Phase 3 |
| F18 quarterly reminder 只 ping | **Phase 2B**：plugin 化後，可加「自動 fetch 元大公告 + diff config」邏輯，但仍不修既有 .py |

---

## 附錄 B：跟 Macro Economy Morning Brief 的關係

Hermes 已有 `4d8197ba6dab morning-brief-delivery`（8:00 派送 weather + 行程 + 夢境建議）跟 `ed214c19c4ac 總體經濟晨報`（8:30 派送 macro 指標）。Phase 1 F6 提到 8:00 + 8:30 撞期。

**Phase 2 對此的態度**：
- 不合併（兩個 cron 是不同 owner：brief 是「起床助理」，macro 是「投資情報」）
- 不改時間（cron schedule 凍結）
- 不改 prompt（Phase 2C 才改）
- 唯一允許的改動：Phase 2C prompt v2.0.0 可加一句「若 8:00 brief 已派送 macro verdict，請引用不重複」

---

## 附錄 C：對應需求的關鍵決策記錄

| 需求 | Phase 2 決策 | 為什麼 |
|---|---|---|
| 4 個 cron 拆不開 | 凍結 cron，用 plugin + dispatcher 包裝 | cron 是 hermes 原生能力，不應繞過 |
| BaseReport 多抽象 | 只要 3 個 method | 多了就 over-engineering，少了不夠 |
| 報告全文存哪 | reports.db（SQLite 新檔） | 可查詢、可 join task_metadata |
| cron prompt 放哪 | jobs.json 短期，config/prompts/<job>/<ver>.md 長期 | jobs.json 是 hermes 內部 schema |
| LLM 怎麼防 reformat | footer 加 task_id + prompt 明文禁止 + sanity check | 雙保險 |
| 多 worker 怎麼不撞 DB | WAL mode + busy_timeout + 不寫同一 table | 不同 table 不同 lock domain |
| 怎麼對接 Orchestrator GPT | Dispatcher REST API（6 endpoint） | 業界標準、可審計、可版本化 |
| Phase 2 何時結束 | 「加第 5 個 report 不動既有 4 個檔案」可達標 | 客觀、可驗證、不模糊 |

---

*報告完*

*作者：M2（Hermes Assistant，鼎鼎的核心 AI 助手）*
*分析日期：2026-07-08*
*配套文件：`/home/ubuntu/macro-report/OPTIMIZATION_REPORT.md`（Phase 1, 2026-07-07）*

