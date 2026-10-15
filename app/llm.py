"""Client for the fox-services LLM gateway (OpenAI-compatible
``/v1/chat/completions``).

Identified by header ``X-Service-Name`` for attribution. Works from inside
docker (``host.docker.internal``) and local dev (``localhost``) via fallback.

Three things this module is responsible for, and why they live here rather than
in the callers:

* **Failover.** The base chain is walked in order, with a connect timeout
  deliberately shorter than the generate timeout — a primary that is
  *blackholed* must not burn the whole budget before the fallback is tried.
* **Lenient JSON.** Small models emit pretty-printed JSON with literal newlines
  inside strings, trailing commas, and prose around the payload. Strict parsing
  turns any of that into a total failure, and for a 2KB answer some deviation is
  the norm rather than the exception. Each repair is tried in order of fidelity,
  so clean output takes exactly the same path.
* **The audit choke point.** Every model call in the app goes through
  :func:`chat`, which is where the ledger record is written. No-ops unless a
  ledger run is open.
"""
import contextvars
import datetime
import json
import os
import re
import threading
import time

import httpx

SERVICE_NAME = "economics-knowledge-mapper"
# Distinct gateway identity for CLI/TUI-driven traffic: same backend and
# database, but the gateway attributes queue slots, usage rows, and project
# grouping to this name.
SERVICE_NAME_TUI = "economics-knowledge-mapper-tui"
CLIENT_LABEL_TUI = "tui"

_client_label: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "econ_client_label", default=None)


def set_client_label(label: object) -> contextvars.Token:
    """Pin the originating frontend for this context.

    Only ``"tui"`` is special (anything else clears); returns a token for
    :func:`reset_client_label` so middleware and job handlers cannot leak the
    label into unrelated work.
    """
    value = (CLIENT_LABEL_TUI if isinstance(label, str)
             and label.strip().lower() == CLIENT_LABEL_TUI else None)
    return _client_label.set(value)


def reset_client_label(token: contextvars.Token) -> None:
    _client_label.reset(token)


def current_client_label() -> str | None:
    return _client_label.get()


def service_name() -> str:
    if _client_label.get() == CLIENT_LABEL_TUI:
        return SERVICE_NAME_TUI
    return SERVICE_NAME


BASE_URL = os.environ.get("LLM_BASE_URL", "http://localhost:8210/v1").rstrip("/")
# Optional second backend (e.g. a mesh peer's Ollama). Tried in order after
# BASE_URL.
FALLBACK_URL = os.environ.get("LLM_FALLBACK_URL", "").rstrip("/")
MODEL = os.environ.get("LLM_MODEL", "qwen3.8:27b")
FALLBACK_MODEL = os.environ.get("LLM_FALLBACK_MODEL", MODEL)
TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "180"))
# Connect-only timeout, deliberately shorter than TIMEOUT_S.
CONNECT_TIMEOUT_S = float(os.environ.get("LLM_CONNECT_TIMEOUT_S", "5"))


class LLMError(Exception):
    pass


def _bases() -> list:
    chain = [BASE_URL]
    if FALLBACK_URL and FALLBACK_URL not in chain:
        chain.append(FALLBACK_URL)
    # docker <-> local-dev swap as last resort
    for b in list(chain):
        alt = (b.replace("host.docker.internal", "localhost")
               if "host.docker.internal" in b else
               b.replace("localhost", "host.docker.internal").replace(
                   "127.0.0.1", "host.docker.internal")
               if ("localhost" in b or "127.0.0.1" in b) else b)
        if alt != b and alt not in chain:
            chain.append(alt)
    return chain


def _host(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc or url
    except Exception:
        return url


# Records when a call was *actually* served by a fallback, so the UI can warn
# about a call that really happened rather than a transient probe.
_fallback_lock = threading.Lock()
_fallback_state = {"uses": 0, "last_at": None, "last_base": None}


def _record_fallback(base: str) -> None:
    with _fallback_lock:
        _fallback_state["uses"] += 1
        _fallback_state["last_at"] = datetime.datetime.now(
            datetime.UTC).isoformat()
        _fallback_state["last_base"] = base


def _post(base: str, payload: dict, extra_headers: dict | None = None
          ) -> tuple[dict, dict]:
    url = base + "/chat/completions"
    try:
        with httpx.Client(
                timeout=httpx.Timeout(TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
        ) as client:
            resp = client.post(
                url, json=payload,
                headers={"X-Service-Name": service_name(),
                         **(extra_headers or {})})
    except Exception as e:
        raise LLMError(f"{url}: {e}") from e
    if resp.status_code >= 400:
        # The gateway body says *why* (unknown model, "does not support chat",
        # context length); a bare status code turns an upstream routing fault
        # into a mystery the caller cannot act on.
        detail = ""
        try:
            detail = " ".join(resp.text.split())[:300]
        except Exception:
            pass
        raise LLMError(
            f"{url}: HTTP {resp.status_code}{(', ' + detail) if detail else ''}")
    try:
        return resp.json(), dict(resp.headers)
    except Exception as e:
        raise LLMError(f"{url}: invalid JSON response: {e}") from e


def _prompt_text(messages: list) -> str:
    """Flatten a message list into the text that will be hashed for the audit
    record. Best effort: malformed entries are skipped, not fatal."""
    parts = []
    for m in messages or []:
        if isinstance(m, dict):
            c = m.get("content")
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, list):
                parts.extend(str(p.get("text", "")) for p in c
                             if isinstance(p, dict))
    return "\n".join(parts)


def _used_model(data: dict, requested: str) -> str:
    """Which model actually served the call. The gateway may answer on a
    fallback base under a different name, and the audit record must name the
    real one."""
    try:
        return data.get("model") or requested
    except AttributeError:
        return requested


def chat(messages: list, model: str = MODEL, temperature: float = 0.2,
         max_tokens: int = 2048) -> str:
    """Plain chat completion, returns assistant text.

    Walks the base chain, using ``FALLBACK_MODEL`` on non-primary bases when the
    caller asked for ``MODEL`` (peer/local model names can differ).
    """
    _t0 = time.time()
    errors = []
    bases = _bases()
    prompt_text = _prompt_text(messages)
    ledger_headers = {}
    try:
        from . import ledger as _ledger_ctx
        run_id = _ledger_ctx.current_run()
        if run_id:
            ledger_headers = {
                "X-Ledger-Run": run_id,
                "X-Ledger-Prompt-Sha256": _ledger_ctx.text_digest(prompt_text),
            }
    except Exception:
        ledger_headers = {}

    for i, base in enumerate(bases):
        m = model if (i == 0 or model != MODEL) else FALLBACK_MODEL
        payload = {"model": m, "messages": messages,
                   "temperature": temperature, "max_tokens": max_tokens}
        try:
            data, raw_headers = _post(base, payload, ledger_headers)
            response_headers = {str(k).lower(): v
                                for k, v in (raw_headers or {}).items()}
            if i > 0:
                _record_fallback(base)
            break
        except LLMError as e:
            errors.append(str(e))
    else:
        raise LLMError("; ".join(errors))

    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"unexpected gateway response: {e}") from e

    # Audit checkpoint. No-ops unless a ledger run is open.
    try:
        usage = data.get("usage") or {}
        record_llm_call(
            used_model=_used_model(data, model), prompt_text=prompt_text,
            output_text=text, latency_ms=int((time.time() - _t0) * 1000),
            tokens_in=int(usage.get("prompt_tokens") or 0),
            tokens_out=int(usage.get("completion_tokens") or 0),
            gateway={
                "request_id": (response_headers.get("x-fox-request-id")
                               or response_headers.get("x-request-id") or ""),
                "proof": response_headers.get("x-fox-proof") or "",
                "provider_request_id": str(data.get("id") or ""),
            })
    except Exception:
        pass  # auditing must never break the call it is recording
    return text


def record_llm_call(*, used_model: str, prompt_text: str, output_text: str,
                    latency_ms: int, tokens_in: int, tokens_out: int,
                    gateway: dict) -> None:
    from . import ledger
    ledger.record_llm_call(
        used_model, prompt_text, output_text, latency_ms=latency_ms,
        tokens_in=tokens_in, tokens_out=tokens_out, gateway=gateway)


def _parse_json_lenient(text: str):
    """Parse model output as JSON, tolerating the ways models actually break it.

    Each repair is tried in order of fidelity: the first parse that succeeds
    wins, so clean output takes exactly the same path as before.
    """
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip(),
                     flags=re.IGNORECASE)
    attempts = []
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        attempts.append(f"strict: {e}")
    try:
        # Literal control characters (real newlines/tabs) inside strings: the
        # single most common model-JSON defect. Still exact otherwise.
        return json.loads(cleaned, strict=False)
    except json.JSONDecodeError as e:
        attempts.append(f"lenient-controls: {e}")

    # Find the first balanced {...} or [...] span instead of a greedy
    # first-brace-to-last-brace regex, which breaks when trailing prose contains
    # braces of its own. When several spans parse, the longest wins: the real
    # payload dwarfs any accidental fragment.
    decoder = json.JSONDecoder(strict=False)
    best = None
    for i, ch in enumerate(cleaned):
        if ch not in "{[":
            continue
        try:
            obj, end = decoder.raw_decode(cleaned[i:])
        except json.JSONDecodeError as e:
            attempts.append(f"span@{i}: {e}")
            continue
        if best is None or end > best[1]:
            best = (obj, end)
    if best is not None:
        return best[0]

    # Last resort: drop trailing commas before } and ], then re-try the span.
    repaired = re.sub(r",\s*([}\]])", r"\1", cleaned)
    if repaired != cleaned:
        best = None
        for i, ch in enumerate(repaired):
            if ch not in "{[":
                continue
            try:
                obj, end = decoder.raw_decode(repaired[i:])
            except json.JSONDecodeError:
                continue
            if best is None or end > best[1]:
                best = (obj, end)
        if best is not None:
            return best[0]
        attempts.append("trailing-comma repair failed")

    raise LLMError(f"LLM did not return JSON ({'; '.join(attempts[:3])}): "
                   f"{(text or '')[:300]}")


def chat_json(messages: list, model: str = MODEL, temperature: float = 0.2,
              max_tokens: int = 2048) -> dict | list:
    """Chat completion parsed as JSON (tolerates code fences / prose / the usual
    model formatting defects — see :func:`_parse_json_lenient`)."""
    return _parse_json_lenient(
        chat(messages, model=model, temperature=temperature, max_tokens=max_tokens))


def health() -> dict:
    """Gateway reachability + model inventory (best effort).

    The UI branches on ``fallback_active`` to warn *before* calls are served by
    the fallback. ``notice`` is a ready-to-show sentence.
    """
    out = {"base_url": BASE_URL, "fallback_url": FALLBACK_URL or None,
           "preferred_url": BASE_URL, "model": MODEL,
           "fallback_model": FALLBACK_MODEL, "service": service_name(),
           "reachable": False, "models": [], "preferred_reachable": False,
           "fallback_active": False, "notice": None}
    for base in _bases():
        try:
            with httpx.Client(
                    timeout=httpx.Timeout(10, connect=CONNECT_TIMEOUT_S)
            ) as client:
                r = client.get(base + "/models",
                               headers={"X-Service-Name": service_name()})
                if r.status_code == 200:
                    ids = [m.get("id") for m in r.json().get("data", [])]
                    preferred_ok = (base == BASE_URL)
                    out.update(reachable=True, via=base, served_by=base,
                               models=ids[:30], model_available=(MODEL in ids),
                               preferred_reachable=preferred_ok,
                               fallback_active=not preferred_ok)
                    break
        except Exception:
            continue
    if not out["reachable"]:
        out["notice"] = "No LLM gateway is reachable."
    elif out["fallback_active"]:
        out["notice"] = (
            f"LLM calls are being served by the fallback gateway "
            f"{_host(out['served_by'])}; the preferred gateway "
            f"{_host(BASE_URL)} is unreachable.")
    with _fallback_lock:
        out["fallback_uses"] = _fallback_state["uses"]
        out["last_fallback_at"] = _fallback_state["last_at"]
        out["last_fallback_base"] = _fallback_state["last_base"]
    return out
