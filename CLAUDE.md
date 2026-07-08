# CLAUDE.md — 一人公司 Discord Bot（常駐守則）

## 專案一句話
私人 Discord 伺服器上的「一人公司」：單一 bot 扮演總經理（Manager Agent），
可委派「工程師」與「研究員」兩個 subagent。
完整規格與驗收條件在 **SPEC.md**——實作或修改任何功能前，先用 Read 讀對應章節，不要憑記憶。

## 最高原則（勝兵先勝而後求戰）
- 【知彼知己】動工前先盤點 repo 現況與官方文件，回報作戰計畫與風險，等董事長批准。
- 【兵貴勝不貴久】以最少代碼交付可運行版本；禁止引入 SPEC 未列出的框架與套件。
- 【不戰而屈人】先寫測試與錯誤處理；危險操作靠 hooks 事前攔截，不靠事後補救。
- 【多算勝】交付前列出 ≥3 個 edge cases 與防禦，並實際跑過 SPEC §6 的驗收指令。

## 技術約束（不得替換）
- discord.py ≥ 2.x／claude-agent-sdk（Python ≥ 3.10）／SQLite／apscheduler。
- 模型：Manager = claude-fable-5；工程師 = claude-sonnet-4-6；研究員 = claude-haiku-4-5-20251001。
- 認證只用 ANTHROPIC_API_KEY 計量付費；嚴禁使用 claude.ai 訂閱的 OAuth 權杖。
- subagent 工具白名單依 SPEC §5.2；subagent 工具清單不得含 Agent（防遞迴委派）。

## 目錄與慣例
- src/：bot.py（Discord 入口）、runtime.py（SDK 封裝）、agents.py、guardrails.py、budget.py、store.py。
- workspace/：工程師 subagent 唯一可寫目錄；其中的 CLAUDE.md 是「runtime Manager 的記憶」，不是給你的守則。
- memory/notes.md：每次任務結束後追加踩坑筆記。
- 測試：`pytest`；啟動：`python -m src.bot`；進度勾選清單維護在 docs/progress.md。

## 工作流程
1. 讀 SPEC.md 對應里程碑 → 產出作戰計畫（影響檔案／步驟／3 個 edge cases）→ 等「批准」。
2. 實作 → `pytest` 全綠 + 通過 SPEC §6 該里程碑驗收 → 回報結果。
3. 把本次踩坑心得追加到 memory/notes.md，並勾掉 docs/progress.md 的完成項。

## 紅線（違反即停工回報）
- 絕不 commit .env、token、API key；workspace/ 與 session 逐字稿必須在 .gitignore。
- 絕不為了讓測試通過而放寬 guardrails.py 黑名單或 budget.py 熔斷參數。
- Discord 單則訊息 >2000 字必分段；一切人類確認走 Discord 按鈕，不使用 TTY 互動提問。
