# Mainnet dual-environment rollout

Mainnet must use a dedicated Binance sub-account. Do not copy the testnet database.

## Files and credentials

- Create `/etc/binance-trade/config.testnet.yaml` and `config.mainnet.yaml`.
- Set `mode: testnet` / `mode: mainnet` and use independent absolute database/log paths.
- Mainnet initial symbols: `BTCUSDT`, `ETHUSDT`, `SOLUSDT`, `BNBUSDT`.
- Mainnet engine key: futures read/trade only, withdrawals disabled, IP allowlist enabled.
- Mainnet web key: independent read-only key.
- Store engine secrets in `/etc/binance-trade/testnet.env` and `mainnet.env`.
- Store web secrets in `/etc/binance-trade/web-testnet.env` and `web-mainnet.env`.
- Set all secret files and SQLite databases to mode `0600`; directories to `0700`.

Mainnet initial risk values are `5x`, order/symbol/total margin `20%/40%/100%`,
order-margin stop loss `30%`, daily loss `20%`, drawdown `20%`, and confidence `0.6`.

## Services

Install the templated units and run:

```bash
systemctl enable --now binance-trade@testnet binance-trade-web@testnet
systemctl enable --now binance-trade@mainnet binance-trade-web@mainnet
```

Set `WEB_PORT=8000` for testnet and `WEB_PORT=8001` for mainnet. Mainnet starts
with new entries paused on every restart. Existing positions remain reconciled
and protected. Normal SIGTERM leaves protected positions untouched; only the
explicit kill switch cancels orders and force-closes positions.

Before manual mainnet resume, verify one-way position mode, isolated margin,
private account queries, protection orders, and emergency flattening with a
minimal controlled position.
