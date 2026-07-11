"""M3 驗收：記憶注入、踩坑筆記自動追加、每日摘要（SPEC §6-M3）。"""

import pytest

from src.bot import compose_daily_report
from src.runtime import (
    MEMORY_NOTES_CHAR_LIMIT,
    NOTE_CHAR_LIMIT,
    append_note,
    build_plan_prompt,
    extract_note,
    load_memory,
)
from src.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


# ---------- 記憶讀取與注入 ----------


def test_load_memory_reads_both_files(tmp_path):
    bank = tmp_path / "CLAUDE.md"
    notes = tmp_path / "notes.md"
    bank.write_text("# Memory Bank\n決策：用 SQLite", encoding="utf-8")
    notes.write_text("## 2026-07-12 / 任務 #1\n坑：編碼要用 utf-8", encoding="utf-8")
    memory = load_memory(bank, notes)
    assert "用 SQLite" in memory and "utf-8" in memory


def test_load_memory_missing_files_ok(tmp_path):
    # edge case：檔案不存在 → 空字串，不炸
    assert load_memory(tmp_path / "no.md", tmp_path / "nope.md") == ""


def test_load_memory_notes_tail_truncated_keeps_newest(tmp_path):
    # edge case：筆記超長 → 尾部截斷，最新的留下、最舊的被丟
    bank = tmp_path / "CLAUDE.md"
    notes = tmp_path / "notes.md"
    old_entry = "## 最舊的筆記\n" + "舊" * 3000
    new_entry = "## 最新的筆記\n關鍵解法在這"
    notes.write_text(f"{old_entry}\n{new_entry}", encoding="utf-8")
    memory = load_memory(bank, notes)
    assert "關鍵解法在這" in memory
    assert "最舊的筆記" not in memory
    assert "已截斷" in memory
    assert len(memory) < MEMORY_NOTES_CHAR_LIMIT + 200


def test_plan_prompt_injects_memory_and_requires_citation(tmp_path):
    prompt = build_plan_prompt("建一個 versions.md", memory="### 踩坑筆記\nPyPI 要用官方 API")
    assert "PyPI 要用官方 API" in prompt
    assert "📚 引用經驗" in prompt  # 計畫格式強制含引用欄位（驗收：第二任務引用第一任務筆記）


def test_plan_prompt_empty_memory_placeholder():
    prompt = build_plan_prompt("任務", memory="")
    assert "（目前沒有任何記憶）" in prompt


# ---------- 踩坑筆記抽取與追加 ----------


def test_extract_note_from_reply():
    reply = "任務完成。\n\n📝 踩坑筆記：\ndiscord.py 的 Embed 上限 4096，要先截斷。"
    assert extract_note(reply) == "discord.py 的 Embed 上限 4096，要先截斷。"


def test_extract_note_absent_returns_none():
    assert extract_note("任務完成，一切順利。") is None
    assert extract_note("📝 踩坑筆記：") is None  # edge case：有標記但空內容


def test_extract_note_capped():
    reply = "📝 踩坑筆記：" + "坑" * 2000
    assert len(extract_note(reply)) <= NOTE_CHAR_LIMIT


async def test_append_note_sequential_entries(tmp_path):
    notes = tmp_path / "memory" / "notes.md"  # edge case：目錄不存在 → 自動建立
    await append_note(1, "第一個任務", "坑A：解法A", notes)
    await append_note(2, "第二個任務", "坑B：解法B", notes)
    text = notes.read_text(encoding="utf-8")
    assert "任務 #1" in text and "坑A：解法A" in text
    assert "任務 #2" in text and "坑B：解法B" in text
    assert text.index("坑A") < text.index("坑B")  # 順序不交錯


async def test_appended_note_visible_to_next_plan(tmp_path):
    """SPEC §6-M3 驗收核心：第一個任務的筆記，出現在第二個任務的計畫 prompt 裡。"""
    bank = tmp_path / "CLAUDE.md"
    notes = tmp_path / "notes.md"
    await append_note(1, "查 discord.py 版本", "PyPI JSON API 比搜尋引擎快照可靠", notes)
    prompt = build_plan_prompt("再查一次版本", memory=load_memory(bank, notes))
    assert "PyPI JSON API 比搜尋引擎快照可靠" in prompt


# ---------- 每日摘要 ----------


def test_tasks_today_filters_by_date(store):
    store.create_task(1, "今天的任務")
    store._conn.execute(
        "INSERT INTO tasks(channel_id, description, created_at) VALUES(1, '昨天的任務', '2026-07-11T09:00:00')"
    )
    store._conn.commit()
    rows = store.tasks_today()
    assert len(rows) == 1 and rows[0]["description"] == "今天的任務"


def test_compose_daily_report_with_tasks(store):
    t1 = store.create_task(1, "任務甲")
    store.set_status(t1, "done")
    store.add_task_cost(t1, 0.5)
    store.create_task(1, "任務乙")  # pending
    body = compose_daily_report(store.tasks_today(), 0.75, 10.0)
    assert "共 2 個任務（完成 1）" in body
    assert "任務甲" in body and "✅" in body
    assert "$0.75" in body and "$10.00" in body


def test_compose_daily_report_empty_day():
    body = compose_daily_report([], 0.0, 10.0)
    assert "今日沒有任務" in body  # edge case：沒任務也照樣發報告
