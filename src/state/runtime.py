"""进程级运行态：跨周期共享的内存状态。

只保存「易变、与交易所对账后可重建」的运行态，不持久化（持久化交给 store）。
设计为可单测：所有变更都是普通方法，不触碰 IO。

包含：
- 每 symbol 的上次决策价/时间/特征快照、连续跳过计数
- 挂单事件队列（成交/状态变化 → 下周期触发 LLM）
- 当日已实现盈亏累计、账户回撤、权益峰值
- 熔断标志 halt_new_entries、全局 kill_switch
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class RuntimeState:
    """主循环共享的运行态。线程/任务内串行访问（asyncio 单线程）。"""

    # 上次触发 LLM 决策时的价格与时间（毫秒），用于 throttle 判定
    last_decision_price: dict[str, float] = field(default_factory=dict)
    last_decision_time: dict[str, int] = field(default_factory=dict)
    last_decision_snapshot: dict[str, dict] = field(default_factory=dict)
    # 连续跳过 LLM 的次数（达到 max_skip_cycles 兜底强制触发）
    skip_count: dict[str, int] = field(default_factory=dict)
    # 待消费的挂单事件标志（成交/撤单/状态变化）
    _order_events: dict[str, bool] = field(default_factory=dict)

    # 当日已实现盈亏累计（USDT，亏损为负）；日界用 day_key 重置
    day_realized_pnl: float = 0.0
    day_key: str = ""
    # 账户历史权益峰值、当前权益与历史回撤(%)
    equity_peak: float = 0.0
    current_equity: float = 0.0
    drawdown_pct: float = 0.0
    # 自然日风险周期：当日峰值与当日回撤用于 max_drawdown_pct 熔断。
    risk_day_key: str = ""
    risk_day_equity_peak: float = 0.0
    risk_day_drawdown_pct: float = 0.0
    # 风控权益剔除当日外部净资金流，避免充值/转出被当作交易损益。
    risk_equity: float = 0.0
    day_net_capital_flow: float = 0.0
    capital_flow_status: str = "CONFIRMED"
    capital_flow_guard_since_ms: int = 0
    capital_flow_ledger_status: str = "CONFIRMED"
    # 人工确认恢复后，仅在该自然日跳过“回撤”熔断；日亏熔断仍有效。
    drawdown_bypass_day: str = ""

    # 熔断/停机标志
    halt_new_entries: bool = False
    halt_new_entries_reason: str = ""
    kill_switch: bool = False

    # 最近一次已知持仓快照（symbol → ccxt position dict）
    positions: dict[str, dict] = field(default_factory=dict)
    # 启动对账恢复的未完成挂单（symbol → list[ccxt order dict]）
    open_orders: dict[str, list[dict]] = field(default_factory=dict)

    # ---------- 决策记账 ----------
    def record_decision(
        self,
        symbol: str,
        price: float,
        ts_ms: int | None = None,
        feature_snapshot: dict | None = None,
    ) -> None:
        self.last_decision_price[symbol] = price
        self.last_decision_time[symbol] = ts_ms or int(time.time() * 1000)
        if feature_snapshot is not None:
            self.last_decision_snapshot[symbol] = feature_snapshot
        self.skip_count[symbol] = 0

    def record_skip(self, symbol: str) -> int:
        self.skip_count[symbol] = self.skip_count.get(symbol, 0) + 1
        return self.skip_count[symbol]

    # ---------- 挂单事件 ----------
    def mark_order_event(self, symbol: str) -> None:
        self._order_events[symbol] = True

    def pop_order_event(self, symbol: str) -> bool:
        """取出并清除该 symbol 的挂单事件标志。"""
        return self._order_events.pop(symbol, False)

    # ---------- 盈亏 / 回撤 ----------
    def roll_day_if_needed(self, now: float | None = None) -> bool:
        """跨自然日时重置当日盈亏（按本地时区，凌晨 0:00 滚动）。返回是否发生了滚动。"""
        key = time.strftime("%Y-%m-%d", time.localtime(
            now if now is not None else time.time()
        ))
        if key != self.day_key:
            self.day_key = key
            self.day_realized_pnl = 0.0
            self.risk_day_key = key
            self.risk_day_equity_peak = max(self.current_equity, 0.0)
            self.risk_day_drawdown_pct = 0.0
            self.risk_equity = max(self.current_equity, 0.0)
            self.day_net_capital_flow = 0.0
            self.capital_flow_status = "CONFIRMED"
            self.capital_flow_ledger_status = "CONFIRMED"
            self.capital_flow_guard_since_ms = 0
            self.drawdown_bypass_day = ""
            return True
        return False

    def add_realized_pnl(self, pnl: float) -> None:
        self.day_realized_pnl += pnl

    def rehydrate_day_pnl(self, by_day: dict[str, float], now: float | None = None) -> None:
        """从 DB 拉到的 {YYYY-MM-DD: pnl} 重算 day_key 与 day_realized_pnl。

        启动时调用一次，把"今天"按本地日界对齐到 DB 真实值，避免重启后
        当日盈亏清零、日亏熔断失真、前端"+0.00"假象。by_day 为空时仅
        初始化 day_key。
        """
        key = time.strftime("%Y-%m-%d", time.localtime(
            now if now is not None else time.time()
        ))
        self.day_key = key
        self.day_realized_pnl = float(by_day.get(key, 0.0))

    def update_equity(
        self,
        equity: float,
        now: float | None = None,
        *,
        net_capital_flow: float | None = None,
        capital_flow_status: str | None = None,
    ) -> None:
        """更新历史与当日峰值，并分别计算历史回撤和当日风控回撤。"""
        key = time.strftime("%Y-%m-%d", time.localtime(
            now if now is not None else time.time()
        ))
        self.current_equity = equity
        if net_capital_flow is not None:
            self.day_net_capital_flow = float(net_capital_flow)
        if capital_flow_status:
            self.capital_flow_status = str(capital_flow_status)
            if capital_flow_status != "RECONCILING":
                self.capital_flow_ledger_status = str(capital_flow_status)
        self.risk_equity = max(equity - self.day_net_capital_flow, 0.0)
        if equity > self.equity_peak:
            self.equity_peak = equity
        if self.equity_peak > 0:
            self.drawdown_pct = max(0.0, (self.equity_peak - equity) / self.equity_peak * 100.0)
        if key != self.risk_day_key:
            self.risk_day_key = key
            self.risk_day_equity_peak = self.risk_equity
            self.risk_day_drawdown_pct = 0.0
            self.drawdown_bypass_day = ""
            self.capital_flow_guard_since_ms = 0
        elif self.risk_equity > self.risk_day_equity_peak:
            self.risk_day_equity_peak = self.risk_equity
        if self.risk_day_equity_peak > 0:
            self.risk_day_drawdown_pct = max(
                0.0,
                (self.risk_day_equity_peak - self.risk_equity)
                / self.risk_day_equity_peak
                * 100.0,
            )

    def restore_daily_risk(
        self,
        *,
        day_key: str,
        equity_peak: float,
        bypass_day: str = "",
        net_capital_flow: float = 0.0,
        capital_flow_status: str = "CONFIRMED",
        now: float | None = None,
    ) -> None:
        """Restore only today's persisted risk cycle; stale days are discarded."""
        today = time.strftime("%Y-%m-%d", time.localtime(
            now if now is not None else time.time()
        ))
        self.risk_day_key = today
        self.risk_day_equity_peak = max(float(equity_peak or 0.0), 0.0) if day_key == today else 0.0
        self.risk_day_drawdown_pct = 0.0
        self.day_net_capital_flow = (
            float(net_capital_flow or 0.0) if day_key == today else 0.0
        )
        self.capital_flow_status = str(capital_flow_status or "CONFIRMED")
        self.capital_flow_ledger_status = self.capital_flow_status
        self.risk_equity = max(self.current_equity - self.day_net_capital_flow, 0.0)
        self.drawdown_bypass_day = today if bypass_day == today else ""

    def grant_drawdown_bypass(self, now: float | None = None) -> str:
        key = time.strftime("%Y-%m-%d", time.localtime(
            now if now is not None else time.time()
        ))
        self.drawdown_bypass_day = key
        return key

    def drawdown_bypass_active(self, now: float | None = None) -> bool:
        key = time.strftime("%Y-%m-%d", time.localtime(
            now if now is not None else time.time()
        ))
        return bool(self.drawdown_bypass_day and self.drawdown_bypass_day == key)

    # ---------- 熔断 ----------
    def halt_entries(self, reason: str = "") -> None:
        self.halt_new_entries = True
        self.halt_new_entries_reason = reason.strip()

    def resume_entries(self) -> None:
        self.halt_new_entries = False
        self.halt_new_entries_reason = ""

    def trip_breaker(self, reason: str = "") -> None:
        detail = reason.strip()
        if detail and not detail.lower().startswith("circuit breaker"):
            detail = f"circuit breaker: {detail}"
        self.halt_entries(detail or "circuit breaker")

    def trigger_kill(self) -> None:
        self.kill_switch = True
        self.halt_entries("kill switch active")
