#!/usr/bin/env python3
"""
reconstruct.py — rebuild each inference's wire request (and response content) from the
normalized doc ALONE, then verify by semantic diff against the raw bodies.

This is the enriched transcript's correctness oracle: if the doc can't reproduce what
actually went over the wire, the capture has a gap — and the diff names it. Reconstruction never reads captures/ (self-containment); the raw body
is used only by the verifier as the oracle.

Semantic equality, not byte equality. Documented permanent exceptions, normalized away
on BOTH sides before comparison:
  - key order / whitespace (vanish on JSON parse),
  - the request's TOP-LEVEL `metadata` (carries user_id; session-level, deliberately
    not in the prelude). Only the top level: a `metadata` key nested inside tool
    input or message content is ordinary content and IS compared,
  - thinking text: the raw request carries the literal "<REDACTED>" (and the raw
    response ""), never the reasoning — both collapse to one placeholder,
  - `redacted_thinking` blocks compare as thinking-with-signature (the normalized
    store deliberately does not distinguish the two withheld forms),
  - string-form system/content vs the equivalent single text block,
  - the response `model`: the server resolves an alias to a dated id
    ("claude-haiku-4-5" -> "claude-haiku-4-5-20251001"), and the document keeps the
    model as REQUESTED, so the comparison is prefix-compatibility rather than
    equality. A model from a different family still fails, which is the point.

The response is compared on its content blocks AND on every field the document
represents: id (correlation.message_id), model, stop_reason, stop_sequence and
usage. Fields the document does not represent at all (`stop_details`, `container`,
`provider`, the response's own `diagnostics`) are outside the contract and are not
compared — extending the contract is what would change that, not loosening this.

Anything else that differs is a REAL capture gap and fails the round-trip.
"""

import json
from typing import Any

_THINK_PLACEHOLDER = "<WITHHELD>"


def object_to_block(
    obj: dict[str, Any], annotations: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Invert block_to_object: one stored object -> one wire content block.
    Content extras stored on the object (citations, caller, ...) flow back out."""
    kind = obj.get("kind")
    block: dict[str, Any]
    if kind == "text":
        block = {"type": "text", "text": obj.get("text", "")}
        _restore_extras(block, obj, "text")
    elif kind == "thinking":
        text = obj.get("text") if obj.get("persisted") == "raw" else _THINK_PLACEHOLDER
        block = {"type": "thinking", "thinking": text}
        if obj.get("signature") is not None:
            block["signature"] = obj["signature"]
    elif kind == "tool_use":
        block = {
            "type": "tool_use",
            "id": obj.get("id", ""),
            "name": obj.get("name", ""),
            "input": obj.get("input", {}),
        }
        _restore_extras(block, obj, "id", "name", "input")
    elif kind == "tool_result":
        block = {"type": "tool_result", "tool_use_id": obj.get("tool_use_id", "")}
        if "content" in obj:
            block["content"] = obj["content"]
        if "is_error" in obj:
            block["is_error"] = obj["is_error"]
        _restore_extras(block, obj, "tool_use_id", "content", "is_error")
    elif kind == "opaque":
        raw = obj.get("raw")
        block = dict(raw) if isinstance(raw, dict) else {"value": raw}
    else:
        raise ValueError(f"unknown object kind: {kind!r}")
    if annotations and "cache_control" in annotations:
        block["cache_control"] = annotations["cache_control"]
    return block


def _restore_extras(block: dict[str, Any], obj: dict[str, Any], *core: str) -> None:
    for k, v in obj.items():
        if k not in core and k != "kind":
            block[k] = v


def reconstruct_request(doc: dict[str, Any], inference: dict[str, Any]) -> dict[str, Any]:
    """The wire request, from the doc alone: params verbatim + model + system/tools
    from the prelude + messages regrouped from the stored context via message_index."""
    objs = doc["objects"]
    prelude = inference.get("prelude") or {}
    req: dict[str, Any] = dict(prelude.get("params") or {})
    req["model"] = inference["model"]

    system: list[dict[str, Any]] = []
    messages: dict[int, dict[str, Any]] = {}
    for entry in inference["context"]:
        block = object_to_block(objs[entry["ref"]], entry.get("annotations"))
        mi = entry.get("message_index")
        # message_index presence (not role!) separates prelude system[] from
        # messages[]: CC appends dynamic system context as a role-system MESSAGE
        # at the end of messages[] (deferred-tools / agent-types injections,
        # discovered 2026-07-02) so the cache-stable system[] never churns.
        if mi is None:
            system.append(block)
        else:
            slot = messages.setdefault(mi, {"role": entry["role"], "content": []})
            slot["content"].append(block)
    if system:
        req["system"] = system
    req["messages"] = [messages[i] for i in sorted(messages)]

    tools = []
    for ref in prelude.get("tool_refs") or []:
        td = objs[ref]
        tool = {k: v for k, v in td.items() if k != "kind"}
        tools.append(tool)
    if tools:
        req["tools"] = tools
    return req


def reconstruct_response_content(
    doc: dict[str, Any], inference: dict[str, Any]
) -> list[dict[str, Any]]:
    """The response's content blocks, from the doc alone (response_block_refs)."""
    objs = doc["objects"]
    return [object_to_block(objs[ref]) for ref in inference.get("response_block_refs") or []]


# usage keys the normalizer renamed on the way in (claude_code._USAGE_RENAME);
# everything else passed through untouched.
_USAGE_RENAME_BACK = {
    "cache_read_tokens": "cache_read_input_tokens",
    "cache_creation_tokens": "cache_creation_input_tokens",
}


def _denorm_usage(usage: dict[str, Any]) -> dict[str, Any]:
    """The doc's normalized usage back in wire field names."""
    return {_USAGE_RENAME_BACK.get(k, k): v for k, v in usage.items()}


def _drop_none(d: Any) -> dict[str, Any]:
    """Absent and explicitly-null are the same claim for usage counters."""
    return {k: v for k, v in d.items() if v is not None} if isinstance(d, dict) else {}


def reconstruct_response(doc: dict[str, Any], inference: dict[str, Any]) -> dict[str, Any]:
    """The response AS THE DOCUMENT REPRESENTS IT: content blocks plus the response
    fields the doc carries. stop_sequence is always null here — the doc has no
    field for it, so it asserts "no stop sequence"; a raw response that carries one
    is a real representation gap and the diff says so."""
    corr = inference.get("correlation") or {}
    return {
        "id": corr.get("message_id"),
        "model": inference.get("model"),
        "stop_reason": inference.get("stop_reason"),
        "stop_sequence": None,
        "usage": _denorm_usage(inference.get("usage") or {}),
        "content": reconstruct_response_content(doc, inference),
    }


# --- the verifier ------------------------------------------------------------------


def _canon(x: Any) -> Any:
    """Normalize away the documented permanent exceptions (both sides get this)."""
    if isinstance(x, dict):
        t = x.get("type")
        if t == "thinking":
            y = {k: _canon(v) for k, v in x.items()}
            if y.get("thinking") in ("", "<REDACTED>", _THINK_PLACEHOLDER):
                y["thinking"] = _THINK_PLACEHOLDER
            return y
        if t == "redacted_thinking":
            # compares as thinking-with-signature: the store deliberately does not
            # distinguish the two withheld forms.
            return {"type": "thinking", "thinking": _THINK_PLACEHOLDER, "signature": x.get("data")}
        return {k: _canon(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_canon(v) for v in x]
    return x


def _canon_request(req: dict[str, Any]) -> dict[str, Any]:
    r = dict(req)
    # TOP-LEVEL metadata only: it carries user_id, is session-level, and is
    # deliberately excluded from the prelude. Nested metadata is content.
    r.pop("metadata", None)
    if isinstance(r.get("system"), str):
        r["system"] = [{"type": "text", "text": r["system"]}]
    if r.get("tools") == []:  # an empty tools[] is semantically absent
        del r["tools"]
    msgs = []
    for m in r.get("messages", []) or []:
        c = m.get("content")
        if isinstance(c, str):
            m = dict(m, content=[{"type": "text", "text": c}])
        msgs.append(m)
    r["messages"] = msgs
    canon: dict[str, Any] = _canon(r)
    return canon


def _diff(a: Any, b: Any, path: str, out: list[str], limit: int = 20) -> None:
    if len(out) >= limit:
        return
    if type(a) is not type(b):
        out.append(f"{path}: type {type(a).__name__} != {type(b).__name__}")
        return
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"{path}.{k}: only in raw")
            elif k not in b:
                out.append(f"{path}.{k}: only in reconstruction")
            else:
                _diff(a[k], b[k], f"{path}.{k}", out, limit)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} != {len(b)}")
            return
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            _diff(x, y, f"{path}[{i}]", out, limit)
    elif a != b:
        out.append(f"{path}: {json.dumps(a)[:60]} != {json.dumps(b)[:60]}")


def semantic_diff_request(reconstructed: dict[str, Any], raw_request: bytes) -> list[str]:
    """[] iff the reconstruction is semantically identical to the wire request."""
    raw: dict[str, Any] = json.loads(raw_request)
    out: list[str] = []
    _diff(_canon_request(reconstructed), _canon_request(raw), "request", out)
    return out


def semantic_diff_response(reconstructed: dict[str, Any], raw_response: bytes) -> list[str]:
    """[] iff the doc's view of the response matches the wire response.

    Compares the content blocks AND the represented scalar fields — a doc that
    reproduces the text but loses the stop_reason, the message id or the token
    counts has a gap, and an oracle that only ever looked at `content` could not
    see it.
    """
    raw: dict[str, Any] = json.loads(raw_response)
    raw_content = raw.get("content")
    if isinstance(raw_content, str):
        raw_content = [{"type": "text", "text": raw_content}]
    out: list[str] = []
    _diff(
        _canon(reconstructed.get("content") or []),
        _canon(raw_content or []),
        "response.content",
        out,
    )
    for field in ("id", "stop_reason", "stop_sequence"):
        if reconstructed.get(field) != raw.get(field):
            out.append(f"response.{field}: {reconstructed.get(field)!r} != {raw.get(field)!r}")
    doc_model, raw_model = reconstructed.get("model"), raw.get("model")
    if not (
        doc_model == raw_model
        or (
            isinstance(doc_model, str)
            and isinstance(raw_model, str)
            and raw_model.startswith(doc_model)
        )
    ):
        out.append(f"response.model: doc has {doc_model!r}, wire says {raw_model!r}")
    _diff(
        _drop_none(reconstructed.get("usage")), _drop_none(raw.get("usage")), "response.usage", out
    )
    return out
