# Data model

## Entities

```mermaid
erDiagram
    INVESTIGATION ||--o{ ARTIFACT : collects
    INVESTIGATION ||--o{ AGENT_RUN : "runs in"
    INVESTIGATION ||--o{ EXPLANATION : answers
    INVESTIGATION ||--o{ RESEARCH_GAP : "has gap in"
    INVESTIGATION ||--o{ RELATIONSHIP : contains
    INVESTIGATION ||--o{ QUERY_SHAPE_YIELD : "learns from"
    AGENT_RUN ||--o{ AGENT_EVENT : logs
    ARTIFACT ||--o{ ARTIFACT_REVIEW : "reviewed by"
    ARTIFACT ||--o{ RELATIONSHIP : source
    ARTIFACT ||--o{ RELATIONSHIP : target
    JOB }o--o| INVESTIGATION : "for"

    INVESTIGATION {
        int id PK
        string title
        string keywords
        text description
        string sources "rss,arxiv,web"
        string preferred_domains "comma-separated taxonomy ids"
        string driver "FK into domains.DRIVERS, nullable"
        text shock "the event, when driver is set"
        string status "idle|running|ready"
        boolean schedule_enabled
        string schedule_cron
        int schedule_max_items
        int schedule_max_rounds
        boolean auto_save_explanations
        boolean hidden
    }

    ARTIFACT {
        int id PK
        int investigation_id FK
        int run_id FK "nullable"
        string title
        string artifact_type
        string url
        text description "ANALYST'S SUMMARY"
        text source_text "PUBLISHED SNIPPET"
        text content "extracted body"
        string domain "taxonomy id - the cross-domain key"
        json entities_json "named actors"
        string direction "up|down|flat|nullable"
        float magnitude "0..1, nullable"
        string horizon "immediate|short|medium|long|nullable"
        float relevance
        text relevance_reason
        float sentiment
        string review "pending|accepted|rejected"
        string origin "agent|manual"
        int drift "flagged off-brief, never deleted"
        string tags
        datetime date_published
    }

    RELATIONSHIP {
        int id PK
        int investigation_id FK
        int source_id FK
        int target_id FK
        string relationship_type
        text description
        float confidence "prior strength, for transmits_to"
        int lag "quarters, for transmits_to"
        int run_id FK "nullable"
    }

    AGENT_RUN {
        int id PK
        int investigation_id FK
        string status "running|done|error"
        string trigger "manual|schedule|gap"
        text plan "JSON: rationale + queries"
        text stats "JSON: counts per stage"
        text error
        datetime started_at
        datetime finished_at
    }

    EXPLANATION {
        int id PK
        int investigation_id FK
        text question
        text answer
        text sources_json
        text critique
        text roadmap
        float confidence
        string status "running|done|no_evidence"
        string mode
        string depth
        string audience
        text domains_json "domains the answer touched"
        int dropped_citations
        text trace "JSON: grounding counts + drops"
        int parent_id "nullable"
        int thread_id "nullable"
        text quiz
        boolean bookmarked
        boolean watched
    }

    RESEARCH_GAP {
        int id PK
        int investigation_id FK
        string dimension
        string domain "nullable"
        text why
        string status "open|ruled_out|investigating"
        text evidence
    }

    QUERY_SHAPE_YIELD {
        int id PK
        int investigation_id FK
        string shape "normalised query + source mix"
        string raw_queries
        string sources
        int times_used
        int llm_calls
        int found
        int kept
        datetime created_at
        datetime last_used_at
    }

    JOB {
        int id PK
        string kind "collect|explain|gaps"
        text payload_json
        string status "queued|running|done|error|cancelled"
        int priority
        int investigation_id
        text result_json
        text error
        int attempts
        int max_attempts
        string lease_owner
        datetime lease_until
    }
```

## The ledger (separate shape, separate module)

```mermaid
erDiagram
    LEDGER_RUN ||--o{ LEDGER_EVENT : chains
    LEDGER_RUN ||--o{ LEDGER_CLAIM : emits
    LEDGER_RUN ||--o{ LEDGER_APPROVAL : gates
    LEDGER_RUN ||--o| LEDGER_RUN : "anchored to session"

    LEDGER_RUN {
        string id PK "caller-supplied, e.g. run-42"
        string kind "task|session"
        string session_id FK
        string client_key
        text mandate_json "policy in force - hashed at creation"
        string mandate_hash
        string genesis_hash
        string head_hash "cheap anchoring"
        int head_seq
        string status
    }

    LEDGER_EVENT {
        int id PK
        string run_id FK
        int seq "0-based, UNIQUE per run"
        string ts "ISO-8601 STRING, not DateTime"
        string actor_type "agent|llm|mcp|human|system"
        string actor
        string kind
        string intent
        string verdict
        string severity
        text data_json "HASHED"
        text input_refs "JSON list of sha256"
        string prev_hash "HASHED"
        string hash "HASHED"
        text proof_json "derived, NOT hashed"
        string trace "observation about the record, NOT hashed"
    }
```

## Why these shapes

### `description` and `source_text` are separate columns

This is the single most consequential modelling decision in the file.

`description` is the **analyst's summary** — one or two sentences the model
wrote. `source_text` is the **published snippet**, as the source itself
rendered it.

Grounding verifies every quote against `source_text`. Verifying against the
summary would be circular: the model could quote the summary back at the
checker and pass without ever having read the source, while a quote genuinely
lifted from the article would fail because the paraphrase paraphrased it. Both
are wrong, and they fail in opposite directions — which means a checker built
on the summary is not merely noisy, it is anti-correlated with truth.

Artifacts collected before the split fall back to the summary, and the
explainer's trace records which sources that applied to, so a drop count never
silently mixes a hallucination with a data gap.

### `ts` is a string, not a `DateTime`

Audit needs the exact authored timestamp with its offset preserved, and
lexicographic order must equal chronological order for the timeline to sort
without a timezone library. A `DateTime` normalises that away.

### `proof_json` and `trace` are outside the hashed core

Both are *observations about the record*, not part of what the record claims.
`proof_json` is recomputable — verification re-derives it and reports drift
rather than trusting it. `trace` says which request wrote the row. Folding
either into `data_json` would change every event hash already on disk and break
verification of every run ever recorded.

### `direction` / `magnitude` / `horizon` are columns, not prose

"Rates up" is not an insight. `up · 0.6 · medium` is something a transmission
chain can be built on and something the summary can rank. Keeping them
structured means the strongest-signal view needs no language model to rank, and
means an artifact with no reading is visibly *background* rather than quietly
counted as a finding.

### `drift` is a flag, not a deletion

A corrected figure is still evidence about what was believed when. A map that
silently drops those rows cannot show that a revision happened.

### `domain` is the cross-domain key

`preferred_domains` on the investigation says what the operator *asked* about.
`domain` on the artifact says what the agent *found*. Those are different
questions, and conflating them is how a map ends up claiming coverage it does
not have. Coverage is computed from the artifact column, always.

## Migration policy

`database._MIGRATIONS` is an additive `ALTER TABLE` list applied at startup.
`create_all` never adds a column to an existing table, so every later addition
lives there.

Additive only, deliberately. A destructive migration would take an
investigation's artifacts with it, and the entire value of the app is what it
has already collected.