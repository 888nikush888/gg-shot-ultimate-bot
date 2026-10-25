# GG-Shot Ultimate v6 Bot

Live-Trading-Bot (Python 3.13, CCXT) fuer Hyperliquid + Aster + Binance (USDT-M) Perps. Multi-Asset parallel, Voll-CLI, Docker 24/7.

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
- `config-validate [--check-markets] / config-show` — Config + Presets pruefen (`--check-markets` gleicht Symbole live mit den Boersen ab)
- `preset-list / preset-show KEY` — z. B. `BTC_1h`
- `backtest --symbol BTC/USDC:USDC --limit 1000` — read-only Smoke ueber CCXT-OHLCV (Symbol passend zur Boerse waehlen)
- `positions-close-all --i-understand-live` — Not-Aus (reduceOnly)
- `kill-engage --reason ...` — Kill-Switch (stoppt neue Entries)
- `telegram-test`, `healthcheck` (Docker)

## Sicherheit
- Secrets nur in `.env` (600), nie im Image/Repo/Logs (Redaction aktiv).
- Non-root-User, `no-new-privileges`, Read-only `config`-Mount.
- Hyperliquid: `HYPERLIQUID_WALLET_ADDRESS` + `HYPERLIQUID_PRIVATE_KEY` (Hex). Aster/Binance: Key/Secret (siehe `.env.example`).
- GitHub: Secret-Scanning + Push-Protection in den Repo-Settings einschalten (fuer public Repos gratis).
- Updates: `docker compose pull/build && config-validate && up -d`. Rollback ueber Image-Tag.
- Backup: `data/state.db` + `config.yaml` regelmaessig sichern.

## Strategie-Paritaet
`config/presets.json` = 1:1-Transkript der Pine-Tabellen (`f_cfg_signal/filter/tp_sl/trail`, 78 Keys + `_finetune_overrides` + `_defaults_*`).
Engine: Closed-Candle-Signale (kein Repaint), Trendlinie/Breakout, ATR/RSI/Volumen-Filter, Cooldown, Stoprisiko/Marginbudget-Sizing, 4 TP-Tranchen, BE/Trailing-Trigger, Oszillator-Exit. Order-Modi pro Symbol: `bot_managed` oder `exchange_native`.

## Ausfuehrungs-Engine (ergaenzt nach Review)
- **Hebel:** wird beim Start pro Symbol per `set_leverage` an die Boerse gepusht. Schlaegt das fehl, ist das Symbol fail-safe blockiert (kein Trading mit falschem Account-Hebel).
- **Equity:** aus der Settlement-Waehrung des Symbols (z. B. USDC bei Hyperliquid). Kein Rueckfall auf 10000 im Live-Modus: ohne Guthaben wird das Symbol mit klarem Fehler blockiert.
- **Positionsschutz:** kein Entry bei offener/verwaister Position; Cooldown in Kerzen nach Close; Kill-Switch blockiert Entries und schliesst live alles.
- **Exits:** `bot_managed` ueberwacht SL/TP1-4/BE/Trailing/Opposite/Oszillator aktiv; `exchange_native` legt volles Bracket (SL + alle TP-Tranchen, gewichtet) und zieht den Boersen-SL bei BE/Trailing nach. Paper tradet die gleiche Logik simuliert.
- **Fixiert:** `legs: 1` + `entry_mode: Market` (andere Modi brechen mit Fail-fast ab statt falsch zu rechnen); `leverage` 1-50 (Config-Validierung); Smooth-Trendlinie nutzt den regime-adaptiven Filter aus Pine v6.
- **Order-Guards:** Entries unter Exchange-Minimum (`min_amount`/`min_cost`) werden geskippt; in `exchange_native` werden TP/SL/BE/TRAIL nur gebucht (Ausfuehrung nativ, kein Doppel-Exit), der Boersen-SL wird per Order-ID nachgezogen; Fehlermeldungen max. 1x pro Symbol und Viertelstunde; Equity wird 60 s gecacht.
- **Presets:** 78 Keys (26 Assets x 3 Timeframes) + Finetune-Overrides. Tages-PnL setzt sich bei Neustart zurueck (Limit gilt pro Laufzeit).
## Einstellungen (alles einstellbar, Presets fuellen)
`config/config.example.yaml` -> `manual:` enthaelt jede Pine-Einstellung mit Pine-Default. Die `presets.json`-Tabellen (Signal/Filter/TP-SL/Trail + Finetune) fuellen die 35 Preset-Felder pro Asset/Timeframe; `manual` ueberschreibt gezielt (Kamel-Schreibweise wie in Pine wird automatisch erkannt, z. B. `oscMode`).
- **Oszillator/Divergenzen:** `osc_mode` (`Aus`/`Beide Richtungen`/`Mit Haupttrend`), `osc_entries`, `osc_reentries`, `osc_exit` + `osc_exit_after`, Langen/Zonen, `div_mode` (`Aus`/`Anzeige`/`Filter`) + `div_age`.
- **Trend/Modus:** `mode` (`Standard`/`Advanced`/`Channel`), `timing` (`Kerzenschluss` = kein Repaint, `Echtzeit`), `allow_long`/`allow_short`, `bounce_on`/`sr_bounce_on`/`reentry_on`/`max_reentries` (nur Advanced wirksam).
- **Session/Datum:** `session_on`, `trade_session`, `session_days`, `session_zone`, `date_start`/`date_end`, `close_session` — ohne Session kein Entry, mit `close_session` wird bei Sessionende geschlossen.
- **Einstieg:** `entry_mode` `Market` oder `Pullback-Limit` (Limit-Abstand via `entry_unit`/`pullback_distance`, Gueltigkeit `limit_life` Kerzen, danach Storno ohne Cooldown).
- **Fail-fast statt falsch rechnen:** `legs` != 1 (Gestaffelt), `mode: Classic`, `tp_mode_*` != Fest, `leverage` ausserhalb 1-125 und unbekannte Exchanges brechen beim Start mit klarer Meldung ab.
## Dashboard (empfohlen)
Interaktives Terminal-UI — einzige Bedienoberflaeche. Start lokal oder im Container:
`docker exec -it gg-bot python -m gg_bot dashboard` (braucht `-it`; im Docker-Desktop-Exec-Tab kann die Darstellung verwaschen sein — Windows-Terminal/SSH bevorzugen).
Beim ersten Start Login einrichten: `python -m gg_bot dashboard-hash-password` erzeugt die `DASHBOARD_PASSWORD_HASH`-Zeile fuer `.env`. Tabs: Overview, Positionen (schliessen mit Bestaetigung), Presets (filtern, Vorschau, Anwaenden), Steuerung (Mode, Symbole an/aus, Kill, Telegram-Test), Backtest, Ereignisse. Schreiben geht immer mit Backup + Validierung.
