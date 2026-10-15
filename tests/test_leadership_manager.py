"""Leadership + manager API: portfolio aggregations and fan-out."""
from fastapi.testclient import TestClient

import pytest

from app import llm
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _stub_llm(monkeypatch):
    """Manager parse tries LLM enrichment; tests must not touch the network."""
    monkeypatch.setattr(llm, "chat", lambda messages, **kw: "{}")
    monkeypatch.setattr(llm, "chat_json", lambda messages, **kw: {})
    from app import agent
    monkeypatch.setattr(agent, "_run_searches", lambda queries, seen: [])


def test_leadership_board_empty_but_honest(client):
    b = client.get("/api/leadership/board").json()
    assert "exposure" in b and "decisions" in b and "health" in b
    assert "integrity" in b and "alerts" in b and "graph" in b
    assert b["exposure"]["n_assessed"] == 0
    assert b["decisions"]["n_waiting"] == 0
    md = client.get("/api/leadership/board.md")
    assert md.status_code == 200
    assert "leadership board" in md.text.lower()


def test_leadership_reflects_an_assessment(client):
    inv = client.post("/api/investigations", json={
        "title": "t", "driver": "rate_decision"}).json()
    client.post(f"/api/investigations/{inv['id']}/artifacts", json={
        "title": "Fed holds", "domain": "monetary_policy",
        "direction": "down", "magnitude": 0.5, "horizon": "short"})
    client.post(f"/api/investigations/{inv['id']}/assess", json={})
    from app import jobqueue
    n = 0
    while jobqueue.run_once() and n < 20:
        n += 1
    b = client.get("/api/leadership/board").json()
    assert b["exposure"]["n_assessed"] == 1
    assert b["decisions"]["n_waiting"] == 1
    assert b["health"]["assessed"] == 1
    # Scoped board filters to the investigation.
    scoped = client.get(
        f"/api/leadership/board?investigation_id={inv['id']}").json()
    assert scoped["exposure"]["n_assessed"] == 1


def test_manager_parse_is_side_effect_free(client):
    before = client.get("/api/investigations").json()["investigations"]
    r = client.post("/api/manager/parse", json={
        "command": "run detailed investigations on rate decision and stablecoin float"}).json()
    assert r["ok"]
    assert any(t["driver"] == "rate_decision" for t in r["topics"])
    after = client.get("/api/investigations").json()["investigations"]
    assert len(before) == len(after), "parse must not create anything"


def test_manager_parse_empty_command(client):
    r = client.post("/api/manager/parse", json={"command": "hello world"}).json()
    assert not r["ok"]
    assert "reason" in r


def test_manager_run_and_compile(client):
    r = client.post("/api/manager/run", json={
        "command": "rate decision and stablecoin float"}).json()
    assert r["manager_run_id"]
    assert len(r["children"]) >= 1
    # Compile without draining: no AgentRun rows exist yet, so nothing is
    # pending and compile succeeds immediately. Draining here would run real
    # collections (network + gateway) and hang the suite — the collect path
    # is already covered end-to-end in test_api.py with stubs.
    c = client.post(f"/api/manager/runs/{r['manager_run_id']}/compile").json()
    assert c["status"] == "done"
    assert c["summary_investigation_id"]
    # Idempotent second compile.
    c2 = client.post(f"/api/manager/runs/{r['manager_run_id']}/compile").json()
    assert c2.get("idempotent") is True
    tl = client.get(f"/api/manager/runs/{r['manager_run_id']}").json()
    assert tl["ok"] and tl["lanes"]


def test_manager_run_rejects_unknown_plan(client):
    r = client.post("/api/manager/run", json={"command": "hello world"})
    assert r.status_code == 400