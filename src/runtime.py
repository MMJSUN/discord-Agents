"""claude-agent-sdk 封裝：Manager session 管理、回覆收集、訊息分段。

M0 範圍：Manager 純對話（工具全關），session 續接先用記憶體內對應表；
M1 會把 channel_id ↔ session_id 換成 SQLite 持久化並加上 guardrails hooks。
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

logger = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2000
RETRY_ATTEMPTS = 3  # SPEC §9：API 斷線 → 指數退避重試 ≤3 次


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


def build_manager_options(settings: Settings, resume: str | None = None) -> ClaudeAgentOptions:
    """M0 的 Manager 選項：純對話、工具全關、不載入本機任何設定檔。

    - permission_mode="dontAsk"：無頭環境沒有 TTY，任何未預先核可的工具一律拒絕
      （SPEC §5.3：不依賴 SDK 內建互動式人類提問）。
    - setting_sources=[]：不吸收開發環境的 CLAUDE.md / settings，避免污染 runtime Manager。
    - disallowed_tools：寫入類工具在 M1 批准閘門上線前一律封死（紅線防護）。
    """
    return ClaudeAgentOptions(
        model=settings.manager_model,
        system_prompt=MANAGER_SYSTEM_PROMPT,
        cwd=str(WORKSPACE_DIR),
        max_turns=settings.max_turns,
        permission_mode="dontAsk",
        allowed_tools=[],
        disallowed_tools=["Write", "Edit", "Bash", "NotebookEdit", "Agent", "Task"],
        setting_sources=[],
        resume=resume,
    )


@dataclass
class ManagerReply:
    text: str
    cost_usd: float
    session_id: str | None


class ManagerRuntime:
    """每個 Discord 頻道對應一條 Manager session（頻道 = 部門 = 上下文隔離）。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._sessions: dict[int, str] = {}  # channel_id -> session_id（M1 改 SQLite）

    async def ask(self, channel_id: int, prompt: str) -> ManagerReply:
        last_error: Exception | None = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                return await self._ask_once(channel_id, prompt)
            except Exception as exc:  # 失敗回報而非崩潰
                last_error = exc
                delay = 2**attempt
                logger.warning("Manager 呼叫失敗（第 %d 次）：%s，%ds 後重試", attempt + 1, exc, delay)
                await asyncio.sleep(delay)
        raise RuntimeError(f"Manager 連續 {RETRY_ATTEMPTS} 次呼叫失敗：{last_error}") from last_error

    async def _ask_once(self, channel_id: int, prompt: str) -> ManagerReply:
        options = build_manager_options(self.settings, resume=self._sessions.get(channel_id))
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
            self._sessions[channel_id] = session_id
        return ManagerReply(text="\n".join(text_parts).strip(), cost_usd=cost, session_id=session_id)
