# 📋 SPEC — Discord「一人公司」MVP 建置規格書

版本 v0.1（2026-07）・交付對象：Claude Code（模型：Claude Fable 5）

**用法**：把本檔放進一個空的 Git repo 根目錄 → 啟動 Claude Code → 下指令：
> 閱讀 SPEC.md。先執行【知彼知己】：查閱第 2 節列出的官方文件，確認 claude-agent-sdk 目前的 subagents / hooks / sessions API 寫法，然後回報「M0 作戰計畫」（影響檔案、實作步驟、3 個 edge cases 與防禦），**不要寫任何程式碼**。待我回覆「批准」後才進入實作。

---

## 0. 最高原則（Rules）— 勝兵先勝而後求戰

1. **知彼知己**：任何里程碑動工前，先盤點 repo 現況與官方文件，回報現況評估與風險；嚴禁盲目 `/implement`。
2. **兵貴勝，不貴久**：每個里程碑以最少代碼交付「可運行」版本。禁止提前抽象、禁止引入本規格未列出的框架（不用 CrewAI / AG2 / LangChain）。
3. **不戰而屈人**：核心邏輯先寫測試與錯誤處理；所有危險操作必須在「執行前」被 hooks 攔截，而不是事後補救。
4. **多算勝**：每個里程碑交付前，列出至少 3 個 edge cases（斷線、API 失敗、惡意輸入）與對應防禦，並實際跑過驗收指令。

違反任何一條即視為該里程碑未完成。

---

## 1. 專案目標與範圍

**一句話**：在一個「私人 Discord 伺服器」裡，用單一機器人扮演「總經理（Manager）」；總經理能把任務委派給「工程師」與「研究員」兩個 subagent，全程受計畫批准機制、工具白名單與成本熔斷器保護。

**互動模式**：玩法 A（單一窗口）——董事長（使用者）只對總經理說話，委派過程以進度訊息回報。

**MVP 明確不做（Out of scope）**：
多個 Discord bot、Gmail / Notion / Spotify 串接、語音功能、公開伺服器、自動雲端部署、RAG 知識庫、CrewAI / AG2 整合比較。這些留給 M4 以後。

---

## 2. 技術選型（不得擅自替換）

| 層 | 選型 | 理由 |
|---|---|---|
| 介面層 | `discord.py` ≥ 2.x | 原生 Buttons / Views 可做人類批准（HITL）；async 與 Agent SDK 天然相容 |
| Agent 執行時 | `claude-agent-sdk`（Python ≥ 3.10） | 即 Claude Code 引擎的程式庫版：內建 agent loop、subagents、sessions、hooks、成本追蹤，免自寫調度框架 |
| Manager 模型 | `claude-fable-5`（fallback：`claude-opus-4-8`） | 最強推理，負責規劃與委派 |
| 工程師模型 | `claude-sonnet-4-6` | 寫扣性價比 |
| 研究員模型 | `claude-haiku-4-5-20251001` | 查資料便宜快速 |
| 認證 | `ANTHROPIC_API_KEY`（計量付費） | **禁止**把 claude.ai 訂閱的 OAuth 權杖餵給第三方程式 |
| 儲存 | SQLite | channel_id ↔ session_id 對應、任務紀錄、成本累計 |
| 沙盒 | 工程師 `cwd` 鎖定 `./workspace/`；若本機有 Docker，Bash 於容器內執行 | 檔案破壞半徑最小化 |

**實作前必讀的官方文件（知彼知己步驟的一部分）**：
- Agent SDK 總覽：https://code.claude.com/docs/en/agent-sdk/overview
- Subagents：https://platform.claude.com/docs/en/agent-sdk/subagents
- discord.py 文件（Intents、Views/Buttons、訊息 2000 字元上限）
- 以官方文件當下版本為準；若與本 SPEC 的 API 描述衝突，回報差異後以官方文件為準。

---

## 3. 系統架構

```
董事長（你）
   │  Discord 訊息 / 斜線指令 / 批准按鈕
   ▼
bot.py（discord.py，async）
   │  channel_id → SQLite 查 session_id → resume（無則新建）
   ▼
runtime.py（claude-agent-sdk 封裝）
   │
   ▼
Manager Agent session（claude-fable-5）
   │  以 Agent 工具委派（allowed_tools 需含 Agent）
   ├──► 工程師 subagent（sonnet；Read/Write/Edit/Bash/Glob/Grep；cwd=workspace/）
   └──► 研究員 subagent（haiku；僅 WebSearch/WebFetch）

橫切面（guardrails.py + budget.py）：
PreToolUse 黑名單攔截 ・ 批准閘門 ・ PostToolUse 審計日誌 ・ max_turns / 成本熔斷
```

每個 Discord 頻道（或討論串）對應一條 Agent SDK session；這就是「頻道 = 部門 = 上下文隔離」的落地。

---

## 4. 目錄結構

```
repo/
├─ SPEC.md
├─ .env.example
├─ pyproject.toml
├─ src/
│  ├─ bot.py           # Discord 入口：事件、指令、批准按鈕 View
│  ├─ runtime.py       # Agent SDK 封裝：session 管理、串流轉發、分段輸出
│  ├─ agents.py        # Manager 系統提示詞 + 兩個 subagent 定義
│  ├─ guardrails.py    # hooks：PreToolUse 黑名單、批准閘門、審計日誌
│  ├─ budget.py        # 熔斷器：單任務成本 / 回合上限、每日總額
│  └─ store.py         # SQLite：sessions / tasks / costs / audit
├─ workspace/          # 工程師唯一可寫目錄（內含 CLAUDE.md 作為 Memory Bank）
│  └─ CLAUDE.md
├─ memory/
│  └─ notes.md         # 任務結束後追加「踩坑筆記」
└─ tests/
```

---

## 5. 行為規格

### 5.1 訊息流程（玩法 A：單一窗口）
- 一般訊息 → Manager 直接回覆。回覆超過 2000 字元 → 自動分段（於段落邊界切分）或轉為 .md 檔案附件。
- `/task <描述>` → Manager 產出「作戰計畫」Embed：目標、步驟、預計委派的 subagent 與工具、至少 3 條風險與防禦 → 附【✅ 批准】/【❌ 否決】按鈕。
- **先勝閘門**：任務未被批准前，Write / Edit / Bash 類工具一律被 PreToolUse hook 拒絕並回報頻道。批准狀態記錄於 SQLite（任務層級，不得跨任務沿用）。
- 執行中：subagent 的關鍵進度（工具呼叫摘要）以訊息回報頻道，避免逐 token 洗版。
- 結束：回報結果摘要 + 本次成本（讀取 ResultMessage 的成本欄位）+ 當日累計花費。

### 5.2 Subagent 規格
- **工程師**：description 寫明「涉及建立／修改檔案、寫程式、執行指令的任務交給我」；tools 僅 Read / Write / Edit / Bash / Glob / Grep；cwd 鎖定 `workspace/`；model = `CODER_MODEL`。
- **研究員**：description「查資料、比較方案、彙整網路資訊」；tools 僅 WebSearch / WebFetch；model = `RESEARCHER_MODEL`。
- Manager 的 allowed_tools 必須包含 `Agent`，否則無法委派（官方 subagents 文件明載）。
- **subagent 的工具清單不得含 `Agent`**——禁止 subagent 再生 subagent，防遞迴。
- 委派時 Manager 必須在 prompt 內附完整上下文（檔案路徑、錯誤訊息、先前決策）——subagent 看不到 Manager 的對話歷史。

### 5.3 防護 SPEC（不戰而屈人）
- PreToolUse(Bash) 正則黑名單（至少）：`rm -rf`、`sudo`、`curl … | sh`、`chmod 777`、寫入 `workspace/` 之外的絕對路徑、`git push --force`。命中 → deny + 頻道通知 + 審計紀錄。
- PostToolUse：所有工具呼叫寫入審計表（時間、agent、工具名、參數摘要 ≤200 字）。
- 存取控制：bot 僅回應 `ALLOWED_GUILD_IDS` 與 `ALLOWED_USER_IDS` 白名單，其餘訊息靜默忽略。
- 不依賴 SDK 內建的互動式人類提問（需要 TTY，無頭環境會靜默失敗）；一切人類確認走 Discord 按鈕。

### 5.4 熔斷 SPEC（Loop Engineering）
- `MAX_TURNS` 回合上限（預設 30）。
- 單任務成本上限 `TASK_BUDGET_USD`（預設 2.0）：逐次累計，超過即中止任務並回報已花費金額與進度。
- 每日總額 `DAILY_BUDGET_USD`（預設 10.0）：超過後 bot 進入唯讀模式（可聊天、拒絕 /task），隔日 00:00 重置。
- 任一熔斷觸發 → 頻道張貼紅色警報 Embed。

---

## 6. 里程碑與可驗證目標（Verifiable Goals）

完成的定義 = 測試綠燈 + 驗收指令通過 + 無未處理例外。**不是「AI 覺得好了」。**

### M0 — 打通（估半天）
交付：bot 上線；`!ping` 3 秒內回 pong；任一訊息可獲得 Manager（Fable 5）回覆；>2000 字自動分段。
驗收：`pytest tests/test_m0.py` 全綠；手動對話一輪成功。

### M1 — 先勝流程（估 1–2 天）
交付：session 續接（同頻道兩則訊息共享上下文；bot 重啟後 resume 仍有效）；`/task` 產生計畫卡與批准按鈕；批准閘門生效。
驗收：`tests/test_guardrails.py` 模擬「未批准即要求寫檔」→ 必須 deny 且留下審計紀錄；模擬 `rm -rf` → deny。

### M2 — 委派與熔斷（估 1–2 天）
交付：任務執行可於審計紀錄觀察到工程師／研究員 subagent 被實際呼叫；成本熔斷生效。
驗收：`TASK_BUDGET_USD` 暫調 0.01 跑任務 → 必須熔斷並回報成本；工程師產出檔案只出現在 `workspace/` 內。

### M3 — 記憶與排程（估 1–2 天）
交付：`workspace/CLAUDE.md` 作為 Memory Bank（Manager 系統提示要求任務前先讀）；任務結束自動追加踩坑筆記至 `memory/notes.md`；apscheduler 每日 09:00 摘要貼到 `REPORT_CHANNEL_ID`。
驗收：連續兩個任務，第二個任務的作戰計畫中引用了第一個任務的筆記內容。

### M4+（本 SPEC 不實作，僅記錄方向）
多 bot「透明開會」玩法 B、Gmail / Notion 之 MCP 串接、與 OpenClaw / CoStaff 的對照評測。

---

## 7. 環境變數（.env.example）

```
DISCORD_BOT_TOKEN=
ANTHROPIC_API_KEY=
ALLOWED_GUILD_IDS=123456789
ALLOWED_USER_IDS=123456789
MANAGER_MODEL=claude-fable-5
CODER_MODEL=claude-sonnet-4-6
RESEARCHER_MODEL=claude-haiku-4-5-20251001
MAX_TURNS=30
TASK_BUDGET_USD=2.0
DAILY_BUDGET_USD=10.0
REPORT_CHANNEL_ID=
```

---

## 8. Manager 系統提示詞範本（置於 agents.py）

```
# 📜 你是「一人公司」的總經理（Manager Agent）

你融合《孫子兵法》的戰略智慧，替董事長調度一支 AI 團隊。嚴格遵守：

1.【知彼知己】收到任務先讀 workspace/CLAUDE.md 與 memory/notes.md，
   盤點現況與相依性後才規劃；絕不盲目動工。
2.【勝兵先勝而後求戰】任何會改動檔案或執行指令的任務，必須先產出
   「作戰計畫」（目標／步驟／委派對象／3 個 edge cases 與防禦），
   經董事長按鈕批准後才執行。未批准前，寫入類工具會被系統直接拒絕。
3.【兵貴勝，不貴久】以 MVP 與漸進式架構優先，禁止過度設計。
4.【多算勝】交付前自行推演至少 3 種極端狀況並確認已處理。
5.【將能而君不御】委派時給 subagent 完整上下文（檔案路徑、錯誤訊息、
   先前決策）——subagent 看不到你的對話歷史。
6. 回報格式：結論先行、精簡條列、附本次成本。
```

---

## 9. 已知風險與對策

| 風險 | 對策 |
|---|---|
| Discord 單則訊息 2000 字元上限 | 分段或轉檔案附件（M0 即實作） |
| SDK 內建人類提問需 TTY，無頭環境失效 | 全部人類確認改走 Discord 按鈕 |
| 多 agent 互呼造成 token 迴圈 | subagent 不含 Agent 工具；MAX_TURNS + 成本熔斷 |
| 提示詞注入（頻道內貼入惡意指示） | 私人伺服器 + 白名單；寫入類工具永遠需批准；Bash 黑名單 |
| API 斷線／限流 | 指數退避重試 ≤3 次，失敗回報而非崩潰 |
| session 逐字稿含敏感資料 | workspace/ 與逐字稿目錄加入 .gitignore |

---

## 10. 給 Claude Code 的開工指令（複製即用）

M0 啟動：
> 閱讀 SPEC.md。先執行【知彼知己】：以 WebFetch 查閱第 2 節官方文件，確認 claude-agent-sdk 目前 subagents / hooks / sessions 的實際 API 寫法，回報「M0 作戰計畫」（影響檔案、實作步驟、3 個 edge cases 與防禦），不要寫任何程式碼。等我批准。

批准後：
> 批准。實作 M0；完成後執行 pytest，回報結果與下一步建議。

---

（本 SPEC 本身即是「先勝」的產物：環境、規則與驗收條件先於程式碼存在。）
