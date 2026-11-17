"""Debate engine, risk guard, research and paper-engine integration. No network, no real Claude calls."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from arena import safety  # noqa: E402
from arena.clock import IST  # noqa: E402
from arena.config import load_config  # noqa: E402
from arena.db import Journal  # noqa: E402
from arena.debate import DebateBrain, DebateEngine, detect_triggers, freeze, guard, judge_approved  # noqa: E402
from arena.events import EventBus  # noqa: E402
from arena.feeds.sim import SimFeed  # noqa: E402
from arena.llm import FakeTransport  # noqa: E402
from arena.research import ResearchEngine, parse_rss  # noqa: E402
from arena.runner import Runner  # noqa: E402
from arena.schemas import JudgeDecision  # noqa: E402

NOW = datetime(2026, 11, 13, 11, 0, tzinfo=IST)


def case(stance="bullish", conf=0.7):
    return {"stance": stance, "thesis": f"{stance} thesis", "points": ["p1"],
            "evidence": [{"claim": "trend up", "source": "snapshot.market.sma9"}],
            "entry_conditions": ["holds VWAP"], "risks": ["r1"], "confidence": conf}


def rebuttal(conf=0.6):
    return {"addresses": ["p1"], "arguments": ["a1"], "evidence": [{"claim": "c", "source": "snapshot.market.rsi14"}],
            "concessions": [], "missing_evidence": ["order book depth"], "confidence": conf}


def judge(action="PAPER_BUY", conf=0.8, fresh=True, **kw):
    d = {"action": action, "confidence": conf, "allocation_pct": 10, "stop_loss_pct": 1.0, "target_pct": 2.0,
         "rationale": "Bull evidence stronger.", "bull_score": 7, "bear_score": 4, "evidence_cited": ["snapshot.market.sma9"],
         "risk_flags": ["near day high"], "missing_information": [], "data_freshness_ok": fresh,
         "news_reliability": "none", "market_regime": "trending"}
    d.update(kw)
    return d


def scripted(judge_reply=None):
    def reply(agent, user):
        task = json.loads(user)["task"]
        if agent == "judge":
            return judge_reply or judge()
        if task.startswith("Opening"):
            return case("bullish" if agent == "bull" else "bearish")
        return rebuttal()
    return reply


def cfg_tmp():
    tmp = tempfile.mkdtemp()
    cfg = load_config()
    cfg["paths"].update(db=f"{tmp}/t.db", report=f"{tmp}/r.html", data_dir=tmp, log=f"{tmp}/l.log")
    return cfg


def snap(**kw):
    s = {"ltp": 1010.0, "chg_pct": 1.0, "day_open": 1000, "day_high": 1010.5, "day_low": 995, "sma9": 1005, "sma21": 1000,
         "sma9_cross": "none", "rsi14": 62, "vwap": 1003, "above_vwap": True, "atr_pct": 0.5, "mom_30m_pct": 0.4,
         "vol_ratio": 1.1, "last6_closes": [1000, 1002, 1004, 1006, 1008, 1010], "quote_age_s": 20}
    s.update(kw)
    return s


def ctx(market=None, positions=None, entries=True):
    return {"time_ist": "2026-11-13 11:00", "market": market or {"TCS": snap()}, "positions": positions or [],
            "entries_allowed": entries, "account": {"equity": 100000, "cash": 100000, "pnl_today": 0}}


def engine(cfg=None, reply=None, **dc):
    cfg = cfg or cfg_tmp()
    cfg.setdefault("debate", {}).update(dc)
    j = Journal(cfg["paths"]["db"])
    fake = FakeTransport(reply or scripted())
    return DebateEngine(cfg, j, 1, lambda: NOW, transport=fake, bus=EventBus()), fake, j


class Triggers(unittest.TestCase):
    dc = {"price_move_pct": 1.0, "volume_spike_ratio": 2.0, "breakout": True, "sma_cross": True, "news": True}

    def test_quiet_market_no_debate(self):
        self.assertEqual(detect_triggers("X", snap(day_high=1050), 1009.0, [], False, self.dc), [])

    def test_each_trigger(self):
        self.assertTrue(any("price_move" in t for t in detect_triggers("X", snap(day_high=1050), 990.0, [], False, self.dc)))
        self.assertTrue(any("volume" in t for t in detect_triggers("X", snap(day_high=1050, vol_ratio=3), None, [], False, self.dc)))
        self.assertTrue(any("breakout" in t for t in detect_triggers("X", snap(), None, [], False, self.dc)))
        self.assertTrue(any("news" in t for t in detect_triggers("X", snap(day_high=1050), None, [{"headline": "Results"}], False, self.dc)))

    def test_snapshot_is_frozen(self):
        s = {"b": 1, "a": {"x": 2}}
        copy, text, h = freeze(s)
        s["a"]["x"] = 99
        self.assertEqual(copy["a"]["x"], 2)
        self.assertEqual(freeze({"a": {"x": 2}, "b": 1})[2], h)  # key order does not change the hash


class Flow(unittest.TestCase):
    def test_full_debate_order_persistence_and_mapping(self):
        eng, fake, j = engine(rounds=1)
        d = eng.run_debate("TCS", ["breakout"], ctx())
        self.assertEqual(fake.calls, ["bull", "bear", "bear", "bull", "bear", "judge"])
        row = j.one("SELECT * FROM debates WHERE id=?", (d["debate_id"],))
        self.assertEqual(row["status"], "complete")
        self.assertEqual(row["final_action"], "BUY")
        self.assertEqual(j.one("SELECT COUNT(*) n FROM debate_events WHERE debate_id=? AND kind='agent_reply'", (row["id"],))["n"], 5)
        self.assertEqual(j.one("SELECT COUNT(*) n FROM agent_usage WHERE debate_id=?", (row["id"],))["n"], 6)
        self.assertEqual(d["action"], "BUY")
        self.assertEqual(d["allocation_pct"], 10)
        self.assertTrue(d["reason"].startswith(f"[debate #{row['id']}]"))

    def test_bear_opening_is_independent(self):
        seen = {}

        def reply(agent, user):
            p = json.loads(user)
            if agent == "bear" and p["task"].startswith("Opening"):
                seen["bear_open"] = p["debate_so_far"]
            return scripted()(agent, user)
        eng, _, _ = engine(reply=reply)
        eng.run_debate("TCS", ["breakout"], ctx())
        self.assertEqual(seen["bear_open"], [])

    def test_two_rounds(self):
        eng, fake, _ = engine(rounds=2)
        eng.run_debate("TCS", ["breakout"], ctx())
        self.assertEqual(len(fake.calls), 8)

    def test_invalid_judge_output_means_no_trade(self):
        eng, _, j = engine(reply=scripted(judge_reply={"action": "YOLO_BUY"}))
        d = eng.run_debate("TCS", ["breakout"], ctx())
        self.assertEqual(d["action"], "HOLD")
        self.assertEqual(j.one("SELECT status FROM debates")["status"], "failed")

    def test_cooldown_and_daily_cap(self):
        eng, _, _ = engine(cooldown_minutes=30, max_debates_per_day=1)
        c = ctx()
        self.assertEqual([s for s, _ in eng.candidates(c)], ["TCS"])
        eng.run_debate("TCS", ["breakout"], c)
        self.assertEqual(eng.candidates(c), [])  # cooldown (and daily cap) now block it

    def test_budget_cap_stops_calls(self):
        eng, fake, j = engine(max_daily_cost_usd=0.000001)
        eng.run_debate("TCS", ["breakout"], ctx())   # first call goes through and spends past the cap
        n = len(fake.calls)
        d = eng.run_debate("INFY", ["breakout"], ctx({"INFY": snap()}))
        self.assertEqual(len(fake.calls), n)          # no further Claude calls
        self.assertEqual(d["action"], "HOLD")
        self.assertEqual(j.one("SELECT status FROM debates WHERE symbol='INFY'")["status"], "budget")


class JudgeDecidesProposals(unittest.TestCase):
    """The strategy only proposes. A proposal is debated and is taken or left on the Judge's verdict."""

    class Proposer:
        name = "stub"
        def __init__(self, action="BUY"):
            self.action = action
        def decide(self, ctx):
            return {"decisions": [dict(symbol="TCS", action=self.action, allocation_pct=15, confidence=0.6,
                                       stop_loss_pct=1, target_pct=2, reason="uptrend")]}

    def brain(self, judge_reply):
        cfg = cfg_tmp()
        j = Journal(cfg["paths"]["db"])
        b = DebateBrain(cfg, j, 1, lambda: NOW, transport=FakeTransport(scripted(judge_reply)), proposer=self.Proposer())
        b.engine.bus = EventBus()
        return b, j

    def quiet(self):  # no market trigger at all: only the proposal can start a debate
        return ctx({"TCS": snap(day_high=1050, vol_ratio=1.0)})

    def test_proposal_is_debated_and_left_when_judge_says_no(self):
        b, j = self.brain(judge("SKIP"))
        out = {d["symbol"]: d for d in b.decide(self.quiet())["decisions"]}
        self.assertEqual(out["TCS"]["action"], "HOLD")                    # left
        row = j.one("SELECT * FROM debates")
        self.assertIn("strategy proposes BUY", row["triggers"])
        self.assertEqual(json.loads(row["snapshot_json"])["proposed_trade"]["action"], "BUY")
        self.assertFalse(judge_approved(j, {"debate_id": row["id"], "symbol": "TCS", "action": "BUY"}))

    def test_proposal_is_taken_when_judge_approves(self):
        b, j = self.brain(judge("PAPER_BUY"))
        d = next(x for x in b.decide(self.quiet())["decisions"] if x["symbol"] == "TCS")
        self.assertEqual(d["action"], "BUY")                              # taken, with the Judge's sizing
        self.assertEqual(d["allocation_pct"], 10)
        self.assertTrue(judge_approved(j, d, 1))
        self.assertFalse(judge_approved(j, {**d, "action": "SELL"}, 1))   # verdict must match exactly
        self.assertFalse(judge_approved(j, {**d, "symbol": "INFY"}, 1))
        self.assertFalse(judge_approved(j, d, 2))                         # other run
        self.assertFalse(judge_approved(j, {k: v for k, v in d.items() if k != "debate_id"}, 1))


class Guard(unittest.TestCase):
    def setUp(self):
        self.cfg = cfg_tmp()

    def base(self, **kw):
        s = {"position": None, "entries_allowed": True, "stale": False, "quote_age_s": 10, "open_positions": 0}
        s.update(kw)
        return s

    def g(self, verdict, **kw):
        return guard(JudgeDecision(**verdict), self.base(**kw), self.cfg)

    def test_buy_allowed(self):
        self.assertEqual(self.g(judge())[0], "BUY")

    def test_blocks(self):
        for kw, why in [({"stale": True}, "stale"), ({"entries_allowed": False}, "entries"),
                        ({"position": {"symbol": "TCS"}}, "already"), ({"open_positions": 99}, "max open")]:
            action, notes = self.g(judge(), **kw)
            self.assertEqual(action, "HOLD", kw)
            self.assertIn(why, notes[0])
        self.assertEqual(self.g(judge(conf=0.3))[0], "HOLD")
        self.assertEqual(self.g(judge(fresh=False))[0], "HOLD")

    def test_sell_needs_position(self):
        self.assertEqual(self.g(judge("PAPER_SELL"))[0], "HOLD")
        self.assertEqual(self.g(judge("PAPER_SELL"), position={"symbol": "TCS"})[0], "SELL")

    def test_skip_and_hold_never_trade(self):
        self.assertEqual(self.g(judge("SKIP"))[0], "HOLD")
        self.assertEqual(self.g(judge("HOLD"))[0], "HOLD")


class Research(unittest.TestCase):
    RSS = """<rss><channel>
<item><title>Reliance Industries Limited</title><link>https://nsearchives.nseindia.com/corporate/RELIANCE_13112026_OUTCOME.pdf</link>
<description>Reliance Industries Limited has submitted to the Exchange, the financial results for the period ended September 30, 2026.</description>
<pubDate>13-Nov-2026 10:30:00</pubDate></item>
<item><title>Infosys Limited</title><link>https://nsearchives.nseindia.com/corporate/x_13112026_AGM.pdf</link>
<description>Infosys Limited has informed the Exchange about a change in address.</description><pubDate>13-Nov-2026 10:31:00</pubDate></item>
<item><title>Slone Infosystems Limited</title><link>https://nsearchives.nseindia.com/corporate/SLONE_1.pdf</link>
<description>Slone Infosystems Limited has submitted to the Exchange, the financial results.</description>
<pubDate>13-Nov-2026 10:33:00</pubDate></item>
<item><title>Some Other Co</title><link>https://nsearchives.nseindia.com/corporate/OTHER_1.pdf</link><description>x</description>
<pubDate>13-Nov-2026 10:32:00</pubDate></item></channel></rss>"""

    def test_parse_maps_symbols_and_materiality(self):
        items = parse_rss(self.RSS, "nse_announcements", ["RELIANCE", "INFY"], {"INFY": ["infosys"]}, NOW)
        by = {i.symbols[0]: i for i in items}
        self.assertEqual(set(by), {"RELIANCE", "INFY"})            # unrelated company dropped
        self.assertEqual(by["RELIANCE"].extraction_confidence, 0.95)  # from the filing link
        self.assertEqual(by["INFY"].extraction_confidence, 0.7)       # from the company name
        self.assertTrue(by["RELIANCE"].material)
        self.assertFalse(by["INFY"].material)
        self.assertTrue(by["RELIANCE"].ts.startswith("2026-11-13T10:30"))

    def test_dedupe_and_news_trigger_once(self):
        cfg = cfg_tmp()
        cfg["research"]["sources"] = ["nse_announcements"]
        cfg["research"]["news_sources"] = []

        class R:
            text = self.RSS
            def raise_for_status(self): pass

        class Http:
            def get(self, url, **kw): return R()
        j = Journal(cfg["paths"]["db"])
        re_ = ResearchEngine(cfg, j, lambda: NOW, http=Http())
        self.assertEqual(len(re_.poll()), 2)
        self.assertEqual(re_.poll(force=True), [])                    # duplicates ignored
        self.assertEqual(len(re_.fresh_for("RELIANCE")), 1)
        re_.mark_debated([r["uid"] for r in re_.fresh_for("RELIANCE")])
        self.assertEqual(re_.fresh_for("RELIANCE"), [])               # one debate per filing

    NEWS = """<rss><channel>
<item><title>HDFC Bank jumps 3% after Q2 beat - Business Standard</title><link>https://news.google.com/a1</link>
<pubDate>Tue, 17 Nov 2026 04:30:00 GMT</pubDate></item>
<item><title>HDFC Life shares fall on margin worries - Mint</title><link>https://news.google.com/a2</link>
<pubDate>Tue, 17 Nov 2026 04:31:00 GMT</pubDate></item>
<item><title>Reliance Power wins order - ET</title><link>https://news.google.com/a3</link>
<pubDate>Tue, 17 Nov 2026 04:32:00 GMT</pubDate></item>
<item><title>Itchy feet: why investors switch funds - ET</title><link>https://news.google.com/a4</link>
<pubDate>Tue, 17 Nov 2026 04:33:00 GMT</pubDate></item></channel></rss>"""

    def test_news_matching_is_context_only(self):
        from arena.research import NEWS_ALIASES, parse_news_rss
        items = parse_news_rss(self.NEWS, "google_news", ["HDFCBANK", "RELIANCE", "ITC"], NEWS_ALIASES, NOW)
        self.assertEqual([i.symbols for i in items], [["HDFCBANK"]])   # HDFC Life, Reliance Power, "Itchy" dropped
        self.assertEqual(items[0].headline, "HDFC Bank jumps 3% after Q2 beat (Business Standard)")
        self.assertFalse(items[0].material)                             # news never triggers a debate
        self.assertTrue(items[0].ts.startswith("2026-11-17T10:00"))     # GMT converted to IST

    def test_news_feeds_allowed_read_only(self):
        for u in ["https://news.google.com/rss/search?q=x", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
                  "https://www.moneycontrol.com/rss/latestnews.xml", "https://www.livemint.com/rss/markets"]:
            safety.check_url("GET", u)
        for m, u in [("POST", "https://news.google.com/rss/search"), ("GET", "https://www.moneycontrol.com/stocks/marketinfo/x")]:
            with self.assertRaises(safety.RealMoneyBlocked):
                safety.check_url(m, u)

    def test_rss_allowed_but_read_only(self):
        safety.check_url("GET", "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml")
        for m, u in [("POST", "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"),
                     ("GET", "https://nsearchives.nseindia.com/api/equity-stockIndices"),
                     ("GET", "https://www.nseindia.com/api/quote-equity?symbol=TCS")]:
            with self.assertRaises(safety.RealMoneyBlocked):
                safety.check_url(m, u)


class PaperIntegration(unittest.TestCase):
    def test_debate_brain_trades_only_through_paper_engine(self):
        cfg = cfg_tmp()
        cfg["account"]["challenge_days"] = 1
        cfg["market"]["watchlist"] = cfg["market"]["watchlist"][:3]
        cfg["strategy"]["brain"] = "debate"
        cfg["debate"].update(price_move_pct=0.3, max_debates_per_day=6, cooldown_minutes=60, max_daily_cost_usd=100)
        feed = SimFeed(cfg["market"]["watchlist"], n_days=1)
        j = Journal(cfg["paths"]["db"])
        rid = j.new_run("debate e2e", "demo", "sim", "debate", "", 100000, cfg, feed.now().isoformat())
        fake = FakeTransport(scripted())
        brain = DebateBrain(cfg, j, rid, feed.now, transport=fake)
        r = Runner(cfg, feed, j, rid, brain=brain)
        r.run()
        debates = j.q("SELECT * FROM debates")
        self.assertGreater(len(debates), 0)
        self.assertLessEqual(len(debates), 6)                          # daily cap respected
        buys = j.q("SELECT * FROM orders WHERE side='BUY' AND status='filled'")
        self.assertGreater(len(buys), 0)
        self.assertTrue(all(b["note"].startswith("[debate #") for b in buys))
        net = sum(t["net_pnl"] for t in j.q("SELECT net_pnl FROM trades"))
        self.assertAlmostEqual(r.broker.cash - 100000, net, places=1)  # single source of accounting
        self.assertEqual(r.broker.positions, {})                       # intraday square-off still applies
        self.assertEqual({x["brain"] for x in j.q("SELECT DISTINCT brain FROM decisions")}, {"debate"})

    def test_unjudged_trades_never_reach_the_engine(self):
        """A brain in debate mode that skips the debate gets every BUY/SELL rejected."""
        class Sneaky:
            name = "debate"
            def decide(self, ctx):
                return {"market_view": "", "decisions": [dict(symbol=s, action="BUY", allocation_pct=10, confidence=0.9,
                        stop_loss_pct=1, target_pct=2, reason="no debate") for s in ctx["market"]]}
        cfg = cfg_tmp()
        cfg["account"]["challenge_days"] = 1
        cfg["market"]["watchlist"] = cfg["market"]["watchlist"][:3]
        feed = SimFeed(cfg["market"]["watchlist"], n_days=1)
        j = Journal(cfg["paths"]["db"])
        rid = j.new_run("gate", "demo", "sim", "debate", "", 100000, cfg, feed.now().isoformat())
        Runner(cfg, feed, j, rid, brain=Sneaky()).run()
        self.assertEqual(j.one("SELECT COUNT(*) n FROM orders WHERE status='filled'")["n"], 0)
        outcomes = {x["outcome"] for x in j.q("SELECT DISTINCT outcome FROM decisions WHERE action='BUY'")}
        self.assertEqual(outcomes, {"rejected: no Judge approval"})

    def test_resume_refuses_feed_switch(self):
        from arena.__main__ import FeedMismatch, check_same_feed
        with self.assertRaises(FeedMismatch):
            check_same_feed({"id": 1, "feed": "yahoo"}, "upstox")
        check_same_feed({"id": 1, "feed": "yahoo"}, "yahoo")


if __name__ == "__main__":
    unittest.main()
