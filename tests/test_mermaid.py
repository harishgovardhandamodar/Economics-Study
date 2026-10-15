"""Design-diagram hygiene: every mermaid block must at least be structural.

The Design tab showed raw code twice for the same underlying reason: blocks
that cannot parse. A full mermaid parse needs the mermaid runtime, which the
test env does not have — but the three failures so far were all structural
(an unclosed ``rect``: 4 opens vs 3 ends; an invalid ``<--|"..."|`` edge; a
bare comma line inside a subgraph that only a node id can legally hold), and
structure is checkable with plain text.

If a block fails these checks it will fail in the browser exactly the way the
tab failed: the fallback renders source instead of a diagram.
"""
import pathlib
import re

DESIGN = pathlib.Path(__file__).parent.parent / "design"


def _blocks():
    out = []
    for f in sorted(DESIGN.glob("*.md")):
        for i, m in enumerate(
                re.finditer(r"```mermaid\n([\s\S]*?)```", f.read_text())):
            out.append((f"{f.name}#{i}", m.group(1)))
    return out


_OPEN = re.compile(r"^\s*(rect|par|alt|opt|loop|critical|break)\b", re.M)
_END = re.compile(r"^\s*end\b", re.M)


def test_sequence_blocks_are_balanced():
    """Every opened rect/par/alt/opt/loop has its end. The parser reports an
    unclosed block past the last line, which reads as a mystery failure."""
    bad = []
    for name, code in _blocks():
        if not code.lstrip().startswith("sequenceDiagram"):
            continue
        opens = len(_OPEN.findall(code))
        ends = len(_END.findall(code))
        if opens != ends:
            bad.append(f"{name}: {opens} opens vs {ends} ends")
    assert not bad, bad


def test_no_invalid_dashed_labeled_edge():
    """``A<--|"text"|B`` is not mermaid syntax (the labeled dashed forms are
    ``<-->|"t"|`` and ``-.->|"t"|``). It parses nowhere, including GitHub."""
    bad = []
    for name, code in _blocks():
        if re.search(r"<--\|", code):
            bad.append(name)
    assert not bad, bad


def test_flowchart_lines_are_nodes_edges_or_directives():
    """A bare text line (commas, no brackets, no arrow) inside a flowchart is
    not a node — only ``id["label"]`` holds text. The ledger doc shipped
    ``run_id, seq, ts, ...`` as a bare line and the whole diagram died."""
    allowed = re.compile(
        r"^\s*flowchart\b|subgraph|end|direction|style|classDef|class\b|click|"
        r"\[|\{|\(|-->|---|-\.->|==>|~~~")
    bad = []
    for name, code in _blocks():
        if not code.lstrip().startswith("flowchart"):
            continue
        for n, line in enumerate(code.splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("%%"):
                continue
            if not allowed.search(s):
                bad.append(f"{name}:{n}: {s[:70]}")
    assert not bad, bad


def test_design_index_matches_files():
    """Every doc the API can serve resolves to a file that exists."""
    from app import design_docs as _docs
    for d in _docs._DOCS:
        resolved = _docs._resolve(d["id"])
        assert resolved is not None
        assert pathlib.Path(resolved["path"]).is_file(), d["id"]