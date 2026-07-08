"""設定載入：讀 .env（不引入 python-dotenv，遵守 SPEC 套件白名單）＋環境變數。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_DIR = PROJECT_ROOT / "workspace"


def parse_env_file(text: str) -> dict[str, str]:
    """解析 KEY=VALUE 格式的 .env 內容；支援註解與前後引號。"""
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            result[key] = value
    return result


def load_env_file(path: Path | None = None) -> None:
    """把 .env 內容塞進 os.environ；已存在的環境變數優先，不覆蓋。"""
    env_path = path or (PROJECT_ROOT / ".env")
    if not env_path.exists():
        return
    for key, value in parse_env_file(env_path.read_text(encoding="utf-8")).items():
        os.environ.setdefault(key, value)


def parse_id_set(raw: str) -> set[int]:
    """把 '123, 456' 這種逗號分隔字串解析成 int 集合；非數字項目忽略。"""
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


@dataclass
class Settings:
    discord_bot_token: str = ""
    anthropic_api_key: str = ""
    allowed_guild_ids: set[int] = field(default_factory=set)
    allowed_user_ids: set[int] = field(default_factory=set)
    manager_model: str = "claude-fable-5"
    coder_model: str = "claude-sonnet-4-6"
    researcher_model: str = "claude-haiku-4-5-20251001"
    max_turns: int = 30
    task_budget_usd: float = 2.0
    daily_budget_usd: float = 10.0
    report_channel_id: int | None = None


def load_settings() -> Settings:
    load_env_file()
    env = os.environ
    report_raw = env.get("REPORT_CHANNEL_ID", "").strip()
    return Settings(
        discord_bot_token=env.get("DISCORD_BOT_TOKEN", ""),
        anthropic_api_key=env.get("ANTHROPIC_API_KEY", ""),
        allowed_guild_ids=parse_id_set(env.get("ALLOWED_GUILD_IDS", "")),
        allowed_user_ids=parse_id_set(env.get("ALLOWED_USER_IDS", "")),
        manager_model=env.get("MANAGER_MODEL", "claude-fable-5"),
        coder_model=env.get("CODER_MODEL", "claude-sonnet-4-6"),
        researcher_model=env.get("RESEARCHER_MODEL", "claude-haiku-4-5-20251001"),
        max_turns=int(env.get("MAX_TURNS", "30")),
        task_budget_usd=float(env.get("TASK_BUDGET_USD", "2.0")),
        daily_budget_usd=float(env.get("DAILY_BUDGET_USD", "10.0")),
        report_channel_id=int(report_raw) if report_raw.isdigit() else None,
    )
