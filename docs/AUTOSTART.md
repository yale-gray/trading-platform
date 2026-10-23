# Auto-start on macOS (paper trading)

This optional setup starts the paper-trading runner and the web API when you log in to your Mac, and restarts them if they crash. It runs as your own user account. It needs no admin password and no `sudo`. It trades simulated money only.

## What gets installed

Two LaunchAgents (macOS's per-user background services):

| Label | What it runs |
|---|---|
| `com.tradebot.runner` | `uv run tradebot runner start` (the paper-trading loop) |
| `com.tradebot.api` | `uv run tradebot api serve` (the API and dashboard at http://127.0.0.1:8000) |

The plist files are written to `~/Library/LaunchAgents/`. The installer fills in the path to `uv` (found with `command -v uv`) and the repo folder. Nothing is copied to system folders.

## Commands

Run these from the repo root.

```bash
scripts/service/tradebot-service.sh install     # write the plists and start both agents
scripts/service/tradebot-service.sh status      # show whether each agent is running
scripts/service/tradebot-service.sh uninstall   # stop both agents and remove the plists
```

After `install`, open the app at http://127.0.0.1:8000.

The script refuses to run as root. If `uv` is not on your PATH, install it first.

## Where the logs are

Both agents write to the `logs/` folder in the repo:

- `logs/runner.out.log` and `logs/runner.err.log`
- `logs/api.out.log` and `logs/api.err.log`

If something looks wrong, check the `.err.log` files first.

## Things to know

- **The Mac must be awake for trading days to process.** The agents only run while the Mac is on and you are logged in. If the Mac is asleep at the market's bar time, that bar is not processed right away.
- **The runner catches up after sleep.** When the Mac wakes, the runner works through the bars it missed, so you do not need to restart it by hand. If the runner reports a gap it will not fill on its own, use `tradebot runner reset-from` (see `tradebot runner --help`).
- **Start at login, not at boot.** The agents start when you log in. A Mac that restarts and sits at the login screen will not run them until you log in.
- **Crash restart.** `KeepAlive` restarts a process if it exits. If it keeps crashing, `status` and the `.err.log` file will show it.
- **Stopping for maintenance.** Run `uninstall` to stop both agents. Logs are kept.
- **Only one runner at a time.** The runner holds `runner.lock`. Do not also start `tradebot runner start` by hand while the agent is running.
