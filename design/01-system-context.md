# System context

## Level 1 — the app in its world

```mermaid
flowchart TB
    subgraph operator["Operator (one person, one machine)"]
        UI["Browser<br/>static/index.html"]
    end

    APP["<b>Economics Cross-Domain Mapper</b><br/>FastAPI · SQLite · vanilla JS<br/>:8304"]

    subgraph outbound["Outbound, read-only"]
        RSS["Central banks · IMF/BIS/OECD<br/>Treasury · EIA/IEA · labs · press<br/>(RSS)"]
        ARXIV["arXiv API"]
        WEB["DuckDuckGo HTML"]
    end

    GW["<b>fox-services gateway</b><br/>OpenAI-compatible /v1<br/>:8210 — local Ollama or mesh peers"]

    subgraph internal["Stays on this machine"]
        DB[("SQLite · WAL<br/>data/econ.db")]
        LEDGER[("Hash-chained<br/>audit ledger")]
        QUEUE[("Job queue<br/>lease-based")]
    end

    APP-.->|"REST + SSE-less polling"| UI
    APP -->|"keyword-filtered scan"| RSS
    APP -->|"full-text search"| ARXIV
    APP -->|"HTML scrape"| WEB
    APP -->|"/v1/chat/completions<br/>prompts + outputs"| GW
    APP --> DB
    DB --- LEDGER
    DB --- QUEUE

    classDef internal fill:#1f6feb22,stroke:#58a6ff
    classDef outbound fill:#3fb95022,stroke:#3fb950
    class DB,LEDGER,QUEUE internal
    class RSS,ARXIV,WEB outbound
```

**Nothing sensitive leaves.** The only outbound traffic is read-only requests
to public endpoints and model inference to a gateway on the operator's own
network. There is no telemetry endpoint, no outbound webhook, and no third-party
service.

## Level 2 — containers

```mermaid
flowchart LR
    subgraph browser["Browser"]
        A["views: graph, artifacts,<br/>review, gaps, explainer,<br/>summary, console, timeline, audit"]
        L["vis-network<br/>mermaid · marked"]
    end

    subgraph app["FastAPI :8304"]
        M["main.py<br/>~60 routes"]
        AR["agent.py<br/>plan→search→analyze→map→refine"]
        EX["explainer.py<br/>retrieve→draft→ground→critique"]
        RG["research_gaps.py<br/>coverage vs. absence"]
        DR["drift.py"]
    end

    subgraph core["Core"]
        DOM["domains.py<br/><b>the taxonomy</b>"]
        LED["ledger.py<br/>hash chain + proofs"]
        YLD["yield_.py<br/>per-shape yield"]
        SCH["scheduler.py · jobqueue.py"]
    end

    subgraph adapt["Adapters"]
        LL["llm.py<br/>gateway client"]
        SR["search.py<br/>rss · arxiv · web"]
        GR["grounding.py<br/>verbatim verification"]
    end

    A -->|"/api/*"| M
    M --> AR & EX & RG & DR
    AR --> DOM & YLD & SR
    EX --> DOM & GR
    RG --> DOM
    AR -.-> LED
    EX -.-> LED
    LL -.-> LED
    M --> SCH
    AR --> LL
    EX --> LL
    AR --> SR

    classDef core fill:#bc8cff22,stroke:#bc8cff,font-weight:bold
    class DOM,LED core
```

## Components

| Module | Responsibility | Depends on |
|---|---|---|
| `domains.py` | The taxonomy: 11 domains, 30+ channels, 13 drivers, relationship types, entity aliases | nothing |
| `models.py` | Investigation, Artifact, Relationship, AgentRun, Explanation, ResearchGap, Job | `database` |
| `ledger.py` / `ledger_models.py` | Hash chain, mandates, proofs, claims, sessions, exports | `database` |
| `ledger_api.py` | The audit surface as its own router | `ledger` |
| `agent.py` | The collection loop | `domains`, `llm`, `search`, `yield_`, `ledger` |
| `explainer.py` | Retrieval, drafting, critique, roadmap | `domains`, `llm`, `grounding`, `ledger` |
| `grounding.py` | Verbatim quote verification, PII, relevance gate | nothing |
| `research_gaps.py` | Coverage, gaps vs. thin, unbridged channels | `domains`, `explainer` |
| `search.py` | RSS / arXiv / DuckDuckGo, economics-first feeds | nothing |
| `yield_.py` | Per-shape query yield across runs | `models` |
| `jobqueue.py` | Persisted, lease-based queue | `models` |
| `scheduler.py` | Cron, validated at set time | `models`, `jobqueue` |
| `main.py` | The API | all of the above |

## The dependency rule that keeps it honest

`domains.py` imports **nothing**. The taxonomy is the one module that every
other module depends on and that depends on none, which is what lets it be
changed in one place and be trusted everywhere. `grounding.py` is the same:
pure functions, no I/O, so the guarantee it makes can be tested without a
database or a network.

Everything that could fail (the LLM, the search providers, the database) is
imported *inside* the function that needs it. That is why a gateway outage
degrades the explainer to a refusal rather than a 500.