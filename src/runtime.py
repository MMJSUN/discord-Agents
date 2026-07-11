"""claude-agent-sdk 封裝：Manager session 管理、回覆收集、訊息分段。

M1：session 由 SQLite 持久化（bot 重啟後 resume 仍有效）；
guardrails hooks 全程掛載——閒聊模式寫入類永遠 deny，
任務模式由批准閘門＋黑名單把關。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from .agents import MANAGER_SYSTEM_PROMPT
from .config import WORKSPACE_DIR, Settings
from .guardrails import NotifyFn, build_hooks
from .store import Store

logger = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2000
RETRY_ATTEMPTS = 3  # SPEC §9：API 斷線 → 指數退避重試 ≤3 次

# 閒聊/擬計畫模式：只能讀。任務執行模式：加開寫入類（受 hooks 把關）。
READ_TOOLS = ["Read", "Glob", "Grep"]
EXEC_TOOLS = READ_TOOLS + ["Write", "Edit", "Bash"]


def split_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """把長訊息切成 ≤limit 的段落，優先在段落（空行）邊界切分。

    空字串回傳空 list（Discord 不允許空訊息）。
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        # 單一段落本身超長 → 逐行切；單行仍超長 → 硬切
        pieces = [paragraph] if len(paragraph) <= limit else _split_oversized(paragraph, limit)
        for piece in pieces:
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks


def _split_oversized(paragraph: str, limit: int) -> list[str]:
    pieces: list[str] = []
    current = ""
    for line in paragraph.split("\n"):
        while len(line) > limit:  # 單行超長只能硬切
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
        else:
            pieces.append(current)
            current = line
    if current:
        pieces.append(current)
    return pieces


def build_manager_options(
    settings: Settings,
    resume: str | None = None,
    hooks: dict | None = None,
    unlock_write_tools: bool = False,
) -> ClaudeAgentOptions:
    """Manager 選項。預設鎖定唯讀；unlock_write_tools 只給已批准的任務執行用。

    - permission_mode="dontAsk"：無頭環境沒有 TTY，未核可工具一律拒絕
      （SPEC §5.3：不依賴 SDK 內建互動式人類提問）。
    - setting_sources=[]：不吸收開發環境的 CLAUDE.md / settings。
    - disallowed_tools：Agent/Task 到 M2 委派功能上線才開；
      閒聊模式連寫入類一起封死（hooks 之外的第二道鎖）。
    """
    if unlock_write_tools:
        allowed = EXEC_TOOLS
        disallowed = ["Agent", "Task", "WebSearch", "WebFetch"]
    else:
        allowed = READ_TOOLS
        disallowed = ["Write", "Edit", "Bash", "NotebookEdit", "Agent", "Task"]
    return ClaudeAgentOptions(
        model=settings.manager_model,
        system_prompt=MANAGER_SYSTEM_PROMPT,
        cwd=str(WORKSPACE_DIR),
        max_turns=settings.max_turns,
        permission_mode="dontAsk",
        allowed_tools=allowed,
        disallowed_tools=disallowed,
        setting_sources=[],
        resume=resume,
        hooks=hooks,
    )


PLAN_PROMPT_TEMPLATE = """\
董事長透過 /task 指令提交了新任務，內容如下：

{description}

請依【勝兵先勝而後求戰】產出「作戰計畫」，格式：
🎯 目標：（一句話）
📋 步驟：（編號列點）
🤝 委派：（預計交給哪個 subagent 與使用工具；目前委派功能未開通則寫「本回合由總經理親自執行」）
⚠️ 風險與防禦：（至少 3 條 edge cases 與對應防禦）

只輸出計畫本體，不要執行任何動作。計畫全文控制在 1800 字以內。"""

EXECUTE_PROMPT_TEMPLATE = """\
董事長已批准任務 #{task_id}：{description}

請依你剛才提出的作戰計畫開始執行。約束：
- 所有檔案操作僅限目前工作目錄（workspace/）之內。
- 黑名單指令（rm -rf、sudo、curl|sh、chmod 777、git push --force）會被系統攔截，不要嘗試。
- 完成後回報：結果摘要、變更的檔案清單、如何驗證、遇到的問題。"""


@dataclass
class ManagerReply:
    text: str
    cost_usd: float
    session_id: str | None


class ManagerRuntime:
    """每個 Discord 頻道對應一條 Manager session（頻道 = 部門 = 上下文隔離）。"""

    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store

    async def ask(self, channel_id: int, prompt: str, notify: NotifyFn | None = None) -> ManagerReply:
        """閒聊模式：唯讀工具，寫入類被 hooks + disallowed 雙重封鎖。"""
        hooks = build_hooks(self.store, channel_id, task_id=None, agent="manager", notify=notify)
        options = build_manager_options(
            self.settings, resume=self.store.get_session(channel_id), hooks=hooks
        )
        return await self._run(channel_id, prompt, options)

    async def plan_task(self, channel_id: int, task_id: int, description: str) -> ManagerReply:
        """擬作戰計畫：同頻道 session（Manager 記得脈絡），但工具仍鎖唯讀。"""
        hooks = build_hooks(self.store, channel_id, task_id=None, agent="manager")
        options = build_manager_options(
            self.settings, resume=self.store.get_session(channel_id), hooks=hooks
        )
        reply = await self._run(channel_id, PLAN_PROMPT_TEMPLATE.format(description=description), options)
        self.store.add_task_cost(task_id, reply.cost_usd)
        self.store.record_cost(reply.cost_usd, task_id, "plan")
        return reply

    async def execute_task(
        self,
        channel_id: int,
        task_id: int,
        description: str,
        notify: NotifyFn | None = None,
    ) -> ManagerReply:
        """執行已批准任務：解鎖寫入類工具，批准閘門＋黑名單 hooks 全程把關。"""
        hooks = build_hooks(self.store, channel_id, task_id=task_id, agent="manager", notify=notify)
        options = build_manager_options(
            self.settings,
            resume=self.store.get_session(channel_id),
            hooks=hooks,
            unlock_write_tools=True,
        )
        prompt = EXECUTE_PROMPT_TEMPLATE.format(task_id=task_id, description=description)
        reply = await self._run(channel_id, prompt, options)
        self.store.add_task_cost(task_id, reply.cost_usd)
        self.store.record_cost(reply.cost_usd, task_id, "execute")
        return reply

    # ---------- 內部 ----------

    async def _run(self, channel_id: int, prompt: str, options: ClaudeAgentOptions) -> ManagerReply:
        last_error: Exception | None = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                return await self._run_once(channel_id, prompt, options)
            except Exception as exc:  # 失敗回報而非崩潰
                last_error = exc
                delay = 2**attempt
                logger.warning("Manager 呼叫失敗（第 %d 次）：%s，%ds 後重試", attempt + 1, exc, delay)
                await asyncio.sleep(delay)
        raise RuntimeError(f"Manager 連續 {RETRY_ATTEMPTS} 次呼叫失敗：{last_error}") from last_error

    async def _run_once(self, channel_id: int, prompt: str, options: ClaudeAgentOptions) -> ManagerReply:
        text_parts: list[str] = []
        cost = 0.0
        session_id: str | None = None

        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
            elif isinstance(message, ResultMessage):
                session_id = message.session_id
                if message.total_cost_usd is not None:
                    cost = message.total_cost_usd
                if not text_parts and message.result:
                    text_parts.append(message.result)

        if session_id:
            self.store.set_session(channel_id, session_id)
        return ManagerReply(text="\n".join(text_parts).strip(), cost_usd=cost, session_id=session_id)
