# Hermes M2 強化計畫（Phase 1）

## 建立可追蹤的 Research / Task Pipeline

**版本**：v1.0\
**日期**：2026-07-07

# 一、背景

目前 Hermes Orchestrator GPT 已可作為任務規劃者（Planner /
Orchestrator），並將工作派送至 Hermes M2。

但目前存在一個主要問題：

> ChatGPT 顯示任務已建立（Run ID），但 Hermes M2 無法追蹤該任務。

代表目前：

-   OpenAI Internal Run
-   Hermes Runtime
-   Hermes M2

三者仍屬於彼此獨立的生命週期。

因此需要建立 Hermes 自己的 Task 管理層。

# 二、本次目標

建立一套 Hermes 自己管理的任務生命週期。

不要依賴 OpenAI Run ID。

應由 Hermes 建立自己的 Task，例如：

    TASK-20260707-0001
    TASK-20260707-0002
    TASK-20260707-0003

所有查詢都以 Hermes Task ID 為主。

# 三、Phase 1 工作內容

## 1. Task Dispatcher

建立 Dispatcher，負責建立、指派、更新、查詢與完成 Task。

建議欄位：

-   Task ID
-   Title
-   Type
-   Priority
-   Owner
-   Created Time
-   Started Time
-   Finished Time
-   Status
-   Progress
-   Result Path
-   Error

Status： - Pending - Queued - Running - Waiting - Completed - Failed -
Cancelled

## 2. Task Queue

建立 Queue，未來可支援 M2、M3、M4 與 Cloud Worker 共用。

## 3. Progress 回報

提供 5%、10%、25%、40%、60%、80%、95%、100%
等進度，並回報目前執行步驟，例如：

-   Reading Scheduler
-   Scanning Repository
-   Loading Config
-   Searching Prompt
-   Generating Report

## 4. Result Metadata

每個 Task 完成後產生 task.json，包含：

-   Task ID
-   開始時間
-   完成時間
-   耗時
-   Git Commit
-   Branch
-   Prompt Version
-   Model
-   Input
-   Output
-   Report Path

## 5. 統一 Report 結構

-   Executive Summary
-   Current Architecture
-   Current Workflow
-   Findings
-   Technical Debt
-   Optimization
-   Priority
-   Roadmap
-   Appendix

## 6. Research Framework

建立共用模組：

    Research/
        scheduler.py
        repo.py
        prompts.py
        report.py

## 7. Prompt Version

集中管理 prompts：

    prompts/
    macro_v1.md
    company_v2.md
    industry_v1.md
    review_v3.md

## 8. Configuration

集中管理：

    config/
    scheduler.yaml
    report.yaml
    research.yaml
    model.yaml

## 9. Logging

每個 Task 建立獨立 log：

    logs/TASK-xxxx.log

## 10. Error Handling

採 Retry 與 Warning 機制，避免單點失敗導致整體終止。

## 11. Observability

提供：

-   hermes task list
-   hermes task show
    ```{=html}
    <task>
    ```
-   hermes task logs
    ```{=html}
    <task>
    ```
-   hermes task rerun
    ```{=html}
    <task>
    ```

## 12. Repository 掃描能力

Research Agent 應可掃描：

-   Scheduler
-   Git Repository
-   Prompt
-   Config
-   Python Package
-   Dependency Graph

並輸出 Architecture Report。

# 四、本階段不修改既有功能

本階段僅完成設計與規劃，不修改既有程式，不更換模型與框架。

# 五、交付成果

1.  現況架構分析
2.  Task Dispatcher 設計
3.  Task Metadata
4.  Queue 設計
5.  Progress 設計
6.  Logging 設計
7.  Report Framework
8.  Prompt Version 管理
9.  Configuration 管理
10. Phase 2 / Phase 3 建議
