# Design documents

Start here. Each document is one diagram-first view of the system.

| Document | Diagram | Contents |
|---|---|---|
| [01-system-context.md](01-system-context.md) | C4 context, container | The app, its dependencies, what leaves the machine |
| [data-model.md](data-model.md) | ER | Every table, its keys, and why the shape is what it is |
| [interaction.md](interaction.md) | sequence | One collection run, end to end, across four actors |
| [activity.md](activity.md) | activity | The collection loop's decision structure |
| [state.md](state.md) | state | Run, job and explanation lifecycles |
| [ui-interaction.md](ui-interaction.md) | UI flow | Views, what each answers, and what it deliberately does not show |
| [privacy.md](privacy.md) | data flow | What is stored, what is hashed, what never leaves |
| [ledger.md](ledger.md) | sequence | The hash chain, its proofs, and its failure modes |

All diagrams are Mermaid, rendered by GitHub and most markdown viewers.

---

## The one-paragraph version

An **investigation** is a question that does not fit in one subfield. The agent
plans searches, reads candidates, classifies each into one of ten economic
**domains**, extracts the **entities** it names, reads a **direction, magnitude
and horizon** off it where it makes a directional claim, and maps the results
into a **knowledge graph** whose cross-domain edges are drawn from the
**channel taxonomy** rather than invented. An **explainer** answers questions
over that graph, quoting only sources it was given and verifying every quote
verbatim against the published text. **Research gaps** report which domains have
nothing behind them. Every step lands on a **hash-chained ledger** that
re-derives from stored rows.

## Three ideas that carry the design

**A domain is a label; a channel is a mechanism.** Fourteen domains would be fourteen
piles. `app/domains.py` declares directed edges between them with a strength
prior and a typical lag, and the whole cross-domain layer is built on that
distinction. The walk is a *prior* — evidence either supports a hop or does
not.

**An edge needs an actor, not a keyword.** Two documents about stablecoins and
the Treasury share almost no vocabulary. They are related because both name
Tether, or both name the US Treasury. Entity overlap draws the edges keyword
search cannot — and it is deliberately hard to trigger, because the failure
mode of the alternative is a graph where everything connects to everything.

**Absence is a finding.** Coverage is reported as gaps, thin domains, and
*unbridged channels* — three lists, never one. The middle list exists because
collapsing "barely covered" into "not covered" is how a map ends up claiming
coverage it does not have.