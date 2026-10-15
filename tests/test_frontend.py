"""Frontend static checks: the class of bug that kills boot().

The knowledge-graph outage was a `$('#graphStats')` reference to an element
that was never in the markup. It threw inside the default view, boot's catch
reported it, and the whole GUI read as "API down". No browser is needed to
catch this shape: every plain `$('#X')` lookup must resolve to an `id="X"`
defined in the same file. (Dynamically built ids via concatenation are out of
scope — this pins the common case.)
"""
import pathlib
import re

HTML = pathlib.Path(__file__).parent.parent / "static" / "index.html"


def _refs_and_ids():
    html = HTML.read_text()
    refs = set(re.findall(r"\$\('#([A-Za-z]+)'\)", html))
    ids = set(re.findall(r'id="([A-Za-z]+)"', html))
    return refs, ids


def test_every_static_element_lookup_resolves():
    refs, ids = _refs_and_ids()
    assert refs, "the regex found nothing — the test itself is broken"
    missing = sorted(r for r in refs if r not in ids)
    assert not missing, (
        f"$() lookups with no matching id= in static/index.html: {missing}. "
        "Each one throws 'Cannot set properties of null' the moment its view loads.")


def test_no_backticks_inside_html_attributes():
    """A backtick template containing another backtick pair ends the outer
    template early — a whole-script parse error that leaves every tab dead.
    The summary view shipped exactly this: an onclick="downloadFile(`...`)"
    inside an outer template literal. HTML attributes are double-quoted in
    this file, so any backtick inside one is the bug."""
    html = HTML.read_text()
    attrs = re.findall(r'onclick="(.*?)"', html, re.S)
    assert attrs, "no onclick attributes found — the test itself is broken"
    bad = [a[:60] for a in attrs if "`" in a]
    assert not bad, (
        f"onclick attributes containing backticks (nested template "
        f"literals — whole-script parse error): {bad}")


def test_top_level_app_switcher():
    """Executive brief and Design are top-level apps beside the Mapper, not
    investigation tabs: they are estate-wide and work with zero investigations
    selected. Three buttons, one switch function, and refreshView must branch
    on State.app before demanding an investigation."""
    html = HTML.read_text()
    apps = re.findall(r'data-app="([a-z]+)"', html)
    assert apps == ["mapper", "brief", "design", "ask"], (
        f"expected exactly the four top-level apps, found {apps}")
    assert "function switchApp(app)" in html
    assert "State.app === 'brief'" in html
    assert "State.app === 'design'" in html
    # The per-investigation tab row hides outside the mapper.
    assert "tabs.style.display" in html
    # The sidebar is Mapper-only: estate apps get the full width.
    assert 'id="sidebar"' in html
    assert "side.style.display" in html
    assert "classList.toggle('single'" in html
