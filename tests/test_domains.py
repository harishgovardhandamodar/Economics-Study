"""The taxonomy is the foundation everything else stands on.

These tests pin the properties the rest of the app relies on without knowing
they rely on them: normalize only ever returns known domains, every channel
edge points at domains that exist, and the transmission walk terminates.
"""
from app import domains as D


def test_domains_cover_the_brief():
    """The ten domains the app exists to map between."""
    expected = {"macro", "monetary_policy", "fiscal_policy", "sovereign_debt",
                "stablecoins", "blockchain", "datacenters", "ai_labs",
                "ai_agents", "agi_asi", "politics"}
    assert expected <= set(D.DOMAINS)


def test_every_channel_endpoint_is_a_real_domain():
    for c in D.CHANNELS:
        assert c["src"] in D.DOMAINS, f"{c} has an unknown src"
        assert c["dst"] in D.DOMAINS, f"{c} has an unknown dst"
        assert c["src"] != c["dst"], "a channel that loops on itself is not a channel"
        assert 0 < c["strength"] <= 1
        assert c["lag"] >= 0


def test_hubs_are_actually_hubs():
    """A hub that nothing routes through is a mislabelled leaf."""
    for hub in D.HUB_DOMAINS:
        assert D.channels_from(hub), f"{hub} has no outbound channels"
        assert D.channels_into(hub), f"{hub} has no inbound channels"


def test_normalize_drops_unknown_names():
    """An unknown domain must not survive: it cannot participate in
    transmission, and letting it through would make coverage look complete."""
    assert D.normalize_domains("macro, nonsense, SOVEREIN_DEBT") == ["macro"]
    assert D.normalize_domains(None) == []
    assert D.normalize_domains("") == []
    assert D.normalize_domains(["macro", "macro"]) == ["macro"]


def test_normalize_accepts_loose_input():
    assert D.normalize_domains("Sovereign Debt") == ["sovereign_debt"]
    assert D.normalize_domains("ai-agents") == ["ai_agents"]
    assert D.normalize_domains(["macro;politics"]) == ["macro", "politics"]


def test_walk_transmission_terminates_and_is_ordered():
    """The graph has cycles (sovereign debt -> politics -> fiscal -> debt), so
    the walk must cut them or it never returns."""
    rows = D.walk_transmission(["sovereign_debt"], max_depth=4)
    assert rows
    # Nearest hops first: that is what makes a short causal chain outrank a
    # long one.
    depths = [r["depth"] for r in rows]
    assert depths == sorted(depths)
    for r in rows:
        assert r["path"][0] == "sovereign_debt"
        assert r["path"][-1] == r["domain"]


def test_walk_respects_min_strength():
    loose = D.walk_transmission(["datacenters"], min_strength=0.0)
    tight = D.walk_transmission(["datacenters"], min_strength=0.8)
    assert len(tight) < len(loose)


def test_walk_from_an_unknown_seed_is_empty_not_an_error():
    assert D.walk_transmission(["nope"], max_depth=2) == []


def test_drivers_reference_real_domains():
    for key, d in D.DRIVERS.items():
        assert d["domains"], f"driver {key} has no domains"
        for dom in d["domains"]:
            assert dom in D.DOMAINS, f"driver {key} names unknown domain {dom}"
        assert d["label"]


def test_domain_catalog_is_serializable_and_consistent():
    rows = D.domain_catalog()
    assert len(rows) == len(D.DOMAINS)
    for r in rows:
        assert r["color"].startswith("#")
        assert isinstance(r["hub"], bool)
        # The degree numbers are what the UI sizes clusters by; they must agree
        # with the channel list they are derived from.
        assert r["out_degree"] == len(D.channels_from(r["id"]))
        assert r["in_degree"] == len(D.channels_into(r["id"]))


def test_relationship_types_all_have_render_metadata():
    for name, meta in D.RELATIONSHIP_TYPES.items():
        assert meta.get("label"), f"{name} has no label"
        assert meta.get("note"), f"{name} has no note"
        assert meta.get("color")


def test_core_cross_domain_channels_exist():
    """The links that make this an economics map rather than ten silos."""
    pairs = {(c["src"], c["dst"]) for c in D.CHANNELS}
    for required in [
        ("ai_labs", "datacenters"),       # capex -> power
        ("datacenters", "macro"),          # load growth -> GDP/CPI
        ("monetary_policy", "macro"),      # transmission
        ("fiscal_policy", "sovereign_debt"),  # deficits -> issuance
        ("sovereign_debt", "monetary_policy"),  # market pressure on the CB
        ("stablecoins", "monetary_policy"),  # dollar substitution
        ("politics", "ai_labs"),           # export controls
        ("agi_asi", "macro"),              # productivity
        ("ai_agents", "macro"),            # labour substitution
        ("trade", "macro"),                # tariffs -> import prices
        ("geopolitics", "macro"),          # conflict -> risk premia
        ("disruptions", "trade"),          # closed strait -> freight
        ("geopolitics", "trade"),          # weaponised interdependence
    ]:
        assert required in pairs, f"missing expected channel {required}"


def test_new_domains_are_first_class():
    """Trade, geopolitics and disruptions participate everywhere a domain
    must: normalisation, catalog, drivers, aliases, and a council seat."""
    from app import council as C
    for did in ("trade", "geopolitics", "disruptions"):
        assert did in D.DOMAINS
        assert D.normalize_domains(did.upper().replace("_", " ")) == [did]
    assert D.DRIVERS["tariff_shock"]["domains"][0] == "trade"
    assert "disruptions" in D.DRIVERS["chokepoint_closure"]["domains"]
    assert "geopolitics" in D.DRIVERS["conflict_outbreak"]["domains"]
    assert "geopolitics" in D.DRIVERS["sanction"]["domains"]
    assert "trade" in D.DRIVERS["export_control"]["domains"]
    assert D.entity_tokens("WTO ruling") >= {"wto", "world"}
    assert D.entity_tokens("Suez transit") >= {"suez", "canal"}
    owners = {d: s["id"] for s in C.SEATS for d in s["owns"]}
    assert owners["trade"] == "statecraft"
    assert owners["geopolitics"] == "statecraft"
    assert owners["disruptions"] == "statecraft"
    # The statecraft seat publishes its blind spot like every other seat.
    seat = C.SEAT_BY_ID["statecraft"]
    assert seat["blind_spot"] and seat["owns"] == (
        "trade", "geopolitics", "disruptions")