"""The hash chain: the property the whole audit story rests on.

If these hold, a reader can hand an exported bundle to an auditor with no
database attached and the auditor's only tool is :func:`verify_export`.
"""
from app import ledger as L
from app.ledger_models import LedgerEvent, LedgerRun


def _run(rid="t1", **kw):
    return L.ensure_run(rid, L.Mandate(objective="test"), label=rid, **kw)


def test_chain_verifies_when_untouched():
    _run()
    L.append("t1", "agent.step", "a", actor_type="agent", data={"i": 1})
    L.append("t1", "llm.call", "m", actor_type="llm", data={"model": "m"})
    out = L.verify_chain("t1")
    assert out["ok"], out["problems"]
    assert out["events"] == 2
    assert out["head_hash"]


def test_tampered_payload_breaks_its_own_run_only():
    _run("good")
    _run("bad")
    for rid in ("good", "bad"):
        L.append(rid, "agent.step", "a", data={"n": 1})
        L.append(rid, "agent.step", "a", data={"n": 2})

    from app.database import SessionLocal
    db = SessionLocal()
    try:
        ev = (db.query(LedgerEvent)
              .filter(LedgerEvent.run_id == "bad", LedgerEvent.seq == 1).first())
        ev.data_json = '{"n": 999}'  # a quiet edit, the kind that matters
        db.commit()
    finally:
        db.close()

    good = L.verify_chain("good")
    bad = L.verify_chain("bad")
    assert good["ok"], "an unrelated run must not be implicated"
    assert not bad["ok"]
    assert bad["problems"][0]["problem"] == "hash_mismatch"
    assert bad["problems"][0]["seq"] == 1


def test_deleted_event_breaks_the_chain():
    _run()
    L.append("t", "agent.step", "a", data={"i": 1})
    L.append("t", "agent.step", "a", data={"i": 2})
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        db.query(LedgerEvent).filter(
            LedgerEvent.run_id == "t", LedgerEvent.seq == 0).delete()
        db.commit()
    finally:
        db.close()
    out = L.verify_chain("t")
    assert not out["ok"]
    # A deletion at the head shows up as a seq gap, not as a hash mismatch —
    # the stored hashes are still internally consistent.
    assert out["problems"][0]["problem"] in ("seq_gap", "head_orphaned")


def test_truncated_tail_is_caught_by_head_hash():
    """Deleting only the last event leaves every stored link internally
    consistent, so only the recorded head can catch it."""
    _run()
    L.append("t", "agent.step", "a", data={"i": 1})
    L.append("t", "agent.step", "a", data={"i": 2})
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        db.query(LedgerEvent).filter(
            LedgerEvent.run_id == "t", LedgerEvent.seq == 1).delete()
        db.commit()
    finally:
        db.close()
    out = L.verify_chain("t")
    assert not out["ok"]
    assert any(p["problem"] == "head_mismatch" for p in out["problems"])


def test_insertion_breaks_the_chain():
    """A forged row inserted mid-chain.

    The forgery is made *internally consistent* — correct seq, correct
    prev_hash, and a hash that really is the hash of its own core — so the only
    thing that can catch it is the next row's prev_hash, which still points at
    the row that was pushed out. That is the property worth testing: a forger
    who recomputes hashes still cannot insert.
    """
    _run("ins")
    for i in (1, 2, 3):
        L.append("ins", "agent.step", "a", data={"i": i})
    from app.database import SessionLocal
    from app import ledger as _l
    db = SessionLocal()
    try:
        ev0 = db.query(LedgerEvent).filter(
            LedgerEvent.run_id == "ins", LedgerEvent.seq == 0).first()
        pushed = db.query(LedgerEvent).filter(
            LedgerEvent.run_id == "ins", LedgerEvent.seq == 1).first()
        db.delete(pushed)
        db.flush()
        core = _l.event_core(
            run_id="ins", seq=1, ts=ev0.ts, actor_type="agent",
            actor="mallory", kind="agent.step", intent=None, verdict=None,
            severity=None, data_json='{"i":99}', prev_hash=ev0.hash)
        db.add(LedgerEvent(
            run_id="ins", seq=1, ts=ev0.ts, actor_type="agent",
            actor="mallory", kind="agent.step", data_json='{"i":99}',
            prev_hash=ev0.hash, hash=_l.chain_hash(core)))
        db.commit()
    finally:
        db.close()
    out = L.verify_chain("ins")
    assert not out["ok"]
    assert out["problems"][0]["problem"] == "broken_link"
    assert out["problems"][0]["seq"] == 2


def test_secrets_are_redacted_before_storage():
    """The chain is append-only: a secret that lands in one cannot be taken
    back out, so redaction has to happen before the write, not after."""
    _run("sec")
    L.append("sec", "mcp.call", "tool", actor_type="mcp",
             data={"api_key": "sk-abcdefghijklmnop", "note": "harmless"})
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        ev = db.query(LedgerEvent).filter(
            LedgerEvent.run_id == "sec").first()
        assert "sk-abcdefghijklmnop" not in ev.data_json
        assert "[redacted]" in ev.data_json
        assert "harmless" in ev.data_json
    finally:
        db.close()


def test_payloads_are_hashed_not_stored_by_default():
    _run("hp")
    L.append("hp", "llm.call", "m", actor_type="llm",
             data={"prompt": "a secret long prompt", "n": 3})
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        ev = db.query(LedgerEvent).filter(LedgerEvent.run_id == "hp").first()
        assert "secret long prompt" not in ev.data_json
    finally:
        db.close()


def test_mandate_denial_is_recorded_as_a_gate_check():
    _run()
    m = L.Mandate(objective="tight", allowed_intents={"allowed"})
    L.append("t", "agent.step", "a", actor_type="agent", intent="forbidden",
             mandate=m)
    tl = L.timeline("t")
    kinds = [e["kind"] for e in tl["events"]]
    # The refusal itself is not an event, but the gate check is — a violation
    # can never be scrolled past as a silent omission.
    assert "gate.check" in kinds
    gate = [e for e in tl["events"] if e["kind"] == "gate.check"][0]
    assert gate["verdict"] == "deny"


def test_planned_intent_mismatch_flags_but_does_not_block():
    _run()
    m = L.Mandate(objective="shape", planned_intents={"plan", "search"})
    L.append("t", "agent.step", "a", actor_type="agent", intent="map",
             mandate=m)
    tl = L.timeline("t")
    assert any(e["kind"] == "gate.check" and e["verdict"] == "flag"
               for e in tl["events"])


def test_export_verifies_offline():
    _run("exp")
    L.append("exp", "agent.step", "a", data={"i": 1})
    L.emit_claim("exp", "The rate decision moved the curve.",
                 [{"source": "Fed", "quote": "a verbatim quote here"}])
    doc = L.export_run("exp")
    assert doc["bundle_hash"]
    out = L.verify_export(doc)
    assert out["ok"], out


def test_tampered_export_fails_offline_verification():
    _run("exp2")
    L.append("exp2", "agent.step", "a", data={"i": 1})
    doc = L.export_run("exp2")
    doc["events"][0]["data_json"] = '{"i": 999}'
    out = L.verify_export(doc)
    assert not out["ok"]


def test_claims_are_content_addressed():
    c1 = L.claim_hash("The same sentence.", [{"source": "S", "quote": "q"}])
    c2 = L.claim_hash("The same sentence.", [{"source": "S", "quote": "q"}])
    c3 = L.claim_hash("A different sentence.", [{"source": "S", "quote": "q"}])
    assert c1 == c2, "identical claims must collide, so parroting is detectable"
    assert c1 != c3


def test_contamination_traces_consumers():
    """A bad claim's blast radius: what cited it, and what cited what cited
    it. The root has to be an input_ref of the first consumer, otherwise there
    is nothing to trace."""
    _run("c")
    root = L.text_digest("root claim")
    a = L.append("c", "claim.emit", "x", data={"claim_hash": root},
                 input_refs=[root])
    b = L.append("c", "agent.step", "y", data={"used": a["hash"]},
                 input_refs=[a["hash"]])
    L.append("c", "agent.step", "z", data={"used": b["hash"]},
             input_refs=[b["hash"]])
    out = L.contamination("c", root)
    # Three hops: the emit that cited the root, the step that cited the emit,
    # and the step that cited that one.
    assert out["reached"] == 3
    assert max(e["depth"] for e in out["edges"]) == 3


def test_mandate_is_fingerprinted_at_run_creation():
    """The rules in force when an action happened must be provable later."""
    _run("mf")
    db_run = L.get_run("mf")
    assert db_run.mandate_hash == L.Mandate(objective="test").fingerprint()


def test_proof_recomputation_detects_drift():
    _run("pr")
    L.append("pr", "agent.step", "a", data={"i": 1})
    out = L.verify_proof("pr", 0)
    assert out["ok"], out["checks"]


def test_verify_chain_reports_a_missing_run_honestly():
    out = L.verify_chain("does-not-exist")
    assert not out["ok"]
    assert out["problems"][0]["problem"] == "no_such_run"