"""Pydantic schemas for research items and the Bull / Bear / Judge agents.

Every agent reply is validated against one of these before anything else
reads it. Facts (research items, the market snapshot) and AI interpretation
(agent arguments) are kept in separate models.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- facts -----------------------------------------------------------------
class ResearchItem(_Strict):
    """One factual item from an official or authorised source. No AI text here."""
    uid: str                      # dedupe key (hash of source + link + title)
    source: str                   # e.g. "nse_announcements"
    ts: str                       # publication time, ISO 8601 IST
    headline: str
    summary: str                  # the source's own text, trimmed
    symbols: list[str]
    url: str
    extraction_confidence: float  # how sure we are about the symbol mapping, 0..1
    material: bool = False        # matched a material-event keyword


# ---- agent replies (AI interpretation) --------------------------------------
class Evidence(_Strict):
    claim: str
    source: str = Field(description="'snapshot.<field>' for market data, or a research uid")


class CaseOutput(_Strict):
    """Opening case from Bull or Bear."""
    stance: Literal["bullish", "bearish", "neutral"]
    thesis: str
    points: list[str]
    evidence: list[Evidence]
    entry_conditions: list[str]   # Bull: what must hold to enter. Bear: what would invalidate the long.
    risks: list[str]
    confidence: float = Field(ge=0, le=1)


class RebuttalOutput(_Strict):
    """A challenge, response, or final counterargument in a debate round."""
    addresses: list[str]          # which of the other side's points this answers
    arguments: list[str]
    evidence: list[Evidence]
    concessions: list[str]
    missing_evidence: list[str]
    confidence: float = Field(ge=0, le=1)


JudgeAction = Literal["PAPER_BUY", "PAPER_SELL", "HOLD", "SKIP"]


class JudgeDecision(_Strict):
    """The Judge's verdict. It never touches cash or positions: the paper engine does."""
    action: JudgeAction
    confidence: float = Field(ge=0, le=1)
    allocation_pct: float = Field(ge=0, le=100)
    stop_loss_pct: float = Field(ge=0, le=20)
    target_pct: float = Field(ge=0, le=50)
    rationale: str
    bull_score: float = Field(ge=0, le=10)
    bear_score: float = Field(ge=0, le=10)
    evidence_cited: list[str]
    risk_flags: list[str]
    missing_information: list[str]
    data_freshness_ok: bool
    news_reliability: Literal["high", "medium", "low", "none"]
    market_regime: str
