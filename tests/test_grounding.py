"""Grounding: the mechanism that stops a citation being invented.

An LLM answer is worth reading only if you can tell how much of it is
load-bearing. These tests pin the two properties that make that possible: a
quote must appear verbatim in the source it is attributed to, and a citation
marker that resolves to nothing is dropped and *counted* rather than kept.
"""
from app import grounding as G


def test_verbatim_quote_verifies():
    src = "The Committee decided to hold the policy rate at four and three quarters percent."
    assert G.verify_quote("hold the policy rate", src)["ok"]


def test_invented_quote_fails():
    src = "The Committee decided to hold the policy rate."
    out = G.verify_quote("cut the policy rate by 50 basis points", src)
    assert not out["ok"]
    assert out["reason"] == "not_found"


def test_short_quote_is_rejected_even_when_present():
    """A two-word quote matches almost any text, which is worse than no quote:
    it looks like verification while verifying nothing."""
    src = "Rates rose across the curve in a steepening move."
    out = G.verify_quote("rates rose", src)
    assert not out["ok"]
    assert out["reason"] == "quote_too_short"


def test_formatting_differences_still_verify():
    """A PDF extraction introduces curly quotes, nbsp and line breaks between
    the source and the model. Those must not fail the check."""
    src = "The Fund's assets under management reached $2.4 trillion.\nThird quarter."
    for variant in [
        "The Fund's assets under management reached $2.4 trillion",
        "The Fund’s assets under management reached $2.4 trillion",
        "The Fund's assets under management  reached   $2.4 trillion",
    ]:
        assert G.verify_quote(variant, src)["ok"], variant


def test_long_quote_spanning_a_sentence_boundary_verifies():
    src = ("Bond yields fell sharply after the announcement. "
           "Traders immediately priced a faster easing path.")
    out = G.verify_quote(
        "Bond yields fell sharply after the announcement. "
        "Traders immediately priced a faster easing path.", src)
    assert out["ok"]


def test_citation_markers_are_parsed():
    assert G.citation_markers("as shown [1], and again [2,3]") == [1, 2, 3]
    assert G.citation_markers("no markers here") == []
    assert G.strip_markers("as shown [1].") == "as shown ."


def test_unresolvable_citation_is_dropped_and_counted():
    """The count is the honest part. An answer that quietly loses three
    citations reads exactly like one that never had them."""
    sources = [{"index": 1, "title": "Real", "text": "the yield curve inverted"}]
    out = G.verify_answer(
        '"the yield curve inverted" [1] but not really [7] and not [9] either.',
        sources)
    assert out["dropped_citations"] == 2
    assert [c["index"] for c in out["citations"]] == [1]
    assert out["verdict"] == "supported"


def test_a_citation_with_no_quote_is_unverified_not_assumed_good():
    """No quoted text means nothing can be checked, so the citation cannot be
    counted as evidence -- it is dropped, and the verdict says so."""
    sources = [{"index": 1, "title": "Real", "text": "the yield curve inverted"}]
    out = G.verify_answer("The curve inverted [1] on Tuesday.", sources)
    assert out["dropped_citations"] == 1
    assert out["citations"] == []
    assert out["verdict"] == "unverified"


def test_citation_with_no_verifiable_quote_is_dropped():
    sources = [{"index": 1, "title": "S", "text": "the yield curve inverted"}]
    out = G.verify_answer("The deficit doubled [1].", sources)
    assert out["dropped_citations"] == 1
    assert out["verdict"] == "unverified"


def test_uncited_answer_is_labelled_not_scored_as_supported():
    out = G.verify_answer("A confident paragraph with no citations at all.",
                          [{"index": 1, "title": "S", "text": "x"}])
    assert out["verdict"] == "uncited"
    assert out["dropped_citations"] == 0


def test_confidence_follows_distinct_sources_not_quote_count():
    """Five quotes from one document must not read as five sources agreeing."""
    one = [{"index": 1, "title": "One", "text":
            "alpha beta gamma delta epsilon zeta"}]
    two = one + [{"index": 2, "title": "Two", "text":
                  "alpha beta gamma delta epsilon zeta"}]
    a = G.verify_answer('"alpha beta gamma" [1]', one)
    b = G.verify_answer('"alpha beta gamma" [1] and "zeta epsilon" [2]', two)
    # One citation, one verified quote -- but a second, *independent* source
    # moves the corroboration count, which is the only signal a model cannot
    # inflate by writing more.
    assert a["distinct_sources"] == 1
    assert b["distinct_sources"] == 2
    assert b["n_citations"] == 2


def test_relevance_gate_rejects_an_unrelated_image():
    ok = G.relevance_gate(
        "The central bank raised the policy rate by 25 basis points.",
        "A stock photograph of a mountain range at sunrise.")
    assert not ok["ok"]


def test_relevance_gate_accepts_a_relevant_image():
    ok = G.relevance_gate(
        "The central bank raised the policy rate by 25 basis points.",
        "Central bank headquarters, interest rate decision, monetary policy.")
    assert ok["ok"]


def test_summary_only_sources_are_reported_separately(db):
    """A citation that fails against a real article is a different finding
    from one that fails against a paraphrase. Averaging the two into a single
    drop count hides which one happened -- and a paraphrase drop is a data
    problem, not a hallucination."""
    from app.models import Artifact, Investigation
    from app import explainer as expl

    inv = Investigation(title="t", keywords="k")
    db.add(inv)
    db.commit()
    # Collected with a published snippet.
    db.add(Artifact(investigation_id=inv.id, title="Real source",
                    domain="macro", description="the analyst's summary",
                    source_text="the yield curve inverted", relevance=1.0))
    # Collected before the snippet was kept separately.
    db.add(Artifact(investigation_id=inv.id, title="Legacy source",
                    domain="macro", description="only a summary exists",
                    relevance=1.0))
    db.commit()

    rows = (db.query(Artifact)
            .filter(Artifact.investigation_id == inv.id).all())
    sources = expl._source_block(expl.rank_artifacts("inverted", rows))
    by_index = {s["index"]: s for s in sources}
    real = next(s for s in sources if s["title"] == "Real source")
    legacy = next(s for s in sources if s["title"] == "Legacy source")
    assert real["quote_verifiable"] is True
    assert legacy["quote_verifiable"] is False

    out = G.verify_answer('"the yield curve inverted" [%d] and a legacy quote [%d]'
                          % (real["index"], legacy["index"]), sources)
    assert out["n_verified"] == 1
    assert out["dropped_citations"] == 1
    assert out["dropped_against_summary_only"] == 1, \
        "the drop must be attributed to the paraphrase, not to the model"


def test_pii_scan_is_conservative():
    """Prefer missing to false alarms: a missed PII is a review item, a false
    alarm redacts a sentence of analysis and teaches people to ignore it."""
    clean = G.pii_scan("The Committee held the rate at 4.75 percent.")
    assert clean["clean"]
    dirty = G.pii_scan("Write to trader@example.com about the 123-45-6789 filing.")
    assert not dirty["clean"]
    assert "email" in dirty["kinds"]


def test_normalize_folds_the_differences_extraction_introduces():
    assert G.normalize("Rates  ROSE\nsharply") == G.normalize("rates rose sharply")