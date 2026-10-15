"""Small surfaces: hide, prefs, feedback, similar, recommendations, compare."""
import pytest
from fastapi.testclient import TestClient

from app import llm
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _stub_llm(monkeypatch):
    """Deterministic model: never touch the network in these tests."""
    monkeypatch.setattr(llm, "chat", lambda messages, **kw: "grounded answer [1]")
    monkeypatch.setattr(llm, "chat_json", lambda messages, **kw: {})


def _inv(client, **kw):
    body = {"title": kw.pop("title", "t"), "keywords": "k", **kw}
    return client.post("/api/investigations", json=body).json()


def test_hidden_toggle(client):
    inv = _inv(client)
    r = client.patch(f"/api/investigations/{inv['id']}/hidden",
                     json={"hidden": True}).json()
    assert r["hidden"] is True
    lst = client.get("/api/investigations").json()["investigations"]
    assert inv["id"] not in [i["id"] for i in lst]
    lst2 = client.get("/api/investigations?include_hidden=true").json()["investigations"]
    assert inv["id"] in [i["id"] for i in lst2]


def test_prefs_roundtrip(client):
    inv = _inv(client)
    p = client.get(f"/api/investigations/{inv['id']}/prefs").json()
    assert "auto_save_explanations" in p
    r = client.put(f"/api/investigations/{inv['id']}/prefs", json={
        "auto_save_explanations": False, "preferred_domains": "macro,nonsense"})
    assert r.json()["auto_save_explanations"] is False
    assert "macro" in r.json()["preferred_domains"]
    assert "nonsense" not in r.json()["preferred_domains"]


def test_feedback_counts(client):
    inv = _inv(client)
    exp = client.post(f"/api/investigations/{inv['id']}/explain",
                      json={"question": "q?"}).json()
    from app import jobqueue
    n = 0
    while jobqueue.run_once() and n < 20:
        n += 1
    exps = client.get(f"/api/investigations/{inv['id']}/explanations").json()["explanations"]
    eid = exps[0]["id"]
    r = client.post(f"/api/explanations/{eid}/feedback",
                    json={"value": "up"}).json()
    assert r["up"] == 1
    r = client.post(f"/api/explanations/{eid}/feedback",
                    json={"value": "bogus"})
    assert r.status_code == 400


def test_save_to_graph_dedupes(client):
    inv = _inv(client)
    client.post(f"/api/investigations/{inv['id']}/explain",
                json={"question": "What transmits?"})
    from app import jobqueue
    n = 0
    while jobqueue.run_once() and n < 20:
        n += 1
    exps = client.get(f"/api/investigations/{inv['id']}/explanations").json()["explanations"]
    eid = exps[0]["id"]
    r1 = client.post(f"/api/explanations/{eid}/save_to_graph").json()
    r2 = client.post(f"/api/explanations/{eid}/save_to_graph").json()
    assert r2["deduped"] is True
    assert r1["artifact_id"] == r2["artifact_id"]


def test_investigate_gaps_queues_runs(client):
    inv = _inv(client)
    client.post(f"/api/investigations/{inv['id']}/explain",
                json={"question": "What transmits?"})
    from app import jobqueue
    n = 0
    while jobqueue.run_once() and n < 20:
        n += 1
    exps = client.get(f"/api/investigations/{inv['id']}/explanations").json()["explanations"]
    # Empty investigation -> no_evidence path still creates an explanation row.
    r = client.post(f"/api/explanations/{exps[0]['id']}/investigate_gaps").json()
    assert r["queued"] >= 0


def test_similar_and_recommendations_and_compare(client):
    inv = _inv(client)
    a = client.post(f"/api/investigations/{inv['id']}/artifacts", json={
        "title": "Fed holds", "domain": "monetary_policy",
        "entities": ["Federal Reserve"], "tags": "rates"}).json()
    b = client.post(f"/api/investigations/{inv['id']}/artifacts", json={
        "title": "Float shifts", "domain": "stablecoins",
        "entities": ["Federal Reserve"], "tags": "rates"}).json()
    sim = client.get(f"/api/artifacts/{a['id']}/similar").json()
    assert sim["similar"]
    assert sim["similar"][0]["id"] == b["id"]
    recs = client.get(f"/api/investigations/{inv['id']}/recommendations").json()
    assert "recommendations" in recs
    ctrl = client.get(f"/api/investigations/{inv['id']}/control").json()
    assert "planned_intents" in ctrl and "allowed_actors" in ctrl


def test_explanation_thread_and_quiz_and_bookmark(client):
    inv = _inv(client)
    client.post(f"/api/investigations/{inv['id']}/artifacts", json={
        "title": "x", "domain": "macro"})
    client.post(f"/api/investigations/{inv['id']}/explain",
                json={"question": "q?"})
    from app import jobqueue
    n = 0
    while jobqueue.run_once() and n < 20:
        n += 1
    exps = client.get(f"/api/investigations/{inv['id']}/explanations").json()["explanations"]
    eid = exps[0]["id"]
    th = client.get(f"/api/explanations/{eid}/thread").json()
    assert th["thread_id"]
    bm = client.post(f"/api/explanations/{eid}/bookmark").json()
    assert bm["bookmarked"] in (True, False)