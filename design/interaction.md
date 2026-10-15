# Interaction — one collection run, end to end

## Sequence

```mermaid
sequenceDiagram
    autonumber
    actor Op as Operator
    participant UI as Browser
    participant API as FastAPI
    participant Q as Job queue
    participant AG as agent.py
    participant PLAN as LLM<br/>(planner)
    participant SRC as search.py
    participant ANA as LLM<br/>(analyst)
    participant MAP as agent.py<br/>_map_relationships
    participant LED as ledger.py
    participant DB as SQLite

    Op->>UI: "Collect"
    UI->>API: POST /investigations/{id}/run
    API->>Q: enqueue("collect")
    API-->>UI: {job_id, queued}
    UI->>API: GET /runs/{id}/events  (poll)
    Q->>AG: claim (lease written)

    rect rgb(31,111,235,0.10)
    AG->>LED: scope(run-42, Mandate)
    LED->>DB: ledger_runs + run.start event

    AG->>PLAN: brief → ≤6 queries + domains
    PLAN-->>AG: {rationale, queries[]}
    AG->>AG: guarantee 1 primary-source query
    AG->>LED: agent.step "plan"

    par fan-out
        AG->>SRC: rss · arxiv · web
        SRC-->>AG: 64 candidates (deduped by URL/title)
    end
    AG->>AG: pre-rank → top max(needed×2, 8)
    AG->>LED: agent.step "search"

    loop batches of 5, until keepers ≥ needed
        AG->>ANA: batch + already-collected
        ANA-->>AG: relevance, domain, entities,<br/>direction/magnitude/horizon, relates_to
        AG->>LED: agent.step "analyze"
    end
    AG->>AG: yield.record_yield(...)  ◄── BEFORE any early exit

    AG->>MAP: agent edges
    MAP->>MAP: draw transmits_to where the taxonomy<br/>has a channel AND two docs share an actor
    AG->>DB: artifacts + relationships
    AG->>LED: agent.step "map"

    alt rounds remain and yield is low
        AG->>PLAN: follow-up queries for uncovered domains
        AG->>AG: rank_queries by historical yield
    end

    AG->>LED: run.end
    LED-->>AG: hash committed
    end
```

## Where the guarantees live

```mermaid
flowchart TB
    START["candidate"] --> DEDUP{"URL or title<br/>already seen?"}
    DEDUP -->|yes| DROP["dropped"]
    DEDUP -->|no| PREFILTER["pre-rank:<br/>keywords + primary source<br/>+ brief domains"]
    PREFILTER --> ANALYZE["LLM batch"]
    ANALYZE --> CLASSIFY{"domain in<br/>the taxonomy?"}
    CLASSIFY -->|unknown| NODOM["stored unclassified<br/>— cannot transmit"]
    CLASSIFY -->|known| KEEP{"relevance ≥ 0.4<br/>or keep?"}
    KEEP -->|no| YIELD1["yield: found, kept=0<br/>◄── recorded anyway"]
    KEEP -->|yes| STORE["artifact row"]
    STORE --> ENT["entities_json"]
    ENT --> NAMED{"agent named<br/>an edge?"}
    NAMED -->|yes| AGENT_EDGE["persist with the agent's type"]
    NAMED -->|no| TAXO["taxonomy fallback"]
    AGENT_EDGE --> TAXO
    TAXO --> CHAN{"channel exists between<br/>the two domains?"}
    CHAN -->|no| STOP["no edge — domains are<br/>not adjacent"]
    CHAN -->|yes| SHARED{"two documents share<br/>a distinctive entity?"}
    SHARED -->|no| STOP2["no edge — adjacency alone<br/>would connect everything"]
    SHARED -->|yes| TX["transmits_to edge<br/>with channel, prior, lag"]
```

The two `no` branches on the right are the design. A graph that drew an edge
whenever the taxonomy said a channel existed would connect every document in
every pair of adjacent domains, which is a picture of the taxonomy rather than
of the evidence. The result of refusing is that the channel shows up in
**Research gaps → unbridged channels**: "both endpoints are covered, but
nothing connects them" — which is a finding, and an actionable one.

## Failure paths

```mermaid
stateDiagram-v2
    [*] --> queued
    queued --> running: worker claims, lease written
    running --> done: artifacts + edges committed
    running --> queued: failure, attempts < max<br/>exponential backoff
    running --> error: failure, attempts ≥ max
    running --> queued: lease expired<br/>(worker died)
    error --> queued: manual retry
    done --> [*]
    queued --> cancelled: operator cancels

    note right of running
        A lease, not a lock: a worker that dies
        stops renewing, the lease expires,
        the job is re-queued. Nothing has to
        notice that a process crashed.
    end note
```

| Failure | Behaviour | Why |
|---|---|---|
| Gateway unreachable at plan | Deterministic keyword-derived plan | A run must never fail because a model did; the fallback flows through the same ensurers and the same yield accounting as a model plan |
| Model returns unparseable JSON | `_parse_json_lenient` repairs fences, control chars, prose wrappers, trailing commas | Small models break JSON constantly; strict parsing turns a cosmetic defect into a total failure |
| Every source fails | Round records `found=0` in yield and the loop breaks | The zero is the useful datum — it is what stops the shape being re-proposed next run |
| Worker dies mid-run | Lease expires, job is re-queued, `attempts` increments | A crash is indistinguishable from a slow run from outside; a lease distinguishes them |
| Ledger write fails | Record lands in `LedgerAuditDrop`, work continues | Auditing is fail-open. A hole that exists only on stderr is a hole nobody reads |
| Ledger write fails repeatedly | Integrity monitor reports `audit_drops > 0` alongside a perfectly verified chain | A chain can verify while the recorder has been failing for hours. Both numbers are reported, because neither alone is sufficient |