"""Daily Judge report by email: which stocks the Judge approved to buy, which it did not, and why.

Sent after each market close (Runner.close_day) or on demand: python -m arena email-report
Credentials stay in .env (never in config.toml):
    ARENA_SMTP_USER=you@gmail.com
    ARENA_SMTP_PASSWORD=<16-character Gmail app password>
    ARENA_MAIL_TO=where-to-send@example.com   (optional; defaults to ARENA_SMTP_USER)
PAPER TRADING ONLY. The email reports simulated trades.
"""
from __future__ import annotations

import json
import logging
import os
import smtplib
import ssl
import threading
from email.message import EmailMessage

from .db import Journal

log = logging.getLogger("arena")

ACTION = {"PAPER_BUY": "BUY", "PAPER_SELL": "SELL", "HOLD": "HOLD", "SKIP": "SKIP"}


def _rs(x: float | None, sign: bool = True) -> str:
    if x is None:
        return "-"
    s = f"{abs(x):,.2f}"
    return (("+" if x >= 0 else "-") if sign else "") + "Rs " + s


def _verdicts(j: Journal, run_id: int, day: str) -> list[dict]:
    out = []
    for d in j.q("SELECT * FROM debates WHERE run_id=? AND substr(ts,1,10)=? ORDER BY id", (run_id, day)):
        v = json.loads(d["judge_json"]) if d["judge_json"] and d["judge_json"] != "null" else None
        trig = json.loads(d["triggers"] or "[]")
        proposed = next((t.split()[2].rstrip(":") for t in trig if t.startswith("strategy proposes ")), None)
        o = j.one("SELECT ts, side, qty, fill_price, status, symbol FROM orders WHERE note LIKE ? ORDER BY id LIMIT 1",
                  (f"[debate #{d['id']}]%",))
        pnl = None
        if o and o["side"] == "SELL":
            t = j.one("SELECT net_pnl FROM trades WHERE symbol=? AND exit_ts=?", (o["symbol"], o["ts"]))
            pnl = t["net_pnl"] if t else None
        out.append({
            "id": d["id"], "time": d["ts"][11:16], "symbol": d["symbol"], "proposed": proposed,
            "judge": ACTION.get(v["action"], v["action"]) if v else None, "confidence": v["confidence"] if v else None,
            "stop": v.get("stop_loss_pct") if v else None, "target": v.get("target_pct") if v else None,
            "reason": v["rationale"] if v else "No verdict: " + "; ".join(json.loads(d["guard_notes"] or "[]")),
            "final": d["final_action"], "order": o, "pnl": pnl,
        })
    return out


def _result(v: dict) -> str:
    o = v["order"]
    if o and o["status"] == "filled":
        txt = f"{'Bought' if o['side'] == 'BUY' else 'Sold'} {o['qty']} @ Rs {o['fill_price']:,.2f}"
        return txt + (f", net {_rs(v['pnl'])}" if v["pnl"] is not None else "")
    if v["judge"] and v["final"] != v["judge"]:
        return f"not traded (risk guard changed it to {v['final']})"
    return "no trade"


def build(j: Journal, run_id: int, day: str) -> tuple[str, str]:
    """Returns (subject, plain-text body) for one trading day."""
    run = j.one("SELECT * FROM runs WHERE id=?", (run_id,))
    dayrow = j.one("SELECT * FROM days WHERE run_id=? AND date=?", (run_id, day))
    vs = _verdicts(j, run_id, day)
    buys = [v for v in vs if v["judge"] == "BUY"]
    sells = [v for v in vs if v["judge"] == "SELL"]
    nobuy = [v for v in vs if v["judge"] in ("HOLD", "SKIP") or v["judge"] is None]

    L = [f"Breaking Money paper-trading arena: Judge report for {day}",
         f"Run #{run_id} '{run['name']}'" + (f", Day {dayrow['day_no']}" if dayrow else ""),
         "PAPER TRADING ONLY. No real money. This is the simulator's reasoning, not investment advice.", ""]

    L.append(f"JUDGE APPROVED TO BUY ({len(buys)})")
    if not buys:
        L.append("  None today.")
    for v in buys:
        L += [f"- {v['symbol']} at {v['time']}, confidence {v['confidence']:.0%}"
              + (f", stop-loss {v['stop']}%, target {v['target']}%" if v["stop"] else "")
              + f"  ->  {_result(v)}", f"  Why buy: {v['reason']}", ""]
    L.append("")

    L.append(f"JUDGE SAID DON'T BUY ({len(nobuy)})")
    if not nobuy:
        L.append("  None today.")
    for v in nobuy:
        head = f"- {v['symbol']} at {v['time']}: " + (f"{v['judge']}, confidence {v['confidence']:.0%}" if v["judge"] else "no verdict")
        if v["proposed"]:
            head += f" (strategy proposed {v['proposed']})"
        L += [head, f"  Why not: {v['reason']}", ""]
    L.append("")

    if sells:
        L.append(f"JUDGE APPROVED TO SELL ({len(sells)})")
        for v in sells:
            L += [f"- {v['symbol']} at {v['time']}, confidence {v['confidence']:.0%}  ->  {_result(v)}",
                  f"  Why sell: {v['reason']}", ""]
        L.append("")

    trades = j.q("SELECT symbol, qty, entry_price, exit_price, net_pnl, exit_reason FROM trades "
                 "WHERE run_id=? AND substr(exit_ts,1,10)=? ORDER BY exit_ts", (run_id, day))
    L.append("DAY SUMMARY")
    if dayrow:
        L.append(f"  Result: {_rs(dayrow['pnl'])} ({dayrow['pnl'] / dayrow['start_equity'] * 100:+.2f}%), "
                 f"account value Rs {dayrow['end_equity']:,.2f}")
        L.append(f"  Closed trades: {dayrow['trades']} ({dayrow['wins']} wins, {dayrow['losses']} losses), charges Rs {dayrow['charges']:,.2f}")
    for t in trades:
        L.append(f"  {t['symbol']}: {t['qty']} @ {t['entry_price']:,.2f} -> {t['exit_price']:,.2f}, "
                 f"net {_rs(t['net_pnl'])} ({'sell decision' if t['exit_reason'] == 'ai' else t['exit_reason'].replace('_', ' ')})")
    if not trades:
        L.append("  No closed trades.")
    if not vs:
        L += ["", "No debates ran today, so the Judge made no calls."]

    pnl = f", {_rs(dayrow['pnl'])}" if dayrow else ""
    subject = f"Paper trading {day}: Judge approved {len(buys)} buy{'s' if len(buys) != 1 else ''}, {len(nobuy)} not bought{pnl}"
    return subject, "\n".join(L) + "\n"


def recipient(cfg: dict) -> str:
    """ARENA_MAIL_TO in .env, else [email] to in config.toml, else the sending account itself."""
    return (os.environ.get("ARENA_MAIL_TO") or cfg.get("email", {}).get("to") or os.environ.get("ARENA_SMTP_USER") or "").strip()


def configured(cfg: dict) -> bool:
    e = cfg.get("email", {})
    return bool(e.get("enabled") and recipient(cfg) and os.environ.get("ARENA_SMTP_USER") and os.environ.get("ARENA_SMTP_PASSWORD"))


def _tls_context() -> ssl.SSLContext:
    # python.org builds of Python on macOS ship without trusted root certificates; use certifi's when present.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def send(cfg: dict, subject: str, body: str) -> None:
    e = cfg.get("email", {})
    user, pw = os.environ.get("ARENA_SMTP_USER"), os.environ.get("ARENA_SMTP_PASSWORD")
    if not (user and pw):
        raise RuntimeError("Email is not set up: add ARENA_SMTP_USER and ARENA_SMTP_PASSWORD to .env")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, user, recipient(cfg)
    msg.set_content(body)
    with smtplib.SMTP_SSL(e.get("smtp_host", "smtp.gmail.com"), int(e.get("smtp_port", 465)),
                          context=_tls_context(), timeout=30) as s:
        s.login(user, pw.replace(" ", ""))
        s.send_message(msg)


def build_verdict(j: Journal, run_id: int, debate_id: int, outcome: str) -> tuple[str, str]:
    """One email per Judge verdict: buy / don't buy / sell, the full reason, and what the engine did."""
    d = j.one("SELECT * FROM debates WHERE id=?", (debate_id,))
    v = next(x for x in _verdicts(j, run_id, d["ts"][:10]) if x["id"] == debate_id)
    acct = j.one("SELECT cash, realized FROM account WHERE run_id=?", (run_id,))
    jv = v["judge"] or "NO VERDICT"
    conf = f" ({v['confidence']:.0%} confidence)" if v["confidence"] is not None else ""
    word = {"BUY": "BUY", "SELL": "SELL", "HOLD": "DON'T BUY (hold)", "SKIP": "DON'T BUY (skip)"}.get(jv, jv)
    why = {"BUY": "Why buy", "SELL": "Why sell"}.get(jv, "Why not")
    done = outcome if outcome and outcome != "hold" else "no trade"
    o = v["order"]
    bought = jv == "BUY" and o and o["status"] == "filled" and o["side"] == "BUY"
    if bought:
        subject = f"BOUGHT {v['symbol']}: {o['qty']} shares @ Rs {o['fill_price']:,.2f} - Judge's reason inside"
    else:
        subject = f"Judge: {word} {v['symbol']}{conf} - {done[:70]}"
    snap = json.loads(d["snapshot_json"] or "{}")
    m = snap.get("market") or {}
    news = json.loads(d["research_json"] or "[]")
    trig = json.loads(d["triggers"] or "[]")

    L = [f"FINAL JUDGEMENT: {word} {v['symbol']}{conf}",
         f"Time: {d['ts'][:16].replace('T', ' ')} IST  (verdict #{debate_id})",
         "PAPER TRADING ONLY. No real money. This is the simulator's reasoning, not investment advice.", ""]
    if bought:
        cost = o["qty"] * o["fill_price"]
        L.append(f"TRADE: bought {o['qty']} {v['symbol']} shares at Rs {o['fill_price']:,.2f} (about Rs {cost:,.0f} of virtual money)")
        if v["stop"]:
            L.append(f"  Sell automatically if it falls to Rs {o['fill_price'] * (1 - v['stop'] / 100):,.2f} "
                     f"(stop-loss -{v['stop']}%) or rises to Rs {o['fill_price'] * (1 + v['target'] / 100):,.2f} (target +{v['target']}%).")
    else:
        L.append(f"WHAT HAPPENED: {done}")
    L += ["", f"{'REASON TO BUY' if jv == 'BUY' else why.upper()} (the Judge's own words):", v["reason"], "",
          "-" * 60, "SUPPORTING DETAILS", ""]

    L += ["WHY THIS STOCK WAS DEBATED"]
    L += [f"  - {_plain_trigger(t)}" for t in trig] or ["  - (no trigger recorded)"]
    pt = snap.get("proposed_trade")
    if pt:
        L.append(f"  - The rules strategy proposed: {pt['action']} with {pt.get('allocation_pct', 0)}% of capital, "
                 f"stop-loss {pt.get('stop_loss_pct')}% below, target {pt.get('target_pct')}% above.")
    L.append("")

    L += ["WHERE THE DATA CAME FROM",
          f"  1. Prices: {_feed_name(snap.get('data_source'))}. One-minute prices grouped into 5-minute candles."
          + (f" The latest price was {m.get('quote_age_s', snap.get('quote_age_s', 0)):.0f} seconds old." if m else ""),
          "  2. Indicators (VWAP, RSI, moving averages, volume, ATR): calculated by this program from those prices.",
          "  3. Whole market: Nifty 50, Nifty Bank and India VIX from Yahoo Finance." if snap.get("market_mood")
          else "  3. Whole market: not available for this debate.",
          "  4. Official filings: NSE's announcement and results feeds (nseindia.com)."]
    official = [n for n in news if str(n.get("source", "")).startswith("nse")]
    press = [n for n in news if not str(n.get("source", "")).startswith("nse")]
    L += _news_lines(official, f"No NSE filings for {v['symbol']} in the last 24 hours.")
    L.append("  5. News headlines: Google News, Economic Times, Moneycontrol and Mint (reporting, not verified facts).")
    L += _news_lines(press, f"No news headlines naming {v['symbol']} were found.")
    L += ["  6. The debate: three Claude agents (via your Claude Code plan) read only items 1-5 above.",
          "     Bull argues for buying, Bear argues against, the Judge weighs both and decides.",
          "     They do not browse the web or use any other source.", ""]

    if m:
        L += ["THE NUMBERS, IN PLAIN WORDS"] + [f"  - {x}" for x in _plain_numbers(m)]
        L += [f"  - {x}" for x in _plain_mood(snap.get("market_mood"))] + [""]

    bull, bear = _opening(j, debate_id, "bull"), _opening(j, debate_id, "bear")
    if bull or bear:
        L.append("WHAT EACH SIDE SAID")
        if bull:
            L += [f"  Bull ({bull[0]}): {bull[1]}"]
        if bear:
            L += [f"  Bear ({bear[0]}): {bear[1]}"]
        L.append("")

    if acct:
        L.append(f"Account now: cash Rs {acct['cash']:,.2f}, realised P&L so far {_rs(acct['realized'])}")
    return subject, "\n".join(L) + "\n"


def _news_lines(items: list[dict], empty: str) -> list[str]:
    from .research import SOURCE_LABELS
    if not items:
        return [f"     - {empty}"]
    out = []
    for n in items[:6]:
        label = SOURCE_LABELS.get(n.get("source", ""), n.get("source", ""))
        out.append(f"     - [{label}] {n.get('ts', '')[5:16].replace('T', ' ')}  {n.get('headline', '')[:170]}")
        if n.get("url") and n.get("source") != "google_news":  # Google's links are long redirects
            out.append(f"       {n['url']}")
    return out


def _plain_mood(mood: dict | None) -> list[str]:
    if not mood:
        return []
    out = []
    for key, name in (("nifty50", "Nifty 50 (the 50 biggest companies)"), ("nifty_bank", "Nifty Bank (bank stocks)")):
        x = mood.get(key)
        if x:
            out.append(f"Market: {name} is {x['chg_pct_today']:+.2f}% today at {x['last']:,.2f}"
                       + (f", {x['mom_30m_pct']:+.2f}% in the last 30 minutes." if x.get("mom_30m_pct") is not None else "."))
    vix = mood.get("india_vix")
    if vix:
        lvl = "calm" if vix["last"] < 14 else "normal" if vix["last"] < 20 else "nervous"
        out.append(f"India VIX (the market's fear gauge) {vix['last']:.2f}, {vix['chg_pct_today']:+.2f}% today: {lvl}.")
    return out


def _feed_name(src: str | None) -> str:
    return {"yahoo": "Yahoo Finance (NSE quotes, free feed, can lag the exchange by a minute or more)",
            "upstox": "Upstox market data (live NSE quotes)"}.get(src or "", src or "unknown")


def _plain_trigger(t: str) -> str:
    if t.startswith("strategy proposes "):
        return "The rules strategy's signal: " + t[len("strategy proposes "):]
    if t.startswith("volume_spike"):
        return f"Unusually heavy trading: {t.split()[-1]} the normal volume for this time of day."
    if "sma9_cross up" in t:
        return "The short-term average price just crossed above the longer-term average (an early uptrend signal)."
    if "sma9_cross down" in t:
        return "The short-term average price just crossed below the longer-term average (an early downtrend signal)."
    if "breakout" in t:
        return "The price reached a new high for the day."
    return t


def _plain_numbers(m: dict) -> list[str]:
    out = []
    if m.get("ltp") is not None:
        out.append(f"Price: Rs {m['ltp']:,.2f} ({m.get('chg_pct', 0):+.2f}% today). Opened at Rs {m.get('day_open', 0):,.2f}; "
                   f"today's range Rs {m.get('day_low', 0):,.2f} to Rs {m.get('day_high', 0):,.2f}.")
    if m.get("vwap"):
        side = "above" if m.get("above_vwap") else "below"
        mood = "buyers slightly in control" if side == "above" else "sellers slightly in control"
        out.append(f"VWAP Rs {m['vwap']:,.2f}: the average price everyone paid today, weighted by volume. "
                   f"Price is {side} it ({mood}).")
    if m.get("rsi14") is not None:
        r = m["rsi14"]
        lab = "overbought (may be due a pullback)" if r >= 70 else "oversold (may be due a bounce)" if r <= 30 else "neutral"
        out.append(f"RSI {r:.1f} on a 0-100 scale: above 70 = overbought, below 30 = oversold. This is {lab}.")
    if m.get("sma9") and m.get("sma21"):
        up = m["sma9"] > m["sma21"]
        out.append(f"Moving averages: last 9 candles avg Rs {m['sma9']:,.2f} vs last 21 candles avg Rs {m['sma21']:,.2f}. "
                   f"Short-term {'above' if up else 'below'} longer-term = {'mild uptrend' if up else 'mild downtrend'}"
                   f" (gap {abs(m['sma9'] - m['sma21']) / m['sma21'] * 100:.2f}%).")
    if m.get("mom_30m_pct") is not None:
        out.append(f"Last 30 minutes: price moved {m['mom_30m_pct']:+.2f}%.")
    if m.get("vol_ratio") is not None:
        vr = m["vol_ratio"]
        lab = "much quieter than usual, so moves are less reliable" if vr < 0.7 else \
              "busier than usual" if vr > 1.5 else "about normal"
        out.append(f"Volume: {vr:.2f}x the normal amount for this time of day ({lab}).")
    if m.get("atr_pct") is not None:
        out.append(f"Typical move per 5-minute candle (ATR): {m['atr_pct']:.2f}%. Buying and selling costs about 0.1% in "
                   f"brokerage and taxes, so small moves can be eaten up by costs.")
    return out


def _opening(j: Journal, debate_id: int, agent: str) -> tuple[str, str] | None:
    row = j.one("SELECT payload_json FROM debate_events WHERE debate_id=? AND agent=? AND kind='agent_reply' "
                "ORDER BY round, id LIMIT 1", (debate_id, agent))
    if not row:
        return None
    try:
        rep = json.loads(row["payload_json"]).get("reply") or {}
    except (ValueError, AttributeError):
        return None
    thesis = rep.get("thesis") or ""
    return (rep.get("stance", "?"), thesis) if thesis else None


def send_async(cfg: dict, subject: str, body: str, what: str) -> None:
    """Send in the background so a slow mail server never delays a trade. Never raises."""
    if not cfg.get("email", {}).get("enabled"):
        return
    if not configured(cfg):
        log.info("Email skipped (%s): add ARENA_SMTP_USER and ARENA_SMTP_PASSWORD to .env.", what)
        return

    def go():
        try:
            send(cfg, subject, body)
            log.info("Emailed %s to %s", what, recipient(cfg))
        except Exception as ex:
            log.warning("Email failed (%s): %s", what, ex)
    threading.Thread(target=go, daemon=True).start()


def send_day_report(cfg: dict, j: Journal, run_id: int, day: str) -> None:
    """Called after the market closes. Never raises: a mail problem must not stop trading."""
    if not cfg.get("email", {}).get("enabled") or not cfg["email"].get("daily", False):
        return
    if not configured(cfg):
        log.info("Daily email skipped: add ARENA_SMTP_USER and ARENA_SMTP_PASSWORD to .env to receive it.")
        return
    try:
        subject, body = build(j, run_id, day)
        send(cfg, subject, body)
        log.info("Daily Judge report emailed to %s", recipient(cfg))
    except Exception as ex:
        log.warning("Daily email failed: %s", ex)
