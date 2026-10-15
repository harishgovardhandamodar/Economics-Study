# Economics Cross-Domain Knowledge Mapper

You define an **investigation** — a question that does not fit inside one
subfield — and an agent plans searches, reads candidates, classifies them into
one of ten economic **domains**, and maps them into a per-investigation
**knowledge graph** with typed edges. An **explainer** then answers questions
over that graph with every quote verified verbatim against the source it is
attributed to. Every agent step, model call and human action lands on a
**hash-chained audit ledger** whose proofs re-derive from stored rows with no
database attached.

The organising question: **how does a shock in one domain reach another?**
A rate decision is not a monetary-policy fact — it is a reserve-yield fact, a
T-bill-demand fact, a stablecoin-float fact, and a fiscal-arithmetic fact, and
the interesting content is in the hops. This app is built to make those hops
visible, and to be honest about the ones nobody has looked at yet.

```
┌──────────────┐     ┌───────────────────┐     ┌──────────────┐
│   Frontend   │────▶│  FastAPI (Python) │────▶│  SQLite (WAL)│
│ (vanilla JS) │◀────│     uvicorn       │◀────│  data/econ.db│
└──────────────┘     └───────────────────┘     └──────────────┘
                        │ collection · explainer · gaps
                        │ LLM via fox-services gateway
                        ▼ (/v1/chat/completions)
                 ┌────────────────┐
                 │ local Ollama / │
                 │   mesh peers   │
                 └────────────────┘
```

---

## The taxonomy

Eleven domains, chosen because they are the places cross-domain economics
actually breaks. `app/domains.py` is the whole model: a domain is a **label**,
a channel is a **mechanism with a lag**, a driver is the **event** an
investigation tracks.

| Domain | Colour | What it covers |
|---|---|---|
| `macro` | blue | Growth, inflation, employment, monetary transmission, FX |
| `monetary_policy` | purple | Central banks, rate paths, QT/QE, the dollar |
| `fiscal_policy` | orange | Budgets, deficits, subsidies, industrial policy |
| `sovereign_debt` | red | Treasuries, gilts, JGBs, EM credit, debt service, auctions |
| `stablecoins` | teal | Issuance, reserves, redemptions, payment flows |
| `blockchain` | green | L1/L2 economics, consensus, MEV, tokenomics |
| `datacenters` | light blue | Power, grid interconnection, water, chips, capex |
| `ai_labs` | amber | OpenAI, Anthropic, DeepMind, Meta, xAI, Mistral, DeepSeek, Qwen |
| `ai_agents` | light purple | Agentic systems, tool use, agent labour, machine payments |
| `agi_asi` | salmon | Capability timelines, compute ceilings, labour displacement |
| `politics` | grey | Elections, geopolitics, sanctions, export controls, state AI policy |

**Domains are deliberately not peers.** `macro`, `monetary_policy` and
`politics` are declared **hubs** in `domains.HUB_DOMAINS`, and the test suite
asserts every hub has both inbound and outbound channels — a "hub" nothing
routes through is a mislabelled leaf, not a hub. That asymmetry is why the
graph is worth looking at, so it is encoded rather than left to emerge.

### Channels are the point

A domain label alone gives you ten piles. A **channel** is a directed edge with
a typical lag and a strength prior:

```
ai_labs    --compute demand (0.95, 1q)-->  datacenters
datacenters --investment & electricity (0.70, 1q)-->  macro
macro      --reaction function (0.90, 2q)-->  monetary_policy
monetary_policy --reserve yield (0.80, 0q)-->  stablecoins
sovereign_debt --safe-haven demand (0.70, 0q)-->  stablecoins
politics   --export controls (0.80, 1q)-->  ai_labs
```

Macro has *feedback* channels, not just inflows. That is why the loop closes:
the cycle is not a one-way pipe from policy into output — inflation and slack
set what the central bank does next, and a recession widens the deficit without
a single new spending decision. Omit those edges and `macro` becomes a
terminal sink and every transmission path becomes one-way.

`GET /api/transmission/{domain}` walks the graph outward and returns the paths
with their cumulative lag. That endpoint is a **prior**, and the app is careful
to say so: it draws a `transmits_to` edge only when two collected documents
actually share a named actor. Domain adjacency alone would connect everything
to everything.

---

## The agent loop

```
brief (keywords + description + driver)
  → PLAN      LLM drafts ≤6 targeted queries, picking rss/arxiv/web per query,
              naming a target domain, and guaranteeing one primary-source query
  → SEARCH    RSS feeds · arXiv API · DuckDuckGo HTML (dedupe by URL/title)
  → ANALYZE   LLM batches: relevance 0..1, keep/drop, **domain**, **entities**,
              **direction/magnitude/horizon**, relates_to[] against what is
              already collected
  → MAP       persist artifacts + agent edges, then draw the cross-domain edges
              the taxonomy says exist between documents sharing an actor
  → REFINE    if yield is low, the planner drafts follow-up queries for domains
              the round did not reach (≤ max_rounds)
```

Progress streams as `AgentEvent`s; the GUI polls `GET /api/runs/{id}/events`.

### What is different from a generic research agent

**The domain is an output, not an input.** The analyst classifies every
candidate into one of the ten taxonomy domains. A cross-domain graph cannot be
built from documents that do not say which domain they belong to.

**Entities are extracted alongside.** A stablecoin-float document and a
T-bill-auction document share almost no vocabulary; they are related because
both name *Tether* and *Circle* versus *the Fed*. Entity overlap draws the edge
that keyword search cannot — and requires a two-hop shared name, so a single
generic word like "treasury" does not connect the whole graph.

**Direction and horizon are first-class.** "Rates up" is not an insight.
`up · magnitude 0.6 · horizon medium` is something a transmission chain can be
built on, so the analyst returns it and it is stored on the row. The summary
surfaces "strongest directional signals" and an artifact without one is
labelled *background, not a finding*.

**Primary sources outrank commentary.** The plan is guaranteed at least one
query aimed at the institution speaking for itself. Commentary about a rate
decision and the decision itself are different evidence: the first tells you
what people thought, the second what happened.

**Yield is credited before the early exits.** A round that analyzed candidates
and kept none is the single most useful thing to remember and is exactly the
round that used to leave no trace. `app/yield_.py` records it per *shape*
(normalised query + source mix) and `rank_queries` re-orders future plans by
what has historically paid off — order only, nothing dropped.

**The analysis budget matches the need.** The pre-rank cap is
`max(needed × 2, 8)`, not `max_items × 3`, and the analyze loop stops once the
keepers comfortably exceed what the round can persist. Candidates arrive
pre-ranked, so the unanalysed tail is the weaker tail by construction, and a
generous cap would spend two thirds of the model-call budget on rows that get
truncated away.

---

## The explainer, and how it avoids inventing things

```
question + mode/depth/audience
  → RETRIEVE  rank artifacts by domain match, entity overlap, then term overlap
  → DRAFT     the model answers, citing [n] markers into the retrieved set
  → GROUND    verify_quote on every quote; drop unresolvable citations and COUNT them
  → CRITIQUE  a second pass over the *grounded* answer, marking SUPPORTED / UNCERTAIN
  → ROADMAP   what the question needs next, drawn from the taxonomy
  → STORE     the answer, its sources, its domains, its confidence — and save it
              back to the graph as a `projection` artifact
```

**Retrieval is three signals in order of trust.** Explicit domain match first
(a question naming *stablecoins* must lead with stablecoin documents), entity
overlap second, term overlap last. Already-accepted artifacts outrank pending
ones at equal relevance: a reviewer accepting an artifact is the strongest
relevance signal available, and ignoring it would make human triage decorative.

**Questions are matched to domains four ways**, because people ask in four
different ways: the label (in any plural *or* singular form, so "stablecoin
float" hits `Stablecoins`), the short name, a **driver** ("rate decision",
"export control" — each driver already declares the domains it moves), and a
channel's leading word.

**Grounding is mechanical, not stylistic.** A quote must appear verbatim in the
cited source's text after NFKD normalisation, casefolding and whitespace
folding — which is what lets it survive curly quotes, non-breaking spaces and
a line break mid-word, the three things a PDF extraction introduces. Minimum 12
characters: a two-word quote matches almost anything, which is worse than no
quote because it *looks* like verification. A citation marker that resolves to
no source, or whose source contains no verifiable quote, is dropped and
counted. Never silently kept — because a dropped-citation count of zero is the
signal that the answer is trustworthy, and hiding it destroys the signal.

**Confidence follows distinct sources.** Five quotes from one document do not
read as five sources agreeing. If fewer than half the citations verify, the
number is cut hard: an answer whose citations do not verify is not partially
trustworthy, it is differently wrong.

**No evidence means no answer.** When retrieval returns nothing, the explainer
says so, refuses to answer from memory, and files a **research gap** so the
next step is obvious. That is the correct answer to an unanswerable question,
and it is a recorded one.

**Answers are evidence too.** A stored explanation lands on the graph as a
pre-accepted `projection` artifact, so a later explanation can cite it.
Otherwise the explainer is a dead end that can never build on itself.

---

## Research gaps

Computed from what is actually stored, never from what the brief asked for.
A brief can name eight domains and the evidence can be in two, and the gap list
has to say so.

Coverage is reported as **three lists, never one**:

- **gaps** — a domain with *nothing at all* behind it. Cheapest possible
  target for a run.
- **thin** — covered, but under three artifacts. Below that, nothing in the
  domain can support a claim about a channel. Collapsing thin into empty would
  let "barely covered" read as "not covered".
- **unbridged channels** — both endpoints are covered, but no edge connects
  them. These are missing *mechanisms*, not missing documents, and the
  distinction changes what a run should be aimed at.

Gaps are **prioritised by connectivity**, not alphabetically: hub domains first
(3.0), then inbound and outbound channel counts. A hole in monetary policy
costs more than a hole in MEV, because everything routes through it.

A gap's run query is **built from its channels**, not from its label.
"Tell me about stablecoins" returns nothing useful; "Monetary policy duration
repricing Stablecoins" asks a question the corpus can answer.

---

## Transmission assessments

An investigation asks "what is true across these domains". An assessment asks
"what follows for this driver and shock": which channels the stored evidence
supports, which it leaves unexamined, what the directional mass says, and what
three futures would each falsify the read.

`POST /api/investigations/{id}/assess` queues the job; everything below is a
pure function of stored rows plus the taxonomy version:

- **Coverage** — domains with evidence / 11.
- **Bridged priors** — relevant channels (walked from the driver's domains)
  with at least one `transmits_to` edge / relevant channels.
- **Confidence** — distinct-source corroboration, cut hard when nothing is
  directional.
- **Exposure** — magnitude-weighted directional mass, hub domains weighing
  double. Exposure here is a reading of how much directional claim the map
  carries, not a risk rating.
- **Overall** — 30% coverage + 40% bridged priors + 30% confidence.

History stays immutable: a re-score (`POST /api/assessments/{id}/rescore`)
creates a **new** row pointing at the old one via `supersedes_id`, never edits
in place, and reports whether the numbers moved. The taxonomy fingerprint
travels on every row, so a later channel edit is visibly a different result.

A human decision (`accept | guardrails | reject | exception`) lands on the
ledger as its own event. Exceptions require an expiry — an override that never
expires is how a guardrail quietly becomes permanent.

## The council

Six seats (`GET /api/council/panel`), each owning domains and declaring the
weakness it is known to have — monetary, fiscal-state, crypto-money,
compute-capacity, labour-agents, and measurement science, which owns nothing
and exists only to attack the other five. A panel that publishes its blind
spots is auditable; one that claims completeness is not.

- Seats draft from stored rows only. A seat with no evidence stays silent.
- Every claim is cross-examined by non-owning seats on checkable grounds.
  `peer_scrutiny` is a scored dimension with **no neutral fallback**: a claim
  nobody attacked scores zero. Unattacked is not unrefutable.
- A falsifier is graded on three decidable questions (names an observation,
  names a threshold, not only abstract) — "review the deployment" scores the
  same as nothing, because it settles nothing.
- Coverage is reported as `covered` vs `unexamined`, never `safe`.

## Scenarios

Three futures per assessment, each falsifying a different part of the read:
**baseline** (transmission as mapped), **stress** (the strongest prior breaks),
**fragmentation** (the unexamined domains carry the transmission instead).
Each carries its own falsifier and a probability. They are stored on the
assessment row, not generated on read.

## Leadership board

`GET /api/leadership/board` — the portfolio view, aggregations only, no new
judgments: exposure by driver, the decision queue (highest exposure first),
coverage health, system integrity (ledger + jobs), live alerts, and the
estate-wide domain map. Also served as Markdown. An alert is a recomputed
finding (priors mostly unbridged, low corroboration, expiring exceptions,
investigations collected but never scored) — never a dismissible state, so
ignoring one cannot hide a risk that is still true.

## Agentic manager

One command fans out to N investigations plus a summary. `POST
/api/manager/parse` understands the command with **no side effects** — parse
as often as you like; topics come deterministically from the driver and domain
catalogue, with optional LLM shock enrichment that can only add. `POST
/api/manager/run` creates one investigation per topic and launches collection
on each. Compile is refused with `409` while any child is still running, and
idempotent once done.

---

## The hash-chained ledger

One append-only, hash-chained stream per run spans every actor: agent steps,
model calls, and human actions. Each event commits to its predecessor, so an
edit, a deletion or an insertion after the fact all break verification — and
break only their own run.

```
┌──────────────┐     ┌───────────────────┐     ┌──────────────┐
│   Frontend   │────▶│  FastAPI (Python) │────▶│  SQLite (WAL)│
│              │◀────│                   │◀────│              │
└──────────────┘     └───────────────────┘     └──────────────┘
```

`verify_chain` re-derives the whole chain from stored rows and names the exact
break: `seq_gap`, `broken_link`, `hash_mismatch`, `unresolved_ref`,
`head_mismatch`. Tests pin each, including a forged row that is *internally
consistent* — correct seq, correct `prev_hash`, and a hash that really is the
hash of its own core. It still fails, because the next row's `prev_hash` still
points at the row that was pushed out.

Design decisions worth knowing:

- **The hashed core is small and explicit.** `CORE_FIELDS` is the whole
  commitment. `proof_json` (derived) and `trace` (an observation *about* the
  record) live outside it on purpose — folding either in would change every
  hash already on disk.
- **The stored head is checked against the last event.** Deleting only the
  tail leaves every stored link internally consistent; only the recorded head
  can catch it.
- **Auditing is fail-open.** A ledger or gateway outage degrades to *unaudited*
  work, never to a 500. The product must keep working when its own audit trail
  is broken — that is exactly when you need it working.
- **Dropped writes are recorded, not printed.** `LedgerAuditDrop` is where a
  failed audit write goes. A chain can verify perfectly while the recorder has
  been quietly failing for hours; the integrity monitor reports both numbers.
- **Two independent content protections.** Secrets are redacted *before*
  anything reaches immutable storage, and anything under a content-bearing key
  (`prompt`, `answer`, `transcript`, …) is stored as a digest by default. The
  *key name* is the signal, not the length — a key called `prompt` is raw
  content whether it is 20 characters or 20KB.
- **Claims are content-addressed.** Two agents asserting the same thing produce
  the same `claim_hash`, which makes "did B simply parrot A?" answerable and
  lets a reviewer pull the blast radius of a bad claim from one hash.
- **Self-verifying exports.** `GET /api/ledger/runs/{id}/export` bundles events,
  claims and the mandate; `POST /api/ledger/verify-export` re-verifies it with
  no database attached. The frontend runs that check before it tells you the
  bundle is good.

---

## GUI tour

**Investigations** (sidebar) — title, keywords, brief, an optional **driver**
(the economic event being tracked), a shock statement, sources, and the domains
of interest. Actions: *Collect*, *Find open gaps*, *Executive summary*, *Full
report* (Markdown dossier), *Schedule* (cron, validated at the point it is
set, not discovered weeks later).

**Knowledge graph** — vis-network. **Node colour = domain** (from the same map
the legend renders from, so they cannot drift apart), node size = agent
relevance, dimmed nodes were rejected, drift-flagged nodes are amber-edged.
`transmits_to` edges carry the channel, its prior and its lag. Double-click a
node for the artifact overlay; click to see its connection count. The legend is
a *filter* — click a domain to isolate it.

**Artifacts** — the *collected* view, as opposed to the triage view.
Filterable by type, domain, review state and free text.

**Review queue** — collapsible groups: *Needs review* and *Drift flagged* open
by default, *Accepted* and *Rejected* folded with their counts kept in the
header. Accepting feeds the graph; rejecting dims the node rather than deleting
it. Every decision is a row (`ArtifactReview`) and a ledger event.

**Research gaps** — coverage per domain as a clickable stat grid, the gap list
with its runnable query, the thin list, and the unbridged-channel table.

**Explainer** — eight modes (`explain`, `deep-dive`, `compare`, `tutor`,
`critique`, `tldr`, `glossary`, `channel-map`), three depths, three audiences.
Answers show their citation markers as chips, their confidence beside a bar,
and buttons for evidence, self-critique, roadmap, follow-up and a comprehension
quiz. Suggested questions are generated from *this investigation's own gaps*.

**Assessment** — transmission assessments with deterministic scores, the
supported vs unbridged channel table, the mermaid transmission map, hypotheses
with their objections, and three falsifiable scenarios. Re-score creates a new
row; decisions land on the ledger.

**Board** — the leadership view: exposure by driver, the decision queue,
coverage health, system integrity, live alerts, and the estate-wide domain
map. Downloadable as Markdown.

**Manager** — one command understood with no side effects, fanned out to N
investigations, compiled into a summary once every child is done.

**Summary** — executive numbers, the strongest directional signals, coverage
by domain, the brief, and the work done. Also served as an overlay, and as a
Markdown dossier that states the brief verbatim, every run's plan, and every
artifact with the reason it was kept.

**Agent console** — live stage-coloured event log with the search plan.

**Timeline** — every ledger chain the investigation touched, interleaved into
one newest-first stream, with per-chain verification.

**Audit ledger** — the integrity monitor (chains verified / broken / audit
drops), a chain browser with per-event verification, the self-verifying
export, and the SIEM export (cursor-paged JSON lines, minimised payloads).

**Frameworks** — twelve policy regimes (Basel III, MiCA, GENIUS, IMF
surveillance…) scored 0..100 per governed domain from stored evidence. Count
caps fast; directional mass decides.

**Agents** — the app's agents as A2A cards (`/.well-known/econ-agents`), every
intent deterministic over stored rows. Invoke one hop directly from
`POST /api/agents/invoke`.

**Executive brief** — estate-wide and works with zero investigations: headline
numbers, the global knowledge graph across sessions (grey hubs are
investigations, colors are domains; click a hub to open that session's graph),
clusters by domain, cross-session bridges where one session's evidence bears
on another's question, plus alerts.

**Design** — the eight design documents as a selectable index, rendered with
diagrams live (Mermaid via CDN, source fallback offline). Served from a
constant index, never a directory listing, so no input can become a path.

PDFs attach where they belong: the dossier, each assessment, and the board all
offer a PDF button next to their Markdown one. Rendered in-process with
reportlab — no chromium, no LaTeX in the image.

---

## Design documents

Start with [design/](design/README.md) — the system drawn: context, data model,
the run sequence, the loop's decision structure, the state machines, the UI
flows, what leaves the machine, and the hash chain's failure modes.

| Doc | Contents |
|---|---|
| [design/README.md](design/README.md) | Orientation, and the three ideas that carry the design |
| [01-system-context](design/01-system-context.md) | C4 context and containers, module map, the dependency rule |
| [data-model](design/data-model.md) | ER diagram, and why `description` and `source_text` are separate columns |
| [interaction](design/interaction.md) | One run end to end; the edge-drawing rule; failure paths |
| [activity](design/activity.md) | The loop's decisions; run, job, explanation and scheduler states |
| [ui-interaction](design/ui-interaction.md) | View map, the review queue, and what the UI deliberately omits |
| [privacy](design/privacy.md) | What leaves, what is hashed, what is not implemented, operator checklist |
| [ledger](design/ledger.md) | The chain, the hashed core, what breaks what, mandates, sessions |

All diagrams are Mermaid.

---

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

./startup.sh --reset      # apply schema, seed five investigations, serve
```

Or step by step:

```bash
.venv/bin/python scripts/seed.py          # five cross-domain investigations
.venv/bin/uvicorn app.main:app --port 8304
```

Open **http://localhost:8304**. `GET /openapi.json` lists every path.

`startup.sh` binds to `127.0.0.1` on purpose: the API has no authentication,
so exposing it beyond loopback exposes the whole database.

### LLM gateway

All reasoning goes through the fox-services gateway (OpenAI-compatible
`/v1/chat/completions`) — local Ollama or mesh peers, no external API keys.

| Variable | Default | Meaning |
|---|---|---|
| `LLM_BASE_URL` | `http://192.168.1.173:8210/v1` | Preferred gateway (Axiom, LAN) |
| `LLM_FALLBACK_URL` | `http://localhost:8210/v1` | This host's gateway, when off Axiom's LAN |
| `LLM_MODEL` | `qwen3.8:27b` | Requested model |
| `ECON_DATABASE_URL` | `sqlite:///data/econ.db` | Database |
| `ECON_SCHEDULER_ENABLED` | `1` | Cron scheduling |
| `ECON_JOB_WORKER` | `1` | Background queue worker |
| `LEDGER_CAPTURE_PAYLOADS` | `0` | Store prompt/output bodies (redacted) instead of digests |

The connect timeout is deliberately shorter than the generate timeout: a
preferred gateway that is *blackholed* must not burn the whole budget before
the fallback is tried. When a call is actually served by a fallback the header
pill turns amber — it reports a call that really happened, not a transient
probe.

---

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

132 tests, all against a scratch database and a **stubbed model**. Stubbing the
model rather than the mapper is deliberate: everything else runs for real — the
loop, the taxonomy, the graph, grounding, the ledger. A test that mocks the
mapper too would only prove the mocks agree with each other.

| File | Pins |
|---|---|
| `test_domains.py` | Every channel endpoint is real; hubs have both directions; the walk terminates; unknown domains cannot survive normalisation |
| `test_ledger.py` | Tamper, deletion, truncation and **internally-consistent forgery**; secret redaction; content hashing; offline export verification; contamination tracing |
| `test_grounding.py` | Verbatim quotes; short-quote rejection; PDF-extraction normalisation; dropped-citation counting; **summary-only drops attributed separately**; distinct-source confidence; PII conservatism |
| `test_mapping.py` | Domain match beats keyword overlap; **entity overlap links documents with no shared vocabulary**; thin ≠ empty; the aliased cross-domain join |
| `test_entities.py` | `US Treasury` = `U.S. Treasury`; acronyms expand; **generic names alone draw no edge** |
| `test_yield.py` | Word-order-independent shapes; a dead shape is hard-demoted, not merely scored lower; nothing is dropped; yield is scoped per investigation |
| `test_api.py` | The full loop end to end; graph colouring; review flow; grounded answers; refusal with a gap filed; the run on the ledger and verifying |
| `test_assessment.py` | Empty scores zero; scores derive from rows; supported vs unbridged; council silence; falsifier grades; peer scrutiny without fallback |
| `test_assess_api.py` | Queue → read → decide → rescore-as-new-row → compare; blind spots published |
| `test_leadership_manager.py` | Empty board is honest; parse is side-effect free; run → compile → idempotent compile |
| `test_small_surfaces.py` | Hide, prefs, feedback, save-to-graph dedupe, gap runs, similar, recommendations, compare |
| `test_remaining.py` | SIEM cursor export + minimised payloads; framework matrix scoring; A2A invoke incl. rejections; real PDFs + hostile-markup renderer test |
| `test_brief_design.py` | Design index/fetch + traversal rejection + missing-dir honesty; global graph namespacing/hubs/scoping; category + cross-session similarity clusters |
| `test_frontend.py` | Static GUI checks: every `$('#X')` resolves; no backticks inside onclick attributes |

---

## Layout

```
app/
  domains.py         the taxonomy: domains, channels, drivers, relationships
  models.py          investigation, artifact, relationship, run, explanation, gap,
                     assessment, manager run
  database.py        engine, WAL, additive migrations
  ledger.py          the hash chain, mandates, proofs, claims, sessions, exports
  ledger_models.py   the ledger's own tables
  ledger_api.py      the audit surface, as its own router
  agent.py           plan → search → analyze → map → refine
  search.py          RSS / arXiv / DuckDuckGo, economics-first feed list
  yield_.py          per-shape query yield, across runs
  explainer.py       retrieval, drafting, grounding, critique, roadmap
  grounding.py       verbatim quote verification, PII, relevance gate
  research_gaps.py   coverage, gaps vs thin, unbridged channels, lifecycle
  assessment.py      transmission scoring, markdown, mermaid (deterministic)
  council.py         six-seat deliberation: claims, cross-examination, coverage
  leadership.py      portfolio board: exposure, decisions, health, alerts
  manager.py         one command → N investigations + compiled summary
  drift.py           artifact drift and watched questions
  jobqueue.py        persisted, lease-based queue
  scheduler.py       cron, validated at set time
  siem.py            cursor-paged JSON-lines export, minimised payloads
  frameworks.py      12 policy regimes scored against collected evidence
  agents.py          A2A cards + deterministic dispatch
  dossier_pdf.py     reportlab A4 reports (dossier, assessment, board)
  design_docs.py     the design set as selectable documents (index, never listing)
  main.py            the API (109 paths)
static/index.html    the GUI
scripts/seed.py      five cross-domain investigations
```

---

## Known limits

- **Cross-domain edges need a shared actor.** Deliberate — the alternative
  connects everything to everything — but it means a channel between two
  domains whose documents happen to share no name is reported as *unbridged*
  rather than drawn. That is the honest answer, not a missing feature.
- **The transmission walk is a prior.** `app/domains.py` encodes mainstream
  macro channels with hand-set strengths and lags. They are not estimated from
  the collected evidence, and no run updates them. The evidence either supports
  a hop or does not; the walk does not know which.
- **Direction and magnitude come from a model.** They are stored, attributed
  and surfaced, but nothing validates that a claimed magnitude is calibrated.
  Treat them as claims to argue with, which is what the self-critique pass is
  for.
- **A 27B local model is slow.** Five candidates per analyse call at roughly
  40 seconds each. The budgets are real; the runtime reflects the model you
  point `LLM_MODEL` at.