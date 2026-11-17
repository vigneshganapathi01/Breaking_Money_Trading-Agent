"""Live research engine: official NSE filings, plus news headlines and market mood.

Sources (all public RSS feeds, no web page scraping, all HTTP through SafeHTTP's read-only allow-list):
- Official: NSE corporate announcements and financial results (nsearchives.nseindia.com). Material
  filings can trigger a debate.
- News (context only, never a trigger): Google News search per company, Economic Times Markets and
  Stocks, Moneycontrol, Mint. Headlines are third-party reporting, not verified facts.
- Market mood: Nifty 50, Nifty Bank and India VIX from Yahoo Finance, so a single stock is judged
  against the whole market.

Each item keeps only facts (source, time, headline, the filing's own summary,
URL) plus our symbol mapping and how confident that mapping is. AI
interpretation happens later, inside the debate, and is stored separately.
Items are de-duplicated by a hash of source + link + title.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time as _time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import quote_plus

from .clock import IST
from .safety import SafeHTTP
from .schemas import ResearchItem

log = logging.getLogger("arena")

FEEDS = {
    "nse_announcements": "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml",
    "nse_results": "https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml",
}

# Third-party news feeds: headlines matched to watched stocks by company name. Context for the agents only.
NEWS_FEEDS = {
    "et_markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "et_stocks": "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms",
    "moneycontrol_buzzing": "https://www.moneycontrol.com/rss/buzzingstocks.xml",
    "moneycontrol_latest": "https://www.moneycontrol.com/rss/latestnews.xml",
    "livemint_markets": "https://www.livemint.com/rss/markets",
}
GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}+when:1d&hl=en-IN&gl=IN&ceid=IN:en"

SOURCE_LABELS = {
    "nse_announcements": "NSE announcement (official)", "nse_results": "NSE results (official)",
    "google_news": "Google News", "et_markets": "Economic Times", "et_stocks": "Economic Times",
    "moneycontrol_buzzing": "Moneycontrol", "moneycontrol_latest": "Moneycontrol", "livemint_markets": "Mint",
}

# How newspapers name the default watchlist (whole words; ALL-CAPS entries match case-sensitively).
NEWS_ALIASES = {
    "RELIANCE": ["Reliance Industries", "RIL"], "TCS": ["TCS", "Tata Consultancy"],
    "HDFCBANK": ["HDFC Bank"], "INFY": ["Infosys"], "ICICIBANK": ["ICICI Bank"],
    "SBIN": ["SBI", "State Bank of India"], "BHARTIARTL": ["Bharti Airtel", "Airtel"], "ITC": ["ITC"],
    "LT": ["Larsen & Toubro", "Larsen and Toubro", "L&T"], "AXISBANK": ["Axis Bank"],
}
# Same name, different company.
NEWS_EXCLUDE = re.compile(r"reliance (power|infra|capital|communications|home|naval)|itc hotels|hdfc (life|amc|ergo)|"
                          r"icici (prudential|lombard|securities)|sbi (life|card|cards|mutual)|l&t (finance|technology|tech)|"
                          r"axis (mutual|securities)|airtel africa|infosys (bpm|mcCamish)", re.I)

# Company names for the default watchlist, used when the filing link does not start with the symbol.
DEFAULT_ALIASES = {
    "RELIANCE": ["reliance industries"], "TCS": ["tata consultancy"], "HDFCBANK": ["hdfc bank"],
    "INFY": ["infosys"], "ICICIBANK": ["icici bank"], "SBIN": ["state bank of india"],
    "BHARTIARTL": ["bharti airtel"], "ITC": ["itc limited"], "LT": ["larsen & toubro", "larsen and toubro"],
    "AXISBANK": ["axis bank"],
}

MATERIAL = re.compile(
    r"financial result|outcome of board|dividend|bonus|split|buy-?back|acquisition|merger|amalgamation|"
    r"demerger|fund ?rais|qip|preferential|rights issue|credit rating|default|resignation|appointment of (md|ceo)|"
    r"penalty|sebi|litigation|award|contract|insolvency|pledge|block deal|bulk deal|guidance|investor presentation",
    re.I)


def parse_rss(xml_text: str, source: str, watchlist: list[str], aliases: dict[str, list[str]],
              fetched_at: datetime) -> list[ResearchItem]:
    root = ET.fromstring(xml_text)
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        desc = (it.findtext("description") or "").strip()
        when = (it.findtext("pubDate") or "").strip()
        try:
            ts = datetime.strptime(when, "%d-%b-%Y %H:%M:%S").replace(tzinfo=IST)
        except ValueError:
            ts = fetched_at
        syms, conf = match_symbols(title, link, watchlist, aliases)
        if not syms:
            continue  # only keep filings about stocks we watch
        uid = hashlib.sha256(f"{source}|{link}|{title}".encode()).hexdigest()[:20]
        text = f"{title} {desc}"
        out.append(ResearchItem(
            uid=uid, source=source, ts=ts.isoformat(), headline=f"{title}: {desc[:120]}".strip(": "),
            summary=desc[:600], symbols=syms, url=link, extraction_confidence=conf,
            material=bool(MATERIAL.search(text)) or source == "nse_results",
        ))
    return out


def _alias_re(alias: str) -> re.Pattern:
    flags = 0 if alias.isupper() else re.I
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", flags)


def match_news(title: str, watchlist: list[str], aliases: dict[str, list[str]]) -> list[str]:
    if NEWS_EXCLUDE.search(title):
        return []
    return [s for s in watchlist if any(_alias_re(a).search(title) for a in aliases.get(s, []))]


def parse_news_rss(xml_text: str, source: str, watchlist: list[str], aliases: dict[str, list[str]],
                   fetched_at: datetime, only: str | None = None) -> list[ResearchItem]:
    """Publisher RSS (RFC 822 dates). Keeps headlines that name a watched company. Never material:
    news is context for the agents, only official filings can trigger a debate."""
    root = ET.fromstring(xml_text)
    out = []
    for it in root.iter("item"):
        title = unescape((it.findtext("title") or "").strip())
        link = (it.findtext("link") or "").strip()
        desc = re.sub(r"<[^>]+>", " ", unescape(it.findtext("description") or ""))
        desc = re.sub(r"\s+", " ", desc).strip()
        try:
            ts = parsedate_to_datetime((it.findtext("pubDate") or "").strip()).astimezone(IST)
        except (TypeError, ValueError):
            ts = fetched_at
        if re.search(r"stock price.*quote|share price live|live updates", title, re.I):
            continue  # quote pages and live blogs, not news
        syms = match_news(title, watchlist, aliases)
        if only:
            syms = [only] if only in syms else []
        if not syms:
            continue
        publisher = ""
        if source == "google_news" and " - " in title:
            title, publisher = title.rsplit(" - ", 1)
        uid = hashlib.sha256(f"{source}|{link}|{title}".encode()).hexdigest()[:20]
        out.append(ResearchItem(
            uid=uid, source=source, ts=ts.isoformat(),
            headline=(f"{title} ({publisher})" if publisher else title)[:240],
            summary=("" if source == "google_news" else desc[:400]), symbols=syms, url=link,
            extraction_confidence=0.75, material=False,
        ))
    return out


def match_symbols(title: str, link: str, watchlist: list[str], aliases: dict[str, list[str]]) -> tuple[list[str], float]:
    """NSE filing links usually start with the symbol: .../corporate/RELIANCE_13112026..._x.pdf"""
    fname = link.rsplit("/", 1)[-1]
    prefix = fname.split("_", 1)[0].upper()
    if prefix in watchlist:
        return [prefix], 0.95
    # The RSS title is the company name: require it to START with the alias as whole words,
    # so "Slone Infosystems Limited" does not match Infosys.
    t = title.lower()
    hits = [s for s in watchlist if any(re.match(rf"{re.escape(a)}\b", t) for a in aliases.get(s, []))]
    return (hits, 0.7) if hits else ([], 0.0)


class ResearchEngine:
    def __init__(self, cfg: dict, journal, clock, http=None):
        rc = cfg.get("research", {})
        self.enabled = rc.get("enabled", True)
        self.sources = [s for s in rc.get("sources", list(FEEDS)) if s in FEEDS]
        self.news_sources = [s for s in rc.get("news_sources", list(NEWS_FEEDS) + ["google_news"])
                             if s in NEWS_FEEDS or s == "google_news"]
        self.news_aliases = {**NEWS_ALIASES, **{k.upper(): v for k, v in rc.get("news_aliases", {}).items()}}
        self.mood_enabled = rc.get("market_mood", True)
        self._mood: tuple[float, dict] | None = None
        # Google News rate-limits bursts: search a few companies per poll in rotation, back off on HTTP 429.
        self.google_per_poll = rc.get("google_news_per_poll", 2)
        self._g_next = 0
        self._g_pause_until = 0.0
        self.poll_minutes = rc.get("poll_minutes", 5)
        self.fresh_minutes = rc.get("news_trigger_minutes", 60)
        self.lookback_hours = rc.get("lookback_hours", 24)
        self.watchlist = list(cfg["market"]["watchlist"])
        self.aliases = {**DEFAULT_ALIASES, **{k.upper(): [a.lower() for a in v] for k, v in rc.get("aliases", {}).items()}}
        self.j, self.clock = journal, clock
        self.http = http or SafeHTTP()
        self.last_poll: datetime | None = None
        self.debated: set[str] = set()  # uids that already triggered a debate

    def poll(self, force: bool = False) -> list[dict]:
        """Fetch feeds if due. Returns only items not seen before."""
        now = self.clock()
        if not self.enabled or (not force and self.last_poll and now - self.last_poll < timedelta(minutes=self.poll_minutes)):
            return []
        self.last_poll = now
        new = []
        jobs = [(src, FEEDS[src], lambda x, s=src: parse_rss(x, s, self.watchlist, self.aliases, now)) for src in self.sources]
        for src in self.news_sources:
            if src == "google_news":
                if _time.time() < self._g_pause_until or not self.watchlist:
                    continue
                n = min(self.google_per_poll, len(self.watchlist))
                batch = [self.watchlist[(self._g_next + i) % len(self.watchlist)] for i in range(n)]
                self._g_next = (self._g_next + n) % len(self.watchlist)
                for sym in batch:
                    name = (self.news_aliases.get(sym) or [sym])[0]
                    jobs.append((src, GOOGLE_NEWS.format(q=quote_plus(f'"{name}"')),
                                 lambda x, s=sym: parse_news_rss(x, "google_news", self.watchlist, self.news_aliases, now, only=s)))
            else:
                jobs.append((src, NEWS_FEEDS[src],
                             lambda x, s=src: parse_news_rss(x, s, self.watchlist, self.news_aliases, now)))
        failed = set()
        for src, url, parse in jobs:
            if src in failed:
                continue
            try:
                r = self.http.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0 (BreakingMoneyArena research reader)"})
                r.raise_for_status()
                items = parse(r.text)
            except Exception as e:
                if src == "google_news" and "429" in str(e):
                    self._g_pause_until = _time.time() + 1800
                    log.info("Google News asked us to slow down; pausing it for 30 minutes.")
                else:
                    log.warning("Research source %s failed: %s", src, e)
                failed.add(src)
                continue
            for it in items:
                cur = self.j.x("INSERT OR IGNORE INTO research_items VALUES (?,?,?,?,?,?,?,?,?,?)",
                               (it.uid, it.source, it.ts, now.isoformat(), it.headline, it.summary,
                                json.dumps(it.symbols), it.url, it.extraction_confidence, int(it.material)))
                if cur.rowcount:
                    new.append(it.model_dump())
        if new:
            nf = sum(1 for n in new if n["source"] in FEEDS)
            log.info("Research: %d new filing(s) and %d news headline(s) for watched stocks", nf, len(new) - nf)
        return new

    def _rows(self, sym: str, since: datetime) -> list[dict]:
        # Official filings first (up to 6), then the newest news headlines (up to 8).
        rows = (self.j.q("SELECT * FROM research_items WHERE ts>=? AND symbols LIKE ? AND source LIKE 'nse%' "
                         "ORDER BY ts DESC LIMIT 6", (since.isoformat(), f'%"{sym}"%'))
                + self.j.q("SELECT * FROM research_items WHERE ts>=? AND symbols LIKE ? AND source NOT LIKE 'nse%' "
                           "ORDER BY ts DESC LIMIT 8", (since.isoformat(), f'%"{sym}"%')))
        for r in rows:
            r["symbols"] = json.loads(r["symbols"])
            r["material"] = bool(r["material"])
        return rows

    def related(self, sym: str) -> list[dict]:
        """Facts handed to the agents: recent filings for this symbol."""
        return self._rows(sym, self.clock() - timedelta(hours=self.lookback_hours))

    def fresh_for(self, sym: str) -> list[dict]:
        """Material filings that arrived recently and have not triggered a debate yet."""
        return [r for r in self._rows(sym, self.clock() - timedelta(minutes=self.fresh_minutes))
                if r["material"] and r["uid"] not in self.debated]

    def mark_debated(self, uids: list[str]) -> None:
        self.debated.update(uids)

    def market_mood(self) -> dict | None:
        """Nifty 50, Nifty Bank and India VIX: today's move and the last 30 minutes. Cached 5 minutes."""
        if not self.mood_enabled:
            return None
        if self._mood and _time.time() - self._mood[0] < 300:
            return self._mood[1]
        try:
            import yfinance as yf
            names = {"^NSEI": "nifty50", "^NSEBANK": "nifty_bank", "^INDIAVIX": "india_vix"}
            raw = yf.download(list(names), period="5d", interval="5m", group_by="ticker", progress=False,
                              auto_adjust=False, threads=False)
            out = {"source": "Yahoo Finance index quotes"}
            for t, key in names.items():
                c = raw[t]["Close"].dropna()
                if c.empty:
                    continue
                last_day = c.index[-1].date()
                today, before = c[c.index.date == last_day], c[c.index.date < last_day]
                prev = float(before.iloc[-1]) if len(before) else float(today.iloc[0])
                out[key] = {"last": round(float(c.iloc[-1]), 2),
                            "chg_pct_today": round((float(c.iloc[-1]) / prev - 1) * 100, 2),
                            "mom_30m_pct": round((float(c.iloc[-1]) / float(c.iloc[-7]) - 1) * 100, 2) if len(c) > 7 else None,
                            "as_of": c.index[-1].tz_convert(IST).strftime("%H:%M")}
            self._mood = (_time.time(), out)
            return out
        except Exception as e:
            log.warning("Market mood unavailable: %s", e)
            return None
