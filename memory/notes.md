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
