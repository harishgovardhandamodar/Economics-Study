# Activity and state

## Collection loop — the decision structure

```mermaid
flowchart TD
    START([run_investigation_agent]) --> INV["load investigation"]
    INV --> LEDGER["open ledger scope<br/>Mandate: allowed actors,<br/>planned stages, domain scope"]
    LEDGER --> PLAN["PLAN — LLM: ≤6 queries,<br/>each naming a domain"]
    PLAN --> ENS{"planner produced<br/>usable JSON?"}
    ENS -->|no| DET["deterministic keyword plan<br/><i>flows through the same<br/>ensurers and yield accounting</i>"]
    ENS -->|yes| PRIMARY{"a primary-source<br/>query present?"}
    DET --> PRIMARY
    PRIMARY -->|no| ADDQ["add one aimed at<br/>the institution itself"]
    PRIMARY -->|yes| RANK
    ADDQ --> RANK["rank by historical yield<br/><i>order only — nothing dropped</i>"]

    RANK --> ROUND{rounds left<br/>AND under max_items?}
    ROUND -->|no| SUMMARY
    ROUND -->|yes| SEARCH["SEARCH — fan out<br/>rss · arxiv · web"]

    SEARCH --> FOUND{"any candidates?"}
    FOUND -->|no| SUMMARY
    FOUND -->|yes| PREFILTER["pre-rank to<br/>max(needed×2, 8)"]

    PREFILTER --> LOOP{"more batches to analyse?"}
    LOOP -->|yes| ANALYZE["ANALYZE — batch of 5"]
    ANALYZE --> EARLY{"keepers ≥<br/>needed + 5?"}
    EARLY -->|no| LOOP
    EARLY -->|yes| STOP["stop early<br/><i>the tail cannot change<br/>what gets persisted</i>"]
    STOP --> YIELD
    LOOP -->|no| YIELD["record yield<br/><b>BEFORE any early exit</b>"]

    YIELD --> KEPT{"anything kept?"}
    KEPT -->|no| SUMMARY
    KEPT -->|yes| MAP["MAP — persist + map"]
    MAP --> ENOUGH{"target reached<br/>OR rounds spent?"}
    ENOUGH -->|no| REFINE["REFINE — planner drafts<br/>follow-ups for uncovered domains"]
    REFINE --> RANK
    ENOUGH -->|yes| SUMMARY

    SUMMARY["SUMMARY — stats, status=done,<br/>run.end on the ledger"] --> END([ ])

    PLAN -.->|exception| ERR["status=error, traceback stored,<br/>scope reopened to record why"]
```

The ordering constraint that matters: **yield is recorded before every early
exit.** A round that analyzed candidates and kept none is the single most
useful thing to remember, and it is exactly the round that leaves no trace if
you record yield at the end.

## Run states

```mermaid
stateDiagram-v2
    [*] --> queued: POST /run
    queued --> running: worker claims + writes lease
    running --> done: artifacts + edges committed
    running --> error: exception

    done --> [*]
    error --> [*]

    note right of error
        inv.status returns to 'ready' even on
        error — a failed run must not leave the
        investigation looking busy forever.
    end note

    note right of done
        The agent reopens the ledger scope briefly
        after a failure, because a dead run is
        exactly the run whose reason you need
        on the chain.
    end note
```

## Job states

```mermaid
stateDiagram-v2
    [*] --> queued
    queued --> running: claim() writes<br/>lease_owner + lease_until
    running --> done: handler returns
    running --> queued: exception, attempts < max<br/>backoff 15 × 2ⁿ s
    running --> error: exception, attempts ≥ max
    running --> queued: <b>lease expired</b><br/>(worker died)
    queued --> cancelled: operator cancels
    running --> queued: cancel refused with 409
    error --> queued: retry requested
    done --> [*]
```

**A lease, not a lock.** Claiming writes the worker's id and a deadline. A
worker that dies stops renewing; the lease expires and the job is re-queued.
Nothing has to notice that a process crashed — which is the only mechanism that
works when the thing that crashed is the thing that would have noticed.

`recover_stale()` runs at startup and re-queues anything whose lease expired
while the process was down.

**A running job cannot be cancelled.** The API returns `409` and explains why:
the worker holds no cooperative cancellation point, and killing it would strand
the lease until it expires. The honest answer is "wait for the lease", not a
button that pretends.

## Explanation states

```mermaid
stateDiagram-v2
    [*] --> running: row created immediately
    running --> done: drafted, grounded, critiqued,<br/>roadmapped, stored
    running --> no_evidence: retrieval returned nothing

    done --> [*]
    no_evidence --> [*]

    note right of no_evidence
        A first-class outcome, not an error.
        The answer states that nothing collected
        speaks to the question, refuses to answer
        from memory, and files a ResearchGap so
        the next step is obvious.
    end note

    note right of done
        The answer is then saved back to the graph
        as a pre-accepted 'projection' artifact —
        an answer is evidence too, and without
        this the explainer could never cite itself.
    end note
```

## Investigation states

```mermaid
stateDiagram-v2
    [*] --> idle: created
    idle --> running: a run is queued
    running --> ready: run finished (done OR error)
    ready --> hidden: operator hides it
    hidden --> ready: operator unhides

    note right of hidden
        Hidden is not deleted. Every run, artifact,
        edge and answer stays; the investigation
        leaves the sidebar and comes back with
        "Show hidden".
    end note
```

## Scheduler

```mermaid
stateDiagram-v2
    [*] --> unscheduled
    unscheduled --> scheduled: cron set + validated
    scheduled --> scheduled: fires → enqueues a job
    scheduled --> unscheduled: disabled
    scheduled --> unscheduled: cron fails to parse → 400

    note right of scheduled
        Validation happens at the point the
        operator sets the timetable. A cron that
        fails to parse must be reported then,
        not discovered three weeks later when
        nothing has been collected.
    end note

    note right of scheduled
        Scheduled runs go through the queue,
        never inline. The scheduler thread is
        single-purpose; letting it run a
        six-minute collection would delay
        every other timetable behind it, and
        a crash would take the scheduler with it.
    end note
```

## Drift states

```mermaid
stateDiagram-v2
    [*] --> current
    current --> flagged: re-fetched, headline moved
    flagged --> current: operator clears the flag
    flagged --> replaced: a new artifact supersedes it

    note right of flagged
        Flagged, never deleted. A corrected
        figure is still evidence about what was
        believed when, and a map that drops those
        rows cannot show that a revision happened.
    end note

    note right of flagged
        'Headline moved' compares normalized
        prefixes. A re-fetch that differs only by
        whitespace, a site name, or a tracking
        suffix is a render change, not drift — and
        a flag that fires on every such page is a
        flag people learn to ignore.
    end note
```