# 資料供應鏈

## 概述

FIE 的資料供應鏈分為三層：原始資料擷取 → Bridge 映射 → 評分層讀取。

## 第一層：原始資料擷取

三個腳本透過外部 API 擷取資料，寫入 `macro_history.db`：

### macro_daily.py

- **來源**：yfinance API（US 10Y / 2Y / 13W bond yields, DXY, VIX, USD/TWD）
- **目標表**：`macro_daily`（PK: `date`）
- **欄位**：us10y, us2y, us13w, dxy, vix, usdtwd, yield_spread, score, verdict, signals_json, created_at
- **更新週期**：每日（交易日）

### company_monthly.py

- **來源**：yfinance API（`.info` property，台股 `.TW` ticker）
- **目標表**：`stock_monthly`（PK: `date, code`）
- **欄位**：name, sector, price, eps_ttm, pe_trailing, pe_forward, roe, roa, gross_margin, operating_margin, profit_margin, dividend_rate, dividend_yield, payout_ratio, pb_ratio, revenue_growth, earnings_growth, nim_growth, interest_spread, high_52, low_52, dist_from_high, target_mean, peg_ratio, market_cap
- **更新週期**：每月（12 日）
- **限制**：yfinance `.info` 僅提供當前快照，無法取得歷史 fundamentals

### institutional.py

- **來源**：TWSE T86 API（法人買賣超）
- **目標表**：`institutional_daily`（PK: `date, code`）
- **欄位**：foreign_net, prop_net, total_net, trading_days
- **更新週期**：依 T86 fetch cycle

### 寫入模式

所有表使用 `INSERT OR REPLACE`（冪等 upsert by PK）。`db.py` 不包含任何 `DELETE` 或修剪語句。

## 第二層：Real-Score Bridge

`phase3/bridge/seed_signals.py` 將 `macro_history.db` 的資料映射為 Phase 3 Signal 記錄：

| 來源表 | 映射目標 | Signal entity_type | 欄位映射 |
|--------|----------|-------------------|----------|
| macro_daily | signal_log | macro / global | us10y, us2y, us13w, dxy, vix, yield_spread |
| stock_monthly | signal_log | company / `<code>` | pe_trailing, pb_ratio, peg_ratio, roe, earnings_growth, revenue_growth, dividend_yield, market_cap, gross_margin |
| institutional_daily | signal_log | company / `<code>` | foreign_net, prop_net（source_type: t86） |
| institutional_daily (聚合) | signal_log | industry / `<industry_id>` | capital_flow（依 `industry_config.json` 聚合） |

### As-of 日期解析

`resolve_as_of_dates(source_db, requested_date)` 對每張表找出 `<= requested_date` 的最新可用日期，確保跨表時間對齊。所有信號以 `requested_date` 作為 `date_bucket`，原始來源日期保留在 `metadata.source_date`。

### Industry 聚合

`industry_config.json` 將 50 檔成分股映射到 8 個產業（AI、半導體、伺服器、PCB、網通、重電、金融、高股息）。Bridge 將每個產業的成分股法人買賣超加總，產生產業層級的 `capital_flow` 信號。其他產業維度（rotation、relative_strength 等）預設為中性——這是正確行為，非資料缺失。

## 第三層：評分層讀取

`SignalLoader`（`phase3/pipeline/signal_loader.py`）從 `signal_log` 讀取信號，經 `InputBuilder` 轉為 Scorer 的輸入格式，再由 `ScoringPipeline` 執行三層評分。

### Pipeline Artifact

`pipeline-export` / `pipeline-run` CLI 產出 JSON artifact，包含：

- 每個 scorer 的分數、維度分解、證據鏈
- `evidence_handles`：每個決策的證據摘要（含 inputs payload，供 real_replay 使用）
- `config_hash`：scorer 配置的雜湊值（用於 drift 偵測）

## 已知限制

1. **stock_monthly 歷史回填**：yfinance `.info` 為當前快照 API，無法取得歷史 fundamentals。已標記為 BLOCKED。
2. **Industry 維度覆蓋**：僅 `capital_flow` 有本地資料，其他 5 個維度預設為中性。
3. **單機 SQLite**：適合單一排程器環境，不適合多寫入者並發。

## 相關文件

- [FIE 架構總覽](finance-intelligence-engine.md)
- [歷史資料治理](historical-data-governance.md)
- [ADR-001: Real-Score Bridge](../adr/adr-001-real-score-bridge.md)