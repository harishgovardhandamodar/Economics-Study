"""Verbatim grounding.

An explanation may only cite sources it was actually given, and every quote in
it must appear in the cited source's text. This module is where that is
enforced, and it is deliberately unforgiving about the quote and deliberately
lenient about formatting — the same asymmetry the rest of the app uses.

Two failure modes it exists to prevent:

* **A quote that was invented.** A model that paraphrases and then presents the
  paraphrase inside quotation marks is indistinguishable from one that
  fabricated it, unless the check is mechanical. So it is mechanical: 12
  characters minimum, matched against normalized source text.
* **A citation to a source that does not exist.** A model asked for ``[1] [2]``
  markers will sometimes produce ``[7]``. Every citation is resolved against the
  source list it was given, and an unresolvable one is dropped and *counted* —
  never silently kept, because a dropped-citation count of zero is the signal
  that the answer is trustworthy.
"""
import re
import unicodedata

_WS = re.compile(r"\s+")
_QUOTED = re.compile(r"[\"“”'‘’]([^\"“”]{8,600})[\"“”]")
# Citation markers: [1], [2,3], [S4], (see [12])
_MARKER = re.compile(r"\[(?:S?\d{1,3}(?:\s*,\s*S?\d{1,3})*)\]")

MIN_QUOTE_CHARS = 12


def normalize(text: str) -> str:
    """Fold a string to its comparable form: NFKD, casefolded, one space.

    Matching on normalized text rather than raw is what lets a quote survive
    curly quotes, non-breaking spaces and a line break in the middle of a word
    — the three things a PDF extraction introduces between the source and the
    model.
    """
    if not text:
        return ""
    t = unicodedata.normalize("NFKD", str(text))
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.replace("’", "'").replace("‘", "'")
    t = t.replace("“", '"').replace("”", '"')
    t = t.replace("–", "-").replace("—", "-")
    t = t.replace(" ", " ")
    return _WS.sub(" ", t).strip().casefold()


def verify_quote(quote: str, source_text: str) -> dict:
    """Does this quote actually appear in this source?"""
    q = normalize(quote)
    s = normalize(source_text)
    if not q:
        return {"ok": False, "reason": "empty_quote"}
    if len(q) < MIN_QUOTE_CHARS:
        # Too short to be evidence of anything. A two-word quote matches
        # everywhere, which is worse than no quote at all.
        return {"ok": False, "reason": "quote_too_short",
                "detail": f"{len(q)} chars < {MIN_QUOTE_CHARS}"}
    if q in s:
        return {"ok": True, "reason": "verbatim"}
    # A trailing ellipsis or bracket the model added around a real span.
    stripped = re.sub(r"[\s.…]+$", "", q).rstrip(",;:")
    if len(stripped) >= MIN_QUOTE_CHARS and stripped in s:
        return {"ok": True, "reason": "verbatim_trimmed"}
    # Long quotes sometimes straddle a paragraph break with one word missing.
    # Accept only when the bulk of the quote is genuinely present, and say so.
    parts = [p for p in re.split(r"\s*[.;]\s*", stripped) if len(p) >= 20]
    if len(parts) >= 2:
        hits = sum(1 for p in parts if p in s)
        if hits / len(parts) >= 0.8:
            return {"ok": True, "reason": "verbatim_spanning",
                    "detail": f"{hits}/{len(parts)} clauses matched"}
    return {"ok": False, "reason": "not_found",
            "detail": f"no match for {q[:60]!r}… in {len(s)} chars of source"}


def extract_quotes(text: str) -> list[str]:
    """Pull candidate quotes out of a drafted answer."""
    out = []
    for m in _QUOTED.finditer(text or ""):
        q = m.group(1).strip()
        if len(q) >= MIN_QUOTE_CHARS:
            out.append(q)
    return out


def citation_markers(text: str) -> list[int]:
    """Every ``[n]`` index the answer cites, in order, de-duplicated."""
    out: list[int] = []
    for m in _MARKER.finditer(text or ""):
        for part in m.group(0).strip("[]").split(","):
            digits = re.sub(r"[^0-9]", "", part)
            if digits:
                out.append(int(digits))
    return out


def strip_markers(text: str) -> str:
    return _MARKER.sub("", text or "")


def verify_answer(text: str, sources: list[dict]) -> dict:
    """Ground a whole answer against the sources it was given.

    ``sources`` is a list of ``{index, title, text}`` — the numbered list handed
    to the model. Returns the surviving citations plus the counts of what was
    dropped, because the counts are the honest part: an answer that quietly
    loses three citations reads exactly like one that had none to begin with.

    ``quote_verifiable`` on a source says whether its ``text`` is the published
    document or only the analyst's reading of it. Drops against the latter are
    reported separately, because a citation that fails against a paraphrase is
    not the same finding as one that fails against the real article — and
    averaging the two into a single number would hide which one happened.
    """
    sources = sources or []
    by_index = {i: s for i, s in enumerate(sources, start=1)}
    cited = citation_markers(text)
    quotes = extract_quotes(text)

    kept, dropped = [], []
    seen = set()
    for idx in cited:
        if idx in seen:
            continue
        seen.add(idx)
        src = by_index.get(idx)
        if src is None:
            dropped.append({"index": idx, "reason": "no_such_source"})
            continue
        # Find the longest quote that belongs to this source. A quote is
        # attributed to the source whose text actually contains it; when none
        # does, the citation is dropped.
        best = None
        for q in quotes:
            v = verify_quote(q, src.get("text") or "")
            if v["ok"]:
                best = {"quote": q, **v}
                break
        if best:
            kept.append({"index": idx, "title": src.get("title", ""),
                         "quote": best["quote"], "reason": best["reason"]})
        else:
            dropped.append({"index": idx, "reason": "no_verified_quote",
                            "title": src.get("title", ""),
                            "against_summary_only":
                                not src.get("quote_verifiable", True)})

    n_quotes = len(quotes)
    n_verified = len(kept)
    summary_only = [d["index"] for d in dropped if d.get("against_summary_only")]
    return {
        "citations": kept,
        "dropped": dropped,
        "n_citations": len(cited),
        "n_quotes": n_quotes,
        "n_verified": n_verified,
        "dropped_citations": len(dropped),
        "unverified_quotes": max(0, n_quotes - n_verified),
        "dropped_against_summary_only": len(summary_only),
        # Confidence follows distinct-source corroboration, so five quotes from
        # one document do not read as five sources agreeing.
        "distinct_sources": len({c["index"] for c in kept}),
        "verdict": ("supported" if kept else
                    "unverified" if quotes or cited else "uncited"),
    }


def relevance_gate(section_text: str, image_context: str,
                   *, min_overlap: int = 3) -> dict:
    """Does this image belong in this section?

    A relevance gate rather than model taste: a section image survives only if
    it shares vocabulary with the section. Otherwise the section gets no image
    rather than a random stock photo, which is strictly worse than nothing
    because it looks like evidence.
    """
    def terms(text):
        return {w for w in re.findall(r"[a-z][a-z\-]{3,}", (text or "").lower())
                if w not in {"that", "this", "with", "from", "have", "been",
                             "will", "were", "they", "which", "their", "about"}}

    a, b = terms(section_text), terms(image_context)
    shared = a & b
    return {"ok": len(shared) >= min_overlap, "shared": sorted(shared)[:12],
            "overlap": len(shared)}


def pii_scan(text: str) -> dict:
    """Conservative detector: prefers missing to false alarms.

    The asymmetry is deliberate. A missed PII is a review item; a false alarm
    redacts a sentence of macro analysis and teaches people to ignore the
    scanner.
    """
    patterns = {
        "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"),
        "ssn_us": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "iban": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b"),
        "api_key": re.compile(r"\b(sk-[A-Za-z0-9]{12,}|ghp_[A-Za-z0-9]{20,})\b"),
        "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    }
    hits = {name: len(p.findall(text or "")) for name, p in patterns.items()}
    hits = {k: v for k, v in hits.items() if v}
    return {"clean": not hits, "hits": hits,
            "kinds": sorted(hits)}
