"""熔斷器：單任務成本上限、每日總額（SPEC §5.4 Loop Engineering）。

策略是「事前算好、事後對帳」的雙保險：
- 事前：每次執行前算出「這一輪最多可花多少」= min(任務剩餘, 每日剩餘)，
  餵給 SDK 的 max_budget_usd，由 agent loop 原生中止（subtype=error_max_budget_usd）。
- 事後：ResultMessage 的實際成本入帳 SQLite；每日總額以本地日期累計，
  隔日 00:00 自然重置（cost_today 只算今天的紀錄）。
"""

from __future__ import annotations

from .config import Settings
from .store import Store

# ResultMessage.subtype → 熔斷說明（SPEC §5.4：任一熔斷觸發 → 紅色警報）
BREAKER_SUBTYPES = {
    "error_max_budget_usd": "成本熔斷：達到本輪執行的成本上限",
    "error_max_turns": "回合熔斷：達到 MAX_TURNS 回合上限",
}

MIN_RUN_BUDGET_USD = 0.001  # 低於這個值視為預算用罄，直接拒絕開跑


class BudgetGuard:
    def __init__(self, settings: Settings, store: Store):
        self.settings = settings
        self.store = store

    # ---------- 每日總額 ----------

    def daily_spent(self) -> float:
        return self.store.cost_today()

    def daily_remaining(self) -> float:
        return self.settings.daily_budget_usd - self.daily_spent()

    def daily_exceeded(self) -> bool:
        return self.daily_remaining() <= 0

    def check_can_start_task(self) -> str | None:
        """回傳拒絕原因；None = 可以開跑。每日超額 → 唯讀模式（可聊天、拒 /task）。"""
        if self.daily_exceeded():
            return (
                f"每日預算已用罄（${self.daily_spent():.2f} / "
                f"${self.settings.daily_budget_usd:.2f}），bot 進入唯讀模式："
                f"可以聊天，但 /task 暫停受理，隔日 00:00 自動重置。"
            )
        return None

    # ---------- 單任務 ----------

    def task_spent(self, task_id: int) -> float:
        task = self.store.get_task(task_id)
        return float(task["cost_usd"]) if task else 0.0

    def task_remaining(self, task_id: int) -> float:
        return self.settings.task_budget_usd - self.task_spent(task_id)

    def max_budget_for_run(self, task_id: int) -> float:
        """本輪執行可花的上限 = min(任務剩餘, 每日剩餘)；用罄回傳 0.0。"""
        remaining = min(self.task_remaining(task_id), self.daily_remaining())
        return remaining if remaining >= MIN_RUN_BUDGET_USD else 0.0

    # ---------- 熔斷判讀 ----------

    @staticmethod
    def describe_breaker(subtype: str | None) -> str | None:
        """ResultMessage.subtype 若屬熔斷，回傳中文說明；否則 None。"""
        return BREAKER_SUBTYPES.get(subtype or "")
