"""CLI 入口：run / kill-switch / backtest。

用法：
    python main.py run                 # 启动主循环（按 config.yaml）
    python main.py run -c config.yaml  # 指定配置
    python main.py kill-switch         # 紧急：撤单 + 平仓 + 停机
    python main.py backtest --symbol BTCUSDT --csv data/btc.csv

mainnet 启动时会二次确认（除非 --yes），防止误触真实主网下单。
SIGINT/SIGTERM 只优雅停止交易引擎；撤单+平仓需显式执行 kill-switch。
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import signal
import sys

from src.config.loader import ConfigError, load_config
from src.utils.logger import setup_logger


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="binance-trade", description="LLM 驱动的币安永续合约交易机器人")
    p.add_argument("-c", "--config", default="config.yaml", help="config.yaml 路径")
    p.add_argument("-e", "--env", default=".env", help=".env 路径")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="启动主循环").add_argument(
        "--yes", action="store_true", help="跳过 mainnet 真实下单二次确认"
    )
    sub.add_parser("kill-switch", help="紧急撤单+平仓+停机")
    bt = sub.add_parser("backtest", help="历史 K 线重放，验证风控夹断")
    bt.add_argument("--symbol", default="BTCUSDT")
    bt.add_argument("--csv", required=True, help="OHLCV CSV: ts,open,high,low,close,volume")
    bt.add_argument("--leverage", type=int, default=5, help="模拟 LLM 给出的杠杆（默认5，触发夹断）")
    bt.add_argument("--size-pct", type=float, default=0.1)
    return p


def _confirm_mainnet(settings, yes: bool) -> bool:
    if not settings.is_mainnet or yes:
        return True
    ans = input("mainnet 真实主网模式，确认启动？输入 yes 继续: ").strip().lower()
    return ans == "yes"


async def _cmd_run(args) -> int:
    from src.engine.loop import TradingEngine

    settings, creds = load_config(args.config, args.env)
    setup_logger(settings.logging)
    if not _confirm_mainnet(settings, args.yes):
        print("已取消。")
        return 1

    engine = TradingEngine(settings, creds)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                sig, lambda: asyncio.ensure_future(engine.stop("signal"))
            )
        except NotImplementedError:
            pass  # 某些平台不支持
    await engine.run()
    return 0


async def _cmd_kill(args) -> int:
    from src.engine.loop import TradingEngine

    settings, creds = load_config(args.config, args.env)
    setup_logger(settings.logging)
    engine = TradingEngine(settings, creds)
    await engine.startup()
    try:
        await engine.kill("manual kill-switch command")
    finally:
        await engine.shutdown()
    print("kill-switch 完成：已撤单并平仓。")
    return 0


def _cmd_backtest(args) -> int:
    from src.backtest.replay import fixed_provider, replay

    settings, _ = load_config(args.config, args.env)
    setup_logger(settings.logging)

    klines: list[list[float]] = []
    with open(args.csv, newline="") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or row[0].lower().startswith(("ts", "time", "date")):
                continue
            klines.append([float(x) for x in row[:6]])
    if len(klines) < 60:
        print(f"K 线太少（{len(klines)}），至少 60 根。")
        return 1

    provider = fixed_provider(
        dict(action="OPEN_LONG", confidence=0.9, size_pct=args.size_pct,
             leverage=args.leverage, stop_loss_pct=0.02, take_profit_pct=0.04,
             reason="backtest fixed")
    )
    stats = replay(symbol=args.symbol, klines=klines, settings=settings, provider=provider)
    print("回测统计：", stats.summary())
    print(f"（杠杆={args.leverage} vs max_leverage={settings.risk.max_leverage}，"
          f"观察 LEVERAGE_EXCEEDED 计数验证夹断）")
    return 0


def cli() -> int:
    args = _build_parser().parse_args()
    try:
        if args.command == "run":
            return asyncio.run(_cmd_run(args))
        if args.command == "kill-switch":
            return asyncio.run(_cmd_kill(args))
        if args.command == "backtest":
            return _cmd_backtest(args)
    except ConfigError as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n中断退出。")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
