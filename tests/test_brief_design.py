"""Design docs + estate-wide knowledge graph: the two ported reference tabs."""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_design_docs_list_and_fetch(client):
    lst = client.get("/api/design/docs").json()
    assert lst["available"] is True
    assert len(lst["docs"]) == 8
    assert all(d["available"] for d in lst["docs"])
    assert {d["id"] for d in lst["docs"]} >= {
        "readme", "system-context", "data-model", "ledger"}
    doc = client.get("/api/design/docs/ledger").json()
    assert doc["ok"] is True
    assert "hash" in doc["markdown"].lower()
    assert doc["truncated"] is False


def test_design_docs_reject_unknown_and_traversal(client):
    assert client.get("/api/design/docs/nope").status_code == 404
    assert client.get("/api/design/docs/..").status_code == 404
    # URL-encoded traversal never resolves to a file either.
    assert client.get("/api/design/docs/%2E%2E").status_code == 404


def test_design_docs_missing_dir_is_state_not_crash(monkeypatch):
    from app import design_docs as _docs
    monkeypatch.setattr(_docs, "DESIGN_DIR", "/nonexistent-dir-xyz")
    out = _docs.list_docs()
    assert out["available"] is False
    assert len(out["docs"]) == 8  # shape kept, entries marked
    assert _docs.get_doc("ledger")["ok"] is False


def _seed_two_sessions(client):
    a = client.post("/api/investigations", json={"title": "sess A"}).json()
    b = client.post("/api/investigations", json={"title": "sess B"}).json()
    ra = client.post(f"/api/investigations/{a['id']}/artifacts", json={
        "title": "Fed holds", "domain": "monetary_policy",
        "entities": ["Federal Reserve"]}).json()
    rb = client.post(f"/api/investigations/{b['id']}/artifacts", json={
        "title": "Float shifts", "domain": "stablecoins",
        "entities": ["Federal Reserve"]}).json()
    return a, b, ra, rb


def test_global_graph_namespaces_and_hubs(client):
    a, b, ra, rb = _seed_two_sessions(client)
    g = client.get("/api/leadership/knowledge-graph").json()
    ids = {n["id"] for n in g["nodes"]}
    assert f"a:{ra['id']}" in ids and f"a:{rb['id']}" in ids
    assert f"inv:{a['id']}" in ids and f"inv:{b['id']}" in ids
    kinds = {e["label"] for e in g["edges"]}
    assert "collected_in" in kinds
    assert g["counts"]["artifacts"] == 2
    assert g["counts"]["investigations"] == 2
    assert g["truncated"] is False
    # Scoped to one session: the other session vanishes entirely.
    scoped = client.get(
        f"/api/leadership/knowledge-graph?investigation_ids={a['id']}").json()
    sids = {n["id"] for n in scoped["nodes"]}
    assert f"a:{ra['id']}" in sids and f"a:{rb['id']}" not in sids
    assert client.get(
        "/api/leadership/knowledge-graph?investigation_ids=abc").status_code == 400


def test_global_clusters_category(client):
    a, b, ra, rb = _seed_two_sessions(client)
    c = client.get(
        "/api/leadership/knowledge-graph/clusters?mode=category").json()
    assert c["mode"] == "category"
    assert c["by_domain"]["monetary_policy"]["artifacts"] == 1
    assert c["by_domain"]["stablecoins"]["artifacts"] == 1
    assert client.get(
        "/api/leadership/knowledge-graph/clusters?mode=bogus").status_code == 400


def test_global_clusters_similarity_bridges_sessions(client):
    """Two sessions naming the same actor are bridged — one session's
    evidence bears on the other's question, which is the finding."""
    a, b, ra, rb = _seed_two_sessions(client)
    c = client.get(
        "/api/leadership/knowledge-graph/clusters?mode=similarity").json()
    assert c["n_bridges"] == 1
    br = c["bridges"][0]
    assert {br["a_investigation"], br["b_investigation"]} == {a["id"], b["id"]}
    assert "Federal Reserve" in br["shared"]
    assert c["sessions_linked"] == 2


def test_global_graph_empty_estate_is_honest(client):
    g = client.get("/api/leadership/knowledge-graph").json()
    assert g["nodes"] == [] and g["edges"] == []
    assert g["counts"]["artifacts"] == 0
    c = client.get(
        "/api/leadership/knowledge-graph/clusters?mode=similarity").json()
    assert c["bridges"] == []