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
