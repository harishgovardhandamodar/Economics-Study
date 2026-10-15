"""Remaining surfaces: SIEM cursor export, framework matrix, A2A cards, PDFs."""
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_siem_export_shape_and_cursor(client):
    inv = client.post("/api/investigations", json={"title": "t"}).json()
    client.post(f"/api/investigations/{inv['id']}/artifacts",
                json={"title": "x", "domain": "macro"})
    r = client.get("/api/ledger/siem-export?limit=200").json()
    assert r["count"] >= 0 and "cursor" in r
    assert r["lines_as_json"] is True
    if r["lines"]:
        import json as _json
        first = _json.loads(r["lines"][0])
        assert "@timestamp" in first and "run_id" in first
        assert "event" in first and "actor" in first and "chain" in first
        # Minimised payload: digests travel, prompts do not.
        econ = first.get("economics", {})
        assert "prompt" not in econ and "prompt_text" not in econ
    # Cursor pagination: second page starts where the first stopped.
    r2 = client.get(f"/api/ledger/siem-export?since={r['cursor']}").json()
    assert r2["cursor"] >= r["cursor"]
    # NDJSON variant parses line by line.
    txt = client.get("/api/ledger/siem-export/text?limit=5")
    assert txt.status_code == 200
    assert "ndjson" in txt.headers["content-type"]


def test_siem_export_scoped_to_run(client):
    inv = client.post("/api/investigations", json={"title": "t"}).json()
    client.post(f"/api/investigations/{inv['id']}/artifacts",
                json={"title": "x", "domain": "macro"})
    from app import ledger as L
    L.ensure_run("siem-test", L.Mandate(objective="siem test"), label="siem")
    L.append("siem-test", "agent.step", "a", actor_type="agent",
             data={"stage": "s"})
    r = client.get("/api/ledger/siem-export?run_id=siem-test").json()
    assert r["count"] >= 1
    import json as _json
    assert all(_json.loads(line)["run_id"] == "siem-test" for line in r["lines"])


def test_frameworks_catalog_is_valid(client):
    r = client.get("/api/frameworks").json()
    assert len(r["frameworks"]) >= 10
    from app import domains as D
    for fw in r["frameworks"]:
        assert fw["domains"], f"{fw['id']} governs nothing"
        for d in fw["domains"]:
            assert d in D.DOMAINS, f"{fw['id']} governs unknown domain {d}"


def test_framework_matrix_empty_scores_zero(client):
    inv = client.post("/api/investigations", json={"title": "t"}).json()
    m = client.get(
        f"/api/frameworks/matrix?investigation_id={inv['id']}").json()
    assert m["investigation_id"] == inv["id"]
    for row in m["frameworks"]:
        assert row["covered"] == 0
        assert row["mean_score"] == 0
        for cell in row["cells"].values():
            assert cell["score"] == 0


def test_framework_matrix_scores_evidence(client):
    inv = client.post("/api/investigations", json={"title": "t"}).json()
    for _ in range(3):
        client.post(f"/api/investigations/{inv['id']}/artifacts", json={
            "title": "Fed holds", "domain": "monetary_policy",
            "direction": "down", "magnitude": 0.5, "horizon": "short"})
    m = client.get(
        f"/api/frameworks/matrix?investigation_id={inv['id']}").json()
    imf = [r for r in m["frameworks"] if r["id"] == "imf-surveillance"][0]
    assert imf["covered"] >= 1
    assert imf["mean_score"] > 0
    for cell in imf["cells"].values():
        assert 0 <= cell["score"] <= 100
    # Directional mass beats bare count: tested at unit level in test file.
    estate = client.get("/api/frameworks/matrix").json()
    assert estate["investigation_id"] is None


def test_agent_cards_list_blind_protocol(client):
    r = client.get("/api/agents/cards").json()
    assert r["protocol"] == "econ-a2a/v1"
    assert len(r["agents"]) >= 5
    for c in r["agents"]:
        assert c["name"] and c["description"] and c["intents"]
    wk = client.get("/.well-known/econ-agents").json()
    assert wk["protocol"] == "econ-a2a/v1"


def test_agent_invoke_walk(client):
    r = client.post("/api/agents/invoke", json={
        "to": "taxonomy", "intent": "transmission.walk",
        "payload": {"domain": "ai_labs"}}).json()
    assert r["ok"] and r["result"]["reached"] > 0


def test_agent_invoke_gaps_and_matrix(client):
    inv = client.post("/api/investigations", json={"title": "t"}).json()
    g = client.post("/api/agents/invoke", json={
        "to": "gaps", "intent": "gaps.compute",
        "payload": {"investigation_id": inv["id"]}}).json()
    assert g["ok"] and "gaps" in g["result"]
    m = client.post("/api/agents/invoke", json={
        "to": "frameworks", "intent": "frameworks.matrix",
        "payload": {}}).json()
    assert m["ok"] and m["result"]["n_frameworks"] >= 10
    p = client.post("/api/agents/invoke", json={
        "to": "manager", "intent": "manager.parse",
        "payload": {"command": "rate decision"}}).json()
    assert p["ok"]


def test_agent_invoke_rejects_unknown(client):
    r = client.post("/api/agents/invoke", json={
        "to": "nope", "intent": "whatever", "payload": {}})
    assert r.status_code == 400
    r = client.post("/api/agents/invoke", json={
        "to": "taxonomy", "intent": "time.travel", "payload": {}})
    assert r.status_code == 400


def test_inline_md_never_breaks_xml_nesting():
    """The dossier emits **bold** inside *emphasis* lines. The parser must
    produce nested elements, never <b><i>x</b> — reportlab raises on the
    latter, which the route used to report as a 404."""
    from app.dossier_pdf import _inline_md, markdown_flowables, _styles
    assert _inline_md("***accepted** · manual") == \
        "<b><i>accepted</i></b> · manual"
    assert _inline_md("**up** 0.5 / short") == "<b>up</b> 0.5 / short"
    assert _inline_md("*steady* at 4.5%") == "<i>steady</i> at 4.5%"
    assert _inline_md("a < b & c") == "a &lt; b &amp; c"
    # A lone marker stays literal instead of breaking the export.
    assert _inline_md("5 * 3 = 15") == "5 * 3 = 15"
    # The whole pipeline over hostile input still builds flowables.
    flows = markdown_flowables(
        "# T **with** <x>\n*line with ***nest*** and <angle>*\n"
        "| A | B |\n|---|---|\n| 5 * 3 | ok |\n> quote *em*\n", _styles())
    assert flows


def test_pdfs_are_real_pdfs(client):
    inv = client.post("/api/investigations", json={
        "title": "Rate path **with** <markup> & \"quotes\"",
        "keywords": "rate",
        "description": "Track *transmission* with ***nested*** emphasis."}).json()
    client.post(f"/api/investigations/{inv['id']}/artifacts", json={
        "title": "Fed **holds** rates", "domain": "monetary_policy",
        "description": "The committee held *steady* at 4.5% — see <note>.",
        "direction": "down", "magnitude": 0.5, "horizon": "short"})
    for url in (f"/api/investigations/{inv['id']}/dossier/pdf",
                "/api/leadership/board.pdf"):
        r = client.get(url)
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pdf"
        assert r.content.startswith(b"%PDF")
        assert len(r.content) > 2000, "a 2-paragraph PDF is a broken renderer"
    client.post(f"/api/investigations/{inv['id']}/assess", json={})
    from app import jobqueue
    n = 0
    while jobqueue.run_once() and n < 20:
        n += 1
    aid = client.get(
        f"/api/investigations/{inv['id']}/assessments").json()["assessments"][0]["id"]
    r = client.get(f"/api/assessments/{aid}/pdf")
    assert r.content.startswith(b"%PDF") and len(r.content) > 2000
    r = client.get("/api/assessments/999999/pdf")
    assert r.status_code == 404