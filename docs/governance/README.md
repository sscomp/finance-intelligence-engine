# 治理文件

本目錄包含 Finance Intelligence Engine (FIE) 的治理歷史、里程碑紀錄與決策軌跡。

## 如何閱讀治理歷史

FIE 的演進分為以下階段：

1. **Phase 3A**：基礎架構搭建（信號引擎、評分模組、持久層）
2. **Phase 3B**：信號配接器、研究圖譜、端到端整合
3. **Phase 4**：圖譜持久化、查詢優化、分數解釋 CLI、Shadow-Run 決策重放
4. **Phase 5**：投資組合領域模型、風險引擎、配置引擎、執行規劃、報告、工程完成驗證、營運驗收
5. **FIE 資料治理**：資料供應鏈、新鮮度守護、歷史資料回填

每個里程碑文件遵循以下結構：

- **Problem / Context**：面臨的問題與背景
- **Decision**：做出的決策
- **Consequences / Trade-offs**：決策的後果與取捨
- **Validation / Evidence**：驗證方式與證據
- **Related Code Areas**：相關的程式碼區域

## 文件清單

| 文件 | 內容 |
|------|------|
| [master-status.md](master-status.md) | 當前系統與里程碑狀態總覽 |
| [decision-log.md](decision-log.md) | 依時間順序的關鍵決策軌跡 |
| [milestones/m6.md](milestones/m6.md) | M6 — 投資組合報告 |
| [milestones/m7.md](milestones/m7.md) | M7 — 工程完成驗證 |
| [milestones/m8.md](milestones/m8.md) | M8 — 營運驗收 |

## ADR

架構決策紀錄（Architecture Decision Records）位於 [../adr/](../adr/) 目錄。