# 踩坑筆記

每次任務結束後追加。格式：日期 / 任務 / 坑 / 解法。

## 2026-07-09 / M0 打通
- **SDK 選對文件**：claude-agent-sdk（Agent SDK）≠ anthropic（Claude API SDK），
  文件在 code.claude.com/docs/en/agent-sdk/，platform.claude.com 的舊網址會 307 轉址過去。
- **AgentDefinition 欄位是 camelCase**：Python SDK 為了對齊 wire format，
  `disallowedTools`、`mcpServers`、`maxTurns` 不是 snake_case，別憑直覺寫。
- **`setting_sources=[]` 必設**：不設的話 runtime Manager 會把開發機的
  CLAUDE.md / settings.json 吸進系統提示，兩個世界就混了。
- **無頭環境用 `permission_mode="dontAsk"`**：SDK 內建人類提問需要 TTY，
  Discord bot 跑起來沒有；未核可工具直接 deny，人類確認一律走 Discord 按鈕（M1）。
- **委派用 Agent 工具**：Manager 的 allowed_tools 需含 `Agent`（舊名 Task，
  v2.1.63 改名；偵測時兩個名字都要比對）。subagent 工具清單不含 Agent 防遞迴。
- **Windows 命令列 8191 字元上限**：subagent prompt 過長會炸，M2 委派時 prompt 保持精簡。
- **hooks 簽名待驗證**：官方 Python 參考頁對 PreToolUse hook 回傳格式描述不一致，
  M1 寫 guardrails.py 前要先用最小腳本實測 HookMatcher / permissionDecision 的實際行為。

## 2026-07-12 / bot 加入伺服器 + M1 先勝流程
- **文件會騙人，site-packages 不會**：文件摘要說 `HookMatcher(tool_name=...)`，
  實際（0.2.113 原始碼）是 `HookMatcher(matcher="Bash", hooks=[cb])`；
  callback 簽名 `(input_data, tool_use_id, context) -> dict`，輸入欄位是
  `tool_name`/`tool_input`（snake_case），deny 要包
  `{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", ...}}`。
- **bot 邀請「看似成功卻沒加入」的兩大元兇**：① 應用開了 User Install，授權時
  選到「為自己新增」→ bot 不進伺服器（解法：邀請連結加 `integration_type=0`）；
  ② Bot 頁的「Requires OAuth2 Code Grant」被誤開 → 授權完成但 bot 永不加入且無錯誤
  （診斷：GET /applications/@me 看 `bot_require_code_grant`）。
- **Windows 主控台 cp950**：印 ✓/❌ 這類字元前先 `sys.stdout.reconfigure(encoding="utf-8")`。
- **斜線指令即時生效**：`tree.copy_global_to(guild=...)` + `tree.sync(guild=...)`
  按伺服器同步（全域同步要等最多 1 小時）。
- **黑名單用 token 化比純正則穩**：`rm -r -f`、`rm --recursive --force` 這類變體
  拆 token 檢查旗標組合才抓得到；黑名單優先於批准（批准過也不准碰紅線）。

## 2026-07-12 / M2 委派與熔斷
- **熔斷雙保險設計**：事前把 min(任務剩餘, 每日剩餘) 餵給 SDK 的 `max_budget_usd`
  （原生中止，subtype=`error_max_budget_usd`）；事後 ResultMessage 實際成本入帳。
  單靠事後對帳會超支一整輪，單靠 SDK 又管不到跨輪累計。
- **subagent 的工具呼叫流經同一組 hooks**：靠 hook 輸入的 `agent_type` 欄位歸戶
  （主線程沒有這欄位 = manager）。黑名單/路徑閘門因此天然覆蓋工程師。
- **subagent 的 Web 工具要進 Manager 的 allowed_tools**：`allowed_tools` 是
  session 全域的自動核可清單，研究員的 WebSearch 不在裡面就會被 dontAsk 拒絕；
  能力上限則由各 AgentDefinition.tools 鎖死，兩層是不同的東西。
- **閒聊也要入帳**：每日預算若只算任務成本，聊天燒的錢就成了帳外黑洞。
- **進度轉發要挑工具**：只轉發 Agent/Bash/Write/Edit/Web，Read/Glob/Grep 不轉，
  不然一個任務幾十次讀檔直接洗版頻道。
- **提示詞殘留會誤導 runtime Manager**：M1 時代 PLAN 模板寫「委派功能未開通」，
  M2 開通後忘了改 → Manager 據此規劃「一人分飾兩角」。教訓：跨里程碑改功能時，
  grep 一遍所有 prompt 模板找過時敘述。
- **勸導不如城牆**：要 Manager 只調度不動手，光改提示詞不保險——
  在 hooks 加 actor 規則（主線程用 Write/Edit/Bash/Web → deny 並提示改委派），
  違規當下模型收到 deny 理由就會改走 Agent 委派，行為立即矯正。

## 2026-07-12 / M3 記憶與排程
- **記憶用注入不用工具**：叫 Manager 自己 Read 記憶檔要多燒 Fable 回合；
  擬計畫時直接把內容夾進 prompt，確定性高又省錢。Memory Bank 取頭部
  （人工整理重點在前）、踩坑筆記取尾部（append-only 最新在後），方向相反。
- **筆記靠「回報格式契約」白嫖**：執行提示詞要求回報以「📝 踩坑筆記」收尾，
  runtime 用 rfind 解析存檔——零額外 API 呼叫。Manager 沒寫就存兜底紀錄。
- **排程掛在 bot 事件迴圈**：AsyncIOScheduler 要在 setup_hook（loop 已存在）啟動；
  job 內全包 try/except，斷線時記 log 明天照常，不讓排程器死掉。
- **notes.md 在 workspace 外是刻意的**：工程師寫不到（路徑閘門），
  只有 runtime 的單一寫入路徑＋asyncio.Lock 能動它，防並發交錯與 subagent 誤改。
