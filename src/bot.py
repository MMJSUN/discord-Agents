"""Discord 入口：閒聊、!ping、/task 作戰計畫與批准按鈕（M1）。

啟動：python -m src.bot（需先在 .env 填 DISCORD_BOT_TOKEN / ANTHROPIC_API_KEY）。
"""

from __future__ import annotations

import logging
import time

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from discord import app_commands

from .budget import BudgetGuard
from .config import Settings, load_settings
from .runtime import ManagerRuntime, append_note, extract_note, split_message
from .store import Store

DAILY_REPORT_HOUR = 9  # SPEC §6-M3：每日 09:00 摘要

STATUS_LABELS = {
    "pending": "⏳ 待批准", "approved": "🟢 已批准", "denied": "❌ 已否決",
    "expired": "⌛ 已過期", "running": "⚔️ 執行中", "done": "✅ 完成", "failed": "🔴 失敗",
}


def compose_daily_report(tasks: list, cost_today: float, daily_budget: float) -> str:
    """每日摘要內文；純函式方便測試。"""
    if not tasks:
        body = "今日沒有任務。"
    else:
        lines = [
            f"#{t['id']} {STATUS_LABELS.get(t['status'], t['status'])}"
            f"　${t['cost_usd']:.4f}　{t['description'][:40]}"
            for t in tasks
        ]
        done = sum(1 for t in tasks if t["status"] == "done")
        body = f"共 {len(tasks)} 個任務（完成 {done}）：\n" + "\n".join(lines)
    return f"{body}\n\n💰 今日花費 ${cost_today:.4f} / 預算 ${daily_budget:.2f}"

logger = logging.getLogger(__name__)

APPROVAL_TIMEOUT_SECONDS = 3600  # 計畫卡等待批准的時效
EMBED_DESCRIPTION_LIMIT = 4000   # Discord Embed description 上限 4096，留餘裕


def _red_alert_embed(title: str, description: str) -> discord.Embed:
    """SPEC §5.4：熔斷警報統一用紅色 Embed。"""
    return discord.Embed(title=f"🔴 {title}", description=description, colour=discord.Colour.red())


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


class ApprovalView(discord.ui.View):
    """作戰計畫卡上的【✅ 批准】/【❌ 否決】按鈕（SPEC §5.1 先勝閘門）。

    批准狀態以 SQLite 為準：bot 重啟後舊按鈕失效，但資料庫裡的
    pending 任務不會被誤放行；逾時未決 → 標記 expired。
    """

    def __init__(self, bot: "CompanyBot", task_id: int, description: str):
        super().__init__(timeout=APPROVAL_TIMEOUT_SECONDS)
        self.bot = bot
        self.task_id = task_id
        self.description = description
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # edge case：白名單伺服器裡的其他人點按鈕 → 只回 ephemeral 警告
        if interaction.user.id not in self.bot.settings.allowed_user_ids:
            await interaction.response.send_message("⛔ 只有董事長能批准或否決任務。", ephemeral=True)
            return False
        return True

    def _is_still_pending(self) -> bool:
        task = self.bot.store.get_task(self.task_id)
        return bool(task) and task["status"] == "pending"

    async def _finalize(self, interaction: discord.Interaction, note: str, colour: discord.Colour) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None
        if embed:
            embed.colour = colour
            embed.set_footer(text=note)
        await interaction.response.edit_message(embed=embed, view=self)
        self.stop()

    @discord.ui.button(label="✅ 批准", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._is_still_pending():
            await interaction.response.send_message("這張計畫卡已失效（已決策或過期）。", ephemeral=True)
            return
        self.bot.store.set_status(self.task_id, "approved")
        await self._finalize(interaction, f"✅ 已批准 by {interaction.user.display_name}", discord.Colour.green())
        await self.bot.run_task(interaction.channel, self.task_id, self.description)

    @discord.ui.button(label="❌ 否決", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if not self._is_still_pending():
            await interaction.response.send_message("這張計畫卡已失效（已決策或過期）。", ephemeral=True)
            return
        self.bot.store.set_status(self.task_id, "denied")
        await self._finalize(interaction, f"❌ 已否決 by {interaction.user.display_name}", discord.Colour.red())

    async def on_timeout(self) -> None:
        if self._is_still_pending():
            self.bot.store.set_status(self.task_id, "expired")
        if self.message:
            for item in self.children:
                item.disabled = True  # type: ignore[attr-defined]
            embed = self.message.embeds[0] if self.message.embeds else None
            if embed:
                embed.colour = discord.Colour.dark_grey()
                embed.set_footer(text="⌛ 計畫卡已過期，請重新 /task")
            try:
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                logger.warning("計畫卡 #%d 過期時編輯訊息失敗", self.task_id)


class CompanyBot(discord.Client):
    def __init__(self, settings: Settings):
        intents = discord.Intents.default()
        intents.message_content = True  # 需在 Developer Portal 開啟 Message Content Intent
        super().__init__(intents=intents)
        self.settings = settings
        self.store = Store()
        self.runtime = ManagerRuntime(settings, self.store)
        self.budget = BudgetGuard(settings, self.store)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        @app_commands.command(name="task", description="把任務交給總經理：先出作戰計畫，批准後才執行")
        @app_commands.describe(content="任務描述")
        async def task_command(interaction: discord.Interaction, content: str) -> None:
            await self._handle_task(interaction, content)

        self.tree.add_command(task_command)
        for gid in self.settings.allowed_guild_ids:
            guild = discord.Object(id=gid)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)

        # SPEC §6-M3：每日 09:00 摘要（未設 REPORT_CHANNEL_ID 則跳過）
        if self.settings.report_channel_id:
            self.scheduler = AsyncIOScheduler()
            self.scheduler.add_job(self._daily_report, CronTrigger(hour=DAILY_REPORT_HOUR, minute=0))
            self.scheduler.start()
            logger.info("每日摘要排程已啟動（%02d:00 → channel %s）",
                        DAILY_REPORT_HOUR, self.settings.report_channel_id)
        else:
            logger.info("未設定 REPORT_CHANNEL_ID，跳過每日摘要排程")

    async def _daily_report(self) -> None:
        # edge case：排程觸發時斷線/頻道拿不到 → 記 log 不炸排程器，明天照常
        try:
            channel = self.get_channel(self.settings.report_channel_id) or await self.fetch_channel(
                self.settings.report_channel_id
            )
            body = compose_daily_report(
                self.store.tasks_today(), self.store.cost_today(), self.settings.daily_budget_usd
            )
            await channel.send(embed=discord.Embed(
                title="📊 一人公司每日摘要", description=body, colour=discord.Colour.blurple(),
            ))
        except Exception:
            logger.exception("每日摘要發送失敗，明日照常重試")

    async def on_ready(self) -> None:
        logger.info("一人公司開張：%s (id=%s)", self.user, self.user.id if self.user else "?")

    # ---------- 閒聊（玩法 A） ----------

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        guild_id = message.guild.id if message.guild else None
        if not is_authorized(
            guild_id, message.author.id,
            self.settings.allowed_guild_ids, self.settings.allowed_user_ids,
        ):
            return

        if message.content.strip() == "!ping":
            latency_ms = round(self.latency * 1000)
            await message.channel.send(f"pong 🏓（{latency_ms} ms）")
            return

        if message.content.strip() == "!reset":
            self.store.clear_session(message.channel.id)
            await message.channel.send(
                "🧹 本頻道 session 已歸零：總經理忘掉這裡的對話史，"
                "下一句話重新開始（換新任務前用這招可大幅省 token）。"
            )
            return

        if not message.content.strip():
            return  # 純附件／貼圖等空文字訊息不送 Manager

        async def notify(text: str) -> None:
            await message.channel.send(text)

        started = time.monotonic()
        try:
            async with message.channel.typing():
                reply = await self.runtime.ask(message.channel.id, message.content, notify)
        except Exception:
            logger.exception("Manager 回覆失敗")
            await message.channel.send("⚠️ 總經理暫時聯絡不上（API 重試 3 次後仍失敗），請稍後再試。")
            return

        elapsed = time.monotonic() - started
        logger.info(
            "Manager 回覆 channel=%s 耗時 %.1fs 成本 $%.4f",
            message.channel.id, elapsed, reply.cost_usd,
        )
        for chunk in split_message(reply.text) or ["（總經理沒有話要說）"]:
            await message.channel.send(chunk)

    # ---------- /task 先勝流程 ----------

    async def _handle_task(self, interaction: discord.Interaction, content: str) -> None:
        if not is_authorized(
            interaction.guild_id, interaction.user.id,
            self.settings.allowed_guild_ids, self.settings.allowed_user_ids,
        ):
            await interaction.response.send_message("⛔ 未授權。", ephemeral=True)
            return
        if not content.strip():
            await interaction.response.send_message("任務描述不能是空的。", ephemeral=True)
            return

        # SPEC §5.4：每日預算用罄 → 唯讀模式（可聊天、拒 /task）
        readonly_reason = self.budget.check_can_start_task()
        if readonly_reason:
            await interaction.response.send_message(
                embed=_red_alert_embed("每日預算熔斷", readonly_reason)
            )
            return

        await interaction.response.defer(thinking=True)
        task_id = self.store.create_task(interaction.channel_id, content)
        try:
            plan = await self.runtime.plan_task(interaction.channel_id, task_id, content)
        except Exception:
            logger.exception("擬定作戰計畫失敗 task=%d", task_id)
            self.store.set_status(task_id, "failed")
            await interaction.followup.send(f"⚠️ 任務 #{task_id} 擬定計畫失敗，請稍後重試。")
            return

        self.store.set_plan(task_id, plan.text)
        description_text = plan.text or "（Manager 未產出計畫內容）"
        if len(description_text) > EMBED_DESCRIPTION_LIMIT:
            description_text = description_text[:EMBED_DESCRIPTION_LIMIT] + "\n…（全文已存入任務紀錄）"
        embed = discord.Embed(
            title=f"📋 作戰計畫 #{task_id}",
            description=description_text,
            colour=discord.Colour.orange(),
        )
        embed.set_footer(
            text=f"任務：{content[:80]}｜規劃成本 ${plan.cost_usd:.4f}｜等待董事長批准（{APPROVAL_TIMEOUT_SECONDS // 60} 分鐘內有效）"
        )
        view = ApprovalView(self, task_id, content)
        view.message = await interaction.followup.send(embed=embed, view=view)

    async def run_task(self, channel: discord.abc.Messageable, task_id: int, description: str) -> None:
        # 開跑前再驗一次預算（批准可能發生在預算燒完之後）
        readonly_reason = self.budget.check_can_start_task()
        if readonly_reason:
            self.store.set_status(task_id, "failed")
            await channel.send(embed=_red_alert_embed("每日預算熔斷", readonly_reason))
            return
        run_budget = self.budget.max_budget_for_run(task_id)
        if run_budget <= 0:
            self.store.set_status(task_id, "failed")
            await channel.send(embed=_red_alert_embed(
                "任務預算熔斷",
                f"任務 #{task_id} 預算已用罄（已花 ${self.budget.task_spent(task_id):.4f} / "
                f"上限 ${self.settings.task_budget_usd:.2f}）。",
            ))
            return

        self.store.set_status(task_id, "running")
        await channel.send(f"⚔️ 任務 #{task_id} 開始執行（本輪成本上限 ${run_budget:.2f}）……")

        async def notify(text: str) -> None:
            await channel.send(text)

        channel_id = getattr(channel, "id", 0)
        try:
            reply = await self.runtime.execute_task(
                channel_id, task_id, description, notify,
                max_budget_usd=run_budget, progress=notify,
            )
        except Exception:
            logger.exception("任務執行失敗 task=%d", task_id)
            self.store.set_status(task_id, "failed")
            await channel.send(f"🔴 任務 #{task_id} 執行失敗（API 重試 3 次後仍失敗）。")
            return

        for chunk in split_message(reply.text) or ["（任務完成，但 Manager 沒有輸出摘要）"]:
            await channel.send(chunk)

        # SPEC §6-M3：任務結束自動追加踩坑筆記（Manager 回報末段的 📝 段落）
        note = extract_note(reply.text)
        try:
            await append_note(
                task_id, description,
                note or f"（Manager 未產出筆記）狀態見任務紀錄，subtype={reply.subtype}",
            )
        except OSError:
            logger.exception("踩坑筆記寫入失敗 task=%d", task_id)

        task = self.store.get_task(task_id)
        task_cost = task["cost_usd"] if task else 0.0
        breaker = self.budget.describe_breaker(reply.subtype)
        if breaker:
            # SPEC §5.4：任一熔斷觸發 → 紅色警報 Embed（含已花費金額與進度）
            self.store.set_status(task_id, "failed")
            await channel.send(embed=_red_alert_embed(
                "熔斷觸發",
                f"任務 #{task_id}：{breaker}\n"
                f"已執行 {reply.num_turns} 回合，本次花費 ${reply.cost_usd:.4f}，"
                f"任務累計 ${task_cost:.4f}。\n上方訊息是中止前的最後進度。",
            ))
        else:
            self.store.set_status(task_id, "done")
        await channel.send(
            f"💰 任務 #{task_id} 成本 ${task_cost:.4f}｜今日累計 ${self.store.cost_today():.4f}"
        )


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
