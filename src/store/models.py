"""SQLAlchemy ORM 表定义：决策 / 拒单 / 订单 / 持仓快照 / 余额快照。

用 async SQLAlchemy 2.0 + aiosqlite。所有时间戳存毫秒 epoch（int），
便于与交易所数据对齐；额外存一个可读 created_at 文本。

注意：决策日志同时覆盖「跳过 LLM(skipped=True)」与「实际决策」两类记录，
满足 SPEC「决策日志含是否跳过 LLM 及原因」。
"""
from __future__ import annotations

import time

from sqlalchemy import Boolean, Float, Integer, String, Text, UniqueConstraint
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
    reason: Mapped[str] = mapped_column(String(1000), default="")

    # 审计：完整输入上下文 JSON（便于复盘）
    context_json: Mapped[str] = mapped_column(Text, default="")
    # LLM 审计：真实 prompt、请求载荷、原始回传（不包含 API key）
    llm_system_prompt: Mapped[str] = mapped_column(Text, default="")
    llm_prompt: Mapped[str] = mapped_column(Text, default="")
    llm_request_json: Mapped[str] = mapped_column(Text, default="")
    llm_response_json: Mapped[str] = mapped_column(Text, default="")
    # LLM 调用耗时与状态：0 表示未采集
    llm_latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    llm_attempts: Mapped[int] = mapped_column(Integer, default=0)
    llm_status: Mapped[str] = mapped_column(String(16), default="")
    llm_error: Mapped[str] = mapped_column(String(200), default="")
    feature_snapshot_json: Mapped[str] = mapped_column(Text, default="")
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
    """下单结果。dry_run 字段仅保留用于兼容旧库。"""
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
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(24), default="")
    exchange_order_id: Mapped[str] = mapped_column(String(64), default="")
    raw_json: Mapped[str] = mapped_column(Text, default="")
    trade_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    trade_role: Mapped[str] = mapped_column(String(24), default="")
    leverage: Mapped[int] = mapped_column(Integer, default=0)
    margin: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    execution_mode: Mapped[str] = mapped_column(String(24), default="")
    time_in_force: Mapped[str] = mapped_column(String(12), default="")
    requested_qty: Mapped[float] = mapped_column(Float, default=0.0)
    filled_qty: Mapped[float] = mapped_column(Float, default=0.0)
    remaining_qty: Mapped[float] = mapped_column(Float, default=0.0)
    requested_price: Mapped[float] = mapped_column(Float, default=0.0)
    limit_price: Mapped[float] = mapped_column(Float, default=0.0)
    avg_price: Mapped[float] = mapped_column(Float, default=0.0)
    liquidity: Mapped[str] = mapped_column(String(12), default="")
    fee: Mapped[float] = mapped_column(Float, default=0.0)
    fee_asset: Mapped[str] = mapped_column(String(16), default="")
    client_order_id: Mapped[str] = mapped_column(String(64), default="")


class TradeRow(Base):
    """一笔完整交易/仓位生命周期，由开仓、保护单、退出单聚合而成。"""
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(Integer, default=_now_ms, index=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now_iso)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    direction: Mapped[str] = mapped_column(String(8), default="")      # long/short
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)

    opened_at_ms: Mapped[int] = mapped_column(Integer, default=0, index=True)
    opened_at: Mapped[str] = mapped_column(String(32), default="")
    closed_at_ms: Mapped[int] = mapped_column(Integer, default=0, index=True)
    closed_at: Mapped[str] = mapped_column(String(32), default="")

    entry_order_id: Mapped[int] = mapped_column(Integer, default=0)
    exit_order_id: Mapped[int] = mapped_column(Integer, default=0)
    entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    exit_price: Mapped[float] = mapped_column(Float, default=0.0)
    qty_opened: Mapped[float] = mapped_column(Float, default=0.0)
    qty_closed: Mapped[float] = mapped_column(Float, default=0.0)

    leverage: Mapped[int] = mapped_column(Integer, default=0)
    entry_notional: Mapped[float] = mapped_column(Float, default=0.0)
    entry_margin: Mapped[float] = mapped_column(Float, default=0.0)
    exit_notional: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    pnl_pct_on_margin: Mapped[float] = mapped_column(Float, default=0.0)
    entry_fee: Mapped[float] = mapped_column(Float, default=0.0)
    exit_fee: Mapped[float] = mapped_column(Float, default=0.0)
    total_fee: Mapped[float] = mapped_column(Float, default=0.0)
    gross_realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    net_realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    net_pnl_pct_on_margin: Mapped[float] = mapped_column(Float, default=0.0)
    entry_liquidity: Mapped[str] = mapped_column(String(12), default="")
    exit_liquidity: Mapped[str] = mapped_column(String(12), default="")

    exit_reason: Mapped[str] = mapped_column(String(24), default="")
    source: Mapped[str] = mapped_column(String(16), default="live")
    confidence: Mapped[str] = mapped_column(String(16), default="exact")


class PositionClaimRow(Base):
    """开仓中的仓位所有权声明，避免交易所成交早于本地 trade 落库时被误判。"""
    __tablename__ = "position_claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(Integer, default=_now_ms, index=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String(32), default=_now_iso)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    side: Mapped[str] = mapped_column(String(8), default="", index=True)  # long/short
    status: Mapped[str] = mapped_column(String(24), default="opening", index=True)
    source: Mapped[str] = mapped_column(String(16), default="strategy")
    planned_qty: Mapped[float] = mapped_column(Float, default=0.0)
    filled_qty: Mapped[float] = mapped_column(Float, default=0.0)
    entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    client_order_id: Mapped[str] = mapped_column(String(64), default="")
    expires_at_ms: Mapped[int] = mapped_column(Integer, default=0, index=True)
    reason: Mapped[str] = mapped_column(String(240), default="")
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
    initial_margin: Mapped[float] = mapped_column(Float, default=0.0)
    isolated_margin: Mapped[float] = mapped_column(Float, default=0.0)
    maintenance_margin: Mapped[float] = mapped_column(Float, default=0.0)
    roi_pct: Mapped[float] = mapped_column(Float, default=0.0)
    liquidation_price: Mapped[float] = mapped_column(Float, default=0.0)
    margin_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    margin_mode: Mapped[str] = mapped_column(String(16), default="")


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


class SymbolRow(Base):
    """动态交易币种注册表。

    业务表不按币种拆表，仍通过各表已有 symbol 字段关联；这里仅保存币种是否纳入
    当前交易环境、是否允许策略交易，以及交易所预检/过滤器结果。
    """
    __tablename__ = "symbols"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    sync_status: Mapped[str] = mapped_column(String(32), default="new", index=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    source: Mapped[str] = mapped_column(String(16), default="web")
    min_qty: Mapped[float] = mapped_column(Float, default=0.0)
    min_notional: Mapped[float] = mapped_column(Float, default=0.0)
    tick_size: Mapped[float] = mapped_column(Float, default=0.0)
    step_size: Mapped[float] = mapped_column(Float, default=0.0)
    raw_filters_json: Mapped[str] = mapped_column(Text, default="")
    exchange_state_json: Mapped[str] = mapped_column(Text, default="")
    disabled_reason_code: Mapped[str] = mapped_column(String(64), default="")
    disabled_reason: Mapped[str] = mapped_column(Text, default="")
    disabled_at: Mapped[str] = mapped_column(String(32), default="")
    disabled_source: Mapped[str] = mapped_column(String(32), default="")
    disabled_action: Mapped[str] = mapped_column(String(32), default="")
    last_enabled_at: Mapped[str] = mapped_column(String(32), default="")
    added_at: Mapped[str] = mapped_column(String(32), default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String(32), default=_now_iso)
    last_filter_sync_at: Mapped[str] = mapped_column(String(32), default="")


class RuntimeSettingRow(Base):
    """运行时设置。

    用于保存 Web 命令修改的有效运行态，避免交易进程、Web 进程和重启后的配置
    各自显示不同状态。
    """
    __tablename__ = "runtime_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[str] = mapped_column(String(32), default=_now_iso)


class LLMProfileRow(Base):
    """运行期可热替换的 LLM 对接源 profile 表。

    - ``api_key`` 明文存库（单租户自托管，DB 文件权限即边界；迁移=直接拷 sqlite）。
    - 同一时刻 ``is_active`` 只能有 1 个（主源/链头），由 Store 写事务保证。
    - ``priority`` 升序决定 fallback 链尝试顺序；``fallback_enabled`` 标记备源是否入链。
    """
    __tablename__ = "llm_profiles"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), default="anthropic")
    model: Mapped[str] = mapped_column(String(128), default="")
    base_url: Mapped[str] = mapped_column(String(256), default="")
    timeout: Mapped[float] = mapped_column(Float, default=60.0)
    max_tokens: Mapped[int] = mapped_column(Integer, default=1024)
    max_retries: Mapped[int] = mapped_column(Integer, default=2)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # API key 明文（不再走 keyring）。旧库遗留的 keyring_ref 列保留但不再读写。
    api_key: Mapped[str] = mapped_column(Text, default="")
    keyring_ref: Mapped[str] = mapped_column(String(200), default="")
    # fallback 链：priority 升序优先；fallback_enabled 决定备源是否入链。
    priority: Mapped[int] = mapped_column(Integer, default=100, index=True)
    fallback_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[str] = mapped_column(String(32), default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String(32), default=_now_iso)


class LLMPromptVersionRow(Base):
    """运行期可热替换的 Prompt 模板版本。

    旧版本只保存 ``content`` 作为附加指令；新版本可保存完整 System/User 模板。
    同一时刻 ``is_active`` 只能有 1 个，由 Store 写事务保证。
    """
    __tablename__ = "llm_prompt_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version: Mapped[int] = mapped_column(Integer, default=1, index=True)
    name: Mapped[str] = mapped_column(String(80), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    render_mode: Mapped[str] = mapped_column(String(24), default="legacy_append")
    system_prompt_template: Mapped[str] = mapped_column(Text, default="")
    user_prompt_template: Mapped[str] = mapped_column(Text, default="")
    template_schema_version: Mapped[int] = mapped_column(Integer, default=1)
    notes: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    source: Mapped[str] = mapped_column(String(32), default="web")
    created_at: Mapped[str] = mapped_column(String(32), default=_now_iso)
    updated_at: Mapped[str] = mapped_column(String(32), default=_now_iso)


class ControlCommandRow(Base):
    """Web 操作面板下发的命令队列。

    交易主进程轮询 status='pending' 的命令并执行，执行后置 done/failed。
    解耦设计：web 进程只写命令，绝不直接碰交易所；命令由交易进程串行消费。
    """
    __tablename__ = "control_commands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(Integer, default=_now_ms, index=True)
    created_at: Mapped[str] = mapped_column(String(32), default=_now_iso)
    command: Mapped[str] = mapped_column(String(32), index=True)  # PAUSE/RESUME/etc.
    arg: Mapped[str] = mapped_column(Text, default="")             # 命令参数
    source: Mapped[str] = mapped_column(String(32), default="web")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending/done/failed
    result: Mapped[str] = mapped_column(String(300), default="")
    executed_at: Mapped[str] = mapped_column(String(32), default="")


class ExchangeEventRow(Base):
    """Durable idempotent inbox for raw Binance private/account events."""
    __tablename__ = "exchange_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    session_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    source: Mapped[str] = mapped_column(String(16), default="")
    event_type: Mapped[str] = mapped_column(String(48), default="", index=True)
    event_time_ms: Mapped[int] = mapped_column(Integer, default=0, index=True)
    transaction_time_ms: Mapped[int] = mapped_column(Integer, default=0)
    received_at_ms: Mapped[int] = mapped_column(Integer, default=_now_ms, index=True)
    applied_at_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="received", index=True)
    error: Mapped[str] = mapped_column(String(500), default="")
    raw_json: Mapped[str] = mapped_column(Text, default="")


class ExchangeStreamSessionRow(Base):
    __tablename__ = "exchange_stream_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), default="", index=True)
    listen_key_hash: Mapped[str] = mapped_column(String(64), default="")
    reason: Mapped[str] = mapped_column(String(500), default="")
    connected_at_ms: Mapped[int] = mapped_column(Integer, default=0)
    disconnected_at_ms: Mapped[int] = mapped_column(Integer, default=0)
    keepalive_at_ms: Mapped[int] = mapped_column(Integer, default=0)
    last_event_at_ms: Mapped[int] = mapped_column(Integer, default=0)
    last_resync_at_ms: Mapped[int] = mapped_column(Integer, default=0)
    updated_at_ms: Mapped[int] = mapped_column(Integer, default=_now_ms)


class LiveBalanceRow(Base):
    __tablename__ = "live_balances"

    asset: Mapped[str] = mapped_column(String(16), primary_key=True)
    wallet_balance: Mapped[float] = mapped_column(Float, default=0.0)
    available_balance: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(16), default="")
    updated_at_ms: Mapped[int] = mapped_column(Integer, default=0)
    raw_json: Mapped[str] = mapped_column(Text, default="")


class LivePositionRow(Base):
    __tablename__ = "live_positions"

    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    side: Mapped[str] = mapped_column(String(8), default="")
    contracts: Mapped[float] = mapped_column(Float, default=0.0)
    entry_price: Mapped[float] = mapped_column(Float, default=0.0)
    mark_price: Mapped[float] = mapped_column(Float, default=0.0)
    leverage: Mapped[int] = mapped_column(Integer, default=0)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    notional: Mapped[float] = mapped_column(Float, default=0.0)
    initial_margin: Mapped[float] = mapped_column(Float, default=0.0)
    isolated_margin: Mapped[float] = mapped_column(Float, default=0.0)
    maintenance_margin: Mapped[float] = mapped_column(Float, default=0.0)
    roi_pct: Mapped[float] = mapped_column(Float, default=0.0)
    liquidation_price: Mapped[float] = mapped_column(Float, default=0.0)
    margin_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    margin_mode: Mapped[str] = mapped_column(String(16), default="")
    source: Mapped[str] = mapped_column(String(16), default="")
    updated_at_ms: Mapped[int] = mapped_column(Integer, default=0)
    raw_json: Mapped[str] = mapped_column(Text, default="")


class LiveOrderRow(Base):
    __tablename__ = "live_orders"
    __table_args__ = (UniqueConstraint("order_class", "exchange_order_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_class: Mapped[str] = mapped_column(String(16), default="regular", index=True)
    exchange_order_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    client_order_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    symbol: Mapped[str] = mapped_column(String(20), default="", index=True)
    kind: Mapped[str] = mapped_column(String(16), default="")
    side: Mapped[str] = mapped_column(String(8), default="")
    order_type: Mapped[str] = mapped_column(String(32), default="")
    qty: Mapped[float] = mapped_column(Float, default=0.0)
    filled_qty: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    trigger_price: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(24), default="")
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(16), default="")
    updated_at_ms: Mapped[int] = mapped_column(Integer, default=0)
    raw_json: Mapped[str] = mapped_column(Text, default="")


class ExchangeStateDriftRow(Base):
    __tablename__ = "exchange_state_drifts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts_ms: Mapped[int] = mapped_column(Integer, default=_now_ms, index=True)
    entity_type: Mapped[str] = mapped_column(String(24), default="", index=True)
    entity_key: Mapped[str] = mapped_column(String(64), default="", index=True)
    reason: Mapped[str] = mapped_column(String(500), default="")
    projection_json: Mapped[str] = mapped_column(Text, default="")
    rest_json: Mapped[str] = mapped_column(Text, default="")
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
