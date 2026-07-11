"""hooks：PreToolUse 黑名單、批准閘門、PostToolUse 審計（SPEC §5.3）。

防線順序（不戰而屈人——全部在執行前攔截）：
1. 批准閘門：寫入/執行類工具在任務批准前一律 deny（任務層級，不跨任務沿用）。
2. Bash 黑名單：rm -rf、sudo、curl|sh、chmod 777、寫 workspace 外絕對路徑、git push --force。
3. 路徑閘門：Write/Edit 目標必須落在 workspace/ 內（含 .. 穿越防禦）。
黑名單優先於批准——批准過的任務也不准碰紅線。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from claude_agent_sdk import HookMatcher

from .config import WORKSPACE_DIR
from .store import Store

# 需要批准閘門的工具（寫入或執行）
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"}
# 需要路徑檢查的工具及其路徑參數
PATH_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path",
              "NotebookEdit": "notebook_path"}

_SUDO = re.compile(r"(^|[\s;&|(])sudo\s", re.IGNORECASE)
_PIPE_TO_SHELL = re.compile(r"\b(curl|wget)\b[^|;&\n]*\|\s*(?:ba|z|da)?sh\b", re.IGNORECASE)
_CHMOD_777 = re.compile(r"\bchmod\b[^;&|\n]*\b0?777\b")
_GIT_PUSH_FORCE = re.compile(r"\bgit\s+push\b[^;&|\n]*(\s--force(-with-lease)?\b|\s-f\b)")
# 會寫入/刪除檔案的指令或重導向（配合絕對路徑檢查用）
_WRITE_INDICATOR = re.compile(
    r"(>>?|\btee\b|\bcp\b|\bmv\b|\brm\b|\bmkdir\b|\btouch\b|\bdel\b|\bcopy\b|\bmove\b"
    r"|\bout-file\b|\bset-content\b|\bremove-item\b)",
    re.IGNORECASE,
)
# 絕對路徑 token：C:\...、C:/...、/usr/...
_ABS_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/)[^\s'\";|&<>]*")


def _has_rm_rf(command: str) -> bool:
    """token 化偵測 rm 遞迴+強制刪除，涵蓋 rm -rf / -fr / -r -f / --recursive --force。"""
    for segment in re.split(r"[;&|]+", command):
        tokens = segment.split()
        if "rm" not in tokens:
            continue
        short_flags = "".join(t.lstrip("-") for t in tokens if re.fullmatch(r"-[A-Za-z]+", t))
        recursive = "r" in short_flags or "R" in short_flags or "--recursive" in tokens
        force = "f" in short_flags or "--force" in tokens
        if recursive and force:
            return True
    return False


def is_path_allowed(path_str: str, workspace_root: Path | str | None = None) -> bool:
    """路徑（含相對路徑與 .. 穿越）解析後必須落在 workspace/ 內。"""
    root = Path(workspace_root or WORKSPACE_DIR).resolve()
    try:
        p = Path(path_str.strip().strip("'\""))
        if not p.is_absolute():
            p = root / p
        return p.resolve().is_relative_to(root)
    except (OSError, ValueError):
        return False


def check_bash_command(command: str, workspace_root: Path | str | None = None) -> str | None:
    """命中黑名單回傳原因，安全回傳 None。"""
    if _has_rm_rf(command):
        return "rm 遞迴強制刪除（rm -rf 及其變體）"
    if _SUDO.search(command):
        return "sudo 提權"
    if _PIPE_TO_SHELL.search(command):
        return "下載腳本直接執行（curl/wget | sh）"
    if _CHMOD_777.search(command):
        return "chmod 777 全開權限"
    if _GIT_PUSH_FORCE.search(command):
        return "git push --force 強推"
    if _WRITE_INDICATOR.search(command):
        for abs_path in _ABS_PATH.findall(command):
            if not is_path_allowed(abs_path, workspace_root):
                return f"寫入 workspace/ 之外的絕對路徑：{abs_path}"
    return None


def evaluate_tool_call(
    store: Store,
    tool_name: str,
    tool_input: dict[str, Any],
    task_id: int | None,
    workspace_root: Path | str | None = None,
) -> str | None:
    """回傳 deny 原因；None = 放行。純函式方便測試。"""
    # 1. 先勝閘門：未批准前寫入/執行類一律拒絕
    if tool_name in WRITE_TOOLS and not store.is_approved(task_id):
        return "任務未批准（先勝閘門）：寫入／執行類工具在董事長批准前一律拒絕"
    # 2. Bash 黑名單（批准了也不准碰紅線）
    if tool_name == "Bash":
        reason = check_bash_command(str(tool_input.get("command", "")), workspace_root)
        if reason:
            return f"Bash 黑名單：{reason}"
    # 3. 檔案工具的路徑閘門
    param = PATH_TOOLS.get(tool_name)
    if param:
        target = str(tool_input.get(param, ""))
        if target and not is_path_allowed(target, workspace_root):
            return f"目標路徑在 workspace/ 之外：{target}"
    return None


def summarize_params(tool_input: dict[str, Any]) -> str:
    try:
        return json.dumps(tool_input, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(tool_input)


NotifyFn = Callable[[str], Awaitable[None]]


def build_hooks(
    store: Store,
    channel_id: int,
    task_id: int | None,
    agent: str = "manager",
    notify: NotifyFn | None = None,
    workspace_root: Path | str | None = None,
) -> dict[str, list[HookMatcher]]:
    """組出掛進 ClaudeAgentOptions.hooks 的設定。

    task_id=None 代表閒聊模式：寫入類工具永遠 deny。
    notify：deny 時回報 Discord 頻道（SPEC §5.1 先勝閘門需回報）。
    """

    async def pre_tool_use(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        tool_name = str(input_data.get("tool_name", ""))
        tool_input = input_data.get("tool_input") or {}
        reason = evaluate_tool_call(store, tool_name, tool_input, task_id, workspace_root)
        if reason:
            store.add_audit(channel_id, task_id, agent, tool_name,
                            summarize_params(tool_input), "deny")
            if notify:
                await notify(f"🛡️ 已攔截 `{tool_name}`：{reason}")
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        return {}

    async def post_tool_use(input_data: dict, tool_use_id: str | None, context: Any) -> dict:
        # SPEC §5.3：所有實際執行的工具呼叫都留審計
        store.add_audit(
            channel_id, task_id, agent,
            str(input_data.get("tool_name", "")),
            summarize_params(input_data.get("tool_input") or {}),
            "allow",
        )
        return {}

    return {
        "PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use])],
        "PostToolUse": [HookMatcher(matcher=None, hooks=[post_tool_use])],
    }
