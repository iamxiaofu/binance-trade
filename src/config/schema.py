"""配置的 pydantic 模型：对 config.yaml 做强类型 + 范围校验。

设计原则：
- 任何非法配置在启动期即失败（fail-fast），绝不带病运行。
- 密钥不在此出现，只放环境变量名，由 loader 从 .env 读取。
- ``on_leverage_exceed`` 固定为 REJECT —— 即使用户在 yaml 里写成别的值也强制拒绝。
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# 支持的 K 线/周期 → 秒数映射，用于把 "5m" 解析成 300 秒
INTERVAL_SECONDS: dict[str, int] = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900,
    "30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400,
    "1d": 86400,
}


class Mode(str, Enum):
    TESTNET = "testnet"
    MAINNET = "mainnet"


class MarginMode(str, Enum):
    ISOLATED = "ISOLATED"
    CROSS = "CROSS"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class ExecutionMode(str, Enum):
    MARKET_TAKER = "MARKET_TAKER"
    MAKER_ONLY = "MAKER_ONLY"
    MAKER_FIRST = "MAKER_FIRST"


class MakerUnfilledAction(str, Enum):
    CANCEL = "CANCEL"
    FALLBACK_MARKET = "FALLBACK_MARKET"


class PartialFillAction(str, Enum):
    PROTECT_AND_CANCEL_REST = "PROTECT_AND_CANCEL_REST"


class _Base(BaseModel):
    """禁止未知字段，防止配置写错键名被静默忽略。"""
    model_config = {"extra": "forbid"}


class AccountConfig(_Base):
    margin_mode: MarginMode = MarginMode.ISOLATED
    quote_asset: str = "USDT"
    initial_capital: float = Field(gt=0)


class CycleConfig(_Base):
    interval: str = "5m"
    heartbeat_on_skip: bool = True

    @field_validator("interval")
    @classmethod
    def _check_interval(cls, v: str) -> str:
        if v not in INTERVAL_SECONDS:
            raise ValueError(f"interval 必须是 {list(INTERVAL_SECONDS)} 之一，收到 {v!r}")
        return v

    @property
    def interval_seconds(self) -> int:
        return INTERVAL_SECONDS[self.interval]


class ThrottleConfig(_Base):
    price_change_pct: float = Field(ge=0)
    pnl_alert_pct: float = Field(ge=0)
    trigger_on_order_event: bool = True
    max_skip_cycles: int = Field(ge=1)  # 兜底强制触发；至少 1
    feature_snapshot_enabled: bool = True
    ema_spread_cross_min_pct: float = Field(default=0.02, ge=0)
    macd_hist_cross_min_abs: float = Field(default=0.0, ge=0)
    rsi_midline: float = Field(default=50.0, ge=0, le=100)
    boll_bandwidth_low_pct: float = Field(default=1.0, ge=0)
    boll_bandwidth_expand_pct: float = Field(default=25.0, ge=0)
    volume_zscore_trigger: float = Field(default=2.0, ge=0)
    micro_return_5m_trigger_pct: float = Field(default=0.5, ge=0)
    micro_range_5m_trigger_pct: float = Field(default=0.8, ge=0)
    near_exit_pnl_pct: float = Field(default=0.8, ge=0)
    review_flat_minutes: int = Field(default=60, ge=1)
    review_position_minutes: int = Field(default=15, ge=1)
    review_near_exit_minutes: int = Field(default=5, ge=1)
    review_high_vol_minutes: int = Field(default=5, ge=1)


class OnLeverageExceed(str, Enum):
    REJECT = "REJECT"


class RiskConfig(_Base):
    max_leverage: int = Field(ge=1, le=125)
    # 固定 REJECT：即便 yaml 写别的也会因 enum 限制而报错，符合"不截断"需求
    on_leverage_exceed: OnLeverageExceed = OnLeverageExceed.REJECT
    # ===== 保证金上限：按「当前账户权益」动态计算，随资金自动缩放 =====
    # 例：max_order_margin_pct=0.2 → 单笔最多占用 权益 × 20% 的保证金。
    # 杠杆只影响名义价值、止损亏损估算和强平距离，不放大本项保证金上限。
    max_order_margin_pct: float = Field(gt=0, le=1)       # 单笔保证金占权益比例
    max_symbol_margin_pct: float = Field(gt=0, le=1)      # 单标的累计保证金占权益比例
    max_total_margin_pct: float = Field(gt=0, le=1)       # 全账户累计保证金占权益比例
    max_loss_per_trade_pct: float = Field(gt=0, le=100)   # 止损触发时理论亏损占权益百分比
    max_drawdown_pct: float = Field(gt=0, le=100)
    # 日亏限额也按权益百分比（随资金缩放）
    daily_max_loss_pct: float = Field(gt=0, le=100)
    liq_distance_min_pct: float = Field(ge=0, le=100)
    min_confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _check_consistency(self) -> "RiskConfig":
        if self.max_order_margin_pct > self.max_symbol_margin_pct:
            raise ValueError("max_order_margin_pct 不应大于 max_symbol_margin_pct")
        if self.max_symbol_margin_pct > self.max_total_margin_pct:
            raise ValueError("max_symbol_margin_pct 不应大于 max_total_margin_pct")
        return self


class LLMConfig(_Base):
    provider: Literal["anthropic"] = "anthropic"
    model: str
    # 可选：Anthropic 兼容的中转/代理端点(如自建网关或第三方中转)。
    # 留空 = 用 Anthropic 官方 api.anthropic.com。填了就指向该 base_url。
    # 必须是 Anthropic Messages API 兼容端点(支持 /v1/messages + tool-use)。
    base_url: str | None = None
    timeout: float = Field(gt=0)
    max_tokens: int = Field(gt=0, le=8192)
    max_retries: int = Field(ge=0, le=5)
    kline_lookback: int = Field(ge=10, le=1000)
    kline_interval: str = "5m"
    prompt_kline_count: int = Field(default=20, ge=1, le=200)
    micro_kline_interval: str = "1m"
    micro_kline_lookback: int = Field(default=30, ge=0, le=300)
    indicators: list[str] = Field(default_factory=list)
    # 多周期共振：额外拉这些更高周期的指标喂给 LLM（空=不启用）
    higher_timeframes: list[str] = Field(default_factory=list)

    @field_validator("kline_interval", "micro_kline_interval")
    @classmethod
    def _check_interval(cls, v: str) -> str:
        if v not in INTERVAL_SECONDS:
            raise ValueError(f"interval 必须是 {list(INTERVAL_SECONDS)} 之一")
        return v

    @field_validator("higher_timeframes")
    @classmethod
    def _check_higher_tf(cls, v: list[str]) -> list[str]:
        for tf in v:
            if tf not in INTERVAL_SECONDS:
                raise ValueError(f"higher_timeframes 含非法周期 {tf!r}")
        return v


class ExecutionConfig(_Base):
    # 兼容旧配置。新逻辑使用 entry_mode / normal_exit_mode / emergency_exit_mode。
    order_type: OrderType | None = None
    entry_mode: ExecutionMode | None = None
    normal_exit_mode: ExecutionMode = ExecutionMode.MARKET_TAKER
    emergency_exit_mode: ExecutionMode = ExecutionMode.MARKET_TAKER
    maker_time_in_force: Literal["GTX"] = "GTX"
    maker_timeout_seconds: float = Field(default=8.0, gt=0, le=120)
    maker_poll_seconds: float = Field(default=1.0, gt=0, le=10)
    maker_max_requotes: int = Field(default=2, ge=0, le=10)
    maker_price_offset_bps: float = Field(default=1.0, ge=0, le=100)
    maker_unfilled_action: MakerUnfilledAction = MakerUnfilledAction.CANCEL
    partial_fill_action: PartialFillAction = PartialFillAction.PROTECT_AND_CANCEL_REST
    attach_sl_tp: bool = True
    rate_limit_backoff: float = Field(gt=1.0)
    max_order_retries: int = Field(ge=0, le=10)
    recv_window: int = Field(ge=1000, le=60000)

    @model_validator(mode="after")
    def _derive_execution_modes(self) -> "ExecutionConfig":
        if self.entry_mode is None:
            self.entry_mode = (
                ExecutionMode.MAKER_FIRST
                if self.order_type is OrderType.LIMIT
                else ExecutionMode.MARKET_TAKER
            )
        if self.emergency_exit_mode is not ExecutionMode.MARKET_TAKER:
            raise ValueError("emergency_exit_mode 当前必须为 MARKET_TAKER")
        return self


class StorageConfig(_Base):
    # db_path 是最终解析后的 SQLite 路径；配置文件优先使用 db_path_template。
    db_path: str = ""
    db_path_template: str = ""
    reconcile_on_start: bool = True

    @model_validator(mode="after")
    def _check_path_config(self) -> "StorageConfig":
        if self.db_path and self.db_path_template:
            raise ValueError("storage.db_path 与 storage.db_path_template 只能配置一个")
        if not self.db_path and not self.db_path_template:
            raise ValueError("storage 必须配置 db_path_template 或兼容字段 db_path")
        if self.db_path_template and "{mode}" not in self.db_path_template:
            raise ValueError("storage.db_path_template 必须包含 {mode}")
        return self

    def resolve_db_path(self, mode: Mode | str) -> str:
        mode_value = mode.value if isinstance(mode, Mode) else str(mode)
        if self.db_path_template:
            return self.db_path_template.format(mode=mode_value)
        return self.db_path


class NotifyConfig(_Base):
    telegram_enabled: bool = False
    telegram_bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    telegram_chat_id_env: str = "TELEGRAM_CHAT_ID"
    notify_events: list[str] = Field(default_factory=list)


class LoggingConfig(_Base):
    level: Literal["TRACE", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    dir: str = "./logs"
    rotation: str = "50 MB"
    retention: str = "30 days"
    serialize: bool = False


# ===== 顶层 Settings =====
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,}USDT$")


class Settings(_Base):
    """完整配置树。运行期密钥不在此，单独由 Credentials 持有。"""
    mode: Mode
    symbols: list[str] = Field(min_length=1)
    account: AccountConfig
    cycle: CycleConfig
    throttle: ThrottleConfig
    risk: RiskConfig
    llm: LLMConfig
    execution: ExecutionConfig
    storage: StorageConfig
    notify: NotifyConfig
    logging: LoggingConfig

    @field_validator("symbols")
    @classmethod
    def _check_symbols(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for s in v:
            su = s.upper()
            if not _SYMBOL_RE.match(su):
                raise ValueError(f"symbol {s!r} 非法，应形如 BTCUSDT（USDT 本位永续）")
            if su in out:
                raise ValueError(f"symbol 重复：{su}")
            out.append(su)
        return out

    @property
    def is_mainnet(self) -> bool:
        return self.mode is Mode.MAINNET

    @model_validator(mode="after")
    def _resolve_storage_path(self) -> "Settings":
        try:
            self.storage.db_path = self.storage.resolve_db_path(self.mode)
        except Exception as e:
            raise ValueError(f"storage.db_path_template 解析失败: {e}") from e
        return self


class Credentials(BaseModel):
    """敏感凭据，单独从 .env 读取，绝不写日志、绝不落库。"""
    binance_api_key: str
    binance_api_secret: str
    anthropic_api_key: str
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    def __repr__(self) -> str:  # 防止意外打印泄露
        return "Credentials(****)"

    __str__ = __repr__
