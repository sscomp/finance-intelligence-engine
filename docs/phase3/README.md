# Phase 3 — Investment Intelligence Engine

**狀態**：DESIGN ONLY（2026-07-08，by M2）
**目的**：建一個可解釋、可追溯、可配置的 investment intelligence 層，**建在 Phase 2B framework 之上**。
**承諾**：Phase 3 結束時，現有 4 支 report 仍 100% byte-identical 運作。Phase 3 是「可選增強層」，永遠可一鍵關閉。

---

## 0. 一句話

把「抓到的數字 / 抓到的文字」轉成「-100~100 的分數 + 解釋 + 證據鏈」。

---

## 1. 文件清單（閱讀順序）

| # | 文件 | 內容 | 必讀？ |
|---|---|---|---|
| 01 | [Architecture Overview](./01_architecture_overview.md) | 五大元件、設計原則、與現有系統關係 | 🔴 必讀 |
| 02 | [Signal Engine](./02_signal_engine.md) | 信號入口、normalize、decay、confidence、aggregation | 🔴 必讀 |
| 03 | [Macro Scoring](./03_macro_scoring.md) | 6 維度：economic / monetary / inflation / rates / liquidity / geopolitics | 🔴 必讀 |
| 04 | [Company Scoring](./04_company_scoring.md) | 7 維度 + cross-layer 調整 | 🔴 必讀 |
| 05 | [Industry Scoring](./05_industry_scoring.md) | 6 維度 + 資金流聚合 | 🔴 必讀 |
| 06 | [Research Graph](./06_research_graph.md) | Node / Edge 設計、traversal、evidence tracing | 🟡 重要 |
| 07 | [Data Model](./07_data_model.md) | frozen dataclass、SQLite schema、JSON manifest | 🟡 重要 |
| 08 | [Risks & Assumptions](./08_risks_assumptions.md) | 10 個風險、7 個假設、kill switch | 🔴 必讀 |
| 09 | [API Specification](./09_api_spec.md) | Python / CLI / JSON-RPC | 🟡 重要 |
| 10 | [Repository Structure](./10_repo_structure.md) | 目錄樹、import 邊界、命名約定 | 🟢 參考 |
| 11 | [Milestone Roadmap](./11_milestone_roadmap.md) | 13 週時程、gate、release artifacts | 🟡 重要 |

---

## 2. 三大承諾

1. **零破壞**：現有 `macro_daily.py` / `company_monthly.py` / `industry_weekly.py` / `institutional.py` / `db.py` / `run.sh*` / cron jobs / `~/.hermes/cron/jobs.json` **完全不動**。Phase 3 在獨立的 `phase3/` 子樹下。

2. **可關閉**：`config/phase3/enabled.yaml` 改一行就能停掉整個 Phase 3 層。既有 4 支 report 照跑。

3. **可解釋**：任何 score 必附 `evidence: list[EvidenceItem]`，可從分數走 graph 追到 source。

---

## 3. 五大元件（一句話總結）

| 元件 | 一句話 |
|---|---|
| **Signal Engine** | 把來源異質資料收斂成統一 schema + decay + confidence |
| **Macro Scorer** | 6 維度加權 → -100~100，附 evidence |
| **Industry Scorer** | 6 維度加權 → -100~100，可讀 macro context |
| **Company Scorer** | 7 維度加權 + macro/industry 調整 → -100~100 |
| **Research Graph** | 節點 + 邊，支援 evidence chain 追溯 |

---

## 4. 與 Phase 2B framework 的關係

```
Phase 2B framework (BaseReport / Pipeline / Registry / Dispatcher / LocalStore)
        ↓ 提供 RawSignal stream
Phase 3 Signal Engine
        ↓ NormalizedSignal / WeightedSignal
Phase 3 Macro / Industry / Company Scorer
        ↓ ScoreResult
Phase 3 Research Graph (population)
        ↓
intelligence.db
        ↓ 查詢
GPT Orchestrator / Telegram / Dashboard
```

**關鍵邊界**：Phase 3 **不 import** Phase 2B framework 內部 class。透過 `RawSignal` 抽象介接。

---

## 5. 13 週時程

- **Phase 3A (3 週)**：核心建構 + 7-day shadow run
- **Phase 3B (4 週)**：雙軌期 + BaseReport 整合 + JSON-RPC server
- **Phase 3C (4 週)**：GPT 整合 + idempotency + LLM opt-in
- **Phase 3D (2 週)**：Dashboard + 文件

詳細見 [11_milestone_roadmap.md](./11_milestone_roadmap.md)。

---

## 6. 不要期待的事

- ❌ 即時交易 / portfolio
- ❌ ML 預測（Phase 4）
- ❌ 即時報價 / 串流 API
- ❌ 多市場
- ❌ 修改任何現有檔案 / cron / prompt

---

## 7. 審查流程

鼎鼎 review 本文件時，建議順序：
1. 01 架構總覽（15 分鐘）
2. 08 風險與假設（10 分鐘）
3. 03/04/05 三個 scorer（各 10 分鐘）
4. 09 API 規格（10 分鐘）
5. 11 Roadmap（5 分鐘）

確認 → 進入 Phase 3A 啟動會議。

---

_本文件群為 DESIGN ONLY。Phase 3A 啟動前必經鼎鼎逐項 confirm。_
