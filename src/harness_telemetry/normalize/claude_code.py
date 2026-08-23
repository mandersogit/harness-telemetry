#!/usr/bin/env python3
"""
claude_code.py — normalize a Claude Code capture into the events.schema.json contract.

Four channels (bodies, transcript, OTEL, hooks), joined by session_id / request_id, all additive and source-tagged so the
output degrades gracefully when a channel is absent:

  - bodies   (captures/<ts>/bodies/) -> objects + inference manifests (the assembled
             request/response per call; exact context).
  - transcript (~/.claude/projects/<cwd>/<sid>.jsonl, auto-discovered) -> message tree/DAG
             + the bookkeeping event zoo (harness_events).
  - OTEL     (captures/<ts>/otel/, from the collector sink) -> per-inference duration,
             prompt_id and cost_usd_reported, backfilled onto inference events by
             request_id; cost_usd itself is derived from tokens by pricing.py.

Load-bearing decisions:
  - objects are BLOCK-level, content-ONLY: cache_control lifted into the inference's
    context entry so identical content dedups across inferences as the breakpoint moves.
  - a thinking block keeps its `signature` as content-identity; redaction is per-channel
    ("<REDACTED>" in OTEL bodies, "" in transcript) -> both normalize to persisted "none".
  - correlation is a TUPLE (client_request_uuid + server_request_id + prompt_id).
  - raw bodies are an audit ring: referenced by hash, never inlined.

stdlib only. Usage:
    harness-normalize <capture-dir> [--out FILE] [--check]
                      [--transcript PATH | --no-transcript]
                      [--otel PATH | --no-otel]
"""

import argparse
import glob
import hashlib
import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from . import pairing, pricing  # derive cost from tokens, not Claude Code's number

SCHEMA_VERSION = "1.5"

# CC >= v2.1.198 embeds the PREVIOUS call's server request id in the system[0]
# billing header (cc_prev_req=req_...;) — per-request correlation riding inside
# system[], discovered live 2026-07-02. Stripped for lineage hashing (it varies
# every call), extracted as correlation + the strongest pairing chain (req_ ids
# ARE response filenames).
_PREV_REQ_RE = re.compile(r"\s*cc_prev_req=([^;]+);")
_BILLING_MARKER = "x-anthropic-billing-header:"


def discount_arg(value: str) -> float:
    """argparse `type=` for --discount: pricing.parse_discount's one rule, with
    argparse's own error path (exit 2) instead of a traceback. Shared with
    watch.py so the CLI flag and HARNESS_COST_DISCOUNT can never diverge."""
    try:
        return pricing.parse_discount(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def sha256_obj(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def block_to_object(block: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Map one API content block to a content-ONLY object (no cache_control).
    Returns (object_dict, annotations_dict)."""
    annotations: dict[str, Any] = {}
    if isinstance(block, dict) and "cache_control" in block:
        annotations["cache_control"] = block["cache_control"]

    if isinstance(block, str):
        return {"kind": "text", "text": block}, annotations

    def _extras(obj: dict[str, Any], *core: str) -> dict[str, Any]:
        # Content fidelity: unknown fields on a known block kind (e.g. a text
        # block's `citations`) are CONTENT and pass through into the object.
        for k, v in block.items():
            if k not in core and k not in ("type", "cache_control"):
                obj[k] = v
        return obj

    btype = block.get("type")
    if btype == "text":
        return _extras({"kind": "text", "text": block.get("text", "")}, "text"), annotations
    if btype == "thinking":
        text = block.get("thinking") or ""
        # Claude Code redacts reasoning per-channel: "" in the transcript, the literal
        # "<REDACTED>" in OTEL request bodies. Either way no reasoning survived -> "none"
        # (the two representations of the same turn then dedup via the shared signature).
        persisted = "none" if text.strip() in ("", "<REDACTED>") else "raw"
        return (
            {
                "kind": "thinking",
                "persisted": persisted,
                "text": block.get("thinking") if persisted == "raw" else None,
                "signature": block.get("signature"),
            },
            annotations,
        )
    if btype == "redacted_thinking":
        return (
            {"kind": "thinking", "persisted": "none", "text": None, "signature": block.get("data")},
            annotations,
        )
    if btype == "tool_use":
        return (
            _extras(
                {
                    "kind": "tool_use",
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "input": block.get("input", {}),
                },
                "id",
                "name",
                "input",
            ),
            annotations,
        )
    if btype == "tool_result":
        obj: dict[str, Any] = {"kind": "tool_result", "tool_use_id": block.get("tool_use_id", "")}
        if "content" in block:
            obj["content"] = block["content"]
        if "is_error" in block:
            obj["is_error"] = bool(block["is_error"])
        return _extras(obj, "tool_use_id", "content", "is_error"), annotations
    # Unrecognized block type (e.g. image): a MARKED opaque object, never a silent
    # text coercion — the loss of interpretation stays visible, the block verbatim.
    # cache_control is still a per-request annotation, lifted out like every other
    # kind (else the same image hashes differently once a breakpoint lands on it).
    if isinstance(block, dict):
        raw = {k: v for k, v in block.items() if k != "cache_control"}
    else:
        raw = {"value": block}
    return (
        {"kind": "opaque", "block_type": btype if isinstance(btype, str) else None, "raw": raw},
        annotations,
    )


_USAGE_RENAME = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_input_tokens": "cache_read_tokens",
    "cache_creation_input_tokens": "cache_creation_tokens",
}


def norm_usage(usage: Any) -> dict[str, Any]:
    if not isinstance(usage, dict):
        return {}
    out = {dst: usage.get(src) for src, dst in _USAGE_RENAME.items()}
    # Every harness-specific extra passes through (schema: additionalProperties true).
    for k, v in usage.items():
        if k not in _USAGE_RENAME:
            out[k] = v
    return out


def _as_blocks(content: Any) -> list[Any]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return list(content or [])


def load_request(path: str) -> dict[str, Any]:
    """Parse one request body into a pairing record + everything emit needs.

    The lineage key (prelude_hash) covers model + content-only system + tool defs
    + params: byte-stable within a lineage (measured — cache_control moves are
    stripped by block_to_object), distinct across main/sub-agent/background.
    """
    with open(path, "rb") as f:
        raw = f.read()
    data: dict[str, Any] = json.loads(raw)
    model = data.get("model")

    sysblocks = _as_blocks(data.get("system"))
    system_objs = [block_to_object(b) for b in sysblocks]

    # Lineage-hash view of the system blocks: the billing header with the
    # per-request cc_prev_req token stripped (content objects stay verbatim).
    prev_req_id: str | None = None
    system_hash_objs: list[dict[str, Any]] = []
    for obj, _ann in system_objs:
        text = obj.get("text")
        if obj.get("kind") == "text" and isinstance(text, str) and text.startswith(_BILLING_MARKER):
            m = _PREV_REQ_RE.search(text)
            if m:
                prev_req_id = m.group(1)
                obj = dict(obj, text=_PREV_REQ_RE.sub("", text))
        system_hash_objs.append(obj)
    tool_objs: list[dict[str, Any]] = []
    for t in data.get("tools", []) or []:
        # Every tool field passes through (e.g. eager_input_streaming) — the
        # round-trip oracle is the arbiter of what matters, not a whitelist.
        td: dict[str, Any] = {"kind": "tool_def", "name": t.get("name", "")}
        for k, v in t.items():
            if k != "name":
                td[k] = v
        tool_objs.append(td)
    params = {
        k: v
        for k, v in data.items()
        if k not in ("system", "messages", "tools", "metadata", "model")
    }
    # diagnostics.previous_message_id (discovered 2026-07-02) is per-request
    # CORRELATION riding in the params: the msg_... id of the lineage's previous
    # response. Kept verbatim in params (round-trip exactness) but excluded from
    # the lineage hash (it varies every call) and surfaced for exact pairing.
    prev_msg_id = (
        (params.get("diagnostics") or {}).get("previous_message_id")
        if isinstance(params.get("diagnostics"), dict)
        else None
    )

    messages = data.get("messages", []) or []
    # Two content views the pairer needs. assistant_hashes answers "which response
    # fed this history"; msg_hashes (ALL roles) answers "which request's history
    # does this one continue" — the discriminator between two same-prelude agents
    # running in parallel, whose assistant histories can be identical (both empty
    # at the first turn) while their user turns differ.
    assistant_hashes: set[str] = set()
    msg_hashes: set[str] = set()
    for msg in messages:
        is_assistant = msg.get("role") == "assistant"
        for b in _as_blocks(msg.get("content")):
            h = sha256_obj(block_to_object(b)[0])
            msg_hashes.add(h)
            if is_assistant:
                assistant_hashes.add(h)

    prelude_hash = sha256_obj(
        {
            "model": model,
            "system": [sha256_obj(o) for o in system_hash_objs],
            "tools": [sha256_obj(t) for t in tool_objs],
            "params": {k: v for k, v in params.items() if k != "diagnostics"},
        }
    )
    return {
        "path": path,
        "mtime": os.path.getmtime(path),
        "raw": raw,
        "data": data,
        "model": model,
        "nmsg": len(messages),
        "size": len(raw),
        "prelude_hash": prelude_hash,
        "assistant_hashes": assistant_hashes,
        "msg_hashes": msg_hashes,
        "prev_msg_id": prev_msg_id,
        "prev_req_id": prev_req_id,
        "system_objs": system_objs,
        "tool_objs": tool_objs,
        "params": params,
    }


def load_response(path: str) -> dict[str, Any]:
    """Parse one response body into a pairing record."""
    with open(path, "rb") as f:
        raw = f.read()
    data: dict[str, Any] = json.loads(raw)
    usage = data.get("usage") or {}
    blocks = [block_to_object(b)[0] for b in _as_blocks(data.get("content"))]
    input_total = sum(
        usage.get(k) or 0
        for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    return {
        "path": path,
        "mtime": os.path.getmtime(path),
        "raw": raw,
        "data": data,
        "model": data.get("model"),
        "msg_id": data.get("id"),
        "block_objs": blocks,
        "block_hashes": [sha256_obj(o) for o in blocks],
        "input_total": input_total,
    }


def load_bodies(bodies_dir: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    reqs = [load_request(p) for p in sorted(glob.glob(os.path.join(bodies_dir, "*.request.json")))]
    resps = [
        load_response(p) for p in sorted(glob.glob(os.path.join(bodies_dir, "*.response.json")))
    ]
    return reqs, resps


def _classify_request(req: dict[str, Any]) -> str | None:
    """Background-call signatures, PER CALL (same heuristics as bin/captures
    CLASSIFY). Per-call because suggestion-mode calls chain into the main
    thread's history (their prev_msg names main's responses) and share its
    prelude — component membership cannot separate them; their own last user
    message can (2026-07-02, found when chain components merged them)."""
    sys_text = " ".join(o.get("text", "") for o, _ in req["system_objs"] if o.get("kind") == "text")
    if "sentence-case title" in sys_text:
        return "background:title"
    last_user_text = ""
    for msg in req["data"].get("messages", []) or []:
        if msg.get("role") == "user":
            for b in _as_blocks(msg.get("content")):
                if isinstance(b, dict) and b.get("type") == "text":
                    last_user_text = b.get("text", "")
                elif isinstance(b, str):
                    last_user_text = b
    if last_user_text.lstrip().startswith("[SUGGESTION MODE"):
        return "background:suggest"
    return None


def parse_user_id(req: dict[str, Any]) -> dict[str, Any]:
    uid = (req.get("metadata") or {}).get("user_id")
    if isinstance(uid, str):
        try:
            parsed: dict[str, Any] = json.loads(uid)
            return parsed
        except (ValueError, TypeError):
            pass
    return {}


def find_transcript(session_id: str | None) -> str | None:
    if not session_id:
        return None
    hits = glob.glob(os.path.expanduser(f"~/.claude/projects/*/{session_id}.jsonl"))
    return hits[0] if hits else None


def ingest_transcript(
    path: str, put: Callable[[dict[str, Any]], str]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Message events (conversation tree/DAG) + harness_events for the bookkeeping zoo,
    into the shared object store. Bookkeeping content is NOT inlined.

    CC writes one record PER CONTENT BLOCK (a storage quirk — the wire groups them
    into one message). Consecutive assistant records sharing a requestId regroup
    into one multi-block message event. Node-identity rule (verified: block records
    chain linearly and children parent off the LAST record): the merged node keeps
    the last record's uuid; its inbound edge is the first record's parentUuid; all
    merged uuids are retained in `uuids`.

    MISSING uuid POLICY: `message.uuid` is a REQUIRED string in the contract, and a
    transcript record without one must not be dropped (that would lose content) nor
    emitted as null (that would produce an invalid doc). Such a record gets a
    synthesized placeholder, `synthetic-<12 hex>` over (line number, record) — a
    deterministic function of the input, so the same transcript always yields the
    same node id and incremental == batch still holds. It is marked by its prefix
    and can never collide with a real reference: nothing in the file can name a
    uuid the record itself did not carry.
    """
    events: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    with open(path) as f:
        for lineno, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            rec_uuid = rec.get("uuid")
            if not isinstance(rec_uuid, str) or not rec_uuid:
                rec_uuid = "synthetic-" + sha256_obj({"line": lineno, "rec": rec})[:12]
            rtype = rec.get("type")
            for k_src, k_dst in (
                ("entrypoint", "entrypoint"),
                ("cwd", "cwd"),
                ("version", "harness_version"),
            ):
                if k_dst not in meta and rec.get(k_src):
                    meta[k_dst] = rec[k_src]
            if rtype in ("user", "assistant"):
                msg = rec.get("message", {}) or {}
                block_refs = [put(block_to_object(b)[0]) for b in _as_blocks(msg.get("content"))]
                rid = rec.get("requestId")
                prev = events[-1] if events else None
                if (
                    rtype == "assistant"
                    and rid is not None
                    and prev is not None
                    and prev["type"] == "message"
                    and prev["role"] == "assistant"
                    and prev.get("request_id") == rid
                ):
                    # regroup: same call, next block record
                    prev["block_refs"].extend(block_refs)
                    prev["uuids"].append(rec_uuid)
                    prev["uuid"] = rec_uuid  # last record's uuid = node id
                    continue
                events.append(
                    {
                        "type": "message",
                        "ts": rec.get("timestamp"),
                        "source": "transcript",
                        "uuid": rec_uuid,
                        "uuids": [rec_uuid],
                        "parent_uuid": rec.get("parentUuid"),
                        "role": msg.get("role") or ("user" if rtype == "user" else "assistant"),
                        "is_sidechain": bool(rec.get("isSidechain", False)),
                        "block_refs": block_refs,
                        "request_id": rid,
                    }
                )
            else:
                # A record's own inner subtype matters (e.g. system/turn_duration,
                # system/model_refusal_fallback — Claude Code can switch model
                # MID-THREAD on a refusal/safeguard fallback, and the transcript
                # records that switch under its own subtype). Compose it in.
                sub = rtype or "unknown"
                if rec.get("subtype"):
                    sub = f"{sub}:{rec['subtype']}"
                ev: dict[str, Any] = {
                    "type": "harness_event",
                    "ts": rec.get("timestamp"),
                    "source": "transcript",
                    "subtype": sub,
                    "uuid": rec.get("uuid"),
                    "parent_uuid": rec.get("parentUuid"),
                }
                # A silent model switch (safeguard/refusal fallback) carries the
                # from->to models; keep them so provenance is auditable, not lost.
                if rec.get("originalModel") and rec.get("fallbackModel"):
                    ev["data"] = {
                        "original_model": rec["originalModel"],
                        "fallback_model": rec["fallbackModel"],
                        "trigger": rec.get("trigger"),
                        "request_id": rec.get("requestId"),
                    }
                events.append(ev)
    return events, meta


def ingest_subagent_tree(tpath: str, put: Callable[[dict[str, Any]], str]) -> list[dict[str, Any]]:
    """The transcript's SIBLING TREE — `<sid>/` next to `<sid>.jsonl`
    (layout verified on CC 2.1.198):

        <sid>/subagents/agent-<aid>.jsonl (+ .meta.json)          per sub-agent
        <sid>/subagents/workflows/<wf>/agent-*.jsonl (+ .meta.json, journal.jsonl)
        <sid>/workflows/<wf>.json                                  run record incl.
                                                                   the full SCRIPT
        <sid>/workflows/scripts/*.js   (not read — <wf>.json.script holds the source)

    Sub-agent records are the same shape as main-transcript records (plus
    agentId), so they reuse ingest_transcript wholesale (regrouping included);
    every resulting event is tagged agent_id (+ workflow_id under workflows/).
    Identity/linkage arrives as harness_events: `subagent_meta` (the .meta.json
    verbatim — its toolUseId is the DAG splice point to the spawning Task
    tool_use), `workflow_meta` (<wf>.json verbatim), `workflow_journal` (one
    per journal line). Content dedups into the shared object store; sub-agent
    CONTENT was already exact from bodies — this adds the per-agent message
    DAG and the spawn/workflow structure."""
    sid_dir = tpath[: -len(".jsonl")]
    if not os.path.isdir(sid_dir):
        return []
    events: list[dict[str, Any]] = []
    sub_glob = sorted(
        glob.glob(os.path.join(sid_dir, "subagents", "agent-*.jsonl"))
        + glob.glob(os.path.join(sid_dir, "subagents", "workflows", "*", "agent-*.jsonl"))
    )
    for jsonl in sub_glob:
        aid = os.path.basename(jsonl)[len("agent-") : -len(".jsonl")]
        parent = os.path.dirname(jsonl)
        wf = (
            os.path.basename(parent)
            if os.path.basename(os.path.dirname(parent)) == "workflows"
            else None
        )
        sub_events, _ = ingest_transcript(jsonl, put)
        for e in sub_events:
            e["agent_id"] = aid
            if wf:
                e["workflow_id"] = wf
        data: dict[str, Any] = {"agent_id": aid}
        if wf:
            data["workflow_id"] = wf
        meta_path = jsonl[: -len(".jsonl")] + ".meta.json"
        if os.path.exists(meta_path):
            with open(meta_path) as f:
                data.update(json.load(f))
        first_ts = next((e["ts"] for e in sub_events if e.get("ts")), None)
        events.append(
            {
                "type": "harness_event",
                "ts": first_ts,
                "source": "transcript",
                "subtype": "subagent_meta",
                "uuid": None,
                "parent_uuid": None,
                "data": data,
            }
        )
        events.extend(sub_events)
    for wfjson in sorted(glob.glob(os.path.join(sid_dir, "workflows", "*.json"))):
        with open(wfjson) as f:
            wdata = json.load(f)
        ts = wdata.get("timestamp") if isinstance(wdata.get("timestamp"), str) else None
        wf_id = os.path.basename(wfjson)[: -len(".json")]
        events.append(
            {
                "type": "harness_event",
                "ts": ts,
                "source": "transcript",
                "subtype": "workflow_meta",
                "uuid": None,
                "parent_uuid": None,
                "data": {"workflow_id": wf_id, "run": wdata},
            }
        )
        journal = os.path.join(sid_dir, "subagents", "workflows", wf_id, "journal.jsonl")
        if os.path.exists(journal):
            with open(journal) as f:
                for line in f:
                    if line.strip():
                        events.append(
                            {
                                "type": "harness_event",
                                "ts": None,
                                "source": "transcript",
                                "subtype": "workflow_journal",
                                "uuid": None,
                                "parent_uuid": None,
                                "data": {"workflow_id": wf_id, "record": json.loads(line)},
                            }
                        )
    return events


# --- OTEL channel -----------------------------------------------------------------


def _aval(v: Any) -> Any:
    if not isinstance(v, dict):
        return None
    for k in ("stringValue", "boolValue", "intValue", "doubleValue"):
        if k in v:
            return v[k]
    if isinstance(v.get("kvlistValue"), dict):
        return _attrs(v["kvlistValue"].get("values"))
    if isinstance(v.get("arrayValue"), dict):
        vals = v["arrayValue"].get("values")
        return [_aval(x) for x in (vals if isinstance(vals, list) else [])]
    return None


def _attrs(lst: Any) -> dict[str, Any]:
    """OTLP key/value attribute list -> plain dict. Tolerant: an entry that is not
    a {key, value} object is skipped rather than raising (a malformed record must
    not take the whole capture down)."""
    out: dict[str, Any] = {}
    for a in lst if isinstance(lst, list) else []:
        if isinstance(a, dict) and isinstance(a.get("key"), str):
            out[a["key"]] = _aval(a.get("value"))
    return out


def _to_int(x: Any) -> int | None:
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _to_float(x: Any) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def ingest_hooks(hooks_file: str) -> list[dict[str, Any]]:
    """The hook channel (v1.3): hookrec.py's hooks.jsonl -> verbatim 'hook' events.
    A line that fails to parse is SKIPPED here; conservation counts lines vs
    events and raises the mismatch loudly (never a silent drop)."""
    try:
        with open(hooks_file) as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        payload = rec.get("payload")
        events.append(
            {
                "type": "hook",
                "ts": rec.get("received_at"),
                "source": "hook",
                "subtype": rec.get("event"),
                "payload": payload if isinstance(payload, dict) else {"raw": payload},
            }
        )
    return events


def find_otel(capture_dir: str) -> str | None:
    d = os.path.join(capture_dir, "otel")
    return d if os.path.isdir(d) else None


def _iso(mtime: float) -> str:
    """File mtime -> the ISO-8601 Zulu shape the transcript/OTEL timestamps use."""
    return datetime.fromtimestamp(mtime, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def order_events(channels: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """A defined total order instead of channel concatenation.

    Sort by ts (ISO-Zulu strings sort lexically); events without a ts inherit the
    nearest preceding ts within their own channel (leading ones sort first);
    tiebreaks are (channel rank, within-channel position) — deterministic, and
    message-less calls land when they happened instead of at the end of the log.
    """
    keyed: list[tuple[str, int, int, dict[str, Any]]] = []
    for rank, channel in enumerate(channels):
        carry = ""
        for pos, ev in enumerate(channel):
            if ev.get("ts"):
                carry = ev["ts"]
            keyed.append((carry, rank, pos, ev))
    keyed.sort(key=lambda k: (k[0], k[1], k[2]))
    return [dict(ev, seq=i) for i, (_, _, _, ev) in enumerate(keyed)]


# Resource-level / content attrs never copied into harness_event data.
_OTEL_DATA_SKIP_PREFIXES = ("user.", "organization.", "session.", "terminal.", "event.", "app.")
_OTEL_DATA_SKIP_KEYS = {"body", "prompt", "tool_input", "tool_parameters"}


def _otel_data(a: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v
        for k, v in a.items()
        if not k.startswith(_OTEL_DATA_SKIP_PREFIXES) and k not in _OTEL_DATA_SKIP_KEYS
    }


def otel_log_records(o: Any) -> tuple[list[dict[str, Any]], str | None]:
    """Walk one parsed OTLP/JSON logs document down to its logRecords.

    Returns (records, reason-it-is-not-one). A file that is syntactically valid
    JSON but structurally not an OTLP logs export (`{}`, `resourceLogs: null`,
    a list where an object belongs) is REJECTED WHOLE with a reason rather than
    half-ingested or allowed to raise — the caller counts rejects so a capture
    gap can never pass as "no telemetry".
    """
    if not isinstance(o, dict):
        return [], f"top level is {type(o).__name__}, not an object"
    rls = o.get("resourceLogs")
    if rls is None:
        return [], "no resourceLogs key"
    if not isinstance(rls, list):
        return [], f"resourceLogs is {type(rls).__name__}, not an array"
    out: list[dict[str, Any]] = []
    for rl in rls:
        if not isinstance(rl, dict):
            return [], f"resourceLogs[] holds {type(rl).__name__}, not objects"
        sls = rl.get("scopeLogs") or []
        if not isinstance(sls, list):
            return [], f"scopeLogs is {type(sls).__name__}, not an array"
        for sl in sls:
            if not isinstance(sl, dict):
                return [], f"scopeLogs[] holds {type(sl).__name__}, not objects"
            lrs = sl.get("logRecords") or []
            if not isinstance(lrs, list):
                return [], f"logRecords is {type(lrs).__name__}, not an array"
            for lr in lrs:
                if not isinstance(lr, dict):
                    return [], f"logRecords[] holds {type(lr).__name__}, not objects"
                out.append(lr)
    return out, None


def rejected_otel_files(otel_dir: str) -> list[tuple[str, str]]:
    """(basename, reason) for every logs-*.json in otel_dir that ingestion skips."""
    bad: list[tuple[str, str]] = []
    for fpath in sorted(glob.glob(os.path.join(otel_dir, "logs-*.json"))):
        try:
            with open(fpath) as f:
                o = json.load(f)
        except OSError as e:
            bad.append((os.path.basename(fpath), f"unreadable: {e.strerror}"))
            continue
        except ValueError as e:
            bad.append((os.path.basename(fpath), f"not valid JSON: {e}"))
            continue
        _, reason = otel_log_records(o)
        if reason:
            bad.append((os.path.basename(fpath), reason))
    return bad


def ingest_otel(
    otel_dir: str,
) -> tuple[
    dict[str, dict[str, Any]],
    list[dict[str, Any]],
    bool,
    dict[str, dict[str, Any]],
    list[tuple[str, str]],
]:
    """Parse the OTLP/JSON logs the sink wrote.

    Returns (by_request, extra_events, has_metrics, body_index, rejected).
    body_index maps a body FILE basename -> {sequence, prompt_id} from the
    api_request_body / api_response_body events: the OTEL->file join (body_ref)
    plus the exporter's monotonic event.sequence — the native ordering/pairing
    anchor. Verified: in OTEL_LOG_RAW_API_BODIES=file: mode these events carry NO
    inline body attr, only body_ref — the file sink is the sole body source.

    `rejected` is (file, reason) for every logs-*.json that was NOT ingested —
    unreadable, not JSON, or not an OTLP logs export. Ingestion never raises on a
    malformed file, but the skip is never silent either: check_invariants counts
    these and reports them (a dropped telemetry file is a capture gap).
    """
    by_request: dict[str, dict[str, Any]] = {}
    extra: list[dict[str, Any]] = []
    body_index: dict[str, dict[str, Any]] = {}
    rejected: list[tuple[str, str]] = []
    for fpath in sorted(glob.glob(os.path.join(otel_dir, "logs-*.json"))):
        try:
            with open(fpath) as f:
                o = json.load(f)
        except OSError as e:
            rejected.append((os.path.basename(fpath), f"unreadable: {e.strerror}"))
            continue
        except ValueError as e:
            rejected.append((os.path.basename(fpath), f"not valid JSON: {e}"))
            continue
        records, reason = otel_log_records(o)
        if reason:
            rejected.append((os.path.basename(fpath), reason))
            continue
        for lr in records:
            a = _attrs(lr.get("attributes") or [])
            body = lr.get("body", {})
            name = a.get("event.name") or (
                body.get("stringValue") if isinstance(body, dict) else None
            )
            ts = a.get("event.timestamp")
            seq = _to_int(a.get("event.sequence"))
            short = (name or "").split(".")[-1]
            if short == "api_request":
                rid = a.get("request_id")
                if rid:
                    by_request[rid] = {
                        "cost_usd": _to_float(a.get("cost_usd")),
                        "duration_ms": _to_int(a.get("duration_ms")),
                        "prompt_id": a.get("prompt.id"),
                        "agent_name": a.get("agent.name"),
                        "sequence": seq,
                        "ts": ts,
                    }
            elif short in ("api_request_body", "api_response_body"):
                ref = a.get("body_ref")
                if ref:
                    body_index[os.path.basename(ref)] = {
                        "sequence": seq,
                        "prompt_id": a.get("prompt.id"),
                    }
            elif short == "tool_decision":
                extra.append(
                    {
                        "type": "tool_decision",
                        "ts": ts,
                        "source": "otel_event",
                        "otel_seq": seq,
                        "tool_name": a.get("tool_name") or "unknown",
                        "tool_use_id": a.get("tool_use_id"),
                        "decision": a.get("decision") or "unknown",
                        "decision_source": a.get("source"),
                    }
                )
            elif short == "compaction":
                extra.append(
                    {
                        "type": "compaction",
                        "ts": ts,
                        "source": "otel_event",
                        "otel_seq": seq,
                        "trigger": a.get("trigger"),
                        "pre_tokens": _to_int(a.get("pre_tokens")),
                        "post_tokens": _to_int(a.get("post_tokens")),
                        "success": a.get("success") in (True, "true"),
                    }
                )
            elif short in ("api_error", "api_refusal"):
                extra.append(
                    {
                        "type": short,
                        "ts": ts,
                        "source": "otel_event",
                        "otel_seq": seq,
                        "correlation": {
                            "client_request_uuid": None,
                            "server_request_id": a.get("request_id"),
                            "prompt_id": a.get("prompt.id"),
                        },
                        "status_code": _to_int(a.get("status_code")),
                        "error": a.get("error"),
                        "attempt": _to_int(a.get("attempt")),
                        "duration_ms": _to_int(a.get("duration_ms")),
                    }
                )
            elif short == "user_prompt":
                extra.append(
                    {
                        "type": "user_prompt",
                        "ts": ts,
                        "source": "otel_event",
                        "otel_seq": seq,
                        "prompt_id": a.get("prompt.id"),
                        "command_name": a.get("command_name"),
                    }
                )
            elif short:
                # The rest of the zoo (tool_result, assistant_response,
                # mcp_server_connection, hook_*, ...) rides through as
                # harness_events — hook TELEMETRY lands in the log with
                # zero hook injection. Content attrs are stripped.
                extra.append(
                    {
                        "type": "harness_event",
                        "ts": ts,
                        "source": "otel_event",
                        "subtype": short,
                        "otel_seq": seq,
                        "data": _otel_data(a),
                    }
                )
    extra.sort(key=lambda e: (e.get("otel_seq") is None, e.get("otel_seq", 0)))
    has_metrics = bool(glob.glob(os.path.join(otel_dir, "metrics-*.json")))
    return by_request, extra, has_metrics, body_index, rejected


def normalize(
    capture_dir: str | Sequence[str],
    transcript: str | None = "auto",
    otel: str | None = "auto",
    discount: float | None = None,
) -> dict[str, Any]:
    # Resume lifecycle: a LOGICAL session may span several capture dirs —
    # an original run plus its `-r`/`--resume` continuations, all sharing one
    # session_id. Phase 1 (load + OTEL + pairing) runs PER CAPTURE, because its
    # anchors are per-run (event.sequence restarts every run; the otel window
    # replays one exporter's order). Phases 3+ run over the union: the chain
    # fields (cc_prev_req / previous_message_id) name responses by globally
    # unique ids, so lineage connects across the capture boundary by itself.
    dirs = [capture_dir] if isinstance(capture_dir, str) else list(capture_dir)
    if not dirs:
        raise ValueError("normalize() needs at least one capture dir")
    if otel not in ("auto", None) and len(dirs) > 1:
        raise ValueError("--otel DIR is ambiguous with multiple captures (use auto)")
    cap_ids = [os.path.basename(os.path.abspath(d)) for d in dirs]
    if len(set(cap_ids)) != len(cap_ids):
        raise ValueError(f"duplicate capture ids in {cap_ids}")

    objects: dict[str, dict[str, Any]] = {}
    raw_bodies: dict[str, dict[str, Any]] = {}

    def put(obj: dict[str, Any]) -> str:
        h = sha256_obj(obj)
        objects.setdefault(h, obj)
        return h

    # --- phase 1, per capture: load bodies + ingest OTEL (its body_index feeds
    # exact pairing), pair within the capture ---
    pairs: list[tuple[dict[str, Any], dict[str, Any] | None, str | None]] = []
    orphan_responses: list[dict[str, Any]] = []
    by_request: dict[str, dict[str, Any]] = {}
    otel_events: list[dict[str, Any]] = []
    hook_events: list[dict[str, Any]] = []
    body_index: dict[str, dict[str, Any]] = {}
    has_metrics = has_otel = False
    n_request_bodies = 0
    sid_of_cap: dict[str, str] = {}
    account_uuid: str | None = None
    first_mtime_of_cap: dict[str, float] = {}
    for d, cap_id in zip(dirs, cap_ids, strict=True):
        requests, responses = load_bodies(os.path.join(d, "bodies"))
        n_request_bodies += len(requests)
        hook_events.extend(ingest_hooks(os.path.join(d, "hooks", "hooks.jsonl")))
        by_request_d: dict[str, dict[str, Any]] = {}
        otel_events_d: list[dict[str, Any]] = []
        body_index_d: dict[str, dict[str, Any]] = {}
        opath = find_otel(d) if otel == "auto" else otel
        if opath and os.path.isdir(opath):
            (by_request_d, otel_events_d, has_metrics_d, body_index_d, _rejected_d) = ingest_otel(
                opath
            )
            has_metrics = has_metrics or has_metrics_d
            has_otel = has_otel or bool(by_request_d or otel_events_d)

        # Call-start times from OTEL (api_request ts - duration): the same-model
        # window disambiguator (a request file's mtime marks when the call began).
        start_time_of: dict[str, float] = {}
        for rid, meta_ in by_request_d.items():
            if meta_.get("ts") and meta_.get("duration_ms") is not None:
                try:
                    end = datetime.strptime(meta_["ts"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                        tzinfo=UTC
                    )
                    start_time_of[rid] = end.timestamp() - meta_["duration_ms"] / 1000.0
                except ValueError:
                    pass

        pairs_d, orphans_d = pairing.pair(requests, responses, body_index_d, start_time_of)
        for req, resp, _m in pairs_d:
            req["cap"] = cap_id
            req["relpath"] = os.path.relpath(req["path"], d)
            if resp is not None:
                resp["cap"] = cap_id
                resp["relpath"] = os.path.relpath(resp["path"], d)
        for resp in orphans_d:
            resp["cap"] = cap_id
            resp["relpath"] = os.path.relpath(resp["path"], d)
        pairs.extend(pairs_d)
        orphan_responses.extend(orphans_d)
        by_request.update(by_request_d)
        body_index.update(body_index_d)
        otel_events.extend(otel_events_d)

        for req in sorted(requests, key=lambda r: r["mtime"]):
            ids = parse_user_id(req["data"])
            if ids.get("session_id"):
                sid_of_cap[cap_id] = ids["session_id"]
                if account_uuid is None:
                    account_uuid = ids.get("account_uuid")
                break
        if requests:
            first_mtime_of_cap[cap_id] = min(r["mtime"] for r in requests)

    distinct_sids = sorted(set(sid_of_cap.values()))
    if len(distinct_sids) > 1:
        raise ValueError(f"captures belong to different sessions, refusing to merge: {sid_of_cap}")
    session_id: str | None = distinct_sids[0] if distinct_sids else None

    # One global timeline: pairs mtime-ordered across captures (comp_root and
    # index/parent_index depend on it); captures listed chronologically so the
    # doc is identical regardless of argument order.
    pairs.sort(key=lambda t: t[0]["mtime"])
    orphan_responses.sort(key=lambda r: r["mtime"])
    hook_events.sort(key=lambda e: e.get("ts") or "")
    captures_chrono = sorted(cap_ids, key=lambda c: (first_mtime_of_cap.get(c, float("inf")), c))

    # --- phase 2: transcript (needed for lineage labeling below) ---
    transcript_events: list[dict[str, Any]] = []
    tmeta: dict[str, Any] = {}
    tpath = find_transcript(session_id) if transcript == "auto" else transcript
    if tpath and os.path.exists(tpath):
        transcript_events, tmeta = ingest_transcript(tpath, put)
        transcript_events += ingest_subagent_tree(tpath, put)
    has_transcript = any(e["type"] == "message" for e in transcript_events)

    # --- phase 3: lineages are CHAIN COMPONENTS (verified on a live interactive
    # capture, 2026-07-02):
    # CC 2.1.198 churns the prelude mid-thread (main's tool set grew 12->13 after
    # call 1), so prelude-hash grouping is version-false. The truth is the chain
    # fields: a request names its predecessor's response via cc_prev_req (file id)
    # and diagnostics.previous_message_id (msg id). Connected components over
    # those edges are the lineages; prelude hash groups only chainless requests
    # (pre-2.1.19x captures, failed captures).
    producer_by_rid: dict[str, dict[str, Any]] = {}
    producer_by_msg: dict[str, dict[str, Any]] = {}
    for req, resp, _method in pairs:
        if resp is not None:
            rid = os.path.basename(resp["path"])[: -len(".response.json")]
            producer_by_rid[rid] = req
            if resp.get("msg_id"):
                producer_by_msg[resp["msg_id"]] = req

    chain_parent: dict[str, dict[str, Any]] = {}
    for req, _resp, _method in pairs:
        parent = None
        if req.get("prev_req_id"):
            parent = producer_by_rid.get(req["prev_req_id"])
        if parent is None and req.get("prev_msg_id"):
            parent = producer_by_msg.get(req["prev_msg_id"])
        if parent is not None:
            chain_parent[req["path"]] = parent

    # Union-find over requests: chain edges join a request to its predecessor;
    # requests with NO chain fields at all (background suggestion calls, failed
    # captures, pre-diagnostics CC) join whatever shares their prelude hash.
    # A suggestion call thus lands INSIDE the main component — which is true
    # (it is a branch of main's history); its label is per-call (below).
    uf: dict[str, str] = {req["path"]: req["path"] for req, _r, _m in pairs}

    def _find(x: str) -> str:
        while uf[x] != x:
            uf[x] = uf[uf[x]]
            x = uf[x]
        return x

    def _union(a: str, b: str) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            uf[ra] = rb

    by_hash: dict[str, str] = {}
    min_nmsg: dict[str, int] = {}
    for req, _resp, _method in pairs:
        by_hash.setdefault(req["prelude_hash"], req["path"])
        h: str = req["prelude_hash"]
        min_nmsg[h] = min(min_nmsg.get(h, req["nmsg"]), req["nmsg"])
    # A chainless request at its prelude group's MINIMUM depth is a fresh
    # agent-instance root and keeps its own component — else two parallel
    # same-type agents merge (observed: 2 workflow + 2 Task agents,
    # 2026-07-02). A chainless request DEEPER than the group minimum is
    # mid-thread (suggestion branches, calls missing diagnostics) and joins
    # its prelude group.
    for req, _resp, _method in pairs:
        p = chain_parent.get(req["path"])
        if p is not None:
            _union(req["path"], p["path"])
        elif (
            not req.get("prev_req_id")
            and not req.get("prev_msg_id")
            and req["nmsg"] > min_nmsg[req["prelude_hash"]]
        ):
            _union(req["path"], by_hash[req["prelude_hash"]])

    comp_of = {req["path"]: _find(req["path"]) for req, _r, _m in pairs}
    comp_root: dict[str, dict[str, Any]] = {}
    for req, _resp, _method in pairs:  # pairs are mtime-ordered: first member = root
        comp_root.setdefault(comp_of[req["path"]], req)

    # Main-lineage election counts MAIN-thread transcript rids only: sub-agent
    # transcripts (v1.4, agent_id-tagged) carry their own rids, and letting them
    # vote would hand the "main" label to a busy sub-agent lineage.
    t_rids = {
        e["request_id"]
        for e in transcript_events
        if e["type"] == "message" and e.get("request_id") and "agent_id" not in e
    }
    lineage_info: dict[str, dict[str, Any]] = {}
    for req, resp, _method in pairs:
        key = comp_of[req["path"]]
        root = comp_root[key]
        info = lineage_info.setdefault(
            key,
            {
                "n": 0,
                "t_hits": 0,
                "background": _classify_request(root),
                "model": root["model"],
                "first_mtime": root["mtime"],
                "lineage_id": root["prelude_hash"][:12],
            },
        )
        info["n"] += 1
        info["first_mtime"] = min(info["first_mtime"], req["mtime"])
        if resp is not None:
            rid = os.path.basename(resp["path"])[: -len(".response.json")]
            if rid in t_rids:
                info["t_hits"] += 1
    main_key: str | None = None
    candidates = [(k, i) for k, i in lineage_info.items() if not i["background"]]
    if candidates:
        # main = the lineage carrying the transcript's thread; else the largest.
        main_key = max(
            candidates, key=lambda ki: (ki[1]["t_hits"], ki[1]["n"], -ki[1]["first_mtime"])
        )[0]
    agent_of = {
        k: (i["background"] or ("main" if k == main_key else f"lineage-{i['lineage_id']}"))
        for k, i in lineage_info.items()
    }

    # --- phase 4: emit one inference event PER REQUEST, paired or not ---
    inference_events: list[dict[str, Any]] = []
    last_in_lineage: dict[str, int] = {}
    index_of_request: dict[str, int] = {}
    attempt_count: dict[tuple[str, int, str], int] = {}
    for idx, (req, resp, method) in enumerate(pairs):
        context: list[dict[str, Any]] = []
        system_refs: list[str] = []
        for obj, ann in req["system_objs"]:
            ref = put(obj)
            system_refs.append(ref)
            e: dict[str, Any] = {"ref": ref, "role": "system"}
            if ann:
                e["annotations"] = ann
            context.append(e)
        for mi, msg in enumerate(req["data"].get("messages", []) or []):
            role = msg.get("role", "user")
            for b in _as_blocks(msg.get("content")):
                obj, ann = block_to_object(b)
                entry: dict[str, Any] = {"ref": put(obj), "role": role, "message_index": mi}
                if ann:
                    entry["annotations"] = ann
                context.append(entry)
        tool_refs = [put(t) for t in req["tool_objs"]]

        req_h = sha256_bytes(req["raw"])
        client_uuid = os.path.basename(req["path"])[: -len(".request.json")]
        raw_bodies[req_h] = {
            "kind": "request",
            "server_request_id": None,
            "body_length": len(req["raw"]),
            "path": req["relpath"],
            "capture": req["cap"],
        }

        comp = comp_of[req["path"]]
        # attempts key on classification too: a suggestion call branching at the
        # same history depth is a sibling, not a retry of the main call.
        akey = (comp, req["nmsg"], _classify_request(req) or "")
        attempt = attempt_count.get(akey, 0)
        attempt_count[akey] = attempt + 1
        # parent = the chain predecessor when the request names one (exact);
        # else the previous inference of the same lineage (order fallback).
        cp = chain_parent.get(req["path"])
        parent_index = index_of_request.get(cp["path"]) if cp is not None else None
        if parent_index is None:
            parent_index = last_in_lineage.get(comp)
        last_in_lineage[comp] = idx
        index_of_request[req["path"]] = idx

        model: str | None = req["model"] or (resp["model"] if resp else None)
        ev: dict[str, Any] = {
            "type": "inference",
            # ts anchors the call in the log even without OTEL: the response file's
            # mtime (the moment the call completed), else the request's. Marked so
            # consumers know it is derived, not reported (OTEL overwrites it below).
            "ts": _iso(resp["mtime"] if resp else req["mtime"]),
            "ts_source": "file-mtime",
            "source": "raw_body",
            "index": idx,
            "attempt": attempt,
            "agent": _classify_request(req) or agent_of[comp],
            "lineage_id": lineage_info[comp]["lineage_id"],
            # v1.5: chain-component identity. lineage_id is the prelude KIND
            # (parallel same-type instances share it); instance_id is the
            # component root's client uuid — distinct per parallel instance,
            # and stable across a resume boundary (one logical thread).
            "instance_id": os.path.basename(comp_root[comp]["path"])[: -len(".request.json")],
            "parent_index": parent_index,
            "model": model or "unknown",
            "correlation": {
                "client_request_uuid": client_uuid,
                "server_request_id": None,
                "server_request_id_kind": None,
                "message_id": None,
                "previous_message_id": req["prev_msg_id"],
                "previous_request_id": req["prev_req_id"],
                "prompt_id": None,
            },
            "context": context,
            "prelude": {
                "system_refs": system_refs,
                "tool_refs": tool_refs,
                "params": req["params"],
            },
            "usage": {},
            "cost_usd": None,
            "cost_usd_reported": None,
            "duration_ms": None,
            "stop_reason": None,
            "thinking": {"present": False, "persisted": "none", "signature_present": False},
            "pairing": method,
            "request_body_ref": req_h,
            "response_body_ref": None,
        }

        if resp is not None:
            # The response file is named by the server request_id for Anthropic
            # first-party (req_...); other endpoints use a client-side UUID —
            # record WHICH so joins don't lie (server_request_id_kind).
            server_rid = os.path.basename(resp["path"])[: -len(".response.json")]
            resp_h = sha256_bytes(resp["raw"])
            raw_bodies[resp_h] = {
                "kind": "response",
                "server_request_id": server_rid,
                "body_length": len(resp["raw"]),
                "path": resp["relpath"],
                "capture": resp["cap"],
            }
            think: dict[str, Any] = {
                "present": False,
                "persisted": "none",
                "signature_present": False,
            }
            response_block_refs: list[str] = []
            for obj in resp["block_objs"]:
                response_block_refs.append(put(obj))
                if obj.get("kind") == "thinking":
                    think = {
                        "present": True,
                        "persisted": obj.get("persisted", "none"),
                        "signature_present": bool(obj.get("signature")),
                    }
            # Cost is DERIVED from token counts (the API delivers no price; CC's own
            # cost_usd is a client-side list-price estimate, kept as cost_usd_reported).
            usage_raw = resp["data"].get("usage") or {}
            ev.update(
                {
                    "correlation": {
                        "client_request_uuid": client_uuid,
                        "server_request_id": server_rid,
                        "server_request_id_kind": (
                            "anthropic_request_id" if server_rid.startswith("req_") else "filename"
                        ),
                        "message_id": resp["msg_id"],
                        "previous_message_id": req["prev_msg_id"],
                        "previous_request_id": req["prev_req_id"],
                        "prompt_id": None,
                    },
                    "usage": norm_usage(usage_raw),
                    "cost_usd": pricing.cost_usd(model, usage_raw, discount=discount),
                    "stop_reason": resp["data"].get("stop_reason"),
                    "thinking": think,
                    "response_block_refs": response_block_refs,
                    "response_body_ref": resp_h,
                }
            )
        inference_events.append(ev)

    # Orphan responses (no request claimed them) still enter the audit ring;
    # check_invariants reports them as unmatched.
    for resp in orphan_responses:
        resp_h = sha256_bytes(resp["raw"])
        server_rid = os.path.basename(resp["path"])[: -len(".response.json")]
        raw_bodies[resp_h] = {
            "kind": "response",
            "server_request_id": server_rid,
            "body_length": len(resp["raw"]),
            "path": resp["relpath"],
            "capture": resp["cap"],
        }

    # --- OTEL backfill (channel ingested in phase 1; join by request_id) ---
    if has_otel:
        for inf in inference_events:
            m = by_request.get(inf["correlation"].get("server_request_id"))
            if m:
                # Keep OTEL's cost only as a cross-check; cost_usd stays token-derived.
                # duration_ms / prompt_id ARE OTEL-authoritative -> backfill those.
                inf["cost_usd_reported"] = m["cost_usd"]
                inf["duration_ms"] = m["duration_ms"]
                inf["correlation"]["prompt_id"] = m["prompt_id"]
                if m.get("sequence") is not None:
                    inf["otel_seq"] = m["sequence"]
                if m.get("ts"):
                    inf["ts"] = m["ts"]
                    inf["ts_source"] = "otel"
            # The api_request_body event names this request's FILE (body_ref) and
            # carries prompt.id + event.sequence — so even a response-less call
            # anchors to its prompt and to the exporter's native order.
            bkey = f"{inf['correlation']['client_request_uuid']}.request.json"
            b = body_index.get(bkey)
            if b:
                if inf["correlation"].get("prompt_id") is None:
                    inf["correlation"]["prompt_id"] = b.get("prompt_id")
                inf.setdefault("otel_seq", b.get("sequence"))
        # OTEL agent.name is capture-verified (2026-07-02): where a
        # lineage's calls carry exactly one attested name, the placeholder
        # lineage-<id> label upgrades to subagent:<name>. Never fabricated.
        attested: dict[str, set[str]] = {}
        for inf in inference_events:
            m = by_request.get(inf["correlation"].get("server_request_id"))
            if m and m.get("agent_name"):
                attested.setdefault(inf["lineage_id"], set()).add(m["agent_name"])
        for inf in inference_events:
            names = attested.get(inf["lineage_id"], set())
            if inf["agent"].startswith("lineage-") and len(names) == 1:
                inf["agent"] = f"subagent:{next(iter(names))}"

    events = order_events([transcript_events, inference_events, otel_events, hook_events])

    # Session model = the MAIN lineage's model (last-write-wins used to let a
    # trailing background Haiku call claim the session).
    session_model: str | None = None
    if main_key is not None:
        session_model = lineage_info[main_key]["model"]
    elif requests:
        session_model = requests[0]["model"]

    return {
        "schema_version": SCHEMA_VERSION,
        "session": {
            "session_id": session_id or "unknown",
            "harness": "claude-code",
            "harness_version": tmeta.get("harness_version"),
            "entrypoint": tmeta.get("entrypoint"),
            "cwd": tmeta.get("cwd"),
            "account_uuid": account_uuid,
            "model": session_model,
            "captures": captures_chrono,
            # Every flag is DERIVED from what the channel actually yielded — an
            # unconditional True here would claim fidelity the doc does not have.
            "captured_with": {
                "transcript": has_transcript,
                "raw_bodies": n_request_bodies > 0,
                "otel_metrics": has_metrics,
                "otel_events": has_otel,
                "hooks": bool(hook_events),
            },
        },
        "objects": objects,
        "events": events,
        "raw_bodies": raw_bodies,
    }


def _has_key(o: Any, key: str) -> bool:
    """True iff `key` appears as a dict KEY anywhere in o. A structural check —
    content that merely *mentions* the key in a string must not trip it."""
    if isinstance(o, dict):
        return key in o or any(_has_key(v, key) for v in o.values())
    if isinstance(o, list):
        return any(_has_key(v, key) for v in o)
    return False


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def is_object_key(key: str) -> bool:
    """A store key must be a full lowercase sha256 hex digest — nothing else.

    Enforced as a SAFETY property, not just a style one: consumers materialize
    the store as files named by the key (project.render writes objects/<key>.json),
    so a key like '../../victim' would escape the tree. Truncated keys are
    rejected too: dedup correctness needs the full digest.
    """
    return isinstance(key, str) and bool(_SHA256_HEX.match(key))


def check_invariants(
    doc: dict[str, Any], capture_dir: str | Sequence[str] | None = None
) -> tuple[dict[str, Any], list[str]]:
    objs = doc["objects"]
    problems: list[str] = []
    total_refs = 0
    for h, o in objs.items():
        if _has_key(o, "cache_control"):
            problems.append(f"object {h[:12]} contains cache_control (must be lifted out)")
        # Key shape (path-safety) and key CONTENT (the key IS the content hash —
        # recompute it with the same canonicalization that minted it, so a
        # tampered or mis-keyed object cannot ride through as authentic).
        if not is_object_key(h):
            problems.append(f"object key is not a sha256 hex digest: {h[:32]!r}")
        elif sha256_obj(o) != h:
            problems.append(
                f"object {h[:12]} does not hash to its key "
                f"(content altered; recomputed {sha256_obj(o)[:12]})"
            )
    nodes = set()
    for e in doc["events"]:
        if e["type"] in ("message", "harness_event"):
            if e.get("uuid"):
                nodes.add(e["uuid"])
            nodes.update(u for u in e.get("uuids", []) if u)
    dangling = 0

    def _check_refs(refs: Any, what: str, idx: Any) -> None:
        """Every ref in the document must resolve in `objects`.

        EVERY ref: an unresolvable prelude or response ref is exactly as broken
        as an unresolvable context ref, and reconstruct.py would KeyError on it.
        """
        nonlocal total_refs
        for r in refs or []:
            total_refs += 1
            if r not in objs:
                problems.append(f"dangling {what} ref {str(r)[:12]} (inference {idx})")

    for ev in doc["events"]:
        if ev["type"] == "inference":
            for e in ev["context"]:
                total_refs += 1
                if e["ref"] not in objs:
                    problems.append(f"dangling context ref {e['ref'][:12]}")
            prelude = ev.get("prelude") or {}
            _check_refs(prelude.get("system_refs"), "prelude system", ev.get("index"))
            _check_refs(prelude.get("tool_refs"), "prelude tool", ev.get("index"))
            _check_refs(ev.get("response_block_refs"), "response block", ev.get("index"))
        elif ev["type"] == "message":
            for r in ev["block_refs"]:
                total_refs += 1
                if r not in objs:
                    problems.append(f"dangling block ref {r[:12]}")
        if ev["type"] in ("message", "harness_event"):
            p = ev.get("parent_uuid")
            if p is not None and p not in nodes:
                dangling += 1

    # The audit-ring index is keyed BY the sha of the raw body, so a key that is
    # not a full digest breaks the round-trip oracle's identity check (and, like
    # an object key, is materialized as a filename by consumers).
    for h in doc.get("raw_bodies") or {}:
        if not is_object_key(h):
            problems.append(f"raw_bodies key is not a sha256 hex digest: {str(h)[:32]!r}")
    body_refs = {"request_body_ref", "response_body_ref"}
    raw_index = doc.get("raw_bodies") or {}
    if raw_index:
        for ev in doc["events"]:
            if ev["type"] != "inference":
                continue
            for field in sorted(body_refs):
                ref = ev.get(field)
                if ref and ref not in raw_index:
                    problems.append(
                        f"inference {ev.get('index')} {field} {str(ref)[:12]} is not in raw_bodies"
                    )
    infs = [e for e in doc["events"] if e["type"] == "inference"]

    # --- contract: validate the document against events.schema.json HERE, where
    # it is produced. A schema break must fail at the emitter, not at whatever
    # consumer reads the file next; --check, the capture finalize and the live
    # watcher all come through this function. Partial docs (unit tests assert
    # ref-integrity on hand-built fragments) are skipped, not failed.
    schema_verdict = "unchecked"
    if "schema_version" in doc and "session" in doc:
        from .validate import schema_errors  # local: validate.py imports us back

        errs = schema_errors(doc)
        if errs is None:
            schema_verdict = "unchecked (jsonschema not installed)"
        elif errs:
            schema_verdict = f"INVALID ({len(errs)} error(s))"
            for e in errs:
                problems.append(f"schema: {e}")
        else:
            schema_verdict = "VALID"

    # --- conservation: the normalizer's inputs must be accounted for in its output.
    # Schema validity can't see dropped inputs — a pairing bug that silently drops
    # a call still produces a schema-valid doc — so these checks relate the doc
    # back to the capture dir itself.
    conservation: dict[str, Any] = {}
    resp_files: set[str] = set()
    cap_dirs = (
        [capture_dir]
        if isinstance(capture_dir, str)
        else list(capture_dir)
        if capture_dir is not None
        else []
    )
    if cap_dirs:
        # A merged doc is checked against the UNION of its capture dirs —
        # every body file in every capture must be accounted for.
        req_files: set[str] = set()
        for cd in cap_dirs:
            bodies_dir = os.path.join(cd, "bodies")
            req_files |= {
                os.path.basename(p)[: -len(".request.json")]
                for p in glob.glob(os.path.join(bodies_dir, "*.request.json"))
            }
            resp_files |= {
                os.path.basename(p)[: -len(".response.json")]
                for p in glob.glob(os.path.join(bodies_dir, "*.response.json"))
            }
        inf_client = {e["correlation"].get("client_request_uuid") for e in infs}
        inf_server = {e["correlation"].get("server_request_id") for e in infs}
        unmatched_req = sorted(req_files - inf_client)
        unmatched_resp = sorted(resp_files - inf_server)
        for u in unmatched_req:
            problems.append(f"request body never became an inference: {u} (dropped by pairing)")
        for u in unmatched_resp:
            problems.append(f"response body never became an inference: {u} (dropped by pairing)")
        conservation = {
            "request_files": len(req_files),
            "response_files": len(resp_files),
            "unmatched_requests": len(unmatched_req),
            "unmatched_responses": len(unmatched_resp),
        }
        # Hook channel: every recorded hook line must be an event in the doc.
        n_hook_lines = 0
        for cd in cap_dirs:
            hf = os.path.join(cd, "hooks", "hooks.jsonl")
            if os.path.exists(hf):
                with open(hf) as f:
                    n_hook_lines += sum(1 for ln in f if ln.strip())
        n_hook_events = sum(1 for e in doc["events"] if e["type"] == "hook")
        if n_hook_lines:
            conservation["hook_records"] = n_hook_lines
        if n_hook_events != n_hook_lines:
            problems.append(
                f"hook records dropped: {n_hook_lines} recorded lines "
                f"but {n_hook_events} hook events in the doc"
            )
        # OTEL channel: a logs-*.json that ingestion had to skip is a DROPPED
        # input, not an absent one. Ingestion tolerates it (never crashes); the
        # accounting refuses to let it pass silently.
        rejected_otel: list[str] = []
        for cd in cap_dirs:
            od = os.path.join(cd, "otel")
            if os.path.isdir(od):
                rejected_otel += [
                    f"{os.path.basename(cd)}/otel/{f}: {why}" for f, why in rejected_otel_files(od)
                ]
        if rejected_otel:
            conservation["otel_files_rejected"] = len(rejected_otel)
            for r in rejected_otel:
                problems.append(f"OTLP log file skipped (not ingested): {r}")

    # A transcript assistant requestId with no inference is only a PROBLEM when
    # this capture actually holds its body (a genuine pairing drop). A resumed or
    # continued session's transcript is cumulative — it references calls whose
    # bodies live in the EARLIER capture (or predate instrumentation); those are
    # expected, not drops. A captured api_error/api_refusal event also accounts
    # for a failed attempt (it has a requestId but never a response body — e.g. a
    # 404). Distinguish by whether the response body file is in THIS capture.
    t_rids = {
        e["request_id"] for e in doc["events"] if e["type"] == "message" and e.get("request_id")
    }
    if t_rids:
        srv_ids = {e["correlation"].get("server_request_id") for e in infs}
        err_ids = {
            (e.get("correlation") or {}).get("server_request_id")
            for e in doc["events"]
            if e["type"] in ("api_error", "api_refusal")
        }
        orphan_rids = sorted(t_rids - srv_ids - err_ids)
        outside = 0
        for rid in orphan_rids:
            if not cap_dirs:
                # No capture dir was supplied, so "dropped by pairing" and "its
                # body lives in the pre-resume capture" are INDISTINGUISHABLE from
                # here. Count it; do not accuse. (A resumed session's doc always
                # carries such ids, and --check / finalize always pass the dir.)
                outside += 1
            elif rid in resp_files:
                problems.append(f"transcript requestId {rid} has no inference event")
            else:
                outside += 1  # pre-resume / pre-capture reference (expected)
        if outside:
            key = "transcript_rids_outside_capture" if cap_dirs else "transcript_rids_unresolved"
            conservation[key] = outside

    by_type: dict[str, int] = {}
    for e in doc["events"]:
        by_type[e["type"]] = by_type.get(e["type"], 0) + 1
    cost: float = sum(e["cost_usd"] for e in infs if e.get("cost_usd"))
    # Cross-check: derived cost vs CC's reported cost, where both exist. ~0 confirms
    # we reproduce CC's client-side list-price math (any gap = a price-table drift).
    deltas: list[float] = [
        abs(e["cost_usd"] - e["cost_usd_reported"])
        for e in infs
        if e.get("cost_usd") is not None and e.get("cost_usd_reported") is not None
    ]
    stats: dict[str, Any] = {
        "schema": schema_verdict,
        "events_by_type": by_type,
        "unique_objects": len(objs),
        "total_refs": total_refs,
        "dedup_ratio": round(total_refs / len(objs), 2) if objs else 0,
        "dag_dangling_parents": dangling,
        "inferences_with_cost": sum(1 for e in infs if e.get("cost_usd") is not None),
        "responseless_inferences": sum(1 for e in infs if e.get("response_body_ref") is None),
        "lineages": len({e.get("lineage_id") for e in infs if e.get("lineage_id")}),
        "pairing_methods": {
            m: sum(1 for e in infs if e.get("pairing") == m)
            for m in {e.get("pairing") for e in infs}
            if m
        },
        "total_cost_usd": round(cost, 6),
        "cost_xcheck_max_delta": round(max(deltas), 9) if deltas else None,
    }
    if conservation:
        stats["conservation"] = conservation

    # Provenance: a SILENT model switch (safeguard/refusal fallback) makes a
    # session hybrid — work you believed one model produced was partly another's.
    # Surface it as a first-class fact, not something you must eyeball in the log.
    fallbacks = [
        e
        for e in doc["events"]
        if e["type"] == "harness_event" and "model_refusal_fallback" in e.get("subtype", "")
    ]
    if fallbacks:
        switches = []
        for e in fallbacks:
            d = e.get("data") or {}
            switches.append(
                {
                    "at": e.get("ts"),
                    "from": d.get("original_model"),
                    "to": d.get("fallback_model"),
                    "trigger": d.get("trigger"),
                }
            )
        stats["model_fallbacks"] = switches
        froms = {s["from"] for s in switches if s["from"]}
        tos = {s["to"] for s in switches if s["to"]}
        problems.append(
            f"SILENT MODEL SWITCH: {len(switches)} safeguard fallback(s) "
            f"{sorted(froms)} -> {sorted(tos)} — this session is HYBRID, not single-model"
        )
    return stats, problems


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "capture_dir",
        nargs="+",
        help="capture dir(s); several = one logical session stitched "
        "across its resume captures (all must share a session_id)",
    )
    ap.add_argument("--out")
    ap.add_argument("--check", action="store_true")
    gt = ap.add_mutually_exclusive_group()
    gt.add_argument("--transcript")
    gt.add_argument("--no-transcript", action="store_true")
    go = ap.add_mutually_exclusive_group()
    go.add_argument("--otel")
    go.add_argument("--no-otel", action="store_true")
    ap.add_argument(
        "--discount",
        type=discount_arg,
        default=None,
        help="Apply a user-supplied discount fraction to derived cost: a "
        "float in [0,1), billed as (1 - discount) (e.g. 0.10 => x0.90). "
        "Default: HARNESS_COST_DISCOUNT env var, else 0 (list price). "
        "CC's own reported cost is always list price.",
    )
    args = ap.parse_args()

    transcript = None if args.no_transcript else (args.transcript or "auto")
    otel = None if args.no_otel else (args.otel or "auto")

    doc = normalize(
        list(args.capture_dir), transcript=transcript, otel=otel, discount=args.discount
    )
    out = json.dumps(doc, indent=2)
    if args.out:
        # normalized.json is a full-text copy of the session: 0600 from creation.
        from ..collector.sink import open_private

        with open_private(args.out) as f:
            f.write(out + "\n")
    else:
        print(out)

    if args.check:
        stats, problems = check_invariants(doc, args.capture_dir)
        cw = doc["session"]["captured_with"]
        print("== invariants ==", file=sys.stderr)
        print(
            f"  channels: transcript={cw['transcript']} raw_bodies={cw['raw_bodies']} "
            f"otel_events={cw['otel_events']} otel_metrics={cw['otel_metrics']} "
            f"hooks={cw.get('hooks')}",
            file=sys.stderr,
        )
        for k, v in stats.items():
            print(f"  {k}: {v}", file=sys.stderr)
        if problems:
            print("  PROBLEMS:", file=sys.stderr)
            for p in problems:
                print("   -", p, file=sys.stderr)
            sys.exit(1)
        print("  ref-integrity: OK   cache_control-in-objects: none", file=sys.stderr)


if __name__ == "__main__":
    main()
