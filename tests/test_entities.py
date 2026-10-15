"""Drawing the cross-domain edge.

The taxonomy says a channel exists between two domains; the map only draws the
edge when two collected documents actually share a named actor. This is the
rule that stops the graph being all-to-all, so the tests here are mostly about
what it *refuses* to connect.
"""
from app.agent import _GENERIC as _GENERIC_FOR_TEST, _norm_entity, _shared_entities


def test_punctuation_variants_match():
    """The same institution written three ways by three sources. Missing these
    pairs means missing edges on a map whose whole purpose is those edges."""
    assert _shared_entities(["US Treasury"], ["U.S. Treasury"]) == ["US Treasury"]
    assert _shared_entities(["Federal Reserve"], ["federal reserve"])
    assert _shared_entities(["Tether"], ["Tether "]) == ["Tether"]


def test_canonical_form_strips_punctuation():
    assert _norm_entity("U.S. Treasury") == "ustreasury"
    assert _norm_entity("Nellie Liang") == "nellieliang"
    assert _norm_entity(None) == ""


def test_near_miss_names_still_match_on_a_distinctive_token():
    """Returns the fullest form the first list spells it, so the edge reads
    the way the source document does."""
    assert _shared_entities(["Nellie Liang"], ["Liang"]) == ["Nellie Liang"]
    assert _shared_entities(["Brent Neiman"], ["Neiman and Liang"]) == \
        ["Brent Neiman"]


def test_unrelated_entities_do_not_match():
    assert _shared_entities(["Tether"], ["Circle"]) == []
    assert _shared_entities(["Circle"], ["US Treasury"]) == []


def test_acronyms_link_to_their_expansion():
    """In an economics corpus the abbreviations *are* most of the named actors.
    Without expansion "IMF" and "International Monetary Fund" share no token
    and the edge silently disappears."""
    assert _shared_entities(["IMF"], ["International Monetary Fund"])
    assert _shared_entities(["the BIS paper"], ["Bank for International Settlements"])
    assert _shared_entities(["ECB"], ["European Central Bank"])


def test_energy_and_grid_acronyms_link():
    """An AI-capex investigation lives or dies on linking these, and sources
    name them by acronym far more often than in full."""
    assert _shared_entities(["IEA"], ["International Energy Agency"])
    assert _shared_entities(["U.S. DOE"], ["Department of Energy"])
    assert _shared_entities(["U.S. FERC"], ["FERC"])
    assert _shared_entities(["LBNL"], ["Lawrence Berkeley National Laboratory"])


def test_treasury_links_because_its_words_are_institution_names():
    """'US Treasury' and 'Treasury Department' are the same actor. Filtering
    'treasury' as too generic left the most important monetary actor in the
    corpus unable to link to anything."""
    assert _shared_entities(["U.S. Treasury"], ["Department of the Treasury"])
    # ...but "Tether" still does not link to it just by being about money.
    assert _shared_entities(["U.S. Treasury"], ["Tether"]) == []


def test_reserve_still_does_not_link_bank_to_the_central_bank():
    """The generic guard has to survive the widening above: bank reserves and
    the Federal Reserve are unrelated actors that share a word."""
    assert _shared_entities(["Bank Reserves"], ["Federal Reserve"]) == []
    assert _shared_entities(["FERC"], ["PJM Interconnection"]) == []


def test_generic_names_alone_do_not_draw_an_edge():
    """The failure mode this rule exists to prevent: every finance document
    mentions a bank, a reserve and a market, so matching on those connects the
    whole corpus into one meaningless blob."""
    assert _shared_entities(["Central Bank"], ["Reserve Bank"]) == []
    assert _shared_entities(["the market"], ["a market"]) == []


def test_a_distinctive_token_rescues_a_generic_sibling():
    """Generic tokens alone are too weak; a distinctive one alongside them is
    enough, which is the case that actually occurs in the corpus."""
    shared = _shared_entities(["Reserve Bank of England"],
                              ["Bank of England"])
    assert shared
    assert "bank of england" not in [t.lower() for t in _GENERIC_FOR_TEST]


def test_empty_inputs_are_safe():
    assert _shared_entities([], ["Tether"]) == []
    assert _shared_entities(["Tether"], []) == []
    assert _shared_entities([], []) == []
    assert _shared_entities(None, None) == []


def test_short_entities_are_refused():
    assert _shared_entities(["EU"], ["EU"]) == []
    assert _shared_entities(["US"], ["US"]) == []


def test_many_documents_produce_stable_output():
    """The edge description embeds this list, so an unstable order would make
    the same pair of documents produce different edges on different runs."""
    a = ["Circle", "Tether", "US Treasury"]
    b = ["Tether", "Circle", "US Treasury"]
    assert _shared_entities(a, b) == _shared_entities(b, a)
    assert _shared_entities(a, b) == ["Circle", "Tether", "US Treasury"]