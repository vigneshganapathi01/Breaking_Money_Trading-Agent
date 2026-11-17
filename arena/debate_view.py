"""Data and page for the live debate dashboard (served by the control panel)."""
from __future__ import annotations

import json

from .db import Journal
from .events import BUS


def dashboard_state(j: Journal, today: str, cap: float, plan: bool = False) -> dict:
    debates = j.q("SELECT id, ts, symbol, triggers, snapshot_hash, status, judge_json, final_action, guard_notes, cost_usd "
                  "FROM debates ORDER BY id DESC LIMIT 25")
    for d in debates:
        for k in ("triggers", "judge_json", "guard_notes"):
            d[k] = json.loads(d[k]) if d[k] else None
        d["events"] = [dict(e, payload=json.loads(e.pop("payload_json")))
                       for e in j.q("SELECT ts, agent, kind, round, payload_json FROM debate_events "
                                    "WHERE debate_id=? AND kind='agent_reply' ORDER BY id", (d["id"],))]
        d["proposed"] = next((t.split()[2].rstrip(":") for t in (d["triggers"] or [])
                              if t.startswith("strategy proposes ")), None)
        o = j.one("SELECT ts, side, qty, fill_price, status, symbol FROM orders WHERE note LIKE ? ORDER BY id LIMIT 1",
                  (f"[debate #{d['id']}]%",))
        if o and o["side"] == "SELL":
            t = j.one("SELECT net_pnl FROM trades WHERE symbol=? AND exit_ts=?", (o["symbol"], o["ts"]))
            o["net_pnl"] = t["net_pnl"] if t else None
        d["order"] = o
    research = j.q("SELECT uid, source, ts, headline, summary, symbols, url, extraction_confidence, material "
                   "FROM research_items ORDER BY ts DESC LIMIT 40")
    for r in research:
        r["symbols"] = json.loads(r["symbols"])
    usage = j.q("SELECT agent, COUNT(*) calls, SUM(input_tokens) tin, SUM(output_tokens) tout, ROUND(SUM(cost_usd),4) cost, "
                "SUM(ok=0) failed FROM agent_usage WHERE substr(ts,1,10)=? GROUP BY agent", (today,))
    verdicts = j.q("SELECT final_action, COUNT(*) n FROM debates WHERE substr(ts,1,10)=? GROUP BY final_action", (today,))
    perf = j.one("SELECT COUNT(*) n, COALESCE(SUM(net_pnl>0),0) wins, COALESCE(ROUND(SUM(net_pnl),2),0) pnl FROM trades "
                 "WHERE entry_reason LIKE '[debate #%'") or {}
    last_market = next((e for e in reversed(BUS.since(0)) if e["kind"] == "market"), None)
    return {
        "debates": debates, "research": research, "usage": usage, "verdicts": verdicts,
        "performance": perf, "spent_today": round(sum(u["cost"] or 0 for u in usage), 4), "cap": cap, "plan": plan,
        "market": last_market, "seq": BUS.last_seq,
    }


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Debate Floor</title>
<style>
:root{--page:#08090c;--surface:#12141a;--surface-2:#1a1d25;--line:#262a35;--ink:#f0efec;--ink-2:#c3c2b7;--muted:#898781;
--accent:#3987e5;--bull:#18a957;--bear:#e0564f;--judge:#8b7cf6;--warn:#fab219}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);font:13.5px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
header{display:flex;flex-wrap:wrap;gap:16px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line);background:var(--surface)}
header b{font-size:15px}.pill{border:1px solid var(--line);border-radius:999px;padding:3px 10px;color:var(--ink-2);font-size:12px}
.pill.live{border-color:var(--bull);color:var(--bull)}.pill.warn{border-color:var(--warn);color:var(--warn)}
.grid{display:grid;grid-template-columns:minmax(260px,1fr) minmax(360px,2fr) minmax(240px,1fr);gap:12px;padding:12px}
@media (max-width:1000px){.grid{grid-template-columns:1fr}}
.col{display:flex;flex-direction:column;gap:12px;min-width:0}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:12px;min-width:0}
h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0 0 8px}
table{width:100%;border-collapse:collapse}td,th{padding:4px 6px;border-bottom:1px solid var(--line);text-align:right;font-variant-numeric:tabular-nums}
td:first-child,th:first-child{text-align:left}th{color:var(--muted);font-weight:600;font-size:11.5px}
.up{color:var(--bull)}.down{color:var(--bear)}.muted{color:var(--muted)}.small{font-size:12px}
.tag{display:inline-block;border-radius:6px;padding:1px 6px;font-size:11px;font-weight:700;margin-right:4px;background:var(--surface-2);color:var(--ink-2)}
.tag.fact{color:var(--accent)}.tag.mat{color:var(--warn)}
.debate{border:1px solid var(--line);border-radius:10px;padding:10px;margin-bottom:10px;background:#0e1016}
.dhead{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap}.sym{font-weight:800;font-size:15px}
.msg{border-left:3px solid var(--line);padding:6px 10px;margin:8px 0;background:var(--surface)}
.msg.bull{border-color:var(--bull)}.msg.bear{border-color:var(--bear)}.msg.judge{border-color:var(--judge)}
.who{font-weight:800;font-size:12px;letter-spacing:.06em}.bull .who{color:var(--bull)}.bear .who{color:var(--bear)}.judge .who{color:var(--judge)}
ul{margin:4px 0 0;padding-left:18px}li{margin:2px 0}.ev{color:var(--muted);font-size:11.5px}
.thinking{color:var(--muted);font-style:italic;animation:p 1.4s infinite}@keyframes p{50%{opacity:.4}}
.verdict{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}.act{font-size:18px;font-weight:900}
.act.BUY{color:var(--bull)}.act.SELL{color:var(--bear)}.act.HOLD{color:var(--ink-2)}
.jv{display:inline-flex;gap:6px;align-items:center;flex-wrap:wrap}
.chip{border-radius:6px;padding:2px 8px;font-size:12px;font-weight:800;border:1px solid var(--line)}
.chip.BUY{color:var(--bull);border-color:var(--bull)}.chip.SELL{color:var(--bear);border-color:var(--bear)}
.chip.HOLD,.chip.SKIP{color:var(--ink-2)}.chip.TAKEN{background:var(--bull);color:#08090c;border-color:var(--bull)}
.chip.LEFT{background:var(--surface-2);color:var(--ink-2)}.chip.WAIT{color:var(--judge);border-color:var(--judge)}
a{color:var(--accent)}svg{vertical-align:middle}.empty{color:var(--muted);padding:12px 0}
</style></head><body>
<header><b>Debate Floor</b><span class="pill" id="h_stream">connecting…</span><span class="pill" id="h_spend"></span>
<span class="pill" id="h_debates"></span><span class="pill warn">PAPER ONLY · VIRTUAL ₹</span><span class="pill" id="h_entries"></span></header>
<div class="grid">
 <div class="col">
  <div class="card"><h2>Watchlist <span class="small muted" id="w_ts"></span></h2><table id="watch"><tr><td class="empty">Waiting for the first market update…</td></tr></table></div>
  <div class="card"><h2>Research <span class="small muted">official NSE filings · facts only</span></h2><div id="research"></div></div>
 </div>
 <div class="col"><div class="card"><h2>Debate timeline</h2><div id="debates"></div></div></div>
 <div class="col">
  <div class="card"><h2>Paper portfolio</h2><div id="portfolio" class="small muted">Waiting…</div><p class="small"><a href="/report" target="_top">Full P&amp;L, trade history →</a></p></div>
  <div class="card"><h2>Judge verdicts</h2><div id="jlog"></div></div>
  <div class="card"><h2>Agent performance</h2><div id="perf"></div></div>
  <div class="card"><h2>Claude usage today</h2><div id="usage"></div></div>
 </div>
</div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const tm=s=>s?String(s).slice(11,19):"";
let S=null,live={};
function spark(a){if(!a||a.length<2)return"";const lo=Math.min(...a),hi=Math.max(...a),w=60,h=16;const p=a.map((v,i)=>`${(i*w/(a.length-1)).toFixed(1)},${(h-(v-lo)/((hi-lo)||1)*h).toFixed(1)}`).join(" ");
 return `<svg width="${w}" height="${h}"><polyline fill="none" stroke="${a[a.length-1]>=a[0]?"#18a957":"#e0564f"}" stroke-width="1.5" points="${p}"/></svg>`}
function list(t,a){return a&&a.length?`<div class="small"><b>${t}</b><ul>${a.map(x=>`<li>${esc(x)}</li>`).join("")}</ul></div>`:""}
function evid(a){return a&&a.length?`<div class="ev">Evidence: ${a.map(e=>esc(e.claim)+" <i>["+esc(e.source)+"]</i>").join(" · ")}</div>`:""}
function reply(e){const r=e.reply||e.payload?.reply||{},a=e.agent||e.payload?.agent,rd=e.round??e.payload?.round;
 const label={bull:"🐂 BULL",bear:"🐻 BEAR"}[a]||a;const task=e.task||e.payload?.task||"";
 return `<div class="msg ${a}"><div class="who">${label} <span class="muted small">· ${rd===0?"opening":"round "+rd} · ${esc(task)} · conf ${(r.confidence??0).toFixed(2)} · ${tm(e.ts)}</span></div>
 ${r.thesis?`<div><b>${esc(r.stance||"")}</b> ${esc(r.thesis)}</div>`:""}${list("Points",r.points)}${list("Arguments",r.arguments)}
 ${list(a==="bull"?"Entry conditions":"Invalidation",r.entry_conditions)}${list("Risks",r.risks)}${list("Concedes",r.concessions)}${list("Missing evidence",r.missing_evidence)}${evid(r.evidence)}</div>`}
const JA={PAPER_BUY:"BUY",PAPER_SELL:"SELL",HOLD:"HOLD",SKIP:"SKIP"};
function outcome(d){const o=d.order;
 if(d.status==="running")return{k:"WAIT",t:"debating…"};
 if(o&&o.status==="filled"){const pl=o.net_pnl!=null?` · net ${o.net_pnl>=0?"+":"−"}₹${Math.abs(o.net_pnl).toFixed(2)}`:"";
  return{k:"TAKEN",t:`${o.side==="BUY"?"Bought":"Sold"} ${o.qty} @ ₹${Number(o.fill_price).toFixed(2)}${pl}`}}
 if(o)return{k:"LEFT",t:`order ${esc(o.status)}`};
 return{k:"LEFT",t:d.judge_json?"no trade":"no verdict ("+esc(d.status)+")"}}
function jchip(d){const v=d.judge_json,j=v?JA[v.action]||v.action:null,oc=outcome(d);
 return `<span class="jv">${d.proposed?`<span class="muted small">proposed ${esc(d.proposed)} →</span>`:""}<span class="chip ${j||"WAIT"}">⚖️ JUDGE: ${j||(d.status==="running"?"…":"—")}</span>${v&&j!==d.final_action?`<span class="muted small">guard → ${esc(d.final_action)}</span>`:""}<span class="chip ${oc.k}">${oc.k}</span><span class="small muted">${oc.t}</span></span>`}
function verdict(d){const v=d.judge_json;if(!v&&d.status==="running")return"";
 if(!v)return `<div class="msg judge"><div class="who">⚖️ JUDGE</div><div>No verdict (${esc(d.status)}). ${esc((d.guard_notes||[]).join("; "))}</div></div>`;
 return `<div class="msg judge"><div class="who">⚖️ JUDGE <span class="muted small">· ${tm(d.vts)}</span></div>
 <div class="verdict"><span class="act ${d.final_action}">${esc(d.final_action)}</span><span class="muted">judge said ${esc(v.action)} · conf ${v.confidence.toFixed(2)} · Bull ${v.bull_score}/10 vs Bear ${v.bear_score}/10 · alloc ${v.allocation_pct}% · SL ${v.stop_loss_pct}% · TP ${v.target_pct}%</span></div>
 <div>${esc(v.rationale)}</div>${list("Risk flags",v.risk_flags)}${list("Missing information",v.missing_information)}${list("Evidence cited",v.evidence_cited)}
 ${d.guard_notes&&d.guard_notes.length?`<div class="small" style="color:var(--warn)">Risk guard: ${esc(d.guard_notes.join("; "))}</div>`:""}
 <div class="ev">regime: ${esc(v.market_regime)} · news reliability: ${esc(v.news_reliability)} · data fresh: ${v.data_freshness_ok} · cost $${(d.cost_usd||0).toFixed(3)}</div></div>`}
function renderDebates(){const el=$("debates");if(!S.debates.length){el.innerHTML='<div class="empty">No debates yet. They start when a trigger fires (price move, volume spike, breakout, SMA cross or material news) while brain = "debate".</div>';return}
 el.innerHTML=S.debates.map(d=>`<div class="debate"><div class="dhead"><span><span class="sym">${esc(d.symbol)}</span> <span class="muted small">#${d.id} · ${tm(d.ts)} · snapshot ${esc(d.snapshot_hash)}</span></span>${jchip(d)}</div>
 <div class="small muted">Triggers: ${esc((d.triggers||[]).join(" · "))}</div>${d.events.map(reply).join("")}
 ${live[d.id]?`<div class="thinking">${esc(live[d.id])} is thinking…</div>`:""}${verdict(d)}</div>`).join("")}
function renderResearch(){$("research").innerHTML=S.research.length?S.research.map(r=>`<div style="margin-bottom:8px"><span class="tag fact">FACT</span>${r.material?'<span class="tag mat">MATERIAL</span>':""}<span class="tag">${esc(r.symbols.join(","))}</span>
 <span class="muted small">${esc(r.source)} · ${esc(String(r.ts).slice(0,16).replace("T"," "))} · match ${(r.extraction_confidence*100).toFixed(0)}%</span><div>${esc(r.headline)}</div>${/^https:\/\//.test(r.url)?`<a class="small" href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">Source filing</a>`:""}</div>`).join(""):'<div class="empty">No filings for watched stocks yet.</div>'}
function renderMarket(){const m=S.market;if(!m)return;const d=m.data;$("w_ts").textContent=tm(m.ts);
 const held=new Set((d.positions||[]).map(p=>p.symbol));
 $("watch").innerHTML="<tr><th>Stock</th><th>LTP</th><th>Chg</th><th>RSI</th><th>Vol×</th><th></th></tr>"+Object.entries(d.market).map(([s,x])=>`<tr><td>${esc(s)}${held.has(s)?' <span class="tag">held</span>':""}</td><td>${x.ltp}</td><td class="${x.chg_pct>=0?"up":"down"}">${x.chg_pct>=0?"+":""}${x.chg_pct}%</td><td>${x.rsi14??"–"}</td><td>${x.vol_ratio??"–"}</td><td>${spark(x.last6_closes)}</td></tr>`).join("");
 const a=d.account;$("portfolio").innerHTML=`<div style="font-size:20px;font-weight:800;color:var(--ink)">₹${a.equity.toLocaleString("en-IN")}</div><div class="${a.pnl_today>=0?"up":"down"}">${a.pnl_today>=0?"+":""}₹${a.pnl_today} today</div><div>Cash ₹${a.cash.toLocaleString("en-IN")} · ${d.positions.length} open</div>`+
 (d.positions.length?"<table>"+d.positions.map(p=>`<tr><td>${esc(p.symbol)}</td><td>${p.qty}</td><td class="${p.unrealized_pct>=0?"up":"down"}">${p.unrealized_pct}%</td></tr>`).join("")+"</table>":"");
 $("h_entries").textContent=d.entries_allowed?"entries open":"entries closed"}
function renderSide(){const u=S.usage;
 const ds=S.debates.filter(d=>d.status!=="running");
 $("jlog").innerHTML=ds.length?"<table><tr><th>Time</th><th>Stock</th><th>Proposed</th><th>Judge</th><th>Result</th></tr>"+ds.map(d=>{const v=d.judge_json,oc=outcome(d);
  return `<tr><td>${tm(d.ts)}</td><td>${esc(d.symbol)}</td><td>${esc(d.proposed||"—")}</td><td><span class="chip ${v?JA[v.action]:""}">${v?JA[v.action]:"—"}</span></td><td><span class="chip ${oc.k}">${oc.k}</span></td></tr>`}).join("")+"</table>":'<div class="empty">No verdicts yet.</div>';
 $("usage").innerHTML=(S.plan?'<div class="small muted" style="margin-bottom:6px">Runs on your Claude Code plan: nothing is charged in dollars. "$" is what the same calls would cost on the API, used only as a daily brake.</div>':"")+(u.length?`<table><tr><th>Agent</th><th>Calls</th><th>Tokens</th><th>${S.plan?"API-equiv $":"$"}</th></tr>`+u.map(x=>`<tr><td>${esc(x.agent)}</td><td>${x.calls}${x.failed?` <span class="down">(${x.failed} failed)</span>`:""}</td><td>${((x.tin||0)+(x.tout||0)).toLocaleString()}</td><td>${(x.cost||0).toFixed(3)}</td></tr>`).join("")+"</table>":'<div class="empty">No Claude calls today.</div>');
 const p=S.performance||{};$("perf").innerHTML=`<div class="small">Verdicts today: ${S.verdicts.map(v=>esc(v.final_action)+" "+v.n).join(" · ")||"none"}</div><div class="small">Closed trades from debates: ${p.n||0}, wins ${p.wins||0}, net <span class="${(p.pnl||0)>=0?"up":"down"}">₹${p.pnl||0}</span></div>`;
 $("h_spend").textContent=S.plan?`Claude Code plan · no $ charged · API-equiv ~$${S.spent_today.toFixed(2)} of $${S.cap.toFixed(2)} daily brake`:`Claude API today $${S.spent_today.toFixed(2)} / $${S.cap.toFixed(2)}`;$("h_debates").textContent=`${S.debates.filter(d=>String(d.ts).slice(0,10)===new Date().toISOString().slice(0,10)).length} debates today`}
function render(){renderDebates();renderResearch();renderMarket();renderSide()}
async function load(){S=await (await fetch("/api/debates")).json();render();return S.seq}
function onEvent(ev){const d=ev.data;
 if(ev.kind==="market"){S.market=ev;renderMarket();return}
 if(ev.kind==="research"){S.research.unshift(d);renderResearch();return}
 if(ev.kind==="debate_start"){S.debates.unshift({id:d.debate_id,ts:ev.ts,symbol:d.symbol,triggers:d.triggers,snapshot_hash:d.snapshot_hash,status:"running",events:[]})}
 const db=S.debates.find(x=>x.id===d.debate_id);
 if(ev.kind==="agent_thinking"){live[d.debate_id]=({bull:"🐂 Bull",bear:"🐻 Bear",judge:"⚖️ Judge"}[d.agent]||d.agent)}
 if(ev.kind==="agent_reply"&&db){db.events.push({ts:ev.ts,...d});delete live[d.debate_id]}
 if(ev.kind==="verdict"&&db){Object.assign(db,{status:d.status,judge_json:d.verdict,final_action:d.final_action,guard_notes:d.guard_notes,cost_usd:d.cost_usd,vts:ev.ts});delete live[d.debate_id];load()}
 renderDebates()}
load().then(seq=>{const es=new EventSource("/api/stream?after="+seq);
 es.onopen=()=>{$("h_stream").textContent="live";$("h_stream").className="pill live"};
 es.onerror=()=>{$("h_stream").textContent="reconnecting…";$("h_stream").className="pill"};
 es.onmessage=m=>onEvent(JSON.parse(m.data))});
setInterval(()=>load(),60000);
</script></body></html>"""
