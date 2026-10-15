"""Ask tab: estate-wide questions, threads that stay threaded, promote, FTS."""
import json

import pytest
from fastapi.testclient import TestClient

from app import llm
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def _answer_stub(messages, **kw):
    sys_p = ""
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "system":
            sys_p = str(m.get("content", ""))
            break
    if "hostile reviewer" in sys_p:
        return "## Supported\n- held [1]\n\n## Uncertain\n- None"
    if "macro-financial analyst" in sys_p:
        return ('"The Committee held the policy rate" [1]. '
                '"Holdings shifted toward bills" [2] follows.')
    if "comprehension checks" in sys_p.lower():
        return '{"quiz": []}'
    return "{}"


def _answer_stub_json(messages, **kw):
    try:
        return json.loads(_answer_stub(messages, **kw))
    except Exception:
        return {}


@pytest.fixture(autouse=True)
def _stub(monkeypatch):
    monkeypatch.setattr(llm, "chat", _answer_stub)
    monkeypatch.setattr(llm, "chat_json", _answer_stub_json)
    from app import search as providers

    def fake_web(query, max_results=10):
        return [{
            "title": "Live: bills market steady",
            "url": "https://example.org/live",
            "description": "Holdings shifted toward bills as dealers stepped back.",
            "content": "", "source": "example.org", "author": "",
            "date_published": None, "artifact_type": "news",
            "query": query, "primary": False}]

    monkeypatch.setattr(providers, "search_web", fake_web)
    from app import agent

    def fake_searches(queries, seen_urls):
        return []

    monkeypatch.setattr(agent, "_run_searches", fake_searches)


def _drain(limit=40):
    from app import jobqueue
    n = 0
    while jobqueue.run_once() and n < limit:
        n += 1
    return n


def _seed_collected(client):
    inv = client.post("/api/investigations", json={
        "title": "Rates", "keywords": "rate"}).json()
    client.post(f"/api/investigations/{inv['id']}/artifacts", json={
        "title": "Fed holds rates",
        "description": "The Committee held the policy rate at its level.",
        "domain": "monetary_policy"})
    client.post(f"/api/investigations/{inv['id']}/artifacts", json={
        "title": "Issuer reserves",
        "description": "Holdings shifted toward bills as dealers stepped back.",
        "domain": "stablecoins"})
    return inv


def _ask(client, **kw):
    body = {"question": "How does the rate decision reach reserves?",
            "scope": "all", **kw}
    r = client.post("/api/ask", json=body)
    assert r.status_code == 200, r.text
    _drain()
    exps = client.get("/api/ask/threads").json()["threads"]
    assert exps, "ask produced no thread"
    return exps[0]


def test_ask_answers_from_collected_plus_web(client):
    _seed_collected(client)
    e = _ask(client)
    assert e["status"] == "done"
    full = client.get(f"/api/explanations/{e['id']}").json()
    assert full["answer"]
    assert full["dropped_citations"] == 0
    srcs = full["sources"]
    origins = {s.get("origin") for s in srcs}
    assert "collected" in origins and "web" in origins
    assert full["trace"]["ask"] is True
    assert full["trace"]["scope"] == "all"
    assert "flowchart" in full["trace"]["diagram"]
    assert "monetary_policy" in full["trace"]["diagram"]


def test_ask_scoped_to_one_investigation(client):
    inv = _seed_collected(client)
    other = client.post("/api/investigations", json={"title": "Other"}).json()
    client.post(f"/api/investigations/{other['id']}/artifacts", json={
        "title": "Unrelated chip story", "domain": "datacenters"})
    e = _ask(client, scope=f"inv:{inv['id']}")
    full = client.get(f"/api/explanations/{e['id']}").json()
    assert full["trace"]["scope"] == f"inv:{inv['id']}"
    assert {s.get("origin") for s in full["sources"]} >= {"collected"}
    assert client.post("/api/ask", json={
        "question": "q", "scope": "inv:999999"}).status_code in (400, 404)
    assert client.post("/api/ask", json={
        "question": "q", "scope": "bogus"}).status_code == 400
    assert client.post("/api/ask", json={
        "question": " ", "scope": "all"}).status_code == 400


def test_ask_without_web(client):
    _seed_collected(client)
    e = _ask(client, include_web=False)
    full = client.get(f"/api/explanations/{e['id']}").json()
    assert full["trace"]["web"] == 0
    assert {s.get("origin") for s in full["sources"]} == {"collected"}


def test_followup_stays_threaded_through_queue(client):
    """The bug this pins: queued follow-ups used to land as roots."""
    _seed_collected(client)
    root = _ask(client)
    r = client.post(f"/api/explanations/{root['id']}/followup",
                    json={"question": "And the horizon?"})
    assert r.status_code == 200
    _drain()
    kids = client.get("/api/ask/threads").json()["threads"]
    assert len(kids) == 1, "follow-up must extend the thread, not start one"
    tree = client.get(f"/api/explanations/{root['id']}/tree").json()
    assert tree["thread_id"] == root["id"]
    assert len(tree["tree"]["children"]) == 1
    child = tree["tree"]["children"][0]
    assert child["question"] == "And the horizon?"
    # Addressable from the child too: a tree is addressed by any member.
    same = client.get(f"/api/explanations/{child['id']}/tree").json()
    assert same["thread_id"] == root["id"]


def test_branching_forks_not_flattens(client):
    _seed_collected(client)
    root = _ask(client)
    for q in ("Branch A?", "Branch B?"):
        client.post(f"/api/explanations/{root['id']}/followup",
                    json={"question": q})
    _drain()
    tree = client.get(f"/api/explanations/{root['id']}/tree").json()
    assert len(tree["tree"]["children"]) == 2
    assert {c["question"] for c in tree["tree"]["children"]} == {
        "Branch A?", "Branch B?"}
    # A reply to a branch nests under it, not beside it.
    leaf_a = [c for c in tree["tree"]["children"]
              if c["question"] == "Branch A?"][0]
    client.post(f"/api/explanations/{leaf_a['id']}/followup",
                json={"question": "Deeper on A?"})
    _drain()
    tree = client.get(f"/api/explanations/{root['id']}/tree").json()
    a2 = [c for c in tree["tree"]["children"]
          if c["question"] == "Branch A?"][0]
    assert len(a2["children"]) == 1
    assert a2["children"][0]["question"] == "Deeper on A?"


def test_promote_copies_thread_into_investigation(client):
    _seed_collected(client)
    root = _ask(client)
    r = client.post("/api/ask/promote",
                    json={"explanation_id": root["id"]}).json()
    assert r["investigation_id"]
    assert r["collect_queued"] is True
    inv = client.get(
        f"/api/investigations/{r['investigation_id']}").json()
    assert inv["title"].startswith("Ask:")
    arts = client.get(
        f"/api/investigations/{r['investigation_id']}/artifacts").json()
    assert arts["count"] == 1  # the answer as first artifact
    assert arts["artifacts"][0]["artifact_type"] == "projection"
    # The thread still lives in the archive: promotion copies, never moves.
    assert client.get(f"/api/explanations/{root['id']}").status_code == 200
    trace = client.get(f"/api/explanations/{root['id']}").json()["trace"]
    assert trace["promoted_to"] == r["investigation_id"]
    assert client.post("/api/ask/promote",
                       json={"explanation_id": 999999}).status_code == 404


def test_threads_list_shape(client):
    _seed_collected(client)
    root = _ask(client)
    lst = client.get("/api/ask/threads").json()
    assert lst["count"] == 1
    assert lst["threads"][0]["replies"] == 0
    client.post(f"/api/explanations/{root['id']}/followup",
                json={"question": "More?"})
    _drain()
    assert client.get("/api/ask/threads").json()["threads"][0]["replies"] == 1


def test_fts_finds_evidence_and_answers(client):
    _seed_collected(client)
    client.post("/api/search/reindex")
    r = client.get("/api/search/all?q=stablecoin+reserves").json()
    assert r["ok"] and r["results"]
    assert any("<mark>" in hit["snippet"] for hit in r["results"])
    # Type filter narrows to answers only once one exists.
    _ask(client)
    client.post("/api/search/reindex")
    only = client.get(
        "/api/search/all?q=reserves&doc_type=explanation").json()
    assert only["results"]
    assert {h["doc_type"] for h in only["results"]} == {"explanation"}
    assert client.get("/api/search/all?q=").status_code == 400
    assert client.get(
        "/api/search/all?q=x&doc_type=bogus").status_code == 400


def test_fts_ranks_title_match_first(client):
    inv = _seed_collected(client)
    client.post(f"/api/investigations/{inv['id']}/artifacts", json={
        "title": "Unrelated filler about nothing in particular",
        "description": "zzzzword appears once here and nowhere else",
        "domain": "macro"})
    client.post(f"/api/investigations/{inv['id']}/artifacts", json={
        "title": "zzzzword in the title",
        "description": "nothing else relevant",
        "domain": "macro"})
    client.post("/api/search/reindex")
    r = client.get("/api/search/all?q=zzzzword").json()
    assert r["results"][0]["title"] == "zzzzword in the title"


def test_fts_state_and_reindex_counts(client):
    _seed_collected(client)
    out = client.post("/api/search/reindex").json()
    assert out["ok"] and out["total"] >= 3
    st = client.get("/api/search/index-state").json()
    assert st["indexed"]["artifact"] >= 2
    assert st["fts"] is True


def test_agents_ask_intent_queues(client):
    r = client.post("/api/agents/invoke", json={
        "to": "ask", "intent": "ask.answer",
        "payload": {"question": "What transmits?"}}).json()
    assert r["ok"] and r["result"]["job_id"]
    r = client.post("/api/agents/invoke", json={
        "to": "ask", "intent": "ask.answer", "payload": {}})
    assert r.status_code == 400