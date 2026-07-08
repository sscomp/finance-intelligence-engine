# Phase 3 — Investment Intelligence Engine 架構總覽

狀態：DESIGN ONLY（2026-07-08，by M2）
上一層：Phase 2B framework（BaseReport / Pipeline / Registry / Dispatcher / LocalStore 已上線）
下一層：可選 Phase 3B（plugin 接入）、Phase 3C（GPT Orchestrator 接入）
本文件群位置：`/home/ubuntu/macro-report/docs/phase3/`

---

## 0. 一句話定義

Investment Intelligence Engine = 一組**可解釋、可追溯、可配置**的 scoring 模組，**建在 Phase 2B framework 之上**，不重複造 data layer。它把「抓到的數字 / 抓到的文字」轉成「-100~100 的分數 + 解釋 + 證據鏈」。

> 不取代任何現有 report。**Phase 3 是評分引擎；現有 report 是發布器。**

---

## 1. 為什麼需要 Phase 3

Phase 2B framework 解掉了「如何跑一個 report」的問題：BaseReport 三段式（fetch / analyze / render）、Dispatcher 任務生命週期、LocalStore 持久化、Registry 動態載入。但 `analyze` 階段目前是**黑盒**：

- `macro_daily.py` 算 `liquidity_score` 用了硬編碼 thresholds，散落在 200 多行。
- `company_monthly.py` 的 `assess_stock` 算 ROE/EPS/PE 等 6 個分項，沒有拆出來、沒有解釋、沒有信心值。
- `industry_weekly.py` 沒有 score，只有「哪個產業被提到幾次」的計數。
- 四支腳本的 scoring 邏輯**無法跨層引用**（例：總體 −20 時要自動調降公司分數 5%），因為沒有統一的 scoring 物件。

Phase 3 要解決的：

1. **拆出獨立 scoring 模組**，跟 fetcher/renderer 解耦 → 之後 4 支腳本都能複用。
2. **可解釋性** — 每個分數附 evidence（哪個指標 / 哪則新聞 / 哪個時間點）。
3. **可追溯性** — 信號從哪裡來、怎麼 decay、怎麼被加總，完整 audit trail。
4. **跨層傳遞** — macro score → industry score → company score，可以讀取上游分數做條件調整。
5. **可配置** — weights / thresholds / decay 全部 config 化，不寫死在 code 裡。

---

## 2. 設計原則

| 原則 | 說明 | 為什麼重要 |
|---|---|---|
| **Layered scoring** | 信號 → 維度 → 個體 → 個體×維度 → 整體。每一層都可以單獨讀、單獨 debug。 | 「台積電的 macro 風險是 −30」比「台積電分數 −30」更有用。 |
| **Explainable by construction** | 任何 score 都必帶 `evidence: list[EvidenceItem]`，EvidenceItem 必含 source / weight / timestamp / decay_applied。 | 沒有 evidence 的分數 = 沒有審計能力。 |
| **Pure functions** | 所有 scorer 都是 `score(context) -> ScoreResult`，沒有 side effect、沒有 I/O、不寫 DB。 | 可單元測試、可重放、可平行。 |
| **Config-driven** | weights / thresholds / decay functions 全部在 YAML 載入，不在 Python 寫死。 | 鼎鼎可隨時調權重，不改 code。 |
| **Composable** | scorer 之間可以串接、可以選擇性啟用、可以替換。 | 之後想換「動能定義」不需要改 framework。 |
| **Immutable artifacts** | 每個 ScoreResult 是 frozen dataclass，附 hash。 | 可做「上次分數 vs 這次分數」diff。 |
| **Backwards compatible** | Phase 3 模組**不 import** 現有 `macro_daily.py` / `company_monthly.py` / `industry_weekly.py`。現有路徑完全不動。 | 零風險加入。 |
| **No LLM in scoring path** | 分數必是 deterministic 算的；LLM 只在「解釋敘述」階段介入。 | 投資分數不能隨機。 |

---

## 3. 五大元件全景

```
┌───────────────────────────────────────────────────────────────────────┐
│                  Investment Intelligence Engine (Phase 3)              │
│                                                                        │
│  ┌──────────────────────┐                                              │
│  │  Signal Engine       │  ← 統一信號入口                              │
│  │  (signals/)          │                                              │
│  │  • ingestion         │  從 Phase 2B fetcher 收 RawSignal             │
│  │  • normalization     │  統一 schema + unit + timestamp               │
│  │  • weighting         │  per-source 信譽權重                          │
│  │  • decay             │  time-based 衰減                              │
│  │  • confidence        │  信心值 0~1                                   │
│  │  • aggregator        │  multi-source 收斂                           │
│  └──────────┬───────────┘                                              │
│             │ normalized signals                                       │
│             ▼                                                          │
│  ┌──────────────────────┐   ┌──────────────────────┐                  │
│  │  Macro Scorer        │   │  Industry Scorer     │  ← 三個維度       │
│  │  (scores/macro.py)   │   │  (scores/industry.py)│    並行            │
│  │  indicators          │   │  rotation / RS        │                  │
│  │  policy / inflation  │   │  cyclicality / flow   │                  │
│  │  rates / liquidity   │   │  industry news        │                  │
│  │  geopolitics         │   │                       │                  │
│  │  → -100~100          │   │  → -100~100           │                  │
│  └──────────┬───────────┘   └──────────┬───────────┘                  │
│             │                          │                              │
│             │     ┌──────────────────────┐                            │
│             │     │  Company Scorer      │ ← 個體                     │
│             │     │  (scores/company.py) │                            │
│             │     │  financial quality   │                            │
│             │     │  growth / profit.    │                            │
│             │     │  valuation / momentum │                            │
│             │     │  risk / news sentiment│                            │
│             │     │  → -100~100          │                            │
│             │     └──────────┬───────────┘                            │
│             │                │                                        │
│             ▼                ▼                                        │
│  ┌──────────────────────────────────────────┐                        │
│  │  Research Graph                           │ ← 證據圖              │
│  │  (graph/)                                 │                        │
│  │  Company ◀▶ Industry ◀▶ Macro            │                        │
│  │       │           │          │             │                        │
│  │       └──▶ News ◀─┴──▶ Signal ◀▶ Report    │                        │
│  │                    │                       │                        │
│  │                    ▼                       │                        │
│  │               Person                       │                        │
│  │                                          │                        │
│  │  BFS/DFS traversal + evidence tracing    │                        │
│  └──────────────────────────────────────────┘                        │
│                                                                        │
└───────────────────────────────────────────────────────────────────────┘
         ▲                                                              │
         │ pure data, no I/O                                            │
         │                                                              │
┌────────┴────────────────────────────────────────────────────────────┐
│  Phase 2B Framework (unchanged)                                       │
│  • BaseReport.fetch()  → RawSignal stream                              │
│  • BaseReport.analyze() → calls Phase 3 scorers, returns ScoreResult  │
│  • BaseReport.render() → reads ScoreResult, formats Telegram report   │
│  • LocalStore           → persists ScoreResult per task               │
│  • Dispatcher           → orchestrates which scorers run              │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 4. 與現有系統的關係

| 現有元件 | Phase 3 怎麼用 | 是否會被取代 |
|---|---|---|
| `macro_daily.py` 算 `liquidity_score` | 抽出來變成 `MacroScorer.liquidity_dim` 的一個維度函式；原檔仍可呼叫舊函式當 fallback | 否（雙軌） |
| `company_monthly.py` 的 `assess_stock` | 變成 `CompanyScorer` 的薄 adapter；新分數附帶 evidence | 否（雙軌） |
| `industry_weekly.py` 的「被提到幾次」 | 變成 `IndustryScorer.news_sentiment_dim` 的 raw input | 否（雙軌） |
| `db.py` SQLite | 新增 `score_snapshot` / `signal_log` / `graph_edge` 三張表 | 否（既有 3 張表不動） |
| `~/.hermes/cron/jobs.json` | 完全不動。Phase 3 透過 framework 接入 | 否 |
| `run.sh` 派送鏈 | 完全不動。新增 `phase3_score.py` 平行執行 | 否 |
| `institutional.py` 的 T86 | 變成 `Signal Engine` 的 source adapter 之一 | 否（共存） |
| `industry_config.json` | Phase 3 載入後合併到 `config/weights/*.yaml` | 否 |
| `taiwan50_config.json` | 同上 | 否 |

**核心承諾：Phase 3 結束時，`bash run.sh` 仍跑出 100% 相同的 Telegram 報告。所有 intelligence 層都是「可選增強」，可開可關。**

---

## 5. 設計分層（Layer Stack）

```
L7  Publisher (Telegram / Discord / API)  ← Phase 2B
L6  Renderer (format_score_to_telegram)   ← Phase 3 模板
L5  Scorers (Macro / Industry / Company)   ← Phase 3 核心
L4  Aggregator (weighted_sum, cross-layer) ← Phase 3
L3  Signal Engine (normalize / weight / decay / confidence)
L2  Source Adapters (yfinance / RSS / T86 / SEC)  ← Phase 2B fetcher
L1  RawData (SQLite / RSS cache / in-memory)
L0  External (Yahoo / DIGITIMES / TWSE / 鉅亨網)
```

每一層都對上一層暴露純介面（`Source → Signal → Score → Report`），任何一層可替換、可 mock。

---

## 6. 不在 Phase 3 範圍內（Out of Scope）

明確標出來，避免擴張：

- ❌ 即時交易 / 下單 / portfolio management
- ❌ ML 預測模型（Phase 4 之後再說）
- ❌ 即時報價 / 串流 API
- ❌ 多市場（目前只覆蓋台股 + 美股 + 國際 macro；不涵蓋 A 股 / 日股等）
- ❌ 法人持倉明細（已用 T86 近月累積，個股完整 13F 申報在 Phase 4+）
- ❌ LLM 主動發問（Phase 3 的 LLM 只做「把 score 翻成中文一句話」）
- ❌ 任何 cron / prompt / 派送鏈修改

---

## 7. 風險摘要（詳細見 08_risks_assumptions.md）

| 風險 | 影響 | 緩解 |
|---|---|---|
| Scorer 數學 bug 導致錯誤分數流到 Telegram | 高 | 7-day shadow run；dry-run 預設；evidence audit log |
| Evidence 爆炸（每個分數帶 100 個 evidence） | 中 | evidence 預設 cap 10；truncation 規則 |
| Config 被亂改導致分數跳動 | 中 | config 改動前必須做 30 天 backtest；hash 鎖版本 |
| 與 Phase 2B framework 不同步演化 | 中 | Phase 3 不 import framework 任何內部 class，只用 `RawSignal` 協議 |
| LLM 解釋階段 hallucination | 低 | 解釋只生成「這個分數的 evidence 摘要」，不可引入新 fact |
| SQLite 三張新表 + 現有三張表 → 維護成本 | 低 | 新表獨立檔案 `intelligence.db` |

---

## 8. 閱讀順序

| 文件 | 內容 |
|---|---|
| **01_architecture_overview.md**（本文件） | 五大元件、設計原則、與現有系統關係 |
| **02_signal_engine.md** | 信號入口、normalize、decay、confidence、aggregation |
| **03_macro_scoring.md** | 6 個維度的指標、公式、threshold、weight 設定 |
| **04_company_scoring.md** | 7 個維度（financial/growth/profit/valuation/momentum/risk/news）、explainable 加權 |
| **05_industry_scoring.md** | 5 個維度（rotation/RS/cyclicality/macro_sensitivity/news/capital_flow） |
| **06_research_graph.md** | 節點類型、edge 類型、BFS/DFS、evidence chain |
| **07_data_model.md** | 8 個 dataclass、SQLite schema、JSON manifest 結構 |
| **08_risks_assumptions.md** | 6 大風險、緩解、out-of-scope 理由 |
| **09_api_spec.md** | Python API + JSON-RPC + CLI 介面 |
| **10_repo_structure.md** | 完整目錄樹、檔案清單、import 邊界 |
| **11_milestone_roadmap.md** | Phase 3A/B/C/D 時程、驗收標準、go/no-go gate |

---

_本文件為 Phase 3 系列之首。閱讀其他文件前請先讀本文件以建立心智模型。_
