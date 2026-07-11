"""claude-agent-sdk 封裝：Manager session 管理、回覆收集、訊息分段。

M1：session 由 SQLite 持久化（bot 重啟後 resume 仍有效）；
guardrails hooks 全程掛載——閒聊模式寫入類永遠 deny，
任務模式由批准閘門＋黑名單把關。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)

from .agents import MANAGER_SYSTEM_PROMPT, build_subagents
from .config import PROJECT_ROOT, WORKSPACE_DIR, Settings
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
    agents: dict | None = None,
    max_budget_usd: float | None = None,
) -> ClaudeAgentOptions:
    """Manager 選項。預設鎖定唯讀；unlock_write_tools 只給已批准的任務執行用。

    - permission_mode="dontAsk"：無頭環境沒有 TTY，未核可工具一律拒絕
      （SPEC §5.3：不依賴 SDK 內建互動式人類提問）。
    - setting_sources=[]：不吸收開發環境的 CLAUDE.md / settings。
    - 任務執行模式開 Agent（委派）與 Web 工具——Web 工具進 allowed 是為了
      讓研究員 subagent 的呼叫能自動核可；各 subagent 的能力上限
      由 AgentDefinition.tools 各自鎖死（SPEC §5.2）。
    - max_budget_usd：本輪執行的成本上限，超過由 SDK 原生熔斷
      （subtype=error_max_budget_usd）。
    """
    if unlock_write_tools:
        allowed = EXEC_TOOLS + ["Agent", "WebSearch", "WebFetch"]
        disallowed: list[str] = []
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
        agents=agents,
        max_budget_usd=max_budget_usd,
    )


# ---------- 記憶機制（M3：Memory Bank + 踩坑筆記） ----------

MEMORY_BANK_PATH = WORKSPACE_DIR / "CLAUDE.md"
MEMORY_NOTES_PATH = PROJECT_ROOT / "memory" / "notes.md"
MEMORY_BANK_CHAR_LIMIT = 4000   # Memory Bank 取頭部（人工整理，重點在前）
MEMORY_NOTES_CHAR_LIMIT = 3000  # 踩坑筆記取尾部（append-only，最新在後）
NOTE_MARKER = "📝 踩坑筆記"
NOTE_CHAR_LIMIT = 800

_NOTES_LOCK = asyncio.Lock()  # 防兩個任務並發寫 notes.md 交錯


def load_memory(
    bank_path: Path | None = None,
    notes_path: Path | None = None,
) -> str:
    """讀 Memory Bank 與踩坑筆記，截斷控 token；檔案不存在則跳過。"""
    sections: list[str] = []
    bank = bank_path or MEMORY_BANK_PATH
    notes = notes_path or MEMORY_NOTES_PATH
    if bank.exists():
        text = bank.read_text(encoding="utf-8").strip()
        if text:
            sections.append(f"### 公司記憶庫（workspace/CLAUDE.md）\n{text[:MEMORY_BANK_CHAR_LIMIT]}")
    if notes.exists():
        text = notes.read_text(encoding="utf-8").strip()
        if text:
            if len(text) > MEMORY_NOTES_CHAR_LIMIT:
                text = "（…較舊筆記已截斷）\n" + text[-MEMORY_NOTES_CHAR_LIMIT:]
            sections.append(f"### 踩坑筆記（memory/notes.md，最新在後）\n{text}")
    return "\n\n".join(sections)


def extract_note(reply_text: str) -> str | None:
    """從 Manager 執行回報中抽出「📝 踩坑筆記」段（到文末），沒有則 None。"""
    idx = reply_text.rfind(NOTE_MARKER)
    if idx < 0:
        return None
    note = reply_text[idx + len(NOTE_MARKER):].lstrip("：: \n").strip()
    return note[:NOTE_CHAR_LIMIT] or None


async def append_note(
    task_id: int,
    description: str,
    note: str,
    notes_path: Path | None = None,
) -> None:
    """任務結束自動追加踩坑筆記（SPEC §6-M3）；單一寫入路徑＋鎖，防並發交錯。"""
    path = notes_path or MEMORY_NOTES_PATH
    entry = (
        f"\n## {datetime.now().date().isoformat()} / 任務 #{task_id}：{description[:40]}\n"
        f"{note.strip()}\n"
    )
    async with _NOTES_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry)


PLAN_PROMPT_TEMPLATE = """\
董事長透過 /task 指令提交了新任務，內容如下：

{description}

【知彼知己】公司記憶（可能為空；規劃前先讀，有相關經驗必須引用）：
{memory}

公司編制（委派功能已開通）：
- engineer（工程師）：建立／修改檔案、寫程式、執行指令，工作目錄鎖定 workspace/。
- researcher（研究員）：WebSearch / WebFetch 查資料、比較方案。
你本人只做讀取、彙整、驗收與調度——寫檔／執行指令／查網路一律委派，
系統 hooks 會直接攔截你親自動手（將能而君不御）。

請依【勝兵先勝而後求戰】產出「作戰計畫」，格式：
🎯 目標：（一句話）
📚 引用經驗：（記憶庫／踩坑筆記中與本任務相關的內容與如何應用；沒有則寫「無」）
📋 步驟：（編號列點）
🤝 委派：（哪些步驟交給 engineer、哪些交給 researcher、附上下文要點）
⚠️ 風險與防禦：（至少 3 條 edge cases 與對應防禦）

只輸出計畫本體，不要執行任何動作。計畫全文控制在 1800 字以內。"""


def build_plan_prompt(description: str, memory: str | None = None) -> str:
    mem = memory if memory is not None else load_memory()
    return PLAN_PROMPT_TEMPLATE.format(description=description, memory=mem or "（目前沒有任何記憶）")

EXECUTE_PROMPT_TEMPLATE = """\
董事長已批准任務 #{task_id}：{description}

請依你剛才提出的作戰計畫開始執行。約束：
- 【將能而君不御】寫程式／改檔案／執行指令交給 engineer，查網路交給 researcher；
  你親自使用這些工具會被系統攔截。委派時在 prompt 內附完整上下文
  （檔案路徑、錯誤訊息、先前決策）——subagent 看不到你的對話歷史。
  委派 prompt 保持精簡（<4000 字）。
- 所有檔案操作僅限目前工作目錄（workspace/）之內。
- 黑名單指令（rm -rf、sudo、curl|sh、chmod 777、git push --force）會被系統攔截，不要嘗試。
- 【多算勝・驗收後交件】subagent 回報完成不等於完成。交件給董事長之前，
  你必須親自用 Read 抽查 engineer 變更的關鍵檔案（檔案多時抽重點即可，
  驗收本身也要控制成本）：內容是否符合任務要求、有無明顯錯誤或遺漏；
  researcher 的結論要檢查是否附來源、多來源是否一致。
  驗收不通過就退回重做（重新委派並說明問題），最多退回 2 次。
- 完成後回報：結果摘要、變更的檔案清單、你的驗收結果（抽查了什麼、確認了什麼）、
  遇到的問題。
- 回報的最後必須以「📝 踩坑筆記」作為獨立段落收尾：1～3 行，記下本次遇到的坑
  與解法；沒有坑就記下對未來任務有用的經驗。系統會自動存入 memory/notes.md。"""


@dataclass
class ManagerReply:
    text: str
    cost_usd: float
    session_id: str | None
    subtype: str | None = None   # ResultMessage.subtype；error_max_budget_usd 等 = 熔斷
    num_turns: int = 0


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
        reply = await self._run(channel_id, prompt, options)
        if reply.cost_usd:
            self.store.record_cost(reply.cost_usd, None, "chat")  # 閒聊也計入每日預算
        return reply

    async def plan_task(self, channel_id: int, task_id: int, description: str) -> ManagerReply:
        """擬作戰計畫：同頻道 session（Manager 記得脈絡），但工具仍鎖唯讀。"""
        hooks = build_hooks(self.store, channel_id, task_id=None, agent="manager")
        options = build_manager_options(
            self.settings, resume=self.store.get_session(channel_id), hooks=hooks
        )
        reply = await self._run(channel_id, build_plan_prompt(description), options)
        self.store.add_task_cost(task_id, reply.cost_usd)
        self.store.record_cost(reply.cost_usd, task_id, "plan")
        return reply

    async def execute_task(
        self,
        channel_id: int,
        task_id: int,
        description: str,
        notify: NotifyFn | None = None,
        max_budget_usd: float | None = None,
        progress: NotifyFn | None = None,
    ) -> ManagerReply:
        """執行已批准任務：解鎖寫入類＋Agent 委派，批准閘門＋黑名單＋熔斷全程把關。"""
        hooks = build_hooks(
            self.store, channel_id, task_id=task_id, agent="manager",
            notify=notify, progress=progress,
        )
        options = build_manager_options(
            self.settings,
            resume=self.store.get_session(channel_id),
            hooks=hooks,
            unlock_write_tools=True,
            agents=build_subagents(self.settings),
            max_budget_usd=max_budget_usd,
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

        subtype: str | None = None
        num_turns = 0

        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
            elif isinstance(message, ResultMessage):
                session_id = message.session_id
                subtype = message.subtype
                num_turns = message.num_turns or 0
                if message.total_cost_usd is not None:
                    cost = message.total_cost_usd
                if not text_parts and message.result:
                    text_parts.append(message.result)

        if session_id:
            self.store.set_session(channel_id, session_id)
        return ManagerReply(
            text="\n".join(text_parts).strip(), cost_usd=cost,
            session_id=session_id, subtype=subtype, num_turns=num_turns,
        )
