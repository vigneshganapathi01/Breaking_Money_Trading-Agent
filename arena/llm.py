"""How the debate agents talk to Claude, plus token and cost tracking.

Transports:
- AnthropicTransport: the Anthropic API with structured outputs (Pydantic-validated).
- CLITransport: the Claude Code CLI (`claude -p`), tools and MCP off; reply is validated here.
- FakeTransport: scripted replies for tests and offline runs. Never calls a network.

Every call is recorded in the `agent_usage` table, and a daily cost cap stops
new calls once it is reached.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

# USD per million tokens (input, output). Used for the daily cost estimate only.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-fable-5-2": (8.00, 40.00),
}


class BudgetExceeded(RuntimeError):
    """The daily Claude cost cap for the debate system has been reached."""


class UsageTracker:
    def __init__(self, journal, run_id: int | None, daily_cap_usd: float, clock: Callable[[], datetime]):
        self.j, self.run_id, self.cap, self.clock = journal, run_id, daily_cap_usd, clock

    def spent_today(self) -> float:
        d = self.clock().date().isoformat()
        r = self.j.one("SELECT COALESCE(SUM(cost_usd),0) c FROM agent_usage WHERE substr(ts,1,10)=?", (d,))
        return float(r["c"])

    def check(self) -> None:
        if self.cap and self.spent_today() >= self.cap:
            raise BudgetExceeded(f"daily Claude budget of ${self.cap:.2f} reached")

    def record(self, agent: str, model: str, debate_id: int | None, tin: int, tout: int, ok: bool) -> float:
        pin, pout = PRICES.get(model, (0.0, 0.0))
        cost = (tin * pin + tout * pout) / 1e6
        self.j.x("INSERT INTO agent_usage(run_id, ts, debate_id, agent, model, input_tokens, output_tokens, cost_usd, ok) "
                 "VALUES (?,?,?,?,?,?,?,?,?)",
                 (self.run_id, self.clock().isoformat(), debate_id, agent, model, tin, tout, round(cost, 6), int(ok)))
        return cost


class AnthropicTransport:
    name = "claude-api"

    def __init__(self):
        import anthropic

        self.client = anthropic.Anthropic()

    def call(self, *, agent: str, model: str, effort: str, system: str, user: str, schema: type[T]) -> tuple[T, int, int]:
        resp = self.client.beta.messages.parse(
            model=model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": effort},
            output_format=schema,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError("model declined this request")
        if resp.stop_reason == "max_tokens" or resp.parsed_output is None:
            raise RuntimeError(f"incomplete reply (stop_reason={resp.stop_reason})")
        return resp.parsed_output, resp.usage.input_tokens, resp.usage.output_tokens


def _extract_json(text: str) -> str:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("no JSON object in reply")
    return m.group(0)


def find_claude() -> str | None:
    """The Claude Code CLI: PATH first, then the standard install locations (a panel started
    from the desktop app may not have ~/.local/bin on PATH). CLAUDE_CLI overrides."""
    import os
    cands = [os.environ.get("CLAUDE_CLI"), shutil.which("claude"),
             os.path.expanduser("~/.local/bin/claude"), os.path.expanduser("~/.claude/local/claude"),
             "/opt/homebrew/bin/claude", "/usr/local/bin/claude"]
    # Last resort: the copy bundled with the Claude desktop app (newest version; must be signed in once).
    import glob
    bundled = glob.glob(os.path.expanduser(
        "~/Library/Application Support/Claude/claude-code/*/*/claude.app/Contents/MacOS/claude"))
    cands += sorted(bundled, key=os.path.getmtime, reverse=True)
    return next((c for c in cands if c and os.path.isfile(c) and os.access(c, os.X_OK)), None)


def run_claude(exe: str, prompt: str, model: str = "") -> dict:
    """One headless `claude -p` call: no tools, no MCP servers, nothing saved to session history."""
    cmd = [exe, "-p", prompt, "--output-format", "json", "--tools", "", "--strict-mcp-config",
           "--no-session-persistence"]
    if model:
        cmd += ["--model", model]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    try:
        data = json.loads(out.stdout)
    except ValueError:
        raise RuntimeError((out.stderr or out.stdout).strip()[:500] or f"claude exited {out.returncode}")
    if data.get("is_error") or out.returncode != 0:
        msg = str(data.get("result") or out.stderr).strip()[:300]
        if "logged in" in msg.lower() or "/login" in msg:
            msg += " (run `claude` in a terminal once and sign in with your Claude account)"
        raise RuntimeError(f"Claude Code: {msg}")
    return data


class CLITransport:
    """Claude Code login instead of an API key. Token counts come from the CLI's JSON output."""
    name = "claude-cli"

    def __init__(self):
        self.exe = find_claude()
        if not self.exe:
            raise RuntimeError("Claude Code CLI not found. Install it and sign in once (see README).")

    def call(self, *, agent: str, model: str, effort: str, system: str, user: str, schema: type[T]) -> tuple[T, int, int]:
        prompt = (f"{system}\n\n{user}\n\nReply with ONLY a JSON object matching this JSON schema, no prose:\n"
                  + json.dumps(schema.model_json_schema()))
        data = run_claude(self.exe, prompt, model)
        usage = data.get("usage") or {}
        parsed = schema.model_validate_json(_extract_json(data.get("result", "")))
        return parsed, int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))


class FakeTransport:
    """Returns replies from a function(agent_role, user_prompt) -> dict. For tests and dry runs."""
    name = "fake"

    def __init__(self, reply: Callable[[str, str], dict]):
        self.reply = reply
        self.calls: list[str] = []

    def call(self, *, agent: str, model: str, effort: str, system: str, user: str, schema: type[T]) -> tuple[T, int, int]:
        self.calls.append(agent)
        return schema.model_validate(self.reply(agent, user)), len(user) // 4, 200


def make_transport(kind: str):
    if kind == "claude-api":
        return AnthropicTransport()
    if kind == "claude-cli":
        return CLITransport()
    raise ValueError(f"Unknown transport '{kind}'. Use 'claude-api' or 'claude-cli'.")


def call_agent(transport, tracker: UsageTracker, *, agent: str, model: str, effort: str, system: str, user: str,
               schema: type[T], debate_id: int | None) -> T:
    """One validated agent call, with budget check and usage recording."""
    tracker.check()
    try:
        out, tin, tout = transport.call(agent=agent, model=model, effort=effort, system=system, user=user, schema=schema)
    except (ValidationError, ValueError, RuntimeError):
        tracker.record(agent, model, debate_id, 0, 0, ok=False)
        raise
    tracker.record(agent, model, debate_id, tin, tout, ok=True)
    return out
