# UI interaction

## View map

```mermaid
flowchart TB
    APP["Economics Mapper"] --> SIDEBAR

    subgraph SIDEBAR["Sidebar"]
        INVLIST["Investigations<br/>counts · domain dots"]
        ACTIONS["Actions<br/>Collect · Gaps · Summary<br/>Report · Schedule · Delete"]
    end

    ACTIONS --> V1
    INVLIST --> V1

    V1["<b>Knowledge graph</b><br/>nodes coloured by domain<br/>legend doubles as a filter"]
    V2["<b>Artifacts</b><br/>the collected view · filterable"]
    V3["<b>Review</b><br/>collapsible groups · accept/reject"]
    V4["<b>Research gaps</b><br/>coverage · gaps · unbridged"]
    V5["<b>Explainer</b><br/>8 modes × 3 depths × 3 audiences"]
    V6["<b>Summary</b><br/>executive numbers · signals · coverage"]
    V7["<b>Agent console</b><br/>live stage-coloured log"]
    V8["<b>Timeline</b><br/>every chain interleaved"]
    V9["<b>Audit ledger</b><br/>integrity · chains · export"]

    V1 -->|"double-click node"| MODAL["Artifact overlay"]
    V2 --> MODAL
    V3 --> MODAL
    V6 --> MODAL

    V4 -->|"run a gap"| V7
    V5 -->|"save to graph"| V1
    V8 -->|"chain event"| V9
```

Each view answers exactly one question:

| View | The question |
|---|---|
| Knowledge graph | What does this investigation actually connect? |
| Artifacts | What was collected, and what is it? |
| Review | What has not been judged yet? |
| Research gaps | What is *missing*, and where? |
| Explainer | What does the evidence say about this question? |
| Summary | What would I tell someone who asked for the whole thing? |
| Agent console | What is the agent doing right now? |
| Timeline | In what order did this happen? |
| Audit ledger | Can this be checked? |

## The graph

```mermaid
sequenceDiagram
    participant Op
    participant G as vis-network
    participant API

    Op->>API: GET /investigations/{id}/graph
    API-->>G: {nodes[], edges[], legend{}}
    Note over G: node colour = domain,<br/>from the SAME map the legend renders<br/>— the two cannot drift apart
    G->>G: size by relevance,<br/>opacity by review state
    Op->>G: click legend row
    G->>G: isolate that domain
    Op->>G: double-click a node
    G->>API: GET /artifacts/{id}
    API-->>Op: overlay with entities, edges, review history
```

The legend being a **filter** rather than a static key is deliberate. A legend
you can click is a legend someone will click, and a filter that works is a claim
the map can actually support.

## The review queue

```mermaid
flowchart LR
    A[Artifacts] --> G1["Needs review<br/>OPEN"]
    A --> G2["Drift flagged<br/>OPEN"]
    A --> G3["Accepted<br/>COLLAPSED, count kept"]
    A --> G4["Rejected<br/>COLLAPSED, count kept"]
    G1 --> DEC{Decision}
    DEC -->|accept| FEED["feeds the graph"]
    DEC -->|reject| DIM["dims the node<br/>never deletes"]
    DEC -->|either| ROW["ArtifactReview row<br/>+ ledger event"]
```

Needs-review and drift open by default; accepted and rejected are folded with
their counts in the header. A long run's triage should be scannable, not a wall
of cards — and the counts are what let someone decide *whether* to unfold it.

Both decisions are rows (`ArtifactReview`) and ledger events, not a flag on the
artifact. "Who reviewed what, when, and what did they say" is the question an
audit actually gets asked.

## Research gaps

```mermaid
flowchart TB
    COV["Coverage grid<br/>11 domains, clickable<br/>→ filters the graph"]
    COV --> GAPS["GAPS<br/>domain with nothing at all<br/>prioritised by connectivity"]
    COV --> THIN["THIN<br/>covered, under 3 artifacts"]
    COV --> UNB["UNBRIDGED CHANNELS<br/>both endpoints covered,<br/>no edge connects them"]
    GAPS --> RUN["Run a targeted collection"]
    RUN --> CONSOLE["queued → console"]
```

Three lists, never one merged list. Collapsing *thin* into *empty* would let
"barely covered" read as "not covered", which is exactly how a map ends up
claiming coverage it does not have. And the unbridged list is the one that tells
you what is missing is a **mechanism**, not documents.

## The explainer

```mermaid
sequenceDiagram
    participant Op
    participant UI
    participant API
    participant Q as queue

    Op->>UI: question + mode + depth + audience
    UI->>API: POST /explain
    API->>Q: enqueue
    UI->>API: poll
    Q->>API: draft → ground → critique → store
    API-->>UI: explanation

    UI->>UI: render citation markers as chips
    Note over UI: green = verified verbatim<br/>red, struck through = dropped
    UI-->>Op: "Evidence" → per-citation quotes + drop reasons
    UI-->>Op: "Self-critique" → SUPPORTED / UNCERTAIN
    UI-->>Op: "Roadmap" → domains this answer never touched
```

The chips are the point. A user can see *which* citations were verified and
which were dropped, rather than being told a count somewhere. And the
**Evidence** drawer separates drops that failed against a real article from
those that failed against a stored summary — a hallucination and a data gap are
different findings, and averaging them hides which happened.

When retrieval finds nothing, the explainer **refuses**, says why, and files a
research gap. That is rendered as a first-class outcome with a tag
(`no evidence — filed as a gap`), not as an error.

## Status and polling

```mermaid
stateDiagram-v2
    [*] --> Idle
    Idle --> Polling: user queues work
    Polling --> Polling: jobs queued or running
    Polling --> Idle: queue drained<br/>→ reload view, toast
    Idle --> Polling: page loads with<br/>work already queued

    note right of Idle
        The header pills poll every 15s
        regardless — gateway health, queue
        depth, chain integrity.
    end note
```

Polling starts only when there is work, and stops the moment there is not. A
page that polls forever is a page that never sleeps.

## What the UI deliberately does not show

- **No coverage percentage.** "47% covered" is a number with no denominator a
  reader can check. The grid shows absolute counts per domain instead.
- **No composite risk score.** Combining eleven domains with different evidence
  densities into one number hides exactly the thing the app is for.
- **No confidence on the transmission prior.** The channels carry strengths, but
  the UI never renders them as certainty — they are a prior, and `unbridged
  channels` exists precisely because the evidence has not been gathered yet.
- **No "AI generated" badge on the artifact summary.** The summary is marked
  `source: explainer` where it belongs, and every artifact is one click from
  the URL it came from.