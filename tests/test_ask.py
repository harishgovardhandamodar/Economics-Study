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


def _upload(client, content, filename="takeout.json"):
    return client.post("/api/ask/import", files={"file": (filename, content)})


def test_import_takeout_search_json(client):
    import json as _json
    payload = _json.dumps([
        {"header": "Search", "title": "Searched for federal reserve rate decision",
         "titleUrl": "https://www.google.com/search?q=federal+reserve+rate+decision",
         "time": "2026-09-01T10:00:00Z"},
        {"header": "Search", "title": "Searched for stablecoin reserves",
         "titleUrl": "https://www.google.com/search?q=stablecoin+reserves",
         "time": "2026-09-02T10:00:00Z"},
        {"header": "Search", "title": "", "titleUrl": ""},
    ]).encode()
    r = _upload(client, payload).json()
    assert r["created"] == 2 and r["skipped"] == 1
    threads = client.get("/api/ask/threads").json()
    assert threads["count"] == 2
    first = [t for t in threads["threads"]
             if "federal reserve" in t["question"].lower()][0]
    assert first["imported"] is True
    assert "Monetary policy" in first["tags"] or first["tags"]
    assert first["group"], "every import lands in a group"
    # Re-importing the same file is a no-op for rows already here.
    r2 = _upload(client, payload).json()
    assert r2["created"] == 0 and r2["skipped"] == 3


def test_import_conversations_and_text_lines(client):
    import json as _json
    conv = _json.dumps({"conversations": [
        {"question": "Will tariffs raise CPI?",
         "answer": "Yes, via import prices over two quarters.",
         "asked_at": "2026-09-03"},
        {"query": {"query_text": "Suez canal freight rates"},
         "response": {"text": "Rates tripled after the closure."}},
    ]}).encode()
    r = _upload(client, conv).json()
    assert r["created"] == 2 and r["answered"] == 2
    lines = b"quantum computing outlook\n\ndatacenter power demand\n"
    r = _upload(client, lines, filename="queries.txt").json()
    assert r["created"] == 2
    # Groups reflect content: tariff question groups under a driver or domain.
    tariffs = [t for t in client.get("/api/ask/threads").json()["threads"]
               if "tariff" in t["question"].lower()][0]
    assert tariffs["group"] != "unclassified"
    suez = [t for t in client.get("/api/ask/threads").json()["threads"]
            if "suez" in t["question"].lower()][0]
    assert any("suez" in str(t).lower() or "canal" in str(t).lower()
               for t in suez["tags"])
    # Group filter narrows; unknown files are rejected honestly.
    g = tariffs["group"]
    assert all(t["group"] == g for t in
               client.get(f"/api/ask/threads?group={g}").json()["threads"])
    groups = client.get("/api/ask/threads").json()["groups"]
    assert any(x["group"] == g and x["count"] >= 1 for x in groups)
    bad = _upload(client, b"\xff\xfe not json {", filename="x.json").json()
    assert bad["created"] == 0
    assert client.post("/api/ask/import").status_code in (400, 422)


def test_imported_question_answers_in_place(client):
    import json as _json
    r = _upload(client, _json.dumps(
        [{"query": "How does the rate decision reach reserves?"}]).encode()).json()
    assert r["created"] == 1
    imported = [t for t in client.get("/api/ask/threads").json()["threads"]
                if t["imported"]][0]
    assert imported["status"] == "imported"
    assert imported["tags"] and imported["group"]
    # Answering fills the same row: no duplicate thread appears.
    client.post("/api/ask", json={"explanation_id": imported["id"]})
    _drain()
    assert client.get("/api/ask/threads").json()["count"] == 1
    full = client.get(f"/api/explanations/{imported['id']}").json()
    assert full["status"] == "done" and full["answer"]
    assert full["trace"]["ask"] is True
    # Tags and group survive answering: they describe the question, not the run.
    again = [t for t in client.get("/api/ask/threads").json()["threads"]
             if t["id"] == imported["id"]][0]
    assert again["tags"] == imported["tags"]
    assert client.post("/api/ask", json={
        "explanation_id": 999999}).status_code == 404


def test_thread_graph_names_messages_evidence_and_edges(client):
    inv = _seed_collected(client)
    root = _ask(client)
    full = client.get(f"/api/explanations/{root['id']}").json()
    assert full["trace"]["artifact_ids"], "the stub answer cites collected rows"
    g = client.get(f"/api/explanations/{root['id']}/graph").json()
    by_group: dict = {}
    for n in g["nodes"]:
        by_group.setdefault(n["group"], []).append(n["id"])
    assert by_group["message"] == [f"e:{root['id']}"]
    # Cited collected artifacts appear as domain-colored nodes.
    assert len(by_group.get("artifact", [])) == 2
    assert all(i.startswith("a:") for i in by_group["artifact"])
    # Live-web sources appear as their own nodes.
    assert len(by_group.get("web", [])) == 1
    assert by_group["web"][0].startswith("w:")
    labels = {e["label"] for e in g["edges"]}
    assert "cites" in labels
    assert g["counts"]["messages"] == 1
    assert g["counts"]["web"] == 1
    # Addressable from any member; unknown ids 404.
    assert client.get(
        f"/api/explanations/{root['id']}/graph").status_code == 200
    assert client.get("/api/explanations/999999/graph").status_code == 404


def test_thread_graph_draws_evidence_edges_and_branches(client):
    inv = _seed_collected(client)
    arts = client.get(
        f"/api/investigations/{inv['id']}/artifacts").json()["artifacts"]
    a, b = arts[0]["id"], arts[1]["id"]
    client.post("/api/relationships", json={
        "investigation_id": inv["id"], "source_id": a,
        "target_id": b, "relationship_type": "transmits_to"})
    root = _ask(client)
    client.post(f"/api/explanations/{root['id']}/followup",
                json={"question": "Branch A?"})
    client.post(f"/api/explanations/{root['id']}/followup",
                json={"question": "Branch B?"})
    _drain()
    g = client.get(f"/api/explanations/{root['id']}/graph").json()
    assert g["counts"]["messages"] == 3
    reply_tos = [e for e in g["edges"] if e["label"] == "reply_to"]
    assert len(reply_tos) == 2  # both branches hang off the root
    assert {e["from"] for e in reply_tos} == {f"e:{root['id']}"}
    # reply_to edges only ever join messages (artifact ids live under a:).
    assert all(e["from"].startswith("e:") and e["to"].startswith("e:")
               for e in reply_tos)
    assert any(e["label"] == "transmits_to" for e in g["edges"])


def test_thread_graph_without_citations_still_graphs(client):
    inv = client.post("/api/investigations", json={"title": "t"}).json()
    client.post(f"/api/investigations/{inv['id']}/explain",
                json={"question": "What about quantum?"})
    _drain()
    root = client.get(
        f"/api/investigations/{inv['id']}/explanations").json()["explanations"][0]
    g = client.get(f"/api/explanations/{root['id']}/graph").json()
    assert g["counts"] == {"messages": 1, "artifacts": 0, "web": 0}
    assert g["edges"] == []