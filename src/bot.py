"""Discord 入口：bot 上線、!ping、任一訊息交給 Manager 回覆（M0）。

啟動：python -m src.bot（需先在 .env 填 DISCORD_BOT_TOKEN / ANTHROPIC_API_KEY）。
"""

from __future__ import annotations

import logging
import time

import discord

from .config import Settings, load_settings
from .runtime import ManagerRuntime, split_message

logger = logging.getLogger(__name__)


def is_authorized(
    guild_id: int | None,
    author_id: int,
    allowed_guild_ids: set[int],
    allowed_user_ids: set[int],
) -> bool:
    """SPEC §5.3：僅回應白名單 guild 內的白名單使用者，其餘靜默忽略（含 DM）。"""
    if guild_id is None or guild_id not in allowed_guild_ids:
        return False
    return author_id in allowed_user_ids


class CompanyBot(discord.Client):
    def __init__(self, settings: Settings):
        intents = discord.Intents.default()
        intents.message_content = True  # 需在 Developer Portal 開啟 Message Content Intent
        super().__init__(intents=intents)
        self.settings = settings
        self.runtime = ManagerRuntime(settings)

    async def on_ready(self) -> None:
        logger.info("一人公司開張：%s (id=%s)", self.user, self.user.id if self.user else "?")

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        guild_id = message.guild.id if message.guild else None
        if not is_authorized(
            guild_id,
            message.author.id,
            self.settings.allowed_guild_ids,
            self.settings.allowed_user_ids,
        ):
            return

        if message.content.strip() == "!ping":
            latency_ms = round(self.latency * 1000)
            await message.channel.send(f"pong 🏓（{latency_ms} ms）")
            return

        if not message.content.strip():
            return  # 純附件／貼圖等空文字訊息不送 Manager

        started = time.monotonic()
        try:
            async with message.channel.typing():
                reply = await self.runtime.ask(message.channel.id, message.content)
        except Exception:
            logger.exception("Manager 回覆失敗")
            await message.channel.send("⚠️ 總經理暫時聯絡不上（API 重試 3 次後仍失敗），請稍後再試。")
            return

        elapsed = time.monotonic() - started
        logger.info(
            "Manager 回覆 channel=%s 耗時 %.1fs 成本 $%.4f",
            message.channel.id, elapsed, reply.cost_usd,
        )
        chunks = split_message(reply.text) or ["（總經理沒有話要說）"]
        for chunk in chunks:
            await message.channel.send(chunk)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    if not settings.discord_bot_token:
        raise SystemExit("缺 DISCORD_BOT_TOKEN：請複製 .env.example 為 .env 並填入憑證。")
    if not settings.anthropic_api_key:
        raise SystemExit("缺 ANTHROPIC_API_KEY：只允許計量付費 API key（禁止 OAuth 權杖）。")
    if not settings.allowed_guild_ids or not settings.allowed_user_ids:
        raise SystemExit("缺白名單：請在 .env 填 ALLOWED_GUILD_IDS 與 ALLOWED_USER_IDS。")
    CompanyBot(settings).run(settings.discord_bot_token)


if __name__ == "__main__":
    main()
