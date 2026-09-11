# 一人公司 Discord Bot

在私人 Discord 伺服器裡，用**單一機器人扮演總經理（Manager Agent）**，把任務委派給「工程師」與「研究員」兩個 subagent。
全程受**計畫批准機制、工具白名單、Bash 黑名單與成本熔斷器**保護——所有危險操作都在執行前被 hooks 攔截，而不是事後補救。

董事長（你）只對總經理說話；委派過程以進度訊息回報到頻道。

> 完整規格與驗收條件見 [SPEC.md](SPEC.md)；本 README 是實作完成後的使用說明。

---

## 目錄

- [核心概念](#核心概念)
- [系統架構](#系統架構)
- [功能總覽](#功能總覽)
- [安全防線](#安全防線)
- [成本熔斷](#成本熔斷)
- [記憶機制](#記憶機制)
- [快速開始](#快速開始)
- [使用方式](#使用方式)
- [環境變數](#環境變數)
- [目錄結構](#目錄結構)
- [測試](#測試)
- [專案狀態](#專案狀態)
- [已知限制](#已知限制)
- [文件索引](#文件索引)

---

## 核心概念

| 角色 | 模型（預設） | 能做什麼 |
|---|---|---|
| **總經理 Manager** | `claude-fable-5` | 讀檔、規劃、委派、驗收、回報。**不得**親自寫檔／執行指令／查網路，會被 hooks 攔截並提示改委派 |
| **工程師 engineer** | `claude-sonnet-4-6` | Read / Write / Edit / Bash / Glob / Grep；工作目錄鎖定 `workspace/` |
| **研究員 researcher** | `claude-haiku-4-5-20251001` | 僅 WebSearch / WebFetch |

三條設計原則：

- **頻道 = 部門 = 一條 session**：每個 Discord 頻道對應一條 Agent SDK session，持久化在 SQLite，bot 重啟後仍可續接。
- **勝兵先勝而後求戰**：任何會改動檔案或執行指令的任務，必須先產出「作戰計畫」，經董事長按鈕批准後才執行。
- **將能而君不御**：Manager 只調度不動手；subagent 的工具清單不含 `Agent`，禁止遞迴委派。

---

## 系統架構

```
董事長（你）
   │  Discord 訊息 / 斜線指令 / 批准按鈕
   ▼
src/bot.py（discord.py，async）
   │  channel_id → SQLite 查 session_id → resume（無則新建）
   ▼
src/runtime.py（claude-agent-sdk 封裝）
   │
   ▼
Manager Agent session（claude-fable-5）
   │  以 Agent 工具委派
   ├──► engineer  subagent（sonnet；Read/Write/Edit/Bash/Glob/Grep；cwd=workspace/）
   └──► researcher subagent（haiku；僅 WebSearch/WebFetch）

橫切面（src/guardrails.py + src/budget.py）：
PreToolUse 批准閘門・委派強制・Bash 黑名單・路徑閘門 ｜ PostToolUse 審計日誌 ｜ max_turns / 成本熔斷
```

技術選型（不得替換）：`discord.py ≥ 2.x`、`claude-agent-sdk`（Python ≥ 3.10）、SQLite、`apscheduler`。
認證只用 `ANTHROPIC_API_KEY` 計量付費，嚴禁使用 claude.ai 訂閱的 OAuth 權杖。

---

## 功能總覽

已交付 M0～M3 四個里程碑：

| 里程碑 | 內容 |
|---|---|
| **M0 打通** | bot 上線、`!ping`、任一訊息獲 Manager 回覆、>2000 字自動分段（優先在段落邊界切） |
| **M1 先勝流程** | session 續接（重啟仍有效）、`/task` 產生作戰計畫 Embed + 批准／否決按鈕、批准閘門、Bash 黑名單、審計日誌 |
| **M2 委派與熔斷** | 工程師／研究員 subagent 實際接上、單任務／每日成本熔斷、每日超額唯讀模式、關鍵進度轉發頻道、工程師產出僅限 `workspace/` |
| **M3 記憶與排程** | `workspace/CLAUDE.md` 作為 Memory Bank 注入計畫提示、任務結束自動追加踩坑筆記到 `memory/notes.md`、每日 09:00 摘要貼到報告頻道 |

---

## 安全防線

所有工具呼叫（含 subagent 內的呼叫）都流經同一組 hooks，依 `agent_type` 歸戶。PreToolUse 的檢查順序：

1. **批准閘門**：Write / Edit / MultiEdit / NotebookEdit / Bash 在任務批准前一律 deny。批准是**任務層級**，不跨任務沿用；閒聊模式永遠 deny。
2. **委派強制**：Manager 主線程親自使用寫入類或 Web 工具 → deny，並提示改用 Agent 委派給 engineer / researcher。
3. **Bash 黑名單**（批准過也不准碰）：`rm -rf` 及其變體（token 化偵測，涵蓋 `-r -f`、`--recursive --force`）、`sudo`、`curl|sh`、`chmod 777`、`git push --force`、寫入 `workspace/` 之外的絕對路徑。
4. **路徑閘門**：Write / Edit 目標必須落在 `workspace/` 內，含 `..` 穿越防禦。

其他：

- **存取控制**：只回應 `ALLOWED_GUILD_IDS` × `ALLOWED_USER_IDS` 白名單，其餘（含 DM）靜默忽略；白名單留空 = 全部拒絕。
- **審計**：PostToolUse 把每次工具呼叫寫入 SQLite `audit` 表（時間、agent、工具名、參數摘要 ≤200 字、allow/deny）。
- **無頭環境**：`permission_mode="dontAsk"`，不依賴 SDK 內建的 TTY 互動提問；一切人類確認走 Discord 按鈕。
- **環境隔離**：`setting_sources=[]`，runtime Manager 不會吸收開發機的 CLAUDE.md / settings。

---

## 成本熔斷

「事前算好、事後對帳」雙保險：

- **事前**：每輪執行前算出本輪上限 = `min(任務剩餘, 每日剩餘)`，餵給 SDK 的 `max_budget_usd`，由 agent loop 原生中止。
- **事後**：`ResultMessage` 的實際成本入帳 SQLite；閒聊、擬計畫、執行都計入每日預算。

| 參數 | 預設 | 行為 |
|---|---|---|
| `MAX_TURNS` | 30 | 回合上限，超過即中止 |
| `TASK_BUDGET_USD` | 2.0 | 單任務累計上限，超過即中止並回報已花費與進度 |
| `DAILY_BUDGET_USD` | 10.0 | 每日總額，超過後 bot 進入**唯讀模式**（可聊天、拒 `/task`），隔日 00:00 自動重置 |

任一熔斷觸發 → 頻道張貼 🔴 紅色警報 Embed。

---

## 記憶機制

- **Memory Bank**（`workspace/CLAUDE.md`）：人工整理的長期記憶，擬計畫時取頭部 4000 字注入提示詞。
- **踩坑筆記**（`memory/notes.md`）：append-only，擬計畫時取尾部 3000 字注入（最新在後）。
- **自動追加**：執行提示詞要求 Manager 回報以「📝 踩坑筆記」段落收尾，runtime 解析後自動寫入 `memory/notes.md`；零額外 API 呼叫。Manager 沒寫就存兜底紀錄。
- 作戰計畫格式強制含「📚 引用經驗」欄位，確保第二個任務能引用第一個任務的筆記。
- `memory/notes.md` 刻意放在 `workspace/` 之外：工程師寫不到，只有 runtime 的單一寫入路徑（加 `asyncio.Lock`）能動它。

---

## 快速開始

### 1. 建立 Discord 應用程式

在 [Discord Developer Portal](https://discord.com/developers/applications)：

1. 建立 Application → Bot，複製 Token。
2. **Bot 頁開啟 Message Content Intent**。
3. **不要**勾選「Requires OAuth2 Code Grant」（勾了會授權成功但 bot 永不加入伺服器，且無錯誤）。
4. 用 OAuth2 URL 邀請 bot 進你的私人伺服器；若應用有開 User Install，邀請連結請加 `integration_type=0`，否則會裝成「為自己新增」而不進伺服器。
5. 在 Discord 開啟開發者模式，取得伺服器 ID、你的使用者 ID、（選填）報告頻道 ID。

### 2. 安裝

```bash
pip install -e ".[dev]"
```

`claude-agent-sdk` 內建 Claude Code CLI，不需另外安裝 Node。

### 3. 設定環境變數

```bash
cp .env.example .env
```

填入 `DISCORD_BOT_TOKEN`、`ANTHROPIC_API_KEY`、`ALLOWED_GUILD_IDS`、`ALLOWED_USER_IDS`（完整清單見 [環境變數](#環境變數)）。缺任一必填項，啟動時會直接報錯退出。

### 4. 啟動

```bash
python -m src.bot
```

看到 log 出現「一人公司開張」即上線。斜線指令按伺服器同步，立即生效。

---

## 使用方式

| 輸入 | 行為 |
|---|---|
| 一般訊息 | Manager 直接回覆（唯讀工具；寫入類永遠被擋）。>2000 字自動分段 |
| `!ping` | 回 pong 與延遲（毫秒） |
| `!reset` | 清掉本頻道的 session 對應，下一句話開新 session（換新任務前用，可大幅省 token） |
| `/task <描述>` | Manager 讀取記憶後產出**作戰計畫** Embed（🎯 目標／📚 引用經驗／📋 步驟／🤝 委派／⚠️ 風險與防禦），附【✅ 批准】【❌ 否決】按鈕 |

### 任務生命週期

```
pending ──批准──► approved ──► running ──► done
   │                                   └──► failed（熔斷／API 失敗）
   ├──否決──► denied
   └──60 分鐘未決──► expired
```

- 只有 `ALLOWED_USER_IDS` 內的使用者能按按鈕，其他人按了只收到 ephemeral 警告。
- 批准狀態以 SQLite 為準：bot 重啟後舊按鈕失效，但 pending 任務不會被誤放行。
- 執行中，委派、寫檔、指令、查網會以一行摘要轉發頻道（Read / Glob / Grep 不轉發，避免洗版）。
- 結束時回報：結果摘要、變更檔案清單、Manager 的抽查驗收結果、本次成本、今日累計。

---

## 環境變數

| 變數 | 必填 | 預設 | 說明 |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | ✅ | — | Discord bot token |
| `ANTHROPIC_API_KEY` | ✅ | — | 計量付費 API key（禁止 OAuth 權杖） |
| `ALLOWED_GUILD_IDS` | ✅ | — | 允許的伺服器 ID，逗號分隔 |
| `ALLOWED_USER_IDS` | ✅ | — | 允許的使用者 ID（董事長），逗號分隔 |
| `MANAGER_MODEL` | | `claude-fable-5` | 總經理模型 |
| `CODER_MODEL` | | `claude-sonnet-4-6` | 工程師模型 |
| `RESEARCHER_MODEL` | | `claude-haiku-4-5-20251001` | 研究員模型 |
| `MAX_TURNS` | | `30` | 回合上限 |
| `TASK_BUDGET_USD` | | `2.0` | 單任務成本上限 |
| `DAILY_BUDGET_USD` | | `10.0` | 每日總額 |
| `REPORT_CHANNEL_ID` | | 空 | 每日 09:00 摘要的頻道；未設定則跳過排程 |

`.env` 由 `src/config.py` 自行解析（不依賴 python-dotenv）；已存在的環境變數優先，不會被 `.env` 覆蓋。

---

## 目錄結構

```
├─ SPEC.md              # 規格書：原則、架構、行為規格、里程碑與驗收條件
├─ CLAUDE.md            # 給開發用 Claude Code 的常駐守則
├─ .env.example
├─ pyproject.toml
├─ src/
│  ├─ bot.py            # Discord 入口：訊息、/task、批准按鈕 View、每日摘要排程
│  ├─ runtime.py        # Agent SDK 封裝：session 管理、提示詞模板、記憶注入、分段
│  ├─ agents.py         # Manager 系統提示詞 + engineer / researcher 定義
│  ├─ guardrails.py     # hooks：批准閘門、委派強制、Bash 黑名單、路徑閘門、審計
│  ├─ budget.py         # 熔斷器：單任務 / 每日預算、熔斷判讀
│  ├─ store.py          # SQLite：sessions / tasks / costs / audit
│  └─ config.py         # .env 解析與 Settings
├─ workspace/           # 工程師唯一可寫目錄；CLAUDE.md 是 runtime Manager 的 Memory Bank
├─ memory/notes.md      # 踩坑筆記（append-only，任務結束自動追加）
├─ docs/progress.md     # 里程碑進度勾選清單
└─ tests/               # test_m0 / test_guardrails / test_budget / test_m3
```

`.gitignore` 已排除 `.env`、`workspace/*`（僅保留 `CLAUDE.md`）、`*.db`、session 逐字稿。

---

## 測試

```bash
pytest
```

共 79 個測試，涵蓋：訊息分段、白名單、設定解析、subagent 白名單、Bash 黑名單各變體、路徑穿越、批准閘門（含跨任務不沿用）、委派強制、審計截斷、session 重啟續接、熔斷邊界、跨日重置、記憶注入與截斷、踩坑筆記抽取、每日摘要。

測試不依賴平台：越界路徑的案例用 pytest 的 `tmp_path` 產生 workspace 外的絕對路徑，Windows 與 Linux / WSL 皆可全綠。

---

## 專案狀態

- [x] M0 打通
- [x] M1 先勝流程
- [x] M2 委派與熔斷
- [x] M3 記憶與排程
- [ ] 手動驗收待董事長：重啟後續聊、`TASK_BUDGET_USD` 調 0.01 觸發熔斷、連續兩任務筆記引用（見 [docs/progress.md](docs/progress.md)）

**M4+ 方向**（本階段不實作）：多 bot「透明開會」、Gmail / Notion 之 MCP 串接、與其他 agent 框架的對照評測。

---

## 已知限制

- 只支援單一私人伺服器、單一 bot；不做語音、公開伺服器、RAG。
- 計畫卡 60 分鐘未決即過期，需重新 `/task`。
- 每日預算以本地日期累計；跨時區部署時注意重置時間。
- 委派給 subagent 的 prompt 建議 <4000 字（Windows 命令列 8191 字元上限）。
- 作戰計畫超過 4000 字會在 Embed 截斷，全文仍存於 SQLite `tasks.plan`。

---

## 文件索引

| 檔案 | 用途 |
|---|---|
| [SPEC.md](SPEC.md) | 規格書：最高原則、技術選型、行為規格、里程碑驗收條件 |
| [CLAUDE.md](CLAUDE.md) | 開發用 Claude Code 的常駐守則（紅線、工作流程） |
| [docs/progress.md](docs/progress.md) | 各里程碑完成項勾選 |
| [memory/notes.md](memory/notes.md) | 踩坑筆記：SDK API 實際寫法、Discord 邀請陷阱、hooks 簽名等 |
| [workspace/CLAUDE.md](workspace/CLAUDE.md) | runtime Manager 的 Memory Bank（非開發守則） |
