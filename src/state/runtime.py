"""进程级运行态：跨周期共享的内存状态。

只保存「易变、与交易所对账后可重建」的运行态，不持久化（持久化交给 store）。
设计为可单测：所有变更都是普通方法，不触碰 IO。

包含：
- 每 symbol 的上次决策价/时间、连续跳过计数
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
    # 连续跳过 LLM 的次数（达到 max_skip_cycles 兜底强制触发）
    skip_count: dict[str, int] = field(default_factory=dict)
    # 待消费的挂单事件标志（成交/撤单/状态变化）
    _order_events: dict[str, bool] = field(default_factory=dict)

    # 当日已实现盈亏累计（USDT，亏损为负）；日界用 day_key 重置
    day_realized_pnl: float = 0.0
    day_key: str = ""
    # 账户权益峰值、当前权益与回撤(%)
    equity_peak: float = 0.0
    current_equity: float = 0.0
    drawdown_pct: float = 0.0

    # 熔断/停机标志
    halt_new_entries: bool = False
    kill_switch: bool = False

    # 最近一次已知持仓快照（symbol → ccxt position dict）
    positions: dict[str, dict] = field(default_factory=dict)
    # 启动对账恢复的未完成挂单（symbol → list[ccxt order dict]）
    open_orders: dict[str, list[dict]] = field(default_factory=dict)

    # ---------- 决策记账 ----------
    def record_decision(self, symbol: str, price: float, ts_ms: int | None = None) -> None:
        self.last_decision_price[symbol] = price
        self.last_decision_time[symbol] = ts_ms or int(time.time() * 1000)
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
        """跨自然日时重置当日盈亏。返回是否发生了滚动。"""
        key = time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))
        if key != self.day_key:
            self.day_key = key
            self.day_realized_pnl = 0.0
            return True
        return False

    def add_realized_pnl(self, pnl: float) -> None:
        self.day_realized_pnl += pnl

    def update_equity(self, equity: float) -> None:
        """更新当前权益、峰值并计算回撤百分比。"""
        self.current_equity = equity
        if equity > self.equity_peak:
            self.equity_peak = equity
        if self.equity_peak > 0:
            self.drawdown_pct = max(0.0, (self.equity_peak - equity) / self.equity_peak * 100.0)

    # ---------- 熔断 ----------
    def trip_breaker(self) -> None:
        self.halt_new_entries = True

    def trigger_kill(self) -> None:
        self.kill_switch = True
