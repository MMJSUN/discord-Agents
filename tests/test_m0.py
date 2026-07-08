"""M0 驗收測試：訊息分段、白名單、設定解析、subagent 規格。"""

from src.agents import ENGINEER_TOOLS, RESEARCHER_TOOLS, build_subagents
from src.bot import is_authorized
from src.config import Settings, parse_env_file, parse_id_set
from src.runtime import DISCORD_MESSAGE_LIMIT, build_manager_options, split_message

# ---------- 訊息分段（>2000 字自動分段） ----------


def test_split_short_message_untouched():
    assert split_message("哈囉") == ["哈囉"]


def test_split_empty_message_returns_nothing():
    # edge case：空字串／純空白 → Discord 不允許空訊息
    assert split_message("") == []
    assert split_message("   \n  ") == []


def test_split_exactly_at_limit_stays_single():
    # edge case：剛好 2000 字不分段
    text = "a" * DISCORD_MESSAGE_LIMIT
    assert split_message(text) == [text]


def test_split_prefers_paragraph_boundary():
    p1, p2 = "甲" * 1500, "乙" * 1500
    chunks = split_message(f"{p1}\n\n{p2}")
    assert chunks == [p1, p2]
    assert all(len(c) <= DISCORD_MESSAGE_LIMIT for c in chunks)


def test_split_oversized_single_line_hard_cut():
    # edge case：惡意輸入——5000 字無任何換行，只能硬切
    text = "x" * 5000
    chunks = split_message(text)
    assert all(len(c) <= DISCORD_MESSAGE_LIMIT for c in chunks)
    assert "".join(chunks) == text


def test_split_preserves_all_content():
    paragraphs = [f"第{i}段：" + "內" * 900 for i in range(6)]
    text = "\n\n".join(paragraphs)
    chunks = split_message(text)
    assert all(len(c) <= DISCORD_MESSAGE_LIMIT for c in chunks)
    # 重組後（段落邊界可能變成 chunk 邊界）內容不遺失
    assert "\n\n".join(chunks).replace("\n\n", "") == text.replace("\n\n", "")


# ---------- 白名單（SPEC §5.3 存取控制） ----------

GUILDS = {111}
USERS = {222}


def test_authorized_guild_and_user():
    assert is_authorized(111, 222, GUILDS, USERS)


def test_wrong_guild_rejected():
    assert not is_authorized(999, 222, GUILDS, USERS)


def test_wrong_user_rejected():
    # edge case：白名單伺服器裡的陌生人（提示詞注入的主要入口）
    assert not is_authorized(111, 999, GUILDS, USERS)


def test_dm_rejected():
    # edge case：DM 沒有 guild → 靜默忽略
    assert not is_authorized(None, 222, GUILDS, USERS)


def test_empty_whitelist_rejects_everyone():
    # edge case：.env 忘了填白名單 → 全部拒絕而不是全部放行
    assert not is_authorized(111, 222, set(), set())


# ---------- 設定解析 ----------


def test_parse_id_set_handles_spaces_and_garbage():
    assert parse_id_set("123, 456") == {123, 456}
    assert parse_id_set("") == set()
    assert parse_id_set("abc, 12x, 789") == {789}


def test_parse_env_file_ignores_comments_and_strips_quotes():
    text = '# 註解\nDISCORD_BOT_TOKEN="secret"\nMAX_TURNS=30\n\nBROKEN_LINE\n'
    parsed = parse_env_file(text)
    assert parsed == {"DISCORD_BOT_TOKEN": "secret", "MAX_TURNS": "30"}


# ---------- Subagent 規格（SPEC §5.2） ----------


def _settings() -> Settings:
    return Settings(coder_model="claude-sonnet-4-6", researcher_model="claude-haiku-4-5-20251001")


def test_engineer_tools_exact_whitelist():
    agents = build_subagents(_settings())
    assert agents["engineer"].tools == ENGINEER_TOOLS == ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]


def test_researcher_tools_web_only():
    agents = build_subagents(_settings())
    assert agents["researcher"].tools == RESEARCHER_TOOLS == ["WebSearch", "WebFetch"]


def test_no_subagent_can_delegate():
    # 紅線：subagent 工具清單不得含 Agent／Task（防遞迴委派）
    for name, agent in build_subagents(_settings()).items():
        assert "Agent" not in (agent.tools or []), name
        assert "Task" not in (agent.tools or []), name


def test_subagent_models_follow_settings():
    agents = build_subagents(_settings())
    assert agents["engineer"].model == "claude-sonnet-4-6"
    assert agents["researcher"].model == "claude-haiku-4-5-20251001"


# ---------- M0 的 Manager 防護姿態 ----------


def test_manager_options_are_locked_down_for_m0():
    options = build_manager_options(_settings())
    assert options.permission_mode == "dontAsk"  # 無 TTY 環境不彈互動提問
    for tool in ("Write", "Edit", "Bash"):
        assert tool in options.disallowed_tools  # M1 批准閘門上線前寫入類全封
    assert options.setting_sources == []  # 不吸收開發機的 CLAUDE.md / settings
    assert options.model == "claude-fable-5"
