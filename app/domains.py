"""The cross-domain economics taxonomy.

Everything domain-specific in this app resolves to a name in here. The point
is that a single flat taxonomy is what makes *cross-domain* mapping possible at
all: an artifact about datacententer power and an artifact about sovereign debt
can only be related if both carry a domain, and only if the relation is typed by
a channel that exists in ``CHANNELS``.

Three ideas, kept deliberately separate:

* **Domains** — where an artifact sits. Ten of them, listed in ``DOMAINS``.
  A domain is a label, never a score.
* **Channels** — how one domain transmits to another. ``CHANNELS`` is a directed
  edge list with a typical lag. This is the vocabulary the transmission engine
  walks, so a chain "datacenter capex -> power demand -> industrial electricity
  prices -> CPI -> central bank reaction" is *expressible* rather than prose.
* **Drivers** — what actually moves. A driver (a policy rate decision, a
  sovereign downgrade) is the *subject* of an analysis; domains are where its
  effects land.

The domains are deliberately not peers in size. Macroeconomics and political
involvement are hubs by construction: almost everything has a monetary or a
policy leg. That asymmetry is the reason the graph is worth looking at, and it
is encoded here rather than left to emerge.
"""

# --------------------------------------------------------------------------
# Domains
# --------------------------------------------------------------------------
# id -> (label, blurb, color for the graph, icon)
DOMAINS: dict[str, dict] = {
    "macro": {
        "label": "Macroeconomics",
        "short": "Macro",
        "blurb": "Growth, inflation, employment, monetary transmission, FX.",
        "color": "#58a6ff",
        "icon": "fa-chart-line",
    },
    "monetary_policy": {
        "label": "Monetary policy",
        "short": "Monetary",
        "blurb": "Central banks, rate paths, QT/QE, reserve regimes, the dollar.",
        "color": "#bc8cff",
        "icon": "fa-building-columns",
    },
    "fiscal_policy": {
        "label": "Fiscal policy",
        "short": "Fiscal",
        "blurb": "Budgets, deficits, subsidies, industrial policy, procurement.",
        "color": "#f0883e",
        "icon": "fa-file-invoice-dollar",
    },
    "sovereign_debt": {
        "label": "Sovereign debt",
        "short": "Sov. debt",
        "blurb": "Treasuries, JGBs, gilts, EM credit, debt-service ratios, auctions.",
        "color": "#f85149",
        "icon": "fa-scale-balanced",
    },
    "stablecoins": {
        "label": "Stablecoins",
        "short": "Stablecoins",
        "blurb": "Issuance, reserves, redemptions, payment flows, market structure.",
        "color": "#56d4dd",
        "icon": "fa-coins",
    },
    "blockchain": {
        "label": "Blockchain",
        "short": "Blockchain",
        "blurb": "L1/L2 economics, consensus, MEV, tokenomics, on-chain settlement.",
        "color": "#3fb950",
        "icon": "fa-link",
    },
    "datacenters": {
        "label": "Datacenters",
        "short": "Datacenters",
        "blurb": "Power, grid interconnection, water, chips, land, capacity, capex.",
        "color": "#79c0ff",
        "icon": "fa-server",
    },
    "ai_labs": {
        "label": "AI frontier labs",
        "short": "AI labs",
        "blurb": "OpenAI, Anthropic, Google DeepMind, Meta, xAI, Mistral, DeepSeek,"
                 "Qwen.",
        "color": "#ffa657",
        "icon": "fa-brain",
    },
    "ai_agents": {
        "label": "AI agents",
        "short": "Agents",
        "blurb": "Agentic systems, tool use, autonomy, agent labour and agent payment"
                 "rails.",
        "color": "#d2a8ff",
        "icon": "fa-robot",
    },
    "agi_asi": {
        "label": "AGI / ASI progression",
        "short": "AGI/ASI",
        "blurb": "Capability timelines, compute scaling, labour displacement,"
                 "governance.",
        "color": "#ff7b72",
        "icon": "fa-arrow-trend-up",
    },
    "politics": {
        "label": "Political involvement",
        "short": "Politics",
        "blurb": "Elections, geopolitics, sanctions, export controls, state AI policy.",
        "color": "#a5a5a5",
        "icon": "fa-landmark",
    },
}

DOMAIN_IDS: list[str] = list(DOMAINS)

# Domains that act as transmission hubs. Used to bias graph layout and to
# decide which chains are worth surfacing first — a chain through monetary
# policy reaches further than one through, say, MEV.
HUB_DOMAINS: tuple[str, ...] = ("macro", "monetary_policy", "politics")


# --------------------------------------------------------------------------
# Transmission channels
# --------------------------------------------------------------------------
# Directed edges between domains. ``lag`` is the typical horizon in quarters:
#   0 = contemporaneous, 1 = next few quarters, 2 = 1-3 years, 4 = structural.
# ``strength`` is the prior likelihood the edge is real *at all* (0..1). It is a
# prior, not a finding — evidence in a run overrides it, and the transmission
# engine reports which way.
CHANNELS: list[dict] = [
    # --- money & prices ---
    {"src": "monetary_policy", "dst": "macro", "channel": "policy transmission",
     "strength": 0.95, "lag": 1,
     "note": "Policy rate -> credit conditions -> demand -> output and prices."},
    # Macro is a hub because the loop closes: the cycle is not a one-way pipe
    # from policy into output, it comes back and sets what policy does next.
    # Omitting these would make macro a terminal sink and every transmission
    # path one-way.
    {"src": "macro", "dst": "monetary_policy", "channel": "reaction function",
     "strength": 0.9, "lag": 2,
     "note": "Inflation and slack set what the central bank does next; the "
             "same path back is why the cycle is not one-way."},
    {"src": "macro", "dst": "fiscal_policy", "channel": "budget arithmetic",
     "strength": 0.75, "lag": 2,
     "note": "The tax base and deficit ratio are cyclical; a recession widens "
             "the deficit without a single new spending decision."},
    {"src": "macro", "dst": "politics", "channel": "voter response",
     "strength": 0.7, "lag": 2,
     "note": "The cost of living is the most reliable predictor of who wins a "
             "close election, which makes the cycle a political variable."},
    {"src": "macro", "dst": "sovereign_debt", "channel": "cyclical deficit",
     "strength": 0.8, "lag": 1,
     "note": "Automatic stabilisers move the primary balance with the cycle "
             "before any discretionary decision is taken."},
    {"src": "monetary_policy", "dst": "sovereign_debt", "channel": "duration repricing",
     "strength": 0.9, "lag": 0,
     "note": "Long-end yields respond to the expected path, not the current rate."},
    {"src": "monetary_policy", "dst": "stablecoins", "channel": "reserve yield",
     "strength": 0.8, "lag": 0,
     "note": "Policy rate sets the risk-free yield that anchors reserve returns."},
    {"src": "fiscal_policy", "dst": "sovereign_debt", "channel": "issuance",
     "strength": 0.95, "lag": 0,
     "note": "Deficits are financed; the stock of debt is the flow accumulated."},
    {"src": "fiscal_policy", "dst": "macro", "channel": "demand impulse",
     "strength": 0.9, "lag": 1,
     "note": "G and T move aggregate demand with a multiplier."},
    {"src": "fiscal_policy", "dst": "datacenters", "channel": "industrial subsidy",
     "strength": 0.75, "lag": 2,
     "note": "Production credits and domestic-content rules lower effective capex."},
    {"src": "fiscal_policy", "dst": "ai_labs", "channel": "public funding",
     "strength": 0.7, "lag": 2,
     "note": "Compute grants and equity stakes substitute for private capital."},

    # --- sovereign credit & the dollar ---
    {"src": "sovereign_debt", "dst": "macro", "channel": "fiscal dominance",
     "strength": 0.8, "lag": 2,
     "note": "Unsustainable debt service crowds out spending and taxes the economy."},
    {"src": "sovereign_debt", "dst": "monetary_policy", "channel": "market pressure",
     "strength": 0.85, "lag": 1,
     "note": "Bond vigilantes raise term premia; central banks face a choice."},
    {"src": "sovereign_debt", "dst": "stablecoins", "channel": "safe-haven demand",
     "strength": 0.7, "lag": 0,
     "note": "T-bills are the dominant reserve asset; a bill shock is a stablecoin"
             "shock."},
    {"src": "sovereign_debt", "dst": "politics", "channel": "market discipline",
     "strength": 0.7, "lag": 1,
     "note": "Bond markets constrain fiscal ambition before elections do."},
    {"src": "sovereign_debt", "dst": "blockchain", "channel": "reserve substitution",
     "strength": 0.45, "lag": 4,
     "note": "Sovereign-adjacent instruments (tokenised bills) as a structural theme."},

    # --- the AI build-out ---
    {"src": "ai_labs", "dst": "datacenters", "channel": "compute demand",
     "strength": 0.95, "lag": 1,
     "note": "Capex commitments become power, land and chip orders."},
    {"src": "ai_labs", "dst": "macro", "channel": "capex impulse",
     "strength": 0.7, "lag": 2,
     "note": "Hyperscaler capex is now a measurable line in national accounts."},
    {"src": "datacenters", "dst": "macro", "channel": "investment & electricity",
     "strength": 0.7, "lag": 1,
     "note": "Construction, grid upgrades and power prices show up in GDP and CPI."},
    {"src": "datacenters", "dst": "sovereign_debt", "channel": "load growth",
     "strength": 0.6, "lag": 2,
     "note": "Data-centre load forecasts are now inputs to utility\n"
            " and muni financing."},
    {"src": "datacenters", "dst": "politics", "channel": "grid politics",
     "strength": 0.75, "lag": 2,
     "note": "Interconnection queues and rate cases become political disputes."},
    {"src": "datacenters", "dst": "agi_asi", "channel": "capability ceiling",
     "strength": 0.85, "lag": 4,
     "note": "Training runs are power-bound before they are algorithm-bound."},
    {"src": "ai_labs", "dst": "agi_asi", "channel": "capability frontier",
     "strength": 0.9, "lag": 1,
     "note": "Each release redefines what the next one must clear."},
    {"src": "ai_agents", "dst": "macro", "channel": "labour substitution",
     "strength": 0.65, "lag": 4,
     "note": "Agent deployment shows up as hours worked, not headcount."},
    {"src": "ai_agents", "dst": "agi_asi", "channel": "autonomy threshold",
     "strength": 0.8, "lag": 2,
     "note": "Reliable long-horizon autonomy is the usual definition being contested."},
    {"src": "ai_agents", "dst": "stablecoins", "channel": "machine payments",
     "strength": 0.6, "lag": 2,
     "note": "Agents need settlement rails they can pay without a human in the loop."},
    {"src": "ai_agents", "dst": "blockchain", "channel": "agentic on-chain",
     "strength": 0.55, "lag": 2,
     "note": "Autonomous agents need identity, reputation and atomic settlement."},
    {"src": "agi_asi", "dst": "macro", "channel": "productivity & distribution",
     "strength": 0.6, "lag": 4,
     "note": "The J-curve question: investment now, output later, or never."},
    {"src": "agi_asi", "dst": "politics", "channel": "governance response",
     "strength": 0.8, "lag": 2,
     "note": "Capability shocks produce compute/export-control legislation."},
    {"src": "agi_asi", "dst": "ai_labs", "channel": "safety commitment",
     "strength": 0.7, "lag": 1,
     "note": "Frontier labs price the capability they are racing."},

    # --- money rails ---
    {"src": "stablecoins", "dst": "blockchain", "channel": "settlement demand",
     "strength": 0.85, "lag": 0,
     "note": "Stablecoin float is a structural source of demand for the underlying"
             "chain."},
    {"src": "stablecoins", "dst": "monetary_policy", "channel": "dollar substitution",
     "strength": 0.7, "lag": 1,
     "note": "A short-duration private money supply is, mechanically, a policy"
             "question."},
    {"src": "stablecoins", "dst": "politics", "channel": "legislative trigger",
     "strength": 0.8, "lag": 1,
     "note": "Reserve, disclosure and state-charter rules are now legislative, not"
             "optional."},
    {"src": "stablecoins", "dst": "macro", "channel": "cross-border flows",
     "strength": 0.6, "lag": 1,
     "note": "Dollar-denominated settlement channels in and out of capital controls."},
    {"src": "blockchain", "dst": "politics", "channel": "regulatory surface",
     "strength": 0.6, "lag": 1,
     "note": "Protocol design choices become regulatory facts."},

    # --- the state layer ---
    {"src": "politics", "dst": "fiscal_policy", "channel": "legislative authority",
     "strength": 0.8, "lag": 1,
     "note": "Elections set the fiscal envelope; coalitions set its composition."},
    {"src": "politics", "dst": "monetary_policy",
     "channel": "central bank independence",
     "strength": 0.7, "lag": 1,
     "note": "Appointments, mandates and political pressure move the\n"
            " reaction function."},
    {"src": "politics", "dst": "ai_labs", "channel": "export controls & compute quotas",
     "strength": 0.8, "lag": 1,
     "note": "Chip and compute restrictions decide which labs can scale, where."},
    {"src": "politics", "dst": "datacenters", "channel": "permitting & nationality",
     "strength": 0.7, "lag": 2,
     "note": "Where compute may be built is now a matter of state policy."},
    {"src": "politics", "dst": "blockchain", "channel": "sanctions & licensing",
     "strength": 0.6, "lag": 1,
     "note": "On-chain addresses are sanctions targets; exchanges are the chokepoint."},
    {"src": "politics", "dst": "stablecoins", "channel": "sanctions & licensing",
     "strength": 0.65, "lag": 1,
     "note": "Freezing addresses is now a routine tool of statecraft."},
    {"src": "politics", "dst": "sovereign_debt", "channel": "sanctions & default risk",
     "strength": 0.5, "lag": 1,
     "note": "Reserve-currency weaponisation carries an issuer's own borrowing cost."},
    {"src": "politics", "dst": "agi_asi", "channel": "compute governance",
     "strength": 0.6, "lag": 2,
     "note": "Frontier training thresholds are increasingly an international affair."},
]

# Derived index, built once at import: ``(src, dst) -> channel``.
def _channel_index() -> dict:
    idx: dict[tuple[str, str], dict] = {}
    for c in CHANNELS:
        idx.setdefault((c["src"], c["dst"]), c)
    return idx


CHANNEL_INDEX: dict[tuple[str, str], dict] = _channel_index()


# --------------------------------------------------------------------------
# Driver classes — the *subjects* an analysis can be run on
# --------------------------------------------------------------------------
# A driver is a scheduled or policy event with an expected transmission. The
# transmission engine walks channels outward from ``domains``.
DRIVERS: dict[str, dict] = {
    "rate_decision": {
        "label": "Central bank rate decision",
        "domains": ["monetary_policy", "macro", "sovereign_debt"],
        "typical_lag": 1,
        "description": "A change in the policy rate or balance-sheet stance.",
    },
    "fiscal_package": {
        "label": "Fiscal package or budget",
        "domains": ["fiscal_policy", "macro", "sovereign_debt"],
        "typical_lag": 1,
        "description": "Tax, spending, subsidy or industrial-policy legislation.",
    },
    "issuance": {
        "label": "Large sovereign issuance / auction surprise",
        "domains": ["sovereign_debt", "macro", "politics"],
        "typical_lag": 0,
        "description": "Coupon, size or tail on a benchmark bond sale.",
    },
    "downgrade": {
        "label": "Sovereign rating action",
        "domains": ["sovereign_debt", "politics", "macro"],
        "typical_lag": 0,
        "description": "Rating change, outlook revision, or watch placement.",
    },
    "stablecoin_regulation": {
        "label": "Stablecoin rule change",
        "domains": ["stablecoins", "politics", "blockchain", "monetary_policy"],
        "typical_lag": 1,
        "description": "Reserve, disclosure, licensing or state-charter requirements.",
    },
    "stablecoin_flow": {
        "label": "Stablecoin float / redemption shock",
        "domains": ["stablecoins", "blockchain", "macro"],
        "typical_lag": 0,
        "description": "A step change in issuance, redemption or market concentration.",
    },
    "compute_commitment": {
        "label": "Frontier-lab compute commitment",
        "domains": ["ai_labs", "datacenters", "sovereign_debt", "macro"],
        "typical_lag": 2,
        "description": "A multi-billion-dollar capex or power reservation.",
    },
    "capability_jump": {
        "label": "Capability jump / release",
        "domains": ["ai_labs", "agi_asi", "ai_agents"],
        "typical_lag": 1,
        "description": "A frontier model release that resets the capability baseline.",
    },
    "export_control": {
        "label": "Export control or compute quota",
        "domains": ["politics", "ai_labs", "datacenters"],
        "typical_lag": 1,
        "description": "Chip, model-weight or compute restrictions across borders.",
    },
    "grid_constraint": {
        "label": "Grid / power constraint",
        "domains": ["datacenters", "macro", "politics", "agi_asi"],
        "typical_lag": 2,
        "description": "Interconnection queue, capacity reservation or price shock.",
    },
    "election": {
        "label": "Election with economic consequence",
        "domains": ["politics", "fiscal_policy", "monetary_policy", "sovereign_debt"],
        "typical_lag": 2,
        "description": "A result that moves the fiscal envelope or the reaction"
                       "function.",
    },
    "sanction": {
        "label": "Sanctions regime change",
        "domains": ["politics", "blockchain", "stablecoins", "sovereign_debt"],
        "typical_lag": 1,
        "description": "New designations, or their removal, with financial reach.",
    },
    "protocol_upgrade": {
        "label": "Protocol or consensus upgrade",
        "domains": ["blockchain", "stablecoins", "ai_agents"],
        "typical_lag": 1,
        "description": "A change in settlement cost, throughput or token economics.",
    },
}


# --------------------------------------------------------------------------
# Artifact types
# --------------------------------------------------------------------------
ARTIFACT_TYPES: dict[str, dict] = {
    "news": {"label": "News", "icon": "fa-newspaper"},
    "paper": {"label": "Paper", "icon": "fa-file-lines"},
    "working_paper": {"label": "Working paper", "icon": "fa-file-lines"},
    "dataset": {"label": "Dataset / release", "icon": "fa-database"},
    "report": {"label": "Report", "icon": "fa-chart-pie"},
    "policy_doc": {"label": "Policy document", "icon": "fa-gavel"},
    "statistic": {"label": "Statistic", "icon": "fa-hashtag"},
    "announcement": {"label": "Announcement", "icon": "fa-bullhorn"},
    "filing": {"label": "Filing", "icon": "fa-file-contract"},
    "speech": {"label": "Speech / testimony", "icon": "fa-microphone"},
    "transcript": {"label": "Transcript / call", "icon": "fa-quote-left"},
    "onchain": {"label": "On-chain observation", "icon": "fa-link"},
    "opinion": {"label": "Opinion", "icon": "fa-comment-dots"},
    "projection": {"label": "Projection", "icon": "fa-arrow-trend-up"},
    "forecast": {"label": "Forecast", "icon": "fa-cloud-arrow-up"},
    "essay": {"label": "Essay", "icon": "fa-feather-pointed"},
    "known_issue": {"label": "Controversy / incident",
                    "icon": "fa-triangle-exclamation"},
}

DEFAULT_ARTIFACT_TYPE = "news"


# --------------------------------------------------------------------------
# Relationship types
# --------------------------------------------------------------------------
RELATIONSHIP_TYPES: dict[str, dict] = {
    "references": {"label": "references", "color": "#8b949e", "dashes": False,
                   "note": "Cites or points at."},
    "supports": {"label": "supports", "color": "#3fb950", "dashes": False,
                 "note": "Evidence for the same claim."},
    "contradicts": {"label": "contradicts", "color": "#f85149", "dashes": False,
                    "note": "Evidence against it."},
    "builds_upon": {"label": "builds upon", "color": "#58a6ff", "dashes": False,
                    "note": "Extends the earlier claim."},
    "responds_to": {"label": "responds to", "color": "#bc8cff", "dashes": False,
                    "note": "Replies to, or revises."},
    "similar_to": {"label": "similar to", "color": "#6e7681", "dashes": True,
                   "note": "Same subject, no stronger claim made."},
    "transmits_to": {"label": "transmits to", "color": "#56d4dd",
                     "dashes": False,
                     "note": "A causal channel between two domains, not two\n"
                            " documents."},
    "priced_by": {"label": "priced by", "color": "#d29922", "dashes": False,
                  "note": "The market that reflects this."},
    "constrains": {"label": "constrains", "color": "#a5a5a5", "dashes": True,
                   "note": "A limit on what can happen next."},
}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def domain_label(domain_id: str | None) -> str:
    d = DOMAINS.get(domain_id or "")
    return d["label"] if d else (domain_id or "unclassified")


def domain_color(domain_id: str | None) -> str:
    d = DOMAINS.get(domain_id or "")
    return d["color"] if d else "#6e7681"


def normalize_domains(values) -> list[str]:
    """Coerce anything into a de-duplicated list of known domain ids.

    Accepts a list, a comma-separated string, or None. Unknown names are
    dropped rather than passed through: an artifact carrying a domain the
    taxonomy has never heard of cannot participate in transmission, and letting
    it through would make the graph lie about how much is mapped.
    """
    if not values:
        return []
    if isinstance(values, str):
        raw = values.replace(";", ",").split(",")
    else:
        raw = []
        for v in values:
            raw.extend(str(v).replace(";", ",").split(","))
    out: list[str] = []
    for v in raw:
        key = str(v).strip().lower().replace(" ", "_").replace("-", "_")
        if key in DOMAINS and key not in out:
            out.append(key)
    return out


def channels_from(domain_id: str) -> list[dict]:
    """Every channel leaving ``domain_id``, strongest first."""
    return sorted((c for c in CHANNELS if c["src"] == domain_id),
                  key=lambda c: -c["strength"])


def channels_into(domain_id: str) -> list[dict]:
    return sorted((c for c in CHANNELS if c["dst"] == domain_id),
                  key=lambda c: -c["strength"])


def walk_transmission(seeds: list[str], max_depth: int = 3,
                      min_strength: float = 0.5) -> list[dict]:
    """Breadth-first walk of the channel graph from ``seeds``.

    Returns one row per reachable (domain, depth, cumulative_lag) with the
    channel path that got there. Cycles are cut by first-visit, which is right
    for a prior: what matters is *whether* a domain is reachable, not how many
    times you can loop back to it.
    """
    seeds = [d for d in seeds if d in DOMAINS]
    seen: set[str] = set(seeds)
    frontier = [(d, 0, 0, [d]) for d in seeds]
    out: list[dict] = []
    while frontier:
        nxt: list[tuple] = []
        for dom, depth, lag, path in frontier:
            for c in channels_from(dom):
                if c["strength"] < min_strength:
                    continue
                dst = c["dst"]
                out.append({
                    "domain": dst,
                    "depth": depth + 1,
                    "lag": lag + c["lag"],
                    "path": path + [dst],
                    "via": c,
                    "seed": dom,
                })
                if dst not in seen and depth + 1 < max_depth:
                    seen.add(dst)
                    nxt.append((dst, depth + 1, lag + c["lag"], path + [dst]))
        frontier = nxt
    # Nearest first, then strongest channel, so the short causal chain always
    # outranks the long one even when the long one has a higher cumulative prior.
    out.sort(key=lambda r: (r["depth"], -r["via"]["strength"], r["lag"]))
    return out


# --------------------------------------------------------------------------
# Entity aliases
# --------------------------------------------------------------------------
# Acronym -> the words it expands to. Entity matching compares tokens, so an
# institution named "IMF" in one document and "International Monetary Fund" in
# the next share nothing and go unlinked -- and in an economics corpus those
# abbreviations are most of the named actors. Deliberately short and explicit:
# a fuzzy or generated expansion table would start connecting documents that
# merely sound alike, which is the exact failure the entity rule prevents.
ENTITY_ALIASES: dict[str, str] = {
    "imf": "international monetary fund",
    "bis": "bank for international settlements",
    "oecd": "organisation for economic cooperation and development",
    "ecb": "european central bank",
    "fed": "federal reserve",
    "boe": "bank of england",
    "boj": "bank of japan",
    "pboc": "people's bank of china",
    "gdp": "gross domestic product",
    "cpi": "consumer price index",
    "ust": "us treasury",
    "treasury": "treasury department",
    "ec": "european commission",
    "ftc": "federal trade commission",
    "doj": "department of justice",
    "bls": "bureau of labor statistics",
    "cfpb": "consumer financial protection bureau",
    "gao": "government accountability office",
    "iosco": "international organisation of securities commissions",
    "fsb": "financial stability board",
    "basel": "basel committee on banking supervision",
    "ocpp": "office of crypto policy",
    "sec": "securities and exchange commission",
    # The energy and grid side. An AI-capex investigation lives or dies on
    # linking these, and they are named by acronym far more often than in full.
    "iea": "international energy agency",
    "eia": "energy information administration",
    "doe": "department of energy",
    "ferc": "federal energy regulatory commission",
    "nerc": "north american electric reliability corporation",
    "lbnl": "lawrence berkeley national laboratory",
    "opec": "organization of the petroleum exporting countries",
    "iso": "independent system operator",
    "nyiso": "new york independent system operator",
    "ercot": "electric reliability council of texas",
    "caem": "california independent system operator",
    "entsoe": "european network of transmission system operators for electricity",
}


def entity_tokens(value: str) -> set[str]:
    """Every token an entity name contributes, alias expansions included.

    Shared by the mapper's entity matcher, so "IMF" and "International
    Monetary Fund" reduce to the same token set and link.
    """
    import re as _re
    out = set()
    for part in _re.split(r"[^A-Za-z0-9]+", str(value or "")):
        if not part:
            continue
        low = part.lower()
        out.add(low)
        if low in ENTITY_ALIASES:
            out.update(ENTITY_ALIASES[low].split())
    return out


def domain_catalog() -> list[dict]:
    rows = []
    for did, d in DOMAINS.items():
        out_ch = channels_from(did)
        in_ch = channels_into(did)
        rows.append({
            "id": did,
            "label": d["label"],
            "short": d["short"],
            "blurb": d["blurb"],
            "color": d["color"],
            "hub": did in HUB_DOMAINS,
            "out_degree": len(out_ch),
            "in_degree": len(in_ch),
            "out_strength": round(sum(c["strength"] for c in out_ch), 2),
            "in_strength": round(sum(c["strength"] for c in in_ch), 2),
        })
    return rows
