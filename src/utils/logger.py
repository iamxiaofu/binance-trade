"""结构化日志配置（loguru）+ 本地轮转。

整个进程只需在启动时调用一次 ``setup_logger``。其余模块直接：

    from loguru import logger
    logger.info("...")
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from src.config.schema import LoggingConfig

_CONFIGURED = False


def setup_logger(cfg: "LoggingConfig") -> None:
    """根据配置初始化全局 logger。幂等：重复调用只生效一次。"""
    global _CONFIGURED
    if _CONFIGURED:
        return

    logger.remove()  # 移除默认 handler

    # 控制台：彩色、人类可读
    logger.add(
        sys.stderr,
        level=cfg.level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
        enqueue=True,
        backtrace=True,
        diagnose=False,  # 不展开变量，避免泄露敏感值
    )

    # 文件：结构化、带轮转与保留
    log_dir = Path(cfg.dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        log_dir / "biance-trade_{time:YYYY-MM-DD}.log",
        level=cfg.level,
        rotation=cfg.rotation,
        retention=cfg.retention,
        encoding="utf-8",
        enqueue=True,
        serialize=cfg.serialize,  # True 时输出 JSON 行，便于采集
        backtrace=True,
        diagnose=False,
    )

    _CONFIGURED = True
    logger.info("logger initialized (level={}, dir={})", cfg.level, cfg.dir)


def get_logger():
    """返回全局 logger（语法糖）。"""
    return logger
