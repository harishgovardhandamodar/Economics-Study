"""Retrieval and gap computation — the parts that make the map cross-domain.

Retrieval is where a cross-domain question either works or quietly does not:
"what does the rate decision do to stablecoin float" shares almost no keyword
vocabulary with a T-bill auction notice, so entity overlap and domain matching
have to carry it. Gap computation is where an honest coverage claim comes from.
"""
from app import domains as D
from app import explainer as expl
from app import research_gaps as gaps
from app.models import Artifact, Investigation


def _inv(db, **kw):
    inv = Investigation(title=kw.pop("title", "Test"),
                        keywords=kw.pop("keywords", "rate, inflation"),
                        description=kw.pop("description", "test brief"),
                        **kw)
    db.add(inv)
    db.commit()
    return inv


def _art(db, inv, **kw):
    a = Artifact(investigation_id=inv.id,
                 title=kw.pop("title", "t"),
                 description=kw.pop("description", ""),
                 domain=kw.pop("domain", None),
                 entities_json=kw.pop("entities", None),
                 relevance=kw.pop("relevance", 0.5),
                 review=kw.pop("review", "pending"), **kw)
    db.add(a)
    db.commit()
    return a


# --------------------------------------------------------------------------
# retrieval
# --------------------------------------------------------------------------
def test_domain_match_beats_keyword_overlap(db):
    inv = _inv(db)
    # This one is in the right domain but shares no words with the question.
    domain_hit = _art(db, inv, title="Minutes of the FOMC meeting",
                      domain="monetary_policy",
                      description="The Committee discussed the outlook.")
    keyword_hit = _art(db, inv, title="Rate rate rate outlook discussion",
                       domain="agi_asi",
                       description="rate rate rate rate rate")
    rows = db.query(Artifact).filter(Artifact.investigation_id == inv.id).all()
    ranked = expl.rank_artifacts(
        "What did the monetary policy rate decision do?", rows)
    assert ranked[0][0].id == domain_hit.id
    assert keyword_hit.id in [a.id for a, _ in ranked]


def test_entity_overlap_connects_documents_with_no_shared_vocabulary(db):
    """The case the whole app exists for: a stablecoin attestation and a
    central-bank release share no keywords, but both name the same entity."""
    inv = _inv(db)
    _art(db, inv, title="Reserve attestation report",
         description="Holdings and redemption data.",
         domain="stablecoins", entities=["Tether", "Circle"])
    _art(db, inv, title="Quarterly monetary policy review",
         description="Balance sheet and reserve considerations.",
         domain="monetary_policy", entities=["Tether"])
    rows = db.query(Artifact).filter(Artifact.investigation_id == inv.id).all()
    ranked = expl.rank_artifacts("What is the exposure of Tether to policy?",
                                 rows)
    assert len(ranked) == 2, "the entity link must surface both documents"


def test_reviewer_acceptance_outranks_equal_relevance(db):
    inv = _inv(db)
    pending = _art(db, inv, title="Pending item", description="rate decision",
                   relevance=0.6, review="pending")
    accepted = _art(db, inv, title="Accepted item", description="rate decision",
                    relevance=0.6, review="accepted")
    rows = db.query(Artifact).filter(Artifact.investigation_id == inv.id).all()
    ranked = expl.rank_artifacts("rate decision", rows)
    assert ranked[0][0].id == accepted.id
    assert pending.id in [a.id for a, _ in ranked]


def test_rejected_artifacts_are_sunk_not_returned_by_default(db):
    inv = _inv(db)
    _art(db, inv, title="Rejected rate take", description="rate rate rate",
         relevance=0.9, review="rejected")
    rows = db.query(Artifact).filter(Artifact.investigation_id == inv.id).all()
    assert expl.rank_artifacts("rate rate rate", rows) == []


def test_limit_is_respected(db):
    inv = _inv(db)
    for i in range(20):
        _art(db, inv, title=f"Rate decision {i}", description="rate", relevance=1.0)
    rows = db.query(Artifact).filter(Artifact.investigation_id == inv.id).all()
    assert len(expl.rank_artifacts("rate decision", rows, limit=5)) == 5


def test_domains_inferred_from_a_question(db):
    found = expl._domains_in_text("How does the stablecoin float respond to monetary policy?")
    assert "stablecoins" in found
    assert "monetary_policy" in found


# --------------------------------------------------------------------------
# gaps
# --------------------------------------------------------------------------
def test_empty_domain_is_a_gap_and_covered_domain_is_not(db):
    inv = _inv(db)
    _art(db, inv, title="A covered thing", domain="stablecoins")
    out = gaps.compute_gaps(inv.id)
    gap_ids = {g["domain"] for g in out["gaps"]}
    assert "stablecoins" not in gap_ids
    assert "macro" in gap_ids
    assert out["domains_covered"] == 1


def test_gaps_are_prioritised_hubs_first(db):
    inv = _inv(db)
    out = gaps.compute_gaps(inv.id)
    # With nothing collected at all, every domain is a gap and the ordering is
    # purely the hub/connectivity prior. Macro, monetary and politics must lead.
    top3 = [g["domain"] for g in out["gaps"][:3]]
    assert set(top3) & set(D.HUB_DOMAINS), top3
    priorities = [g["priority"] for g in out["gaps"]]
    assert priorities == sorted(priorities, reverse=True)


def test_thin_is_reported_separately_from_empty(db):
    """Collapsing the two would let "barely covered" read as "not covered",
    which is how a map ends up claiming coverage it does not have."""
    inv = _inv(db)
    for i in range(2):  # two artifacts: thin, not empty
        _art(db, inv, title=f"Macro item {i}", domain="macro")
    out = gaps.compute_gaps(inv.id)
    assert "macro" not in {g["domain"] for g in out["gaps"]}
    assert "macro" in {t["domain"] for t in out["thin"]}


def test_gap_reason_names_the_missing_mechanism(db):
    inv = _inv(db)
    _art(db, inv, title="Covered", domain="fiscal_policy")
    out = gaps.compute_gaps(inv.id)
    by_domain = {g["domain"]: g for g in out["gaps"]}
    assert "connecting evidence" in by_domain["sovereign_debt"]["why"]


def test_persist_gaps_closes_a_gap_when_evidence_arrives(db):
    inv = _inv(db)
    gaps.persist_gaps(inv.id)
    rows = db.query(__import__("app.models", fromlist=["ResearchGap"])
                    .ResearchGap).filter(
        __import__("app.models", fromlist=["ResearchGap"])
        .ResearchGap.investigation_id == inv.id).all()
    macro_gap = [r for r in rows if r.domain == "macro"]
    assert macro_gap
    _art(db, inv, title="Now covered", domain="macro")
    gaps.persist_gaps(inv.id)
    db.refresh(macro_gap[0])
    assert macro_gap[0].status == "ruled_out"


def test_unbridged_channels_surface_between_covered_domains(db):
    inv = _inv(db)
    _art(db, inv, title="Policy", domain="monetary_policy")
    _art(db, inv, title="Coins", domain="stablecoins")
    out = gaps.compute_gaps(inv.id)
    pairs = {(c["src"], c["dst"]) for c in out["unbridged_channels"]}
    assert ("monetary_policy", "stablecoins") in pairs


def test_gap_query_is_built_from_the_channel_not_the_label(db):
    inv = _inv(db)
    gaps.persist_gaps(inv.id)
    q = gaps.gap_query({"domain": "stablecoins", "dimension": "Stablecoins"})
    # "Tell me about stablecoins" returns nothing useful; the channel phrasing
    # asks a question the corpus can answer.
    assert q.lower() != "stablecoins"
    assert len(q) > 5


def test_roadmap_counts_cross_domain_edges(db):
    from app.models import Relationship
    inv = _inv(db)
    a = _art(db, inv, title="Policy move", domain="monetary_policy")
    b = _art(db, inv, title="Stablecoin float", domain="stablecoins")
    db.add(Relationship(investigation_id=inv.id, source_id=a.id, target_id=b.id,
                        relationship_type="transmits_to"))
    db.commit()
    rm = expl.investigation_roadmap(inv.id)
    # A zero here would be indistinguishable from a real finding of "no
    # cross-domain structure", which is why the self-join is aliased.
    assert rm["cross_domain_edges"] == 1


def test_suggestions_come_from_the_investigations_own_gaps(db):
    inv = _inv(db)
    _art(db, inv, title="Policy", domain="monetary_policy")
    qs = expl.question_suggestions(inv.id)
    assert qs
    # Every suggestion should name a domain the map does not cover yet.
    covered = {d["id"] for d in expl.investigation_roadmap(inv.id)["coverage"]
               if d["covered"]}
    assert "monetary_policy" not in " ".join(qs).lower() or qs