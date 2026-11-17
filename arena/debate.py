"""Debate orchestrator: event-driven Bull vs Bear debates, judged, then paper-traded.

Flow for one triggered symbol:
  1. trigger detected (price move, volume spike, breakout/breakdown, SMA cross, material news)
  2. immutable market snapshot is frozen (JSON + SHA-256 hash stored)
  3. related research collected
  4. Bull opening case          5. Bear opening case (independent: does not see Bull)
  6. Bear challenges Bull       7. Bull answers the objections   (repeated `rounds` times)
  8. Bear final counterarguments
  9. Judge verdict (PAPER_BUY / PAPER_SELL / HOLD / SKIP)
 10. Risk guard validates the verdict against the snapshot and config (it can only make it MORE cautious)
 11. Eligible verdicts are handed to the existing Runner/PaperBroker as a normal BUY/SELL/HOLD decision
 12. Every step is persisted in `debates` / `debate_events` and published on the event bus

DebateBrain exposes the same decide(ctx) interface as the other brains, so the
existing Runner, PaperBroker, charges, risk rules and reports are reused unchanged.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta

from . import agents
from .events import BUS
from .llm import BudgetExceeded, UsageTracker, call_agent, make_transport
from .schemas import CaseOutput, JudgeDecision, RebuttalOutput

log = logging.getLogger("arena")

DEFAULTS = {
    "rounds": 1,
    "cooldown_minutes": 30,
    "max_debates_per_cycle": 2,
    "max_debates_per_day": 20,
    "max_daily_cost_usd": 5.0,
    "price_move_pct": 1.0,
    "volume_spike_ratio": 2.0,
    "breakout": True,
    "sma_cross": True,
    "news": True,
    "transport": "claude-api",
    "proposer": "rules",
    "require_judge": True,
}
AGENT_DEFAULTS = {"model": "claude-opus-5-5", "effort": "medium"}


def debate_cfg(cfg: dict) -> dict:
    return {**DEFAULTS, **cfg.get("debate", {})}


def agent_cfg(cfg: dict, role: str) -> dict:
    a = cfg.get("agents", {})
    return {**AGENT_DEFAULTS, **{k: v for k, v in a.items() if not isinstance(v, dict)}, **a.get(role, {})}


# ---- triggers ----------------------------------------------------------------
def detect_triggers(sym: str, snap: dict, ref_price: float | None, news: list[dict], held: bool, dc: dict) -> list[str]:
    """Why this symbol deserves a debate now. Empty list means no debate."""
    out = []
    ltp = snap.get("ltp")
    if ref_price and ltp and abs(ltp / ref_price - 1) * 100 >= dc["price_move_pct"]:
        out.append(f"price_move {(ltp / ref_price - 1) * 100:+.2f}% since last check")
    if (snap.get("vol_ratio") or 0) >= dc["volume_spike_ratio"]:
        out.append(f"volume_spike {snap['vol_ratio']}x")
    if dc["breakout"] and "sma9" in snap and ltp:
        if ltp >= snap["day_high"] * 0.999 and snap["sma9"] > snap["sma21"]:
            out.append("breakout at day high")
        elif held and ltp <= snap["day_low"] * 1.001:
            out.append("breakdown at day low (position held)")
    if dc["sma_cross"] and snap.get("sma9_cross") in ("up", "down"):
        out.append(f"sma9_cross {snap['sma9_cross']}")
    if dc["news"] and news:
        out.append(f"news: {news[0]['headline'][:80]}")
    return out


def freeze(snapshot: dict) -> tuple[dict, str, str]:
    """Return (deep copy, canonical JSON, SHA-256). Agents all see the same bytes."""
    text = json.dumps(snapshot, sort_keys=True, default=str)
    return json.loads(text), text, hashlib.sha256(text.encode()).hexdigest()[:16]


# ---- risk guard ----------------------------------------------------------------
def guard(dec: JudgeDecision, snap: dict, cfg: dict) -> tuple[str, list[str]]:
    """Map a Judge verdict to an engine action. Can only downgrade, never escalate."""
    st, notes = cfg["strategy"], []
    held = snap["position"] is not None
    action = {"PAPER_BUY": "BUY", "PAPER_SELL": "SELL"}.get(dec.action, "HOLD")
    if action == "BUY":
        if held:
            notes.append("blocked: already holding")
        elif not snap["entries_allowed"]:
            notes.append("blocked: new entries not allowed now")
        elif snap["stale"]:
            notes.append(f"blocked: market data stale ({snap['quote_age_s']:.0f}s old)")
        elif snap["open_positions"] >= st["max_positions"]:
            notes.append("blocked: max open positions")
        elif dec.confidence < max(st.get("min_confidence", 0), cfg.get("debate", {}).get("min_judge_confidence", 0.6)):
            notes.append(f"blocked: judge confidence {dec.confidence:.2f} below threshold")
        elif not dec.data_freshness_ok:
            notes.append("blocked: judge flagged data freshness")
        if notes:
            action = "HOLD"
    elif action == "SELL" and not held:
        notes.append("ignored: PAPER_SELL without a position (long-only)")
        action = "HOLD"
    return action, notes


# ---- orchestrator --------------------------------------------------------------
class DebateEngine:
    def __init__(self, cfg: dict, journal, run_id: int | None, clock, transport=None, research=None, bus=BUS):
        self.cfg, self.j, self.run_id, self.clock, self.bus = cfg, journal, run_id, clock, bus
        self.dc = debate_cfg(cfg)
        self.transport_error = ""
        try:
            self.transport = transport or make_transport(self.dc["transport"])
        except Exception as e:  # never crash the session: stops, targets and square-off must keep running
            self.transport, self.transport_error = None, str(e)
            log.warning("Judge unavailable (%s). No debates, so no new trades; risk exits still run.", e)
        self.tracker = UsageTracker(journal, run_id, self.dc["max_daily_cost_usd"], clock)
        self.research = research
        self.ref_price: dict[str, float] = {}
        self.last_debate: dict[str, datetime] = {}
        self.pending_news: dict[str, list[str]] = {}
        # Set by the Runner: runs stop-loss/target/daily-loss checks while a debate is in progress.
        self.between_calls = None

    def _risk_check(self) -> None:
        if self.between_calls:
            try:
                self.between_calls()
            except Exception as e:  # a failed check must not kill the debate
                log.warning("Risk check during debate failed: %s", e)

    # events: persisted and published
    def _event(self, debate_id: int | None, agent: str, kind: str, rnd: int, data: dict) -> None:
        now = self.clock()
        if debate_id:
            self.j.x("INSERT INTO debate_events(debate_id, ts, agent, kind, round, payload_json) VALUES (?,?,?,?,?,?)",
                     (debate_id, now.isoformat(), agent, kind, rnd, json.dumps(data, default=str)))
        self.bus.publish(kind, {"debate_id": debate_id, "agent": agent, "round": rnd, **data}, now)

    def debates_today(self) -> int:
        d = self.clock().date().isoformat()
        return self.j.one("SELECT COUNT(*) n FROM debates WHERE substr(ts,1,10)=?", (d,))["n"]

    def candidates(self, ctx: dict, proposals: dict | None = None) -> list[tuple[str, list[str]]]:
        """Stocks to debate now: every strategy proposal, plus market and news events."""
        proposals = proposals or {}
        now = self.clock()
        held = {p["symbol"] for p in ctx["positions"]}
        cool = timedelta(minutes=self.dc["cooldown_minutes"])
        out = []
        for sym, snap in ctx["market"].items():
            news = self.research.fresh_for(sym) if self.research else []
            trig = detect_triggers(sym, snap, self.ref_price.get(sym), news, sym in held, self.dc)
            if sym in proposals:
                p = proposals[sym]
                trig.insert(0, f"strategy proposes {p['action']}: {str(p.get('reason', ''))[:100]}")
            self.pending_news[sym] = [n["uid"] for n in news]
            if sym not in self.ref_price and snap.get("ltp"):
                self.ref_price[sym] = snap["ltp"]
            if not trig:
                continue
            last = self.last_debate.get(sym)
            if last and now - last < cool and not any(t.startswith("news") for t in trig):
                continue  # cooldown; material news can still break through
            out.append((sym, trig))
        # Proposed trades first, then held positions, then news.
        out.sort(key=lambda x: (x[0] not in proposals, x[0] not in held, not any(t.startswith("news") for t in x[1])))
        room = max(0, self.dc["max_debates_per_day"] - self.debates_today())
        return out[:min(self.dc["max_debates_per_cycle"], room)]

    def build_snapshot(self, sym: str, ctx: dict, proposal: dict | None = None) -> dict:
        snap = ctx["market"][sym]
        pos = next((p for p in ctx["positions"] if p["symbol"] == sym), None)
        age = snap.get("quote_age_s", 0) or 0
        return {
            "symbol": sym, "time_ist": ctx["time_ist"], "market": snap,
            "proposed_trade": ({k: proposal.get(k) for k in ("action", "allocation_pct", "stop_loss_pct", "target_pct", "reason")}
                               if proposal else None),
            "quote_age_s": age, "stale": age > self.cfg["market"].get("max_quote_age_seconds", 300),
            "data_source": self.cfg["market"]["feed"],
            "position": pos, "open_positions": len(ctx["positions"]),
            "entries_allowed": ctx["entries_allowed"],
            "account": {k: ctx["account"][k] for k in ("equity", "cash", "pnl_today")},
            "limits": {k: self.cfg["strategy"][k] for k in ("max_positions", "max_position_pct", "max_daily_loss_pct",
                                                              "no_new_entries_after", "product")},
        }

    def run_debate(self, sym: str, triggers: list[str], ctx: dict, proposal: dict | None = None) -> dict:
        """Run one full debate. Returns a decision dict in the existing brain format."""
        now = self.clock()
        snap = self.build_snapshot(sym, ctx, proposal)
        mood = self.research.market_mood() if self.research and hasattr(self.research, "market_mood") else None
        if mood:
            snap["market_mood"] = mood
        snapshot, snap_json, snap_hash = freeze(snap)
        research = self.research.related(sym) if self.research else []
        cur = self.j.x("INSERT INTO debates(run_id, ts, symbol, triggers, snapshot_json, snapshot_hash, research_json, status) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       (self.run_id, now.isoformat(), sym, json.dumps(triggers), snap_json, snap_hash,
                        json.dumps(research, default=str), "running"))
        did = cur.lastrowid
        self.last_debate[sym] = now
        if self.research:
            self.research.mark_debated(self.pending_news.pop(sym, []))
        self.ref_price[sym] = snapshot["market"].get("ltp") or self.ref_price.get(sym)
        self._event(did, "system", "debate_start", 0,
                    {"symbol": sym, "triggers": triggers, "snapshot_hash": snap_hash, "snapshot": snapshot,
                     "research": research})
        transcript: list[dict] = []

        def ask(role: str, system: str, task: str, schema, rnd: int, visible: list[dict]):
            ac = agent_cfg(self.cfg, role)
            self._risk_check()
            self._event(did, role, "agent_thinking", rnd, {"task": task})
            out = call_agent(self.transport, self.tracker, agent=role, model=ac["model"], effort=ac["effort"],
                             system=system, user=agents.payload(snapshot, research, visible, task),
                             schema=schema, debate_id=did)
            entry = {"agent": role, "round": rnd, "task": task, "reply": out.model_dump()}
            self._event(did, role, "agent_reply", rnd, entry)
            return entry

        try:
            bull = ask("bull", agents.BULL, "Opening case: argue the long side.", CaseOutput, 0, [])
            bear = ask("bear", agents.BEAR, "Opening case: independent risk assessment of a long.", CaseOutput, 0, [])
            transcript += [bull, bear]
            for r in range(1, int(self.dc["rounds"]) + 1):
                transcript.append(ask("bear", agents.BEAR, "Challenge the Bull's thesis point by point.",
                                      RebuttalOutput, r, list(transcript)))
                transcript.append(ask("bull", agents.BULL, "Answer the Bear's objections. Concede what is true.",
                                      RebuttalOutput, r, list(transcript)))
            transcript.append(ask("bear", agents.BEAR, "Final counterarguments. Keep only the strongest.",
                                  RebuttalOutput, int(self.dc["rounds"]) + 1, list(transcript)))
            ac = agent_cfg(self.cfg, "judge")
            self._risk_check()
            self._event(did, "judge", "agent_thinking", 99, {"task": "Verdict"})
            verdict = call_agent(self.transport, self.tracker, agent="judge", model=ac["model"], effort=ac["effort"],
                                 system=agents.JUDGE,
                                 user=agents.payload(snapshot, research, transcript, "Deliver the verdict."),
                                 schema=JudgeDecision, debate_id=did)
        except BudgetExceeded as e:
            return self._finish(did, sym, None, "HOLD", [str(e)], "budget")
        except Exception as e:  # any agent failure: no trade from this debate
            log.warning("Debate %s for %s failed: %s", did, sym, e)
            return self._finish(did, sym, None, "HOLD", [f"agent error: {e}"], "failed")
        action, notes = guard(verdict, snapshot, self.cfg)
        return self._finish(did, sym, verdict, action, notes, "complete")

    def _finish(self, did, sym, verdict: JudgeDecision | None, action: str, notes: list[str], status: str) -> dict:
        v = verdict.model_dump() if verdict else None
        cost = self.j.one("SELECT COALESCE(SUM(cost_usd),0) c FROM agent_usage WHERE debate_id=?", (did,))["c"]
        self.j.x("UPDATE debates SET status=?, judge_json=?, final_action=?, guard_notes=?, cost_usd=? WHERE id=?",
                 (status, json.dumps(v), action, json.dumps(notes), round(cost, 4), did))
        self._event(did, "judge", "verdict", 99, {"symbol": sym, "verdict": v, "final_action": action,
                                                  "guard_notes": notes, "status": status, "cost_usd": round(cost, 4)})
        st = self.cfg["strategy"]
        reason = f"[debate #{did}] " + (v["rationale"] if v else "; ".join(notes))
        if notes and v:
            reason += " | guard: " + "; ".join(notes)
        return {
            "symbol": sym, "action": action,
            "allocation_pct": v["allocation_pct"] if v and action == "BUY" else 0,
            "confidence": v["confidence"] if v else 0,
            "stop_loss_pct": (v["stop_loss_pct"] or st["default_stop_loss_pct"]) if v and action == "BUY" else 0,
            "target_pct": (v["target_pct"] or st["default_target_pct"]) if v and action == "BUY" else 0,
            "reason": reason[:5000], "debate_id": did,
        }


def make_proposer(cfg: dict):
    """Who suggests trades for the agents to debate: 'rules' (free), 'claude-api', 'claude-cli' or 'none'."""
    kind = debate_cfg(cfg)["proposer"]
    if kind == "none":
        return None
    from .brain import ClaudeAPIBrain, ClaudeCLIBrain, RulesBrain
    return {"rules": RulesBrain, "claude-api": ClaudeAPIBrain, "claude-cli": ClaudeCLIBrain}[kind](cfg)


def judge_approved(journal, decision: dict, run_id: int | None = None) -> bool:
    """True only if a completed debate's final, risk-checked action matches this BUY/SELL exactly."""
    did = decision.get("debate_id")
    if not did:
        return False
    row = journal.one("SELECT symbol, status, final_action, run_id FROM debates WHERE id=?", (did,))
    return bool(row and row["status"] == "complete" and row["symbol"] == decision.get("symbol")
                and row["final_action"] == decision.get("action") and (run_id is None or row["run_id"] == run_id))


class DebateBrain:
    """Plugs the debate engine into the existing Runner (same interface as the other brains)."""
    name = "debate"

    def __init__(self, cfg: dict, journal, run_id: int | None, clock, transport=None, research=None, proposer=None):
        self.cfg = cfg
        self.engine = DebateEngine(cfg, journal, run_id, clock, transport, research)
        self.research = research
        self.proposer = proposer if proposer is not None else make_proposer(cfg)

    def decide(self, ctx: dict) -> dict:
        if self.research:
            try:
                new = self.research.poll()
                for item in new:
                    BUS.publish("research", item, self.engine.clock())
            except Exception as e:
                log.warning("Research poll failed: %s", e)
        BUS.publish("market", {"market": ctx["market"], "account": ctx["account"], "positions": ctx["positions"],
                               "entries_allowed": ctx["entries_allowed"]}, self.engine.clock())
        held = {p["symbol"] for p in ctx["positions"]}
        proposals = {}
        if self.proposer:
            try:  # the strategy only PROPOSES; nothing it says reaches the engine without a Judge verdict
                for d in self.proposer.decide(ctx).get("decisions", []):
                    if d.get("action") in ("BUY", "SELL") and d.get("symbol") in ctx["market"]:
                        proposals[d["symbol"]] = d
            except Exception as e:
                log.warning("Proposer %s failed: %s", getattr(self.proposer, "name", "?"), e)
        decisions, debated = [], set()
        if self.engine.transport is None:
            for sym, p in proposals.items():
                log.info("[debate] %s: proposed %s left untaken (Judge unavailable)", sym, p["action"])
            return {"market_view": f"Judge unavailable: {self.engine.transport_error}",
                    "decisions": [{"symbol": s, "action": "HOLD", "allocation_pct": 0, "confidence": 0, "stop_loss_pct": 0,
                                   "target_pct": 0, "reason": "Judge unavailable; nothing is traded without a verdict."}
                                  for s in ctx["market"]]}
        for sym, trig in self.engine.candidates(ctx, proposals):
            log.info("[debate] %s triggered: %s", sym, "; ".join(trig))
            decisions.append(self.engine.run_debate(sym, trig, ctx, proposals.get(sym)))
            debated.add(sym)
        for sym, p in proposals.items():
            if sym not in debated:
                log.info("[debate] %s: proposed %s left untaken (no debate slot: cooldown or daily limit)", sym, p["action"])
        for sym in ctx["market"]:
            if sym not in debated:
                decisions.append({"symbol": sym, "action": "HOLD", "allocation_pct": 0, "confidence": 0,
                                  "stop_loss_pct": 0, "target_pct": 0,
                                  "reason": "No debate trigger." if sym not in held else "Held; no new trigger."})
        spent = self.engine.tracker.spent_today()
        view = (f"Debate engine: {len(debated)} debate(s) this cycle"
                + (f" ({', '.join(sorted(debated))})" if debated else "")
                + f". Claude spend today ${spent:.2f} of ${self.engine.dc['max_daily_cost_usd']:.2f}.")
        return {"market_view": view, "decisions": decisions}

    def write(self, system: str, user: str) -> str:
        raise RuntimeError("Use brain = 'claude-api' or 'claude-cli' for the written coach review.")
