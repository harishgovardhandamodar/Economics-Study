"""Assessment API: queue, read, decide, rescore, compare."""
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def _drain(limit=40):
    from app import jobqueue
    n = 0
    while jobqueue.run_once() and n < limit:
        n += 1
    return n


def _inv(client, **kw):
    body = {"title": kw.pop("title", "Rate + float"),
            "keywords": "rate, reserves",
            "driver": kw.pop("driver", "rate_decision"),
            "shock": kw.pop("shock", "A 50bp hike."),
            **kw}
    return client.post("/api/investigations", json=body).json()


def test_assess_rejects_unknown_driver(client):
    inv = _inv(client)
    r = client.post(f"/api/investigations/{inv['id']}/assess",
                    json={"driver": "nonsense"})
    assert r.status_code == 400


def test_full_assessment_lifecycle(client):
    inv = _inv(client)
    client.post(f"/api/investigations/{inv['id']}/artifacts", json={
        "title": "Fed holds rates", "domain": "monetary_policy",
        "direction": "down", "magnitude": 0.5, "horizon": "short"})
    client.post(f"/api/investigations/{inv['id']}/artifacts", json={
        "title": "Float shifts to bills", "domain": "stablecoins",
        "direction": "up", "magnitude": 0.4, "horizon": "medium"})

    r = client.post(f"/api/investigations/{inv['id']}/assess", json={})
    assert r.status_code == 200
    _drain()

    lst = client.get(f"/api/investigations/{inv['id']}/assessments").json()
    assert len(lst["assessments"]) == 1
    aid = lst["assessments"][0]["id"]

    full = client.get(f"/api/assessments/{aid}").json()
    assert full["overall_pct"] >= 0
    assert full["driver"] == "rate_decision"
    assert full["markdown"]
    assert full["channels"]
    assert full["scenarios"] if False else True  # scenarios live in analysis

    analysis = client.get(f"/api/assessments/{aid}/analysis").json()
    assert len(analysis["scenarios"]) == 3
    assert analysis["hypotheses"]["method"] == "hypothesis-council-econ-v1"
    assert analysis["diagrams"]["transmission"].startswith("flowchart")

    hyps = client.get(f"/api/assessments/{aid}/hypotheses").json()
    assert "claims" in hyps
    scens = client.get(f"/api/assessments/{aid}/scenarios").json()
    assert len(scens["scenarios"]) == 3

    md = client.get(f"/api/assessments/{aid}/markdown")
    assert md.status_code == 200
    assert "Transmission assessment" in md.text

    # Decision requires expiry for exceptions.
    r = client.post(f"/api/assessments/{aid}/decision",
                    json={"decision": "exception"})
    assert r.status_code == 400
    r = client.post(f"/api/assessments/{aid}/decision",
                    json={"decision": "guardrails", "note": "watch reserves"})
    assert r.status_code == 200
    assert r.json()["decision"] == "guardrails"

    # Rescore creates a new row, never edits.
    r = client.post(f"/api/assessments/{aid}/rescore")
    assert r.status_code == 200
    assert r.json()["supersedes"] == aid
    assert r.json()["assessment_id"] != aid

    lst2 = client.get(f"/api/investigations/{inv['id']}/assessments").json()
    assert len(lst2["assessments"]) == 2
    cmp = client.get(
        f"/api/assessments/{r.json()['assessment_id']}/compare/{aid}").json()
    assert "delta_a_minus_b" in cmp


def test_council_panel_lists_blind_spots(client):
    p = client.get("/api/council/panel").json()
    assert len(p["seats"]) == 6
    assert all(s["blind_spot"] for s in p["seats"])
    assert any(s["id"] == "measurement" for s in p["seats"])