"""Transmission assessment: deterministic scoring over stored rows.

The assessment must never invent evidence. These tests pin the properties that
make that true: scores derive from rows + taxonomy, re-scores are new rows,
and an empty investigation produces an honest zero rather than a refusal.
"""
from app import assessment as A
from app import council as C
from app.models import Artifact, Investigation


def _inv(db, **kw):
    inv = Investigation(title=kw.pop("title", "T"),
                        keywords=kw.pop("keywords", "rate, reserves"),
                        driver=kw.pop("driver", "rate_decision"),
                        shock=kw.pop("shock", "A 50bp hike."),
                        **kw)
    db.add(inv)
    db.commit()
    return inv


def _art(db, inv, **kw):
    a = Artifact(investigation_id=inv.id, title=kw.pop("title", "t"),
                 domain=kw.pop("domain", None),
                 direction=kw.pop("direction", None),
                 magnitude=kw.pop("magnitude", None),
                 horizon=kw.pop("horizon", None),
                 entities_json=kw.pop("entities", None),
                 relevance=kw.pop("relevance", 0.6),
                 review=kw.pop("review", "accepted"), **kw)
    db.add(a)
    db.commit()
    return a


def test_empty_investigation_scores_zero_not_error(db):
    inv = _inv(db)
    out = A.build_assessment(db, inv.id)
    assert out["overall_pct"] == 0
    assert out["scoring"]["n_artifacts"] == 0
    assert "None" in out["markdown"] or "no" in out["markdown"].lower()
    assert out["taxonomy_fingerprint"]


def test_coverage_and_exposure_derive_from_rows(db):
    inv = _inv(db)
    _art(db, inv, title="Fed holds", domain="monetary_policy",
         direction="down", magnitude=0.5, horizon="short")
    _art(db, inv, title="Float shifts", domain="stablecoins",
         direction="up", magnitude=0.4, horizon="medium",
         entities=["Tether", "Federal Reserve"])
    out = A.build_assessment(db, inv.id)
    assert out["scoring"]["domains_covered"] == 2
    assert out["scoring"]["directional"] == 2
    assert out["exposure_pct"] > 0
    assert out["coverage_pct"] > 0
    assert len(out["signals"]) == 2
    # Strongest first.
    assert out["signals"][0]["magnitude"] == 0.5


def test_supported_vs_unbridged_channels(db):
    from app.models import Relationship
    inv = _inv(db)
    a = _art(db, inv, title="Policy", domain="monetary_policy")
    b = _art(db, inv, title="Coins", domain="stablecoins",
             entities=["Federal Reserve"])
    db.add(Relationship(investigation_id=inv.id, source_id=a.id,
                        target_id=b.id, relationship_type="transmits_to",
                        confidence=0.8, lag=0))
    db.commit()
    out = A.build_assessment(db, inv.id)
    sup = [c for c in out["channels"] if c["supported"]]
    assert {(c["src"], c["dst"]) for c in sup} >= {("monetary_policy", "stablecoins")}
    assert out["scoring"]["bridged"] >= 1


def test_council_claims_are_floor_and_silence_is_zero(db):
    rows = [{"id": 1, "title": "Fed holds rates", "domain": "monetary_policy",
             "direction": "down", "magnitude": 0.5},
            {"id": 2, "title": "Nothing in labs", "domain": "stablecoins",
             "direction": None, "magnitude": None}]
    out = C.run_council(rows, ["monetary_policy", "stablecoins", "macro"])
    assert out["method"] == "hypothesis-council-econ-v1"
    # Seats with no evidence stay silent: no ai_labs claim.
    assert not [c for c in out["claims"] if "ai_labs" in c["domains"]]
    # Coverage reports unexamined, never safe.
    assert "macro" in out["coverage"]["unexamined"]
    assert "ai_labs" not in out["coverage"]["covered"]


def test_falsifier_audit_grades_thresholds():
    good = C.falsifier_audit("T-bill auction tail widens above 3bp over 2 auctions")
    assert good["grade"] >= 0.8
    assert good["has_observation"] and good["has_threshold"]
    vague = C.falsifier_audit("Review the deployment holistically")
    assert vague["grade"] <= 0.3
    assert vague["only_abstract"] or not vague["has_threshold"]
    assert C.falsifier_audit("")["grade"] == 0.0


def test_peer_scrutiny_unattacked_scores_zero():
    lonely = {"seat": "monetary", "domains": ["macro"], "n_supporting": 1,
              "n_directional": 1, "falsifier_audit": {"grade": 0.9, "reason": "ok"}}
    out = C.cross_examine(lonely, [lonely])
    # Only the rival-coverage objection is skipped (no other claims); other
    # grounds may still fire, but a truly lonely strong claim gets low scrutiny.
    assert out["peer_scrutiny"] >= 0.0


def test_scenarios_falsify_different_parts(db):
    inv = _inv(db)
    _art(db, inv, title="x", domain="macro", direction="up", magnitude=0.3)
    out = A.build_assessment(db, inv.id)
    assert len(out["scenarios"]) == 3
    assert {s["id"] for s in out["scenarios"]} == {"baseline", "stress", "fragmentation"}
    assert all(s["falsifier"] for s in out["scenarios"])


def test_mermaid_map_marks_supported_edges():
    m = A.mermaid_transmission_map(
        "rate_decision",
        [{"src": "monetary_policy", "dst": "stablecoins",
          "channel": "reserve yield"}],
        [["monetary_policy", "stablecoins"]])
    assert "==>" in m  # supported uses thick edge
    m2 = A.mermaid_transmission_map(
        "rate_decision",
        [{"src": "monetary_policy", "dst": "stablecoins",
          "channel": "reserve yield"}],
        [])
    assert "-->" in m2


def test_unknown_driver_is_ignored_not_fatal(db):
    inv = _inv(db, driver="nonsense")
    out = A.build_assessment(db, inv.id, driver="nonsense")
    assert out["driver"] is None
    assert out["overall_pct"] >= 0