"""Query yield: what each *shape* of query has actually returned.

The planner works from a blank slate every run, so without this ledger a shape
that returned nothing last time is proposed first again, and the app spends its
whole budget re-asking a question that already has the answer "nothing".

These tests are written against a real database rather than a mock because the
failure this guards against was a column that did not exist: the write raised,
the caller logged "yield not recorded" to the event stream, and collection
carried on perfectly well while silently learning nothing.
"""
from app.models import Investigation
from app import yield_ as yld


def _inv(db):
    inv = Investigation(title="t", keywords="rate, inflation")
    db.add(inv)
    db.commit()
    return inv


def test_shape_is_word_order_independent(db):
    """A shape is a *question*; word order is not part of what makes two
    queries the same question."""
    a = {"text": "rate decision federal reserve", "sources": ["web"]}
    b = {"text": "federal reserve decision rate", "sources": ["web"]}
    assert yld.shape_of(a) == yld.shape_of(b)


def test_shape_key_includes_the_source_mix(db):
    """The same words asked against RSS and against the web are two different
    questions with two different histories."""
    a = {"text": "rate decision", "sources": ["web"]}
    b = {"text": "rate decision", "sources": ["rss"]}
    assert yld.shape_key(a) != yld.shape_key(b)


def test_record_yield_persists_a_row(db):
    inv = _inv(db)
    q = [{"text": "federal reserve rate decision", "sources": ["web"]}]
    yld.record_yield(db, inv.id, q, found=10, kept=4, llm_calls=2)
    shapes = yld.shape_stats(db, inv.id)
    assert len(shapes) == 1
    assert shapes[0]["times_used"] == 1
    assert shapes[0]["kept"] == 4.0


def test_record_yield_accumulates_across_runs(db):
    inv = _inv(db)
    q = [{"text": "rate decision", "sources": ["web"]}]
    for _ in range(3):
        yld.record_yield(db, inv.id, q, found=6, kept=2, llm_calls=1)
    s = yld.shape_stats(db, inv.id)[0]
    assert s["times_used"] == 3
    assert s["kept"] == 6.0
    assert s["created_at"] is not None


def test_a_round_that_kept_nothing_is_still_recorded(db):
    """The most useful thing to remember, and the round that used to leave no
    trace at all."""
    inv = _inv(db)
    q = [{"text": "obscure query", "sources": ["web"]}]
    yld.record_yield(db, inv.id, q, found=12, kept=0, llm_calls=3)
    assert yld.useless_shapes(db, inv.id, min_uses=1)
    stats = yld.shape_stats(db, inv.id)[0]
    assert stats["times_used"] == 1
    assert stats["found"] > 0 and stats["kept"] == 0


def test_a_proven_shape_outranks_an_untested_one(db):
    inv = _inv(db)
    proven = {"text": "rate decision auction", "sources": ["web"]}
    fresh = {"text": "quantum something", "sources": ["web"]}
    yld.record_yield(db, inv.id, [proven], found=8, kept=6, llm_calls=1)
    ranked = yld.rank_queries(db, inv.id, [fresh, proven])
    assert ranked[0]["text"] == proven["text"]


def test_a_proven_but_empty_shape_is_hard_demoted(db):
    """Scored lower is not enough: a shape that has been tried twice and kept
    nothing must not still outrank a genuinely unknown one, or the app keeps
    re-asking it forever."""
    inv = _inv(db)
    dead = {"text": "never returns anything", "sources": ["web"]}
    fresh = {"text": "something unexplored", "sources": ["web"]}
    for _ in range(2):
        yld.record_yield(db, inv.id, [dead], found=5, kept=0, llm_calls=2)
    ranked = yld.rank_queries(db, inv.id, [dead, fresh])
    assert ranked[0]["text"] == fresh["text"]


def test_ranking_drops_nothing(db):
    """Re-ordering is the yield ledger's whole power. Silently discarding a
    query the planner asked for would leave it with no way to learn that the
    query was a good one."""
    inv = _inv(db)
    q = [{"text": f"query number {i}", "sources": ["web"]} for i in range(5)]
    ranked = yld.rank_queries(db, inv.id, q)
    assert len(ranked) == len(q)
    assert {x["text"] for x in ranked} == {x["text"] for x in q}


def test_ranking_costs_per_kept_item_favours_efficient_shapes(db):
    """A shape that burns four model calls for one artifact is worse than one
    that gets three for the same spend, at identical keep *rate*."""
    inv = _inv(db)
    wasteful = {"text": "wasteful shape", "sources": ["web"]}
    efficient = {"text": "efficient shape", "sources": ["web"]}
    yld.record_yield(db, inv.id, [wasteful], found=4, kept=1, llm_calls=4)
    yld.record_yield(db, inv.id, [efficient], found=4, kept=1, llm_calls=1)
    ranked = yld.rank_queries(db, inv.id, [wasteful, efficient])
    assert ranked[0]["text"] == efficient["text"]


def test_shapes_are_scoped_to_one_investigation(db):
    a, b = _inv(db), _inv(db)
    q = [{"text": "rate decision", "sources": ["web"]}]
    yld.record_yield(db, a.id, q, found=5, kept=3, llm_calls=1)
    assert yld.shape_stats(db, a.id)
    assert yld.shape_stats(db, b.id) == [], "yield must not leak across briefs"


def test_investigation_yield_summary(db):
    inv = _inv(db)
    q = [{"text": "rate decision", "sources": ["web"]}]
    yld.record_yield(db, inv.id, q, found=9, kept=5, llm_calls=2)
    out = yld.investigation_yield(db, inv.id)
    assert out["total_kept"] == 5.0
    # "kept per use" is not a rate and routinely exceeds 1.0 -- one
    # well-shaped query can yield six artifacts.
    assert out["best"]["kept_per_use"] == 5.0
    assert out["best"]["hit"] is True
    assert out["investigation_id"] == inv.id


def test_recording_nothing_is_a_no_op_not_a_crash(db):
    inv = _inv(db)
    yld.record_yield(db, inv.id, [], found=3, kept=1, llm_calls=1)
    assert yld.shape_stats(db, inv.id) == []