# The hash chain

## Why a chain and not a log

An append-only table of events is not auditable. It records that something
happened; it does not record that the record has not since been changed. Every
row in `ledger_events` commits to its predecessor:

```mermaid
flowchart LR
    G["genesis<br/>0000…0000"] --> E0["seq 0<br/>prev: 0000…<br/>hash: a3f1…"]
    E0 --> E1["seq 1<br/>prev: a3f1…<br/>hash: 7c22…"]
    E1 --> E2["seq 2<br/>prev: 7c22…<br/>hash: e914…"]
    E2 --> E3["seq 3<br/>prev: e914…<br/>hash: 02bd…"]

    style G fill:#30363d,stroke:#6e7681
    style E3 fill:#1f6feb22,stroke:#58a6ff
```

Edit any payload, delete any row, or insert a row after the fact, and the link
breaks. Verification recomputes the whole chain from the stored rows and names
the exact event where it stopped — and stops there, because once a link is
broken nothing after it can be trusted and continuing would report a cascade of
failures that are really one.

## The hashed core

```mermaid
flowchart TB
    subgraph EVENT["ledger_events row"]
        subgraph HASHED["HASHED — CORE_FIELDS"]
            CORE["run_id, seq, ts, actor_type,<br/>actor, kind, intent, verdict,<br/>severity, data_json, prev_hash"]
        end
        subgraph OUTSIDE["NOT hashed"]
            proof_json["proof_json<br/><i>derived state —<br/>recomputed and compared,<br/>drift reported</i>"]
            trace["trace<br/><i>which request wrote<br/>this row</i>"]
        end
    end
    HASHED --> hash["hash = sha256(<br/>canon(core))"]
    hash --> prev_hash["becomes the next row's prev_hash"]

    style HASHED fill:#3fb95022,stroke:#3fb950
    style OUTSIDE fill:#d2992222,stroke:#d29922
```

Two exclusions, both deliberate:

**`proof_json`** is recomputable. Verification rebuilds it from the stored event
and reports drift rather than trusting the stored copy — a proof that had to be
trusted would not be a proof.

**`trace`** is an observation *about* the record: which request wrote this row.
It says when and under which request, not what the record claims. Folding it in
would change every event hash on disk and break verification of every run ever
written.

## What breaks what

| Tamper | Detected as | At seq |
|---|---|---|
| Edited payload | `hash_mismatch` | the edited event |
| Deleted row (head) | `seq_gap` | the following event |
| Deleted row (tail only) | `head_mismatch` | run level — every stored link is still internally consistent, so only the recorded head can catch it |
| Forged row, recomputed hash | `broken_link` | the *next* row, whose `prev_hash` still points at the row that was pushed out |
| Deleted claim referenced by an event | `unresolved_ref` | the citing event |
| Reordered rows | `seq_gap` | the first out-of-place row |
| Edited run label | nothing | `label` is not in the core — a cosmetic field, not a claim |

The forged-row case is the one worth dwelling on. A forger who recomputes
every hash correctly still cannot insert, because the next row's `prev_hash` is
already fixed. `test_insertion_breaks_the_chain` pins exactly this.

## Proofs

Every event gets a re-derivable artifact, attached outside the hash:

```mermaid
flowchart LR
    E["event"] --> BP["build_proof<br/>run_id, seq, event_hash,<br/>prev_hash, kind, actor, ts"]
    BP --> PH["proof_hash"]
    E -.->|recompute| PC["proof_checks"]
    BP -.-> PC
    PC --> R1["✓ proof_hash<br/>✓ event_hash binds this event<br/>✓ prev_hash binds this predecessor<br/>✓ claim digest matches<br/>✓ input refs are sha256"]
    PC --> R2["named check that failed"]
```

`verify_chain` reports `proof_drift` as a count, separate from `ok`. A missing
proof degrades the audit story without breaking the chain — it is derived state,
and treating its absence as evidence of tampering would be wrong.

## Claims, and what a bad one reaches

```mermaid
flowchart TB
    A["agent asserts:<br/>claim + citations"] --> H["claim_hash =<br/>sha256(text + sorted citations)"]
    H --> L["ledger_claims row"]
    L --> E1["event: claim.emit<br/>input_refs: [claim_hash]"]
    E1 --> E2["agent B consumes it<br/>input_refs: [event_hash]"]
    E2 --> E3["summary cites B<br/>input_refs: [event_hash]"]
    E3 --> Q["contamination(root)<br/>→ who consumed this?"]

    style H fill:#bc8cff22,stroke:#bc8cff
```

Two properties fall out of hashing the claim's *own content*:

1. **Parroting is detectable.** Two agents asserting the same thing produce the
   same `claim_hash`. "Did B simply restate A?" becomes a question with a
   yes/no answer.
2. **Blast radius is a query.** `GET /api/ledger/runs/{id}/contamination/{hash}`
   walks `input_refs` transitively and answers "if this claim was wrong, what
   did it reach?"

## Mandates — policy as data

A `Mandate` is hashed at run creation, so the rules in force when an action
happened are provable afterwards rather than inferred from whatever the code
says today.

```mermaid
flowchart TB
    E["an event"] --> C{"policy_engine"}
    C --> A1{"actor_type<br/>in allowed_actors?"}
    A1 -->|no| DENY["deny + gate.check event"]
    A1 -->|yes| A2{"intent in<br/>allowed_intents?"}
    A2 -->|no| DENY
    A2 -->|yes| A3{"tool in<br/>allowed_tools?"}
    A3 -->|no| DENY
    A3 -->|yes| A4{"planned_intents<br/>declared?"}
    A4 -->|not declared| FLAG["flag + gate.check<br/><b>not blocked</b>"]
    A4 -->|yes| A5{"budget<br/>remaining?"}
    A5 -->|exhausted| DENY
    A5 -->|yes| OK["append"]

    style DENY fill:#f8514922,stroke:#f85149
    style FLAG fill:#d2992222,stroke:#d29922
    style OK fill:#3fb95022,stroke:#3fb950
```

Four rules of deliberately different force:

- **Hard boundary** — actors, intents, tools, domains. A violation is denied.
- **Soft shape** — `planned_intents`. An undeclared intent is *flagged, not
  refused*: a run that discovers a new step is fine, and a run that quietly
  stopped doing the work it declared is not.
- **Budget** — `max_events`, `max_llm_calls`.
- **Human gates** — kinds held for sign-off.

Every refusal also emits a `gate.check` event, so a violation can never be
scrolled past as a silent omission.

## Sessions — order across runs

Each browser sitting gets a spine:

```mermaid
flowchart LR
    S["session spine<br/>ses-4f2a…<br/>kind=session"] --> A1["run.anchor<br/>run-42"]
    S --> A2["run.anchor<br/>run-43"]
    S --> A3["run.anchor<br/>exp-12"]
    A1 --> C1["run-42's own chain<br/>verifies alone"]
    A2 --> C2["run-43's own chain<br/>verifies alone"]

    style S fill:#1f6feb22,stroke:#58a6ff
```

The spine does not hold the work — it holds one `run.anchor` per task, in the
order the tasks were adopted. So each task still verifies independently, and the
session chain independently proves the *order* they happened in. One chain
implementation, not two.

A client key idle past `ECON_SESSION_IDLE_S` (default 30 min) starts a **new**
sitting: the old chain stays intact and verifiable, and new work anchors to a
fresh spine rather than to a chain that has been quiet for an hour.

## Self-verifying exports

```mermaid
sequenceDiagram
    participant Auditor
    participant JSON as exported bundle

    Auditor->>JSON: GET /api/ledger/runs/run-42/export
    Note over JSON: {run, events[], claims[],<br/>bundle_hash}
    Auditor->>JSON: POST /api/ledger/verify-export
    Note right of JSON: recompute every event hash,<br/>recompute the bundle hash,<br/>resolve every input_ref
    JSON-->>Auditor: {ok, problems[]}
```

No database, no application, no trust in the exporting process. The bundle
hash covers the content only — verification is *derived from* that content and
would otherwise be a self-referential field that changes the hash it is meant
to check.

The frontend runs this offline check before it tells you the bundle is good,
rather than asserting it.

## What is fail-open, and what is not

| Component | On failure | Why |
|---|---|---|
| `ledger.append` | Returns `None`, records a `LedgerAuditDrop` | A failed audit write must not kill the work it was recording |
| `llm.chat` audit block | Swallowed entirely | Same: the call succeeded; the record of it failing to be written is not the caller's problem |
| Session resolution in middleware | Proceeds unaudited | An audit outage must never become a product 500 |
| `monitor_integrity` | Returns the broken chains it found | It reports on failures; it must not add one |
| `verify_chain` | Reports the exact break and stops | Continuing past a break would report a cascade of failures that are really one |

The one thing that is **not** fail-open: `verify_chain` itself. It never
reports `ok` for a chain it could not verify. If the database cannot be read,
that is an error, not a pass.