# Breaking Money · AI Trading Arena

**I gave Claude ₹1,00,000 of virtual money and 15 trading days. This is the exact app it trades with. Now you can run your own.**

The Arena is a local AI paper-trading agent for Indian stocks (NSE). It watches live prices, lets **Claude** make BUY / SELL / HOLD calls with a written reason for each one, fills them in a realistic simulator (spread, slippage and real Indian charges), and builds a results dashboard with P&L, win rate, drawdown and Claude's trade journal.

> ⚠️ **Paper trading only. Not financial advice.** This app uses virtual money and **cannot place real orders**: broker order URLs and trading SDKs are blocked, and it refuses to start if `paper_only` is switched off. Simulated results are not real returns. Read [DISCLAIMER.md](DISCLAIMER.md).

**What you need:** a Windows or Mac computer, [Python 3.11 or newer](https://www.python.org/downloads/), and about 5 minutes. No broker account and no paid data. Prices come free from Yahoo Finance.

---

## Quick start

### 1. Get the code

Either click the green **Code** button on this page, choose **Download ZIP** and unzip it, or run:

```bash
git clone https://github.com/akshaymshankar/Breaking_Money_Trading-Agent.git
cd Breaking_Money_Trading-Agent
```

### 2. Install and open the Arena

**Windows:** install Python from python.org and **tick "Add python.exe to PATH"** on the first installer screen. Then double-click **`setup.bat`**, and after it finishes, double-click **`START-TRADING.bat`**.

**Mac:** open Terminal in the folder and run:

```bash
./setup.sh
./start-trading.command
```

The control panel opens in your browser. Press **Start** during market hours (09:15 to 15:30 IST, Monday to Friday), or try the offline demo first:

```bash
# Windows: .venv\Scripts\activate     Mac: source .venv/bin/activate
python -m arena demo        # 15 days of made-up prices, runs in seconds, opens the results page
```

### 3. Choose who makes the calls

The app picks the best option it finds automatically (`brain = "auto"` in `config.toml`):

| Option | Cost | How |
|---|---|---|
| **Claude Code** (recommended) | Included with a Claude Pro / Max plan | Install [Claude Code](https://claude.com/claude-code) and log in once. The Arena finds it on its own. |
| **Anthropic API key** | Pay per use, roughly a few US dollars per trading day with the defaults | Open `.env` (created by setup), paste `ANTHROPIC_API_KEY=...` from [console.anthropic.com](https://console.anthropic.com). |
| **Built-in rules** | Free | Used automatically if neither of the above is set up. No AI, a simple momentum strategy to compare against. |

Run `python -m arena check` any time to see which one is active and to watch the real-money block in action.

### 4. Run your own challenge

1. In the control panel open **Settings**: starting capital (default ₹1,00,000), number of days (default 15), risk profile (**Balanced** or **Aggressive**), and your watchlist.
2. Press **Start**. Leave the computer on and awake during market hours. Stop any time; Start again resumes the same challenge with the same cash and positions.
3. Each evening, open the dashboard (or `reports/results.html`) to see the day's P&L and every call Claude made, with its reason.

Want to try your own strategy idea? Write it in plain English in **Settings → Notes** (or `notes` under `[strategy]` in `config.toml`), for example *"Only buy breakouts above the opening-range high with volume above 1.5x average."* Claude reads it before every decision.

### Troubleshooting

- **"Python was not found"** on Windows: reinstall Python and tick *Add python.exe to PATH*, or install it from the Microsoft Store.
- **Mac says the file can't be opened:** run `chmod +x setup.sh start-trading.command` in Terminal, or right-click the file and choose **Open**.
- **No prices / "no quotes returned":** the market is closed or Yahoo is briefly unavailable. Try again during market hours, or run the offline `demo`.
- **Nothing happens after Start:** the agent waits for 09:15 IST. Outside market hours it sleeps until the next open.

---

## How it works

```
 live prices ──► Runner (every 30 s) ──► stop-loss / target / square-off checks
                    │
                    └─(every 15 min)─► Claude: BUY / SELL / HOLD + reason + confidence
                                            │
                                            ▼
                              Paper engine (deterministic)
                     sizing · cash checks · risk limits · re-quote after the call
                     fill = price + half spread + slippage + size impact
                     Indian charges: brokerage, STT, NSE fee, SEBI, stamp, GST, DP
                                            │
                                            ▼
                     SQLite journal ──► results page (P&L, insights, Claude's reasons)
```

- **Claude decides, code executes.** Claude never chooses share quantities or touches cash. The engine sizes each order from `allocation_pct`, caps it at `max_position_pct`, and rejects anything that breaks a rule.
- **Realistic fills.** After Claude answers, the engine fetches a fresh price (the market moved while Claude was thinking) and fills against it with spread, slippage and impact. A "₹1,500 → ₹1,505" trade does not book a clean ₹5/share.
- **Risk rules** on every price poll: per-trade stop-loss and target, a daily loss kill switch (`max_daily_loss_pct`), no new entries after 14:45, and auto square-off at 15:15 in intraday mode.
- **Long-only V1.** BUY opens, SELL closes. Short selling is a later version.

## Multi-agent debate mode (Bull vs Bear, Judge decides)

Debate mode is the default (`brain = "debate"`). **Every trade the agent takes is decided by the Judge.** The strategy (`[debate] proposer`, the free rules by default) only *proposes* BUY/SELL ideas. Each proposal is debated by Bull and Bear, and it is **taken** only if the Judge approves it and the risk guard agrees. Otherwise it is **left**. Market events (price moves, volume spikes, breakouts, SMA crosses, material NSE filings) can also start a debate on their own.

`require_judge = true` enforces this at the engine boundary. In debate mode, and in every live session, a BUY/SELL is executed only if it matches a completed debate's final verdict exactly: same run, same stock, same action. Anything else is rejected with "no Judge approval", including the rules fallback when Claude is down. Automatic risk exits (stop-loss, target, daily loss kill switch, 15:15 square-off) are not debated: they protect the account and always run. Without Claude access (an API key or Claude Code login), no debate can finish, so **no new trades are taken**.

```
 strategy proposal (BUY/SELL idea) or event (price move, volume spike, breakout, SMA cross, material NSE filing)
   └─► frozen market snapshot (hashed) + related NSE filings
         └─► 🐂 Bull opening ─┐
             🐻 Bear opening ─┤ (independent)
             🐻 Bear challenges ► 🐂 Bull answers   (× rounds)
             🐻 Bear final counterarguments
               └─► ⚖️ Judge: PAPER_BUY / PAPER_SELL / HOLD / SKIP (Pydantic-validated)
                     └─► risk guard (can only downgrade) ► existing PaperBroker ► SQLite journal
```

- **The engine still owns the money.** The Judge's verdict becomes a normal BUY/SELL/HOLD decision. Sizing, cash checks, charges, slippage, stops, targets, the daily loss limit and square-off are unchanged.
- **The risk guard** turns a buy into HOLD when the data is stale (`max_quote_age_seconds`), entries are closed, the stock is already held, positions are full, the Judge's confidence is below `min_judge_confidence`, or the Judge flags stale data. PAPER_SELL without a position is ignored (long-only).
- **Cost control:** event triggers, a per-stock cooldown, `max_debates_per_cycle`, `max_debates_per_day`, and a daily dollar cap (`max_daily_cost_usd`). Every call's tokens and estimated cost go into the `agent_usage` table. A debate is 6 Claude calls at `rounds = 1`.
- **Research:** NSE's official RSS feeds for corporate announcements and financial results, polled every `poll_minutes`, de-duplicated, mapped to your watchlist with a confidence score, and flagged *material* by keyword. Filings are stored as facts. The agents' interpretation is stored separately, in the debate events.
- **Live dashboard:** the control panel's **Debates** tab streams every agent reply as it arrives (Server-Sent Events). It shows the watchlist with sparklines, the research panel, the debate timeline with evidence and risk flags, the Judge's verdict and the guard's notes, the portfolio, agent performance and Claude spend.
- **Claude access:** `[debate] transport = "claude-cli"` (default: your Claude Code login, no API key; runs with all tools and MCP servers off and saves no chat sessions) or `"claude-api"` (needs `ANTHROPIC_API_KEY` in `.env`). The CLI is found on PATH or in `~/.local/bin`; set `CLAUDE_CLI` to point elsewhere. The copy bundled inside the Claude desktop app is not signed in on its own, so install the standalone CLI and sign in once. Model and effort are set in `[agents]`, with optional per-agent overrides. Current Claude models don't accept a temperature setting, so `effort` is the depth and cost control.
- **Provider lock:** resuming a challenge with a different `feed` is refused. Start a new challenge to switch provider.

New files: `arena/debate.py` (orchestrator, triggers, guard, `DebateBrain`), `arena/agents.py` (role prompts), `arena/schemas.py` (Pydantic models), `arena/llm.py` (Claude transports, usage and budget), `arena/research.py`, `arena/events.py`, `arena/debate_view.py`, `tests/test_debate.py`.

Not yet included: Upstox WebSocket streaming, FYERS v3 (its official SDK bundles order APIs, which the safety layer blocks, so it needs a read-only adapter), and index (NIFTY) updates. Polling Yahoo or Upstox REST still works as before.

## Market data

Set `feed` in `config.toml`.

| Feed | Cost | Setup | Notes |
|---|---|---|---|
| `yahoo` (default) | Free | None | Unofficial (yfinance), for personal use. Prices can lag a little. Also used by `fetch` for replay history. |
| `upstox` | Free API | Upstox account + an app at upstox.com/developer | Official exchange-licensed data. Token expires daily: run `python -m arena upstox-login` each morning. Only market-data endpoints are used. |

We do not support Zerodha Kite for this, because Kite's terms restrict virtual/mock-trading use of its data. We also do not scrape the NSE website, because NSE's terms prohibit that. Before you publish results from any feed, check that its terms allow your use.

## Commands

| Command | What it does |
|---|---|
| `python -m arena gui` | Control panel in your browser: Start/Stop, settings, live log and dashboard. `START-TRADING.bat` opens it. |
| `python -m arena demo [--days 15] [--brain rules\|claude-api]` | Full challenge on synthetic prices. Safe to run any time, offline. |
| `python -m arena check` | Proves order URLs and broker SDKs are blocked, then tests prices and Claude. |
| `python -m arena run [--new] [--name "..."] [--brain debate]` | Live session. Resumes the active challenge unless you pass `--new`. |
| `python -m arena fetch [--source yahoo\|upstox] [--days 30]` | Saves past 5-minute bars to `data/history/` for replay. |
| `python -m arena replay [--days 10 \| --start YYYY-MM-DD --end YYYY-MM-DD]` | Runs the agent through history on a virtual clock. It never sees a future price. |
| `python -m arena compete --brains rules,claude-api --days 10` | Several strategies, same prices, same capital, leaderboard. |
| `python -m arena report [--ai]` | Rebuilds the results page. `--ai` adds Claude's coach review. |
| `python -m arena status` | Lists all runs with equity and P&L. |

## The 15-day Breaking Money challenge

1. Fill `holidays` in `config.toml` from NSE's holiday list, so the 15 days are counted correctly.
2. The evening before Day 1: `python -m arena check`.
3. Day 1 morning: `python -m arena run --new --name "Breaking Money Challenge"`.
4. Each evening, `reports/results.html` has that day's P&L and notes. Run `python -m arena report --ai` for Claude's coach review to use in your video.
5. The run stops on its own after 15 trading days (change `challenge_days` for a different length).

To try a different style mid-series, edit `notes` under `[strategy]` (for example, "Only trade breakouts above the opening-range high with volume above 1.5x average"). Claude reads it on every decision.

## Results page

`reports/results.html` is one self-contained page. It includes portfolio value, net P&L after charges, win rate, profit factor, max drawdown, charges and slippage paid, and a Sharpe estimate. It also shows the day-by-day challenge tracker, the equity curve, daily P&L bars, key insights, day-by-day notes, open positions, Claude's latest market view, every BUY/SELL call with Claude's reason and the engine's result, P&L by stock and by exit reason, the trade log, and a leaderboard of all runs.

## Cost of running Claude

With the defaults (10 stocks, a decision every 15 minutes, `claude-opus-5-5` at `effort = "medium"`), expect roughly 25 calls a day at a few cents each. That is a few US dollars a day. Lower it with `decision_minutes = 30`, fewer stocks, or `effort = "low"`.

## Files

```
arena/
  safety.py      real-money block (allow-list, SDK import block, paper_only lock)
  engine.py      virtual account, fills, stops, square-off
  charges.py     Indian charges (edit rates in config.toml)
  brain.py       Claude API / Claude CLI / rules baseline
  debate.py      multi-agent debates: triggers, Bull/Bear/Judge, risk guard
  research.py    official NSE filings feed (facts only)
  runner.py      market loop, used for live, replay and demo
  insights.py    analytics + plain-English observations + Claude coach review
  report.py      the one-page results dashboard
  feeds/         yahoo, upstox, replay, synthetic
config.toml      all settings
data/arena.db    SQLite journal (runs, decisions, orders, trades, equity, days, debates, research, agent usage)
tests/           python -m unittest discover tests
```

## Limits of V1

- Fills are modelled. The engine does not see order-book depth, so very large or illiquid orders are only approximated (`max_participation` caps size at 5% of a bar's volume).
- Stops are checked on each poll (every 30 s by default), not tick by tick. A fast gap can fill below the stop, which is realistic.
- The machine must stay on during market hours. Run it on an always-on PC or a small cloud VM for an unattended 15-day challenge.
- Charge rates are a typical discount-broker schedule. Update `[charges]` if rates change.

## Licence and disclaimer

MIT licensed, see [LICENSE](LICENSE). Paper trading only. Not investment advice. Simulated results do not guarantee real-world results. See [DISCLAIMER.md](DISCLAIMER.md).

## Running it unattended (macOS background service)

`~/Library/LaunchAgents/com.breakingmoney.arena.plist` runs the control panel as a background service:
`arena gui --no-open --port 8800 --lan --resume`, wrapped in `caffeinate -i` so the Mac does not idle-sleep.

- Starts when you log in, and restarts within ~30 s if it crashes. On start it continues the active live challenge
  (it waits for the next market open, then trades every `decision_minutes`).
- Pressing **Stop** in the panel is remembered (`data/paused_by_user`): the service will not resume until you press **Resume**.
- Panel: http://127.0.0.1:8800/ on this Mac (full control). Service output: `data/service.log`.
- `--lan`: devices on the same Wi-Fi can open http://<this Mac's Wi-Fi IP>:8800/ **view only** (dashboard, debates, live stream). Start/Stop/settings are refused unless the request really comes from this Mac. Remove `--lan` from the plist to stop sharing.
- Turn it off: `launchctl bootout gui/$(id -u)/com.breakingmoney.arena` (and delete the plist to stop it starting at login).
- It cannot run while the Mac is shut down, logged out, or asleep with the lid closed.

## Judge verdicts by email

Each Judge decision triggers an email: BUY (with "Why buy"), DON'T BUY (hold or skip, with "Why not") or SELL. The email also says what the engine did, for example "BUY 18 SBIN @ 1085.40" or "rejected: entries closed". Automatic stop-loss and target exits get a short email too. The emails are sent in the background, so trading never waits for them. Set the recipient and choose `each_verdict` / `exits` / `daily` (end-of-day summary, off by default) in `[email]` in `config.toml`. Put the Gmail login in `.env`, never in the config:

```
ARENA_SMTP_USER=you@gmail.com
ARENA_SMTP_PASSWORD=<16-character app password from https://myaccount.google.com/apppasswords>
ARENA_MAIL_TO=where-to-send@example.com   # optional, defaults to ARENA_SMTP_USER
```

Without these two values, the email is skipped and the log says why. Trading continues either way. You can preview or send a report at any time:

```
python -m arena email-report --dry-run            # print the latest day's report
python -m arena email-report --date 2026-11-13    # send that day's report
```
