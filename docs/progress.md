# 進度勾選清單

## M0 — 打通
- [x] 專案骨架（.gitignore / .env.example / pyproject.toml / 目錄結構）
- [x] src/config.py：env 載入與白名單解析（不依賴 python-dotenv）
- [x] src/agents.py：Manager 系統提示詞 + 工程師/研究員 subagent 定義
- [x] src/runtime.py：claude-agent-sdk 封裝 + >2000 字自動分段
- [x] src/bot.py：bot 上線、!ping 回 pong、任一訊息獲 Manager 回覆
- [x] tests/test_m0.py 全綠（18 passed，2026-07-09）
- [ ] 手動對話一輪成功（需要 DISCORD_BOT_TOKEN + ANTHROPIC_API_KEY，董事長驗收）

## M1 — 先勝流程
- [ ] session 續接（同頻道共享上下文；重啟後 resume 有效）
- [ ] /task 產生作戰計畫 Embed + 批准/否決按鈕
- [ ] 批准閘門（未批准前 Write/Edit/Bash 一律 deny）
- [ ] src/store.py：SQLite sessions/tasks/audit
- [ ] src/guardrails.py：PreToolUse 黑名單 + PostToolUse 審計
- [ ] tests/test_guardrails.py 全綠

## M2 — 委派與熔斷
- [ ] 工程師/研究員 subagent 實際被呼叫（審計可見）
- [ ] src/budget.py：單任務成本 / 每日總額熔斷
- [ ] TASK_BUDGET_USD=0.01 驗收熔斷
- [ ] 工程師產出檔案僅出現在 workspace/

## M3 — 記憶與排程
- [ ] workspace/CLAUDE.md 作為 Memory Bank（任務前先讀）
- [ ] 任務結束自動追加 memory/notes.md
- [ ] apscheduler 每日 09:00 摘要至 REPORT_CHANNEL_ID
- [ ] 第二個任務的計畫引用第一個任務的筆記
