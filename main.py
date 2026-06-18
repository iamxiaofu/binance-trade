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
import time

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
    ext = sub.add_parser(
        "external-backfill",
        help="预览或导入 Binance 外部/手工成交（默认最近30天 dry-run）",
    )
    ext.add_argument("--days", type=int, default=30, help="回填天数，默认30")
    ext.add_argument("--apply", action="store_true", help="正式写入外部成交账本")
    ext.add_argument("--yes", action="store_true", help="跳过 mainnet MAINNET 二次确认")
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


async def _cmd_external_backfill(args) -> int:
    from src.exchange.client import ExchangeClient
    from src.exchange.fills import ccxt_trade_fill
    from src.store.repo import Store

    settings, creds = load_config(args.config, args.env)
    setup_logger(settings.logging)
    days = min(max(int(args.days), 1), 90)
    if args.apply and settings.is_mainnet and not args.yes:
        answer = input(
            f"即将向 {settings.mode.value} 数据库导入最近 {days} 天外部成交。"
            "输入 MAINNET 继续: "
        ).strip()
        if answer != "MAINNET":
            print("已取消。")
            return 1

    client = ExchangeClient(settings, creds)
    store = Store(settings.storage.db_path)
    await store.connect(run_backfills=False)
    cutoff = int(time.time() * 1000) - days * 86_400_000
    now_ms = int(time.time() * 1000)
    stats = {
        "fetched": 0, "engine": 0, "external": 0, "mixed": 0,
        "unknown": 0, "duplicates": 0, "inserted": 0,
    }
    try:
        for symbol in settings.symbols:
            window_start = cutoff
            while window_start < now_ms:
                window_end = min(window_start + 6 * 86_400_000, now_ms)
                since = window_start
                while since <= window_end:
                    trades = await client.fetch_my_trades(
                        symbol, since=since, until=window_end, limit=1000
                    )
                    if not trades:
                        break
                    max_ts = since
                    for trade in trades:
                        fill = ccxt_trade_fill(trade, symbol)
                        if fill is None:
                            continue
                        stats["fetched"] += 1
                        preview = await store.preview_exchange_fill(fill)
                        ownership = str(preview["ownership"])
                        stats[ownership] += 1
                        if preview["duplicate"]:
                            stats["duplicates"] += 1
                        if args.apply:
                            result = await store.ingest_exchange_fill(fill)
                            if result.get("inserted"):
                                stats["inserted"] += 1
                        max_ts = max(max_ts, int(fill.get("ts_ms") or 0))
                    if len(trades) < 1000 or max_ts <= since:
                        break
                    since = max_ts + 1
                window_start = window_end + 1
    finally:
        await client.close()
        await store.close()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"external-backfill {mode}: mode={settings.mode.value} days={days}")
    for key, value in stats.items():
        print(f"  {key}: {value}")
    if not args.apply:
        print("未写入业务数据；确认统计后加 --apply 执行。")
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
        if args.command == "external-backfill":
            return asyncio.run(_cmd_external_backfill(args))
    except ConfigError as e:
        print(f"配置错误：{e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n中断退出。")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
