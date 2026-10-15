"""The design set, served as selectable documents to the Design tab.

The diagrams that describe this system already exist as Mermaid-in-Markdown
under ``design/``. This module does not author them and does not copy them
into the database: it makes them *selectable*. The tab lists the documents, a
click fetches one, and the browser renders the Markdown and the Mermaid.

Two properties matter more than the feature itself:

* **The index is a constant, not a directory listing.** A document is reachable
  only if its id is one of ``_DOCS`` below, and the file it resolves to is
  re-checked against the design root before it is read. A caller cannot reach a
  file by passing a path, so a traversal attempt is a 404 like any other
  unknown id — there is no code path that turns user input into a path.
* **A missing design directory is a state, not a crash.** The docs are
  documentation; an image built without them should show an explanation, not
  a 500. ``available: false`` with a reason is the honest answer, and each
  entry is marked unavailable rather than dropped so the list keeps its shape.
"""
from __future__ import annotations

import os

#: Repository root, i.e. the parent of the ``app`` package.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DESIGN_DIR = os.environ.get("ECON_DESIGN_DIR") or os.path.join(REPO_ROOT, "design")

#: Safety valve. The largest design document is tens of kB; this is orders of
#: magnitude above it, so hitting it means something is wrong with the file,
#: not that the tab should refuse to render. Truncation is reported, never
#: silent.
MAX_BYTES = 1_000_000

#: id, file, title, group, kind, and the one question the document answers.
_DOCS: list[dict[str, str]] = [
    {"id": "readme", "file": "README.md", "title": "The design set",
     "group": "Start here", "kind": "index · conventions · reading order",
     "answers": "How to read the set, the three ideas that carry the design"},
    {"id": "system-context", "file": "01-system-context.md",
     "title": "System context", "group": "Structure",
     "kind": "context · container · deployment",
     "answers": "What is in the box, what is outside it, and what runs where"},
    {"id": "data-model", "file": "data-model.md", "title": "Data model",
     "group": "Structure", "kind": "ER · lifecycle",
     "answers": "What is stored, and why the summary and the source stay apart"},
    {"id": "interaction", "file": "interaction.md", "title": "Interaction",
     "group": "Behaviour", "kind": "sequence",
     "answers": "One run end to end, the edge-drawing rule, failure paths"},
    {"id": "activity", "file": "activity.md",
     "title": "Activity and state", "group": "Behaviour",
     "kind": "activity · state machines",
     "answers": "The loop's decisions; run, job, explanation and scheduler states"},
    {"id": "ui-interaction", "file": "ui-interaction.md",
     "title": "UI interaction", "group": "User-facing",
     "kind": "navigation · overlays · controls",
     "answers": "What each view answers, and what the UI deliberately omits"},
    {"id": "privacy", "file": "privacy.md", "title": "Privacy",
     "group": "Privacy & assurance", "kind": "boundaries · data flow · redaction",
     "answers": "What leaves, what is hashed, what is not implemented"},
    {"id": "ledger", "file": "ledger.md", "title": "The hash chain",
     "group": "Privacy & assurance",
     "kind": "chain · proofs · mandates · sessions · exports",
     "answers": "What breaks what, and what is fail-open vs fail-closed"},
]


def _design_root() -> str:
    return os.path.abspath(DESIGN_DIR)


def _resolve(doc_id: str) -> dict | None:
    """The doc entry plus its absolute path, re-checked against the root."""
    entry = next((d for d in _DOCS if d["id"] == doc_id), None)
    if entry is None:
        return None
    root = _design_root()
    path = os.path.abspath(os.path.join(root, entry["file"]))
    if not path.startswith(root + os.sep) and path != root:
        return None
    return {**entry, "path": path}


def list_docs() -> dict:
    root = _design_root()
    if not os.path.isdir(root):
        return {"available": False,
                "reason": f"design directory not present ({root})",
                "docs": [{**d, "available": False} for d in _DOCS]}
    out = []
    for d in _DOCS:
        resolved = _resolve(d["id"])
        ok = resolved is not None and os.path.isfile(resolved["path"])
        out.append({**d, "available": ok,
                    **({} if ok else {"reason": "file missing"})})
    return {"available": True, "root": root, "docs": out}


def get_doc(doc_id: str) -> dict:
    resolved = _resolve(doc_id)
    if resolved is None:
        return {"ok": False, "reason": "no such document"}
    path = resolved["path"]
    if not os.path.isfile(path):
        return {"ok": False, "reason": "document file missing"}
    try:
        size = os.path.getsize(path)
        with open(path, encoding="utf-8") as f:
            text = f.read(MAX_BYTES + 1)
    except OSError as e:
        return {"ok": False, "reason": f"cannot read: {e}"}
    truncated = len(text) > MAX_BYTES
    return {"ok": True, "id": resolved["id"], "title": resolved["title"],
            "group": resolved["group"], "kind": resolved["kind"],
            "answers": resolved["answers"], "bytes": size,
            "truncated": truncated, "markdown": text[:MAX_BYTES]}
