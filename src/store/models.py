"""SQLAlchemy ORM 表定义：决策 / 拒单 / 订单 / 持仓快照 / 余额快照。

用 async SQLAlchemy 2.0 + aiosqlite。所有时间戳存毫秒 epoch（int），
便于与交易所数据对齐；额外存一个可读 created_at 文本。

注意：决策日志同时覆盖「跳过 LLM(skipped=True)」与「实际决策」两类记录，
满足 SPEC「决策日志含是否跳过 LLM 及原因」。
"""
from __future__ import annotations

import time

from sqlalchemy import Boolean, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


class Base(DeclarativeBase):
    pass


class DecisionRow(Base):
    """每周期每 symbol 一条：跳过或实际决策都记录。"""
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(Integer, default=_now_ms, index=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now_iso)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    skipped: Mapped[bool] = mapped_column(Boolean, default=False)
    skip_reason: Mapped[str] = mapped_column(String(200), default="")

    # 实际决策字段（skipped=True 时多为默认值）
    action: Mapped[str] = mapped_column(String(16), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    size_pct: Mapped[float] = mapped_column(Float, default=0.0)
    leverage: Mapped[int] = mapped_column(Integer, default=0)
    stop_loss_pct: Mapped[float] = mapped_column(Float, default=0.0)
    take_profit_pct: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(String(500), default="")

    # 审计：完整输入上下文 JSON（便于复盘）
    context_json: Mapped[str] = mapped_column(Text, default="")
    # 决策时的参考价
    ref_price: Mapped[float] = mapped_column(Float, default=0.0)


class RejectRow(Base):
    """风控/精度拒单记录。"""
    __tablename__ = "rejects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(Integer, default=_now_ms, index=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now_iso)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    code: Mapped[str] = mapped_column(String(32), default="")
    reason: Mapped[str] = mapped_column(String(300), default="")
    action: Mapped[str] = mapped_column(String(16), default="")
    leverage: Mapped[int] = mapped_column(Integer, default=0)
    size_pct: Mapped[float] = mapped_column(Float, default=0.0)


class OrderRow(Base):
    """下单结果（含 dry-run）。"""
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(Integer, default=_now_ms, index=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now_iso)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    client_kind: Mapped[str] = mapped_column(String(16), default="")  # OPEN/CLOSE/SL/TP
    side: Mapped[str] = mapped_column(String(8), default="")          # buy/sell
    order_type: Mapped[str] = mapped_column(String(16), default="")
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    notional: Mapped[float] = mapped_column(Float, default=0.0)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(24), default="")
    exchange_order_id: Mapped[str] = mapped_column(String(64), default="")
    raw_json: Mapped[str] = mapped_column(Text, default="")


class OpenOrderRow(Base):
    """启动对账时从交易所拉取的未完成挂单快照（用于恢复 SL/TP 等挂单）。"""
    __tablename__ = "open_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(Integer, default=_now_ms, index=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now_iso)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    exchange_order_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    order_type: Mapped[str] = mapped_column(String(24), default="")
    side: Mapped[str] = mapped_column(String(8), default="")
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    stop_price: Mapped[float] = mapped_column(Float, default=0.0)
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24), default="")
    raw_json: Mapped[str] = mapped_column(Text, default="")


class PositionSnapshotRow(Base):
    """周期性持仓快照。"""
    __tablename__ = "position_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(Integer, default=_now_ms, index=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now_iso)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    side: Mapped[str] = mapped_column(String(8), default="")
    contracts: Mapped[float] = mapped_column(Float, default=0.0)
    entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    mark_price: Mapped[float] = mapped_column(Float, default=0.0)
    leverage: Mapped[int] = mapped_column(Integer, default=0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    notional: Mapped[float] = mapped_column(Float, default=0.0)


class BalanceSnapshotRow(Base):
    """周期性余额快照。"""
    __tablename__ = "balance_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(Integer, default=_now_ms, index=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now_iso)
    quote_asset: Mapped[str] = mapped_column(String(10), default="USDT")
    total_equity: Mapped[float] = mapped_column(Float, default=0.0)
    available_margin: Mapped[float] = mapped_column(Float, default=0.0)
    day_realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    drawdown_pct: Mapped[float] = mapped_column(Float, default=0.0)


class RuntimeSettingRow(Base):
    """运行时设置。

    用于保存 Web 命令修改的有效运行态，避免交易进程、Web 进程和重启后的配置
    各自显示不同状态。
    """
    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[str] = mapped_column(String(32), default=_now_iso)


class ControlCommandRow(Base):
    """Web 操作面板下发的命令队列。

    交易主进程每周期轮询 status='pending' 的命令并执行，执行后置 done/failed。
    解耦设计：web 进程只写命令，绝不直接碰交易所；命令由交易进程串行消费，
    不会与主循环状态打架。延迟上限为一个周期。
    """
    __tablename__ = "control_commands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(Integer, default=_now_ms, index=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now_iso)
    command: Mapped[str] = mapped_column(String(32), index=True)  # PAUSE/RESUME/SET_DRY_RUN/etc.
    arg: Mapped[str] = mapped_column(String(64), default="")       # 命令参数(如 dry_run 的 true/false)
    source: Mapped[str] = mapped_column(String(32), default="web")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending/done/failed
    result: Mapped[str] = mapped_column(String(300), default="")
    executed_at: Mapped[str] = mapped_column(String(32), default="")
