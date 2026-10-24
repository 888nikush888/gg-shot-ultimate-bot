# GG-Shot Ultimate v6 Bot

Live-Trading-Bot (Python 3.14, CCXT) fuer Hyperliquid + Aster Perps. Multi-Asset parallel, Voll-CLI, Docker 24/7.

> WARNUNG: `mode: live` handelt mit echtem Geld. Starte immer mit `mode: paper`, dann `config-validate`,
> dann genau ein Symbol mit Min-Size. `kill-engage` und `positions-close-all` sind deine Not-Aus.

## Schnellstart (VPS)
1. `cp .env.example .env && chmod 600 .env` und Keys eintragen (nur Trade-Rechte, Withdraw AUS, IP-Allowlist wenn moeglich).
2. `cp config/config.example.yaml config/config.yaml` und Symbole/Presets anpassen.
3. `docker compose build && docker compose up -d`
4. `docker compose exec gg-bot python -m gg_bot config-validate --config /app/config/config.yaml`
5. Telegram testen: `docker compose exec gg-bot python -m gg_bot telegram-test`
6. Paper-Loop testen: `docker compose exec gg-bot python -m gg_bot run --config /app/config/config.yaml --once`
7. Erst danach `BOT_MODE=live` in `.env` setzen und neu starten.

## CLI
- `run [--once]` — Engine starten (einmal oder Loop)
- `status` — Mode, Symbole, Zyklen aus SQLite
- `config-validate / config-show` — Config + Presets pruefen
- `preset-list / preset-show KEY` — z. B. `BTC_1h`
- `backtest --symbol BTC/USDT:USDT --limit 1000` — read-only Smoke ueber CCXT-OHLCV
- `positions-close-all --i-understand-live` — Not-Aus (reduceOnly)
- `kill-engage --reason ...` — Kill-Switch (stoppt neue Entries)
- `telegram-test`, `healthcheck` (Docker)

## Sicherheit
- Secrets nur in `.env` (600), nie im Image/Repo/Logs (Redaction aktiv).
- Non-root-User, `no-new-privileges`, Read-only `config`-Mount.
- Hyperliquid: `HYPERLIQUID_WALLET_ADDRESS` + `HYPERLIQUID_PRIVATE_KEY` (Hex). Aster: Key/Secret.
- Updates: `docker compose pull/build && config-validate && up -d`. Rollback ueber Image-Tag.
- Backup: `data/state.db` + `config.yaml` regelmaessig sichern.

## Strategie-Paritaet
`config/presets.json` = 1:1-Transkript der Pine-Tabellen (`f_cfg_signal/filter/tp_sl/trail`, 78 Keys + `_finetune_overrides` + `_defaults_*`).
Engine: closed-bar Signale, Trendlinie/Breakout, ATR/RSI/Volumen-Filter, Pyramiding/Cooldown, Stoprisiko/Marginbudget-Sizing, 4 TP-Tranchen, BE/Trailing-Trigger, Oszillator-Exit. Order-Modi pro Symbol: `bot_managed` oder `exchange_native`.
