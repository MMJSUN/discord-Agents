"""M2 驗收：熔斷器（單任務／每日）、00:00 重置、subagent 歸戶與進度轉發。"""

import pytest

from src.budget import BREAKER_SUBTYPES, BudgetGuard
from src.config import Settings
from src.guardrails import build_hooks, progress_line
from src.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def guard(store):
    return BudgetGuard(Settings(task_budget_usd=2.0, daily_budget_usd=10.0), store)


# ---------- 每日總額（唯讀模式） ----------


def test_daily_budget_boundary(guard, store):
    store.record_cost(9.99)
    assert not guard.daily_exceeded()
    assert guard.check_can_start_task() is None
    store.record_cost(0.01)  # 正好觸頂
    assert guard.daily_exceeded()
    reason = guard.check_can_start_task()
    assert reason is not None and "唯讀模式" in reason


def test_yesterday_costs_reset_at_midnight(guard, store):
    # edge case：跨日重置——昨天花再多，今天歸零（SPEC §5.4 隔日 00:00 重置）
    store._conn.execute(
        "INSERT INTO costs(date, amount_usd, task_id, note) VALUES('2026-07-11', 99.0, NULL, '')"
    )
    store._conn.commit()
    assert guard.daily_spent() == 0.0
    assert not guard.daily_exceeded()


# ---------- 單任務上限 ----------


def test_task_budget_remaining(guard, store):
    task_id = store.create_task(1, "任務")
    store.add_task_cost(task_id, 1.5)
    assert guard.task_remaining(task_id) == pytest.approx(0.5)
    assert guard.max_budget_for_run(task_id) == pytest.approx(0.5)


def test_run_budget_is_min_of_task_and_daily(guard, store):
    # 每日只剩 0.3、任務還剩 2.0 → 本輪上限取小者 0.3
    task_id = store.create_task(1, "任務")
    store.record_cost(9.7)
    assert guard.max_budget_for_run(task_id) == pytest.approx(0.3)


def test_run_budget_zero_when_task_exhausted(guard, store):
    # edge case：任務超支後再批准執行 → 上限 0，拒絕開跑
    task_id = store.create_task(1, "任務")
    store.add_task_cost(task_id, 2.5)
    assert guard.max_budget_for_run(task_id) == 0.0


def test_run_budget_zero_below_minimum(guard, store):
    # edge case：剩餘額度小到無意義（<0.001）→ 視為用罄
    task_id = store.create_task(1, "任務")
    store.add_task_cost(task_id, 1.9995)
    assert guard.max_budget_for_run(task_id) == 0.0


# ---------- 熔斷判讀 ----------


def test_breaker_subtypes_recognized(guard):
    assert guard.describe_breaker("error_max_budget_usd") is not None
    assert guard.describe_breaker("error_max_turns") is not None
    assert guard.describe_breaker("success") is None
    assert guard.describe_breaker(None) is None
    assert set(BREAKER_SUBTYPES) == {"error_max_budget_usd", "error_max_turns"}


# ---------- subagent 歸戶與進度轉發（SPEC §6-M2：審計可觀察 subagent 被呼叫） ----------


async def test_subagent_tool_calls_attributed_in_audit(store, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    task_id = store.create_task(7, "委派任務")
    store.set_status(task_id, "approved")
    hooks = build_hooks(store, 7, task_id, "manager", None, ws)
    post_hook = hooks["PostToolUse"][0].hooks[0]

    # 模擬工程師 subagent 內的工具呼叫（帶 agent_type）
    await post_hook(
        {"tool_name": "Write", "tool_input": {"file_path": "hello.txt"},
         "tool_response": "ok", "agent_type": "engineer"},
        "tu-1", None,
    )
    # 模擬 Manager 主線程的委派呼叫（不帶 agent_type）
    await post_hook(
        {"tool_name": "Agent", "tool_input": {"subagent_type": "engineer", "prompt": "建 hello.txt"}},
        "tu-2", None,
    )
    rows = store.recent_audit()
    assert rows[1]["agent"] == "engineer" and rows[1]["tool_name"] == "Write"
    assert rows[0]["agent"] == "manager" and rows[0]["tool_name"] == "Agent"


async def test_progress_relayed_for_key_tools_only(store, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    task_id = store.create_task(7, "任務")
    store.set_status(task_id, "approved")
    lines: list[str] = []

    async def progress(text: str) -> None:
        lines.append(text)

    hooks = build_hooks(store, 7, task_id, "manager", None, ws, progress)
    pre_hook = hooks["PreToolUse"][0].hooks[0]

    await pre_hook({"tool_name": "Read", "tool_input": {"file_path": "a.md"}}, "t1", None)
    await pre_hook(
        {"tool_name": "Agent",
         "tool_input": {"subagent_type": "researcher", "prompt": "查 discord.py 版本"}},
        "t2", None,
    )
    await pre_hook(
        {"tool_name": "Bash", "tool_input": {"command": "python -m pytest"},
         "agent_type": "engineer"},
        "t3", None,
    )
    # Read 不轉發（避免洗版）；Agent 與 Bash 轉發，且標明執行者
    assert len(lines) == 2
    assert "researcher" in lines[0]
    assert "engineer" in lines[1] and "pytest" in lines[1]


async def test_denied_subagent_call_blocked_and_attributed(store, tmp_path):
    # edge case：工程師 subagent 想寫 workspace 外 → deny 且審計歸戶到 engineer
    ws = tmp_path / "ws"
    ws.mkdir()
    task_id = store.create_task(7, "任務")
    store.set_status(task_id, "approved")
    hooks = build_hooks(store, 7, task_id, "manager", None, ws)
    pre_hook = hooks["PreToolUse"][0].hooks[0]

    outside = str(tmp_path / "outside" / "evil.txt")  # workspace 外的絕對路徑（跨平台）
    result = await pre_hook(
        {"tool_name": "Write", "tool_input": {"file_path": outside},
         "agent_type": "engineer"},
        "t4", None,
    )
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert store.recent_audit()[0]["agent"] == "engineer"


def test_progress_line_formats():
    assert "委派給 **engineer**" in progress_line(
        "manager", "Agent", {"subagent_type": "engineer", "prompt": "x"})
    assert "`git status`" in progress_line("engineer", "Bash", {"command": "git status"})
    long_cmd = "echo " + "x" * 300
    assert len(progress_line("engineer", "Bash", {"command": long_cmd})) < 120
