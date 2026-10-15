"""End-to-end through the API, with the model stubbed.

The point of stubbing rather than mocking every function is that everything
*else* runs for real: the loop, the taxonomy, the graph, grounding, the ledger.
A test that mocks the mapper too would only prove the mocks agree with each
other.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app import llm
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


# --------------------------------------------------------------------------
# A scripted model
# --------------------------------------------------------------------------
PLAN = {
    "rationale": "Probe the policy transmission and the reserve channel.",
    "queries": [
        {"text": "federal reserve rate decision", "sources": ["web"],
         "domain": "monetary_policy"},
        {"text": "stablecoin reserve bill holdings", "sources": ["web"],
         "domain": "stablecoins"},
    ],
}

ANALYSIS = [
    {"index": 0, "relevance": 0.9, "keep": True,
     "reason": "Primary release of the policy decision.",
     "artifact_type": "policy_doc", "domain": "monetary_policy",
     "entities": ["Federal Reserve", "FOMC"], "tags": ["rates", "fomc"],
     "sentiment": 0.1, "summary": "The Committee held the policy rate.",
     "direction": "down", "magnitude": 0.5, "horizon": "short",
     "relates_to": [], "drift": False},
    {"index": 1, "relevance": 0.8, "keep": True,
     "reason": "Reserve composition changed with the policy rate.",
     "artifact_type": "report", "domain": "stablecoins",
     "entities": ["Tether", "Federal Reserve"], "tags": ["reserves"],
     "sentiment": 0.0, "summary": "Issuer holdings shifted toward bills.",
     "direction": "up", "magnitude": 0.4, "horizon": "medium",
     "relates_to": [{"id": 1, "relationship_type": "transmits_to",
                     "description": "reserve yield"}],
     "drift": False},
]


def _fake_chat(messages, **kw):
    """Route on the system prompt so each stage gets its own scripted answer."""
    sys_p = ""
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "system":
            sys_p = str(m.get("content", ""))
            break
    if "search planner" in sys_p:
        return json.dumps(PLAN)
    if "research analyst" in sys_p:
        return json.dumps(ANALYSIS)
    if "hostile reviewer" in sys_p:
        return "## Supported\n- The rate was held [1]\n\n## Uncertain\n- None"
    if "macro-financial analyst" in sys_p:
        # Both citations carry a quote that appears verbatim in the cited
        # artifact, so the grounding check has something real to verify.
        return ('"The Committee held the policy rate" [1]. '
                '"Holdings shifted toward bills" [2] follows from the reserve '
                'yield.')
    return "{}"


def _fake_chat_json(messages, **kw):
    text = _fake_chat(messages, **kw)
    try:
        return json.loads(text)
    except Exception:
        return {}


@pytest.fixture(autouse=True)
def _stub_llm(monkeypatch):
    monkeypatch.setattr(llm, "chat", _fake_chat)
    monkeypatch.setattr(llm, "chat_json", _fake_chat_json)


class _FakeCandidate(dict):
    pass


def _candidates():
    # The ``description`` here is the *published snippet*, which is what the
    # grounding check verifies quotes against. It has to contain the text the
    # stub answer quotes, exactly as a real article would.
    return [
        {"title": "Federal Reserve holds rates",
         "url": "https://example.org/fomc",
         "description": "The Committee held the policy rate at its current level "
                        "on Tuesday.",
         "content": "", "source": "Federal Reserve", "author": "",
         "date_published": None, "artifact_type": "policy_doc",
         "query": "federal reserve rate decision", "primary": True},
        {"title": "Issuer reserve composition",
         "url": "https://example.org/reserves",
         "description": "Holdings shifted toward bills as the curve steepened.",
         "content": "", "source": "Circle", "author": "",
         "date_published": None, "artifact_type": "report",
         "query": "stablecoin reserve bill holdings", "primary": True},
    ]


@pytest.fixture(autouse=True)
def _stub_search(monkeypatch):
    from app import agent

    def fake_searches(queries, seen_urls):
        """Honour the dedupe set, like the real fan-out does.

        Without this the refinement round re-finds the same two candidates every
        time and the loop ends up persisting duplicates -- a stub that is more
        naive than the code it stands in for tests something the app cannot do.
        """
        out = []
        for c in _candidates():
            key = (c.get("url") or "").strip().lower() or c["title"].lower()
            if key in seen_urls:
                continue
            seen_urls.add(key)
            out.append(c)
        return out

    monkeypatch.setattr(agent, "_run_searches", fake_searches)


def _drain_queue(client, limit=40):
    """Run queued jobs inline rather than waiting for the worker thread."""
    from app import jobqueue
    n = 0
    while jobqueue.run_once() and n < limit:
        n += 1
    return n


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------
def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"]


def test_taxonomy_is_served(client):
    r = client.get("/api/domains")
    assert r.status_code == 200
    body = r.json()
    assert len(body["domains"]) == 14
    assert body["channels"]
    assert body["drivers"]


def test_transmission_walk(client):
    r = client.get("/api/transmission/ai_labs")
    assert r.status_code == 200
    body = r.json()
    assert body["reached"] > 0
    assert all(p["seed_label"] and p["domain_label"] for p in body["paths"])


def test_unknown_domain_is_a_404(client):
    assert client.get("/api/domains/nonsense").status_code == 404


def test_investigation_crud(client):
    r = client.post("/api/investigations", json={
        "title": "Rate path and stablecoin float",
        "keywords": "rate decision, stablecoin reserves",
        "description": "Track the transmission.",
        "preferred_domains": "monetary_policy,stablecoins,macro",
    })
    assert r.status_code == 200
    inv = r.json()
    assert inv["domains"] == ["monetary_policy", "stablecoins", "macro"]

    assert client.get(f"/api/investigations/{inv['id']}").status_code == 200
    assert inv["id"] in [i["id"] for i in
                         client.get("/api/investigations").json()["investigations"]]

    r = client.put(f"/api/investigations/{inv['id']}",
                   json={"keywords": "rate decision, T-bill holdings"})
    assert r.json()["keywords"].startswith("rate decision")

    assert client.delete(
        f"/api/investigations/{inv['id']}").status_code == 200
    assert client.get(f"/api/investigations/{inv['id']}").status_code == 404


def test_unknown_domains_are_reported_not_silently_dropped(client):
    r = client.post("/api/investigations", json={
        "title": "x", "preferred_domains": "macro,not_a_domain"})
    body = r.json()
    assert body["domains"] == ["macro"]
    assert any("not_a_domain" in w for w in body.get("warnings", []))


def test_full_collection_loop(client):
    """Plan -> search -> analyze -> map, with the ledger recording all of it."""
    inv = client.post("/api/investigations", json={
        "title": "Rate path and stablecoin float",
        "keywords": "rate decision, stablecoin reserves",
        "description": "Track the transmission."}).json()

    r = client.post(f"/api/investigations/{inv['id']}/run", json={})
    assert r.status_code == 200
    _drain_queue(client)

    arts = client.get(f"/api/investigations/{inv['id']}/artifacts").json()
    assert arts["count"] == 2
    by_domain = {a["domain"] for a in arts["artifacts"]}
    assert by_domain == {"monetary_policy", "stablecoins"}

    # Direction, magnitude and horizon are stored, not flattened into prose.
    pol = [a for a in arts["artifacts"] if a["domain"] == "monetary_policy"][0]
    assert pol["direction"] == "down"
    assert pol["magnitude"] == 0.5
    assert pol["horizon"] == "short"
    assert "Federal Reserve" in pol["entities"]

    runs = client.get(f"/api/runs?investigation_id={inv['id']}").json()["runs"]
    assert runs and runs[0]["status"] == "done"
    assert runs[0]["stats"]["artifacts_kept"] == 2

    events = client.get(f"/api/runs/{runs[0]['id']}/events").json()["events"]
    stages = {e["stage"] for e in events}
    assert {"plan", "search", "analyze", "map", "summary"} <= stages


def test_graph_is_coloured_by_domain_and_edges_cross_domains(client):
    inv = client.post("/api/investigations", json={
        "title": "t", "keywords": "k", "description": "d"}).json()
    client.post(f"/api/investigations/{inv['id']}/run", json={})
    _drain_queue(client)

    g = client.get(f"/api/investigations/{inv['id']}/graph").json()
    assert len(g["nodes"]) == 2
    assert g["nodes"][0]["color"]["background"].startswith("#")
    # The taxonomy fallback must have drawn the cross-domain edge the model
    # named nothing for beyond the one it did name.
    cross = [e for e in g["edges"] if e["label"] == "transmits_to"]
    assert cross, "a shared entity across two linked domains must draw an edge"
    assert g["stats"]["cross_domain_edges"] >= 1
    assert g["legend"]["domains"]


def test_clusters_report_isolation(client):
    inv = client.post("/api/investigations", json={
        "title": "t", "keywords": "k", "description": "d"}).json()
    arts = client.get(f"/api/investigations/{inv['id']}/artifacts")
    client.post(f"/api/investigations/{inv['id']}/artifacts", json={
        "title": "lonely", "domain": "macro"})
    client.post(f"/api/investigations/{inv['id']}/artifacts", json={
        "title": "linked a", "domain": "stablecoins"})
    b = client.post(f"/api/investigations/{inv['id']}/artifacts", json={
        "title": "linked b", "domain": "blockchain"}).json()
    a = [x for x in client.get(
        f"/api/investigations/{inv['id']}/artifacts").json()["artifacts"]
        if x["title"] == "linked a"][0]
    client.post("/api/relationships", json={
        "investigation_id": inv["id"], "source_id": a["id"],
        "target_id": b["id"], "relationship_type": "transmits_to"})

    c = client.get(f"/api/investigations/{inv['id']}/graph/clusters").json()
    assert c["isolated_count"] == 1
    assert c["cross_domain_edges"] == 1
    assert c["cross_domain_ratio"] == 1.0


def test_review_flow_and_pending_badge(client):
    inv = client.post("/api/investigations", json={
        "title": "t", "keywords": "k"}).json()
    a = client.post(f"/api/investigations/{inv['id']}/artifacts", json={
        "title": "thing", "domain": "macro"}).json()
    assert a["review"] == "accepted"  # manual additions are pre-accepted

    client.post(f"/api/investigations/{inv['id']}/run", json={})
    _drain_queue(client)
    detail = client.get(
        f"/api/investigations/{inv['id']}").json()
    assert detail["counts"]["pending"] == 2

    arts = client.get(
        f"/api/investigations/{inv['id']}/artifacts?review=pending").json()
    r = client.post(f"/api/artifacts/{arts['artifacts'][0]['id']}/review",
                    json={"decision": "reject", "reviewer": "fox",
                          "note": "off brief"})
    assert r.json()["review"] == "rejected"
    assert client.get(
        f"/api/artifacts/{arts['artifacts'][0]['id']}").json()["reviews"]


def test_explainer_is_grounded_and_saves_to_the_graph(client):
    inv = client.post("/api/investigations", json={
        "title": "t", "keywords": "k", "description": "d"}).json()
    client.post(f"/api/investigations/{inv['id']}/run", json={})
    _drain_queue(client)

    r = client.post(f"/api/investigations/{inv['id']}/explain",
                    json={"question": "How does the rate decision transmit "
                                      "to stablecoin reserves?",
                          "mode": "explain"})
    assert r.status_code == 200
    _drain_queue(client)

    exps = client.get(
        f"/api/investigations/{inv['id']}/explanations").json()["explanations"]
    assert exps
    e = exps[0]
    assert e["status"] == "done"
    assert e["answer"]
    assert e["domains"], "the answer must record which domains it touched"
    # Every citation carried a verbatim quote, so none was dropped.
    assert e["dropped_citations"] == 0
    assert 0 < e["confidence"] <= 1

    trace = client.get(f"/api/explanations/{e['id']}").json()["trace"]
    assert trace["grounding"]["distinct_sources"] == 2
    assert len(trace["artifact_ids"]) == 2

    # An answer is evidence too: it lands on the graph as a projection.
    after = client.get(
        f"/api/investigations/{inv['id']}/artifacts"
        f"?artifact_type=projection").json()
    assert after["count"] == 1


def test_explainer_refuses_when_nothing_was_collected(client):
    """The correct answer to an unanswerable question is to say so and file a
    gap, not to answer from memory."""
    inv = client.post("/api/investigations", json={
        "title": "t", "keywords": "k"}).json()
    client.post(f"/api/investigations/{inv['id']}/explain",
                json={"question": "What about quantum computing?"})
    _drain_queue(client)
    e = client.get(
        f"/api/investigations/{inv['id']}/explanations").json()["explanations"][0]
    assert e["status"] == "no_evidence"
    assert "research gap" in e["answer"].lower()
    rows = client.get(
        f"/api/investigations/{inv['id']}/research-gaps").json()["rows"]
    assert any("quantum" in g["dimension"].lower() for g in rows)


def test_explain_rejects_a_bad_mode(client):
    inv = client.post("/api/investigations", json={"title": "t"}).json()
    r = client.post(f"/api/investigations/{inv['id']}/explain",
                    json={"question": "q", "mode": "interpret-dance"})
    assert r.status_code == 400


def test_an_answer_with_an_invented_citation_reports_it(client, monkeypatch):
    """The end-to-end version of the grounding guarantee: a citation the model
    makes up is dropped, counted, and shown on the answer."""
    inv = client.post("/api/investigations", json={
        "title": "t", "keywords": "k", "description": "d"}).json()
    client.post(f"/api/investigations/{inv['id']}/run", json={})
    _drain_queue(client)

    def lying(messages, **kw):
        sys_p = ""
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "system":
                sys_p = str(m.get("content", ""))
                break
        if "hostile reviewer" in sys_p:
            return "## Supported\n- nothing"
        if "macro-financial analyst" in sys_p:
            return ('"The Committee held the policy rate" [1], which per the '
                    'IMF raises growth by 3% [2].')
        return _fake_chat(messages, **kw)

    def lying_json(messages, **kw):
        text = lying(messages, **kw)
        try:
            return json.loads(text)
        except Exception:
            return {}

    # Swapped in only now: the collection above needed the honest planner, and
    # stubbing it with the liar would have left the investigation empty and
    # "tested" nothing.
    monkeypatch.setattr(llm, "chat", lying)
    monkeypatch.setattr(llm, "chat_json", lying_json)

    client.post(f"/api/investigations/{inv['id']}/explain",
                json={"question": "How does the rate decision transmit?"})
    _drain_queue(client)

    e = client.get(
        f"/api/investigations/{inv['id']}/explanations").json()["explanations"][0]
    assert e["dropped_citations"] == 1, "the IMF citation was never quoted"
    trace = client.get(f"/api/explanations/{e['id']}").json()["trace"]
    assert any(d.get("index") == 2 for d in trace["dropped"])
    # One verifiable citation, so confidence stays low rather than looking
    # healthy on a single source.
    assert e["confidence"] <= 0.6


def test_research_gaps_endpoint(client):
    inv = client.post("/api/investigations", json={
        "title": "t", "keywords": "k"}).json()
    client.post(f"/api/investigations/{inv['id']}/run", json={})
    _drain_queue(client)
    g = client.get(
        f"/api/investigations/{inv['id']}/research-gaps").json()
    covered = {"monetary_policy", "stablecoins"}
    gap_ids = {x["domain"] for x in g["gaps"]}
    assert not (gap_ids & covered)
    assert "macro" in gap_ids
    # Coverage is reported per domain so the UI can draw the whole taxonomy,
    # with empty ones visibly empty rather than absent.
    uncovered = [d for d, n in g["coverage"].items() if n == 0]
    assert "macro" in uncovered
    assert g["domains_total"] == 14
    assert g["domains_covered"] == 2


def test_running_a_gap_launches_a_targeted_job(client):
    inv = client.post("/api/investigations", json={"title": "t"}).json()
    g = client.get(
        f"/api/investigations/{inv['id']}/research-gaps?refresh=true").json()
    assert g["gaps"], "a fresh investigation has every domain as a gap"
    assert g["rows"], "refreshing must persist the gap rows"
    rid = g["rows"][0]["id"]
    r = client.post(
        f"/api/investigations/{inv['id']}/research-gaps/run?gap_id={rid}")
    assert r.status_code == 200
    assert r.json()["goal"]
    from app.models import ResearchGap
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        row = db.query(ResearchGap).filter(ResearchGap.id == rid).first()
        assert row.status == "investigating"
    finally:
        db.close()


def test_schedule_rejects_a_bad_cron(client):
    inv = client.post("/api/investigations", json={"title": "t"}).json()
    r = client.put(f"/api/investigations/{inv['id']}/schedule",
                   json={"enabled": True, "cron": "not a cron"})
    assert r.status_code == 400
    r = client.put(f"/api/investigations/{inv['id']}/schedule",
                   json={"enabled": True, "cron": "0 3 * * *"})
    assert r.status_code == 200
    assert r.json()["next_run_at"]


def test_summary_and_dossier(client):
    inv = client.post("/api/investigations", json={
        "title": "Rate path and stablecoin float",
        "keywords": "rate decision, stablecoin reserves",
        "description": "Track the transmission.",
        "driver": "rate_decision",
        "shock": "A 50bp hike."}).json()
    client.post(f"/api/investigations/{inv['id']}/run", json={})
    _drain_queue(client)

    s = client.get(f"/api/investigations/{inv['id']}/summary").json()
    assert s["counts"]["artifacts"] == 2
    assert s["strongest_signals"], "directional claims must surface"
    assert any(d["artifacts"] for d in s["domains"])
    assert any(not d["artifacts"] for d in s["domains"])

    md = client.get(f"/api/investigations/{inv['id']}/dossier")
    assert md.status_code == 200
    text = md.text
    assert "Rate path and stablecoin float" in text
    assert "Central bank rate decision" in text   # the driver, by label
    assert "A 50bp hike." in text           # the shock, verbatim
    assert "_Kept because_" in text         # why each artifact was kept


def test_the_run_is_on_the_ledger_and_verifies(client):
    inv = client.post("/api/investigations", json={
        "title": "t", "keywords": "k"}).json()
    client.post(f"/api/investigations/{inv['id']}/run", json={})
    _drain_queue(client)

    runs = client.get(f"/api/runs?investigation_id={inv['id']}").json()["runs"]
    rid = runs[0]["id"]

    chain = client.get(f"/api/ledger/runs/run-{rid}").json()
    assert chain["ok"]
    assert chain["verified"]["ok"], chain["verified"]
    kinds = {e["kind"] for e in chain["events"]}
    assert "run.start" in kinds
    assert "agent.step" in kinds
    assert "run.end" in kinds

    tl = client.get(f"/api/investigations/{inv['id']}/timeline").json()
    assert tl["events"]
    assert tl["verified"][f"run-{rid}"] is True


def test_integrity_monitor_reports_healthy(client):
    r = client.get("/api/ledger/integrity-monitor")
    assert r.status_code == 200
    assert r.json()["broken"] == []
    assert "audit_drops" in r.json()


def test_jobs_endpoint(client):
    r = client.get("/api/jobs")
    assert r.status_code == 200
    assert "counts" in r.json()


def test_openapi_is_complete(client):
    spec = client.get("/openapi.json").json()
    assert len(spec["paths"]) >= 40


def test_search(client):
    inv = client.post("/api/investigations", json={"title": "t"}).json()
    client.post(f"/api/investigations/{inv['id']}/artifacts", json={
        "title": "Treasury auction tail widens", "domain": "sovereign_debt",
        "tags": "auction,yields"})
    r = client.get("/api/search?q=auction")
    assert len(r.json()["results"]) == 1
    assert r.json()["results"][0]["investigation_title"] == "t"