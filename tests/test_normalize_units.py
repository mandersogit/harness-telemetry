"""claude_code.py + pairing.py unit behaviors on synthetic inputs (no captures needed)."""

import argparse
import json
import stat
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.conftest import mk_synth_parallel_agents, mk_synth_resume_pair

from harness_telemetry.normalize import pairing
from harness_telemetry.normalize.claude_code import (
    block_to_object,
    check_invariants,
    discount_arg,
    ingest_otel,
    ingest_transcript,
    is_object_key,
    norm_usage,
    normalize,
    order_events,
    otel_log_records,
    sha256_obj,
)


def test_order_events_interleaves_by_ts() -> None:
    # Message-less calls land WHEN THEY HAPPENED, not at the end of the log.
    transcript = [
        {"type": "message", "ts": "2026-01-01T00:00:01.000Z"},
        {"type": "harness_event", "ts": None},  # inherits 00:01
        {"type": "message", "ts": "2026-01-01T00:00:05.000Z"},
    ]
    inferences = [
        {"type": "inference", "ts": "2026-01-01T00:00:02.000Z"},
        {"type": "inference", "ts": "2026-01-01T00:00:04.000Z"},
    ]
    otel = [{"type": "tool_decision", "ts": "2026-01-01T00:00:03.000Z"}]
    ordered = order_events([transcript, inferences, otel])
    assert [e.get("ts") for e in ordered] == [
        "2026-01-01T00:00:01.000Z",
        None,
        "2026-01-01T00:00:02.000Z",
        "2026-01-01T00:00:03.000Z",
        "2026-01-01T00:00:04.000Z",
        "2026-01-01T00:00:05.000Z",
    ]
    assert [e["seq"] for e in ordered] == list(range(6))


def test_text_block() -> None:
    obj, ann = block_to_object({"type": "text", "text": "hi"})
    assert obj == {"kind": "text", "text": "hi"} and ann == {}


def test_bare_string_block() -> None:
    obj, _ = block_to_object("hi")
    assert obj == {"kind": "text", "text": "hi"}


def test_cache_control_lifted_out_of_content() -> None:
    a = {"type": "text", "text": "same", "cache_control": {"type": "ephemeral"}}
    b = {"type": "text", "text": "same"}
    obj_a, ann_a = block_to_object(a)
    obj_b, ann_b = block_to_object(b)
    # cache_control is a per-REQUEST caching annotation, not content: identical
    # text must hash identically even as the cache breakpoint moves around it,
    # or the object store stops deduplicating and the successor-embedding tier
    # stops recognizing a replayed block.
    assert sha256_obj(obj_a) == sha256_obj(obj_b)
    assert ann_a == {"cache_control": {"type": "ephemeral"}} and ann_b == {}


def test_thinking_redaction_normalizes_to_none_per_channel() -> None:
    # OTEL bodies carry the literal "<REDACTED>"; the transcript carries "".
    for text in ("", "<REDACTED>"):
        obj, _ = block_to_object({"type": "thinking", "thinking": text, "signature": "sig1"})
        assert obj["persisted"] == "none" and obj["text"] is None and obj["signature"] == "sig1"
    obj, _ = block_to_object({"type": "thinking", "thinking": "real reasoning", "signature": "s"})
    assert obj["persisted"] == "raw" and obj["text"] == "real reasoning"


def test_same_turn_thinking_dedups_across_channels_via_signature() -> None:
    otel, _ = block_to_object({"type": "thinking", "thinking": "<REDACTED>", "signature": "s1"})
    transcript, _ = block_to_object({"type": "thinking", "thinking": "", "signature": "s1"})
    assert sha256_obj(otel) == sha256_obj(transcript)


def test_tool_use_and_result() -> None:
    obj, _ = block_to_object({"type": "tool_use", "id": "t1", "name": "Bash", "input": {"c": 1}})
    assert obj == {"kind": "tool_use", "id": "t1", "name": "Bash", "input": {"c": 1}}
    obj, _ = block_to_object(
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok", "is_error": False}
    )
    assert obj["kind"] == "tool_result" and obj["content"] == "ok" and obj["is_error"] is False


def test_unknown_block_becomes_marked_opaque() -> None:
    # Unknown blocks are a MARKED opaque object (raw preserved verbatim),
    # never a silent text coercion.
    block = {"type": "image", "source": {"type": "base64", "data": "xx"}}
    obj, _ = block_to_object(block)
    assert obj == {"kind": "opaque", "block_type": "image", "raw": block}


def test_opaque_lifts_cache_control_out_of_raw() -> None:
    # A breakpointed image must dedup with its unbreakpointed twin, so
    # cache_control is lifted out of the opaque raw (not stored in the object).
    plain = {"type": "image", "source": {"type": "base64", "data": "xx"}}
    breakpointed = {**plain, "cache_control": {"type": "ephemeral"}}
    obj_a, ann_a = block_to_object(plain)
    obj_b, ann_b = block_to_object(breakpointed)
    assert sha256_obj(obj_a) == sha256_obj(obj_b)  # same image -> same object
    assert "cache_control" not in obj_b["raw"]
    assert ann_b == {"cache_control": {"type": "ephemeral"}} and ann_a == {}


def test_norm_usage_core_four_and_passthrough() -> None:
    u = {
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 4,
        "service_tier": "standard",
        "cache_creation": {"ephemeral_1h_input_tokens": 4},
        "iterations": [{"x": 1}],
        "inference_geo": "us",
    }
    out = norm_usage(u)
    assert out["input_tokens"] == 1 and out["cache_read_tokens"] == 3
    assert out["service_tier"] == "standard" and "cache_creation" in out
    # Every extra field passes through (schema: additionalProperties true).
    assert out["iterations"] == [{"x": 1}] and out["inference_geo"] == "us"
    assert norm_usage(None) == {}


@given(st.text())
def test_object_hash_is_deterministic_and_content_only(text: str) -> None:
    with_cc = {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}
    without = {"type": "text", "text": text}
    assert sha256_obj(block_to_object(with_cc)[0]) == sha256_obj(block_to_object(without)[0])


def _req(
    path: str,
    mtime: float,
    lineage: str,
    nmsg: int,
    *,
    model: str = "m",
    assistant_hashes: set[str] | None = None,
    prev_msg_id: str | None = None,
    size: int = 100,
) -> dict[str, Any]:
    return {
        "path": path,
        "mtime": mtime,
        "prelude_hash": lineage,
        "nmsg": nmsg,
        "model": model,
        "assistant_hashes": assistant_hashes or set(),
        "prev_msg_id": prev_msg_id,
        "size": size,
    }


def _resp(
    path: str,
    mtime: float,
    *,
    model: str = "m",
    msg_id: str | None = None,
    block_hashes: list[str] | None = None,
    input_total: int = 0,
) -> dict[str, Any]:
    return {
        "path": path,
        "mtime": mtime,
        "model": model,
        "msg_id": msg_id,
        "block_hashes": block_hashes or [],
        "input_total": input_total,
    }


def test_pairing_interleaved_lineages_all_pair() -> None:
    # The exact shape that makes mtime-alternation pairing drop a call: two
    # lineages whose requests interleave in time. Successor-embedding binds
    # them exactly.
    reqs = [
        _req("A1.request.json", 0, "linA", 1),
        _req("B1.request.json", 1, "linB", 1),
        _req("A2.request.json", 4, "linA", 3, assistant_hashes={"ra1"}),
        _req("B2.request.json", 5, "linB", 3, assistant_hashes={"rb1"}),
    ]
    resps = [
        _resp("req_a1.response.json", 2, block_hashes=["ra1"], input_total=10),
        _resp("req_b1.response.json", 3, block_hashes=["rb1"], input_total=12),
        _resp("req_a2.response.json", 6, block_hashes=["ra2"], input_total=30),
        _resp("req_b2.response.json", 7, block_hashes=["rb2"], input_total=20),
    ]
    pairs, orphans = pairing.pair(reqs, resps)
    assert orphans == []
    bound = {r["path"]: (resp["path"] if resp else None, m) for r, resp, m in pairs}
    assert bound["A1.request.json"] == ("req_a1.response.json", "successor-embedding")
    assert bound["B1.request.json"] == ("req_b1.response.json", "successor-embedding")
    # tails resolve by token-affinity rank (A2 is the bigger request by size proxy)
    assert bound["A2.request.json"][0] is not None
    assert bound["B2.request.json"][0] is not None


def test_pairing_message_id_chain_beats_content() -> None:
    reqs = [
        _req("r1.request.json", 0, "L", 1),
        _req("r2.request.json", 2, "L", 3, prev_msg_id="msg_X", assistant_hashes={"h"}),
    ]
    resps = [_resp("req_1.response.json", 1, msg_id="msg_X", block_hashes=["h"])]
    pairs, orphans = pairing.pair(reqs, resps)
    assert orphans == [] or len(orphans) == 0
    bound = {r["path"]: (resp["path"] if resp else None, m) for r, resp, m in pairs}
    assert bound["r1.request.json"] == ("req_1.response.json", "message-id-chain")
    assert bound["r2.request.json"] == (None, None)  # tail with no response: kept, unpaired


def test_pairing_never_drops_a_request() -> None:
    # A request with no response at all (crash / in-flight kill) is returned
    # with resp=None — the caller emits a response-less inference, never silence.
    reqs = [_req("only.request.json", 0, "L", 1)]
    pairs, orphans = pairing.pair(reqs, [])
    assert pairs == [(reqs[0], None, None)] and orphans == []


def test_transcript_regroup_block_split(tmp_path: Path) -> None:
    # CC writes one record per content block; consecutive assistant records with
    # one requestId regroup into one message. Node rule (verified against real
    # transcripts): node id = LAST record's uuid; inbound edge = FIRST record's
    # parentUuid.
    lines = [
        {
            "type": "user",
            "uuid": "u0",
            "parentUuid": None,
            "timestamp": "T0",
            "message": {"role": "user", "content": "hi"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "parentUuid": "u0",
            "timestamp": "T1",
            "requestId": "req_1",
            "message": {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "", "signature": "s"}],
            },
        },
        {
            "type": "assistant",
            "uuid": "a2",
            "parentUuid": "a1",
            "timestamp": "T2",
            "requestId": "req_1",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t", "name": "B", "input": {}}],
            },
        },
        {
            "type": "user",
            "uuid": "u1",
            "parentUuid": "a2",
            "timestamp": "T3",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t"}]},
        },
    ]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(line) for line in lines))
    store: dict[str, Any] = {}

    def put(obj: dict[str, Any]) -> str:
        h = sha256_obj(obj)
        store[h] = obj
        return h

    events, _ = ingest_transcript(str(p), put)
    msgs = [e for e in events if e["type"] == "message"]
    assert len(msgs) == 3  # user, merged assistant, tool_result user
    merged = msgs[1]
    assert merged["uuid"] == "a2" and merged["uuids"] == ["a1", "a2"]
    assert merged["parent_uuid"] == "u0" and len(merged["block_refs"]) == 2
    # the tool_result user still parents cleanly off the merged node
    assert msgs[2]["parent_uuid"] == "a2"


def test_pairing_one_to_one_leftover_ignores_model_mismatch() -> None:
    # The GLM fixture: response reports a different model string than requested.
    reqs = [_req("g.request.json", 0, "L", 1, model="z-ai/glm-5.2")]
    resps = [_resp("uuid.response.json", 1, model="glm-5.2-fp8")]
    pairs, orphans = pairing.pair(reqs, resps)
    assert orphans == []
    assert pairs[0][1] is not None and pairs[0][2] == "elimination"


def test_norm_usage_rejects_non_dict() -> None:
    out: dict[str, Any] = norm_usage("nope")
    assert out == {}


def test_cache_control_check_is_structural_not_substring() -> None:
    # Regression: content that merely MENTIONS
    # cache_control (e.g. a captured session working on this repo) must pass...
    # (object keys are the real content hashes — check_invariants verifies that too)
    ok_obj = {"kind": "text", "text": "lift cache_control into annotations"}
    mentions = {"objects": {sha256_obj(ok_obj): ok_obj}, "events": []}
    _, problems = check_invariants(mentions)
    assert problems == []
    # ...while an actual leaked cache_control KEY (even nested) must fail.
    bad_obj = {
        "kind": "tool_result",
        "tool_use_id": "t",
        "content": [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}],
    }
    leaked = {"objects": {sha256_obj(bad_obj): bad_obj}, "events": []}
    _, problems = check_invariants(leaked)
    assert len(problems) == 1 and "cache_control" in problems[0]


def test_conservation_reports_unmatched_bodies(tmp_path: Path) -> None:
    # A capture dir with a request the doc has no inference for -> loud problem.
    bodies = tmp_path / "bodies"
    bodies.mkdir()
    (bodies / "aaaa.request.json").write_text("{}")
    (bodies / "req_x.response.json").write_text("{}")
    doc: dict[str, Any] = {"objects": {}, "events": []}
    stats, problems = check_invariants(doc, str(tmp_path))
    assert stats["conservation"] == {
        "request_files": 1,
        "response_files": 1,
        "unmatched_requests": 1,
        "unmatched_responses": 1,
    }
    assert any("aaaa" in p and "dropped by pairing" in p for p in problems)
    assert any("req_x" in p for p in problems)


def _msg_with_rid(rid: str) -> dict[str, Any]:
    return {"type": "message", "role": "assistant", "request_id": rid, "block_refs": []}


def test_silent_model_switch_flagged_as_hybrid() -> None:
    # A safeguard/refusal fallback is a PROVENANCE problem — the session
    # is hybrid, not the single model you thought. check_invariants surfaces the
    # from->to switch as a problem + a stats entry, never something to eyeball.
    doc: dict[str, Any] = {
        "objects": {},
        "events": [
            {
                "type": "harness_event",
                "subtype": "system:model_refusal_fallback",
                "ts": "2026-07-02T22:02:10Z",
                "data": {
                    "original_model": "claude-fable-5",
                    "fallback_model": "claude-opus-4-8",
                    "trigger": "refusal",
                },
            }
        ],
    }
    stats, problems = check_invariants(doc)
    assert stats["model_fallbacks"][0]["from"] == "claude-fable-5"
    assert stats["model_fallbacks"][0]["to"] == "claude-opus-4-8"
    assert any("SILENT MODEL SWITCH" in p and "HYBRID" in p for p in problems)


def test_transcript_rid_drop_vs_resume_reference(tmp_path: Path) -> None:
    # A transcript requestId with no inference is a PROBLEM only when its
    # body is in THIS capture (a real drop) — not when it predates the capture
    # (a resumed/continued session references the earlier capture's calls).
    bodies = tmp_path / "bodies"
    bodies.mkdir()
    (bodies / "req_here.response.json").write_text("{}")  # a real drop: body present
    doc: dict[str, Any] = {
        "objects": {},
        "events": [
            _msg_with_rid("req_here"),  # body here, no inf
            _msg_with_rid("req_elsewhere"),
        ],
    }  # pre-resume
    stats, problems = check_invariants(doc, str(tmp_path))
    assert any("req_here" in p and "no inference" in p for p in problems)
    assert not any("req_elsewhere" in p for p in problems)
    assert stats["conservation"]["transcript_rids_outside_capture"] == 1


# --- Multi-capture merge (resume lifecycle) -------------------------------
# (synthetic capture builders live in conftest — shared with the watch tests)


def test_multi_capture_merge_stitches_one_logical_session(tmp_path: Path) -> None:
    # Two captures of the SAME session (original + resume). The resumed
    # call chains to the pre-resume response ACROSS the capture boundary
    # (previous_message_id — proven on real resume captures), so the
    # merged doc is ONE logical session: one lineage, exact parentage.
    a, b = mk_synth_resume_pair(tmp_path)
    doc = normalize([str(a), str(b)], transcript=None)
    infs = [e for e in doc["events"] if e["type"] == "inference"]
    assert len(infs) == 2
    assert [i["agent"] for i in infs] == ["main", "main"]
    assert infs[0]["lineage_id"] == infs[1]["lineage_id"]
    assert infs[1]["parent_index"] == infs[0]["index"]  # chain crossed the boundary
    # v1.5: one logical thread keeps ONE instance_id across the resume boundary
    assert infs[0]["instance_id"] == infs[1]["instance_id"] == "aaaa0001"
    assert doc["session"]["session_id"] == "sid-merge"
    assert doc["session"]["captures"] == ["capA", "capB"]  # chronological
    # every raw body says which capture holds it (path resolution + provenance)
    assert {rb["capture"] for rb in doc["raw_bodies"].values()} == {"capA", "capB"}
    # conservation runs over the UNION of capture dirs
    stats, problems = check_invariants(doc, [str(a), str(b)])
    assert problems == []
    assert stats["conservation"] == {
        "request_files": 2,
        "response_files": 2,
        "unmatched_requests": 0,
        "unmatched_responses": 0,
    }


def test_multi_capture_merge_is_arg_order_invariant(tmp_path: Path) -> None:
    # captures order chronologically (earliest body wins), not by argv order —
    # the merged doc is identical either way.
    a, b = mk_synth_resume_pair(tmp_path)
    doc_ab = normalize([str(a), str(b)], transcript=None)
    doc_ba = normalize([str(b), str(a)], transcript=None)
    assert doc_ab == doc_ba
    assert doc_ab["session"]["captures"] == ["capA", "capB"]


def test_single_capture_doc_still_names_its_capture(tmp_path: Path) -> None:
    # the single-dir path keeps its shape, plus the new provenance fields —
    # str and 1-element-list spellings agree.
    a, _ = mk_synth_resume_pair(tmp_path)
    doc_str = normalize(str(a), transcript=None)
    doc_list = normalize([str(a)], transcript=None)
    assert doc_str == doc_list
    assert doc_str["session"]["captures"] == ["capA"]
    assert all(rb["capture"] == "capA" for rb in doc_str["raw_bodies"].values())


def test_multi_capture_merge_rejects_mixed_sessions(tmp_path: Path) -> None:
    # merging captures of DIFFERENT sessions is a usage error, loudly.
    a, b = mk_synth_resume_pair(tmp_path, sid_b="sid-other")
    with pytest.raises(ValueError, match="different session"):
        normalize([str(a), str(b)], transcript=None)


def test_conservation_unions_multiple_capture_dirs(tmp_path: Path) -> None:
    # check_invariants with a LIST of dirs: body files union across dirs; a
    # transcript rid whose body is in ANY dir is a drop, one in NONE is a
    # benign outside-capture reference.
    a = tmp_path / "a" / "bodies"
    b = tmp_path / "b" / "bodies"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    (a / "aaaa.request.json").write_text("{}")
    (b / "req_y.response.json").write_text("{}")
    doc: dict[str, Any] = {
        "objects": {},
        "events": [_msg_with_rid("req_y"), _msg_with_rid("req_gone")],
    }
    stats, problems = check_invariants(doc, [str(tmp_path / "a"), str(tmp_path / "b")])
    assert any("req_y" in p and "no inference" in p for p in problems)
    assert not any("req_gone" in p for p in problems)
    assert any("aaaa" in p and "dropped by pairing" in p for p in problems)
    assert stats["conservation"]["request_files"] == 1
    assert stats["conservation"]["response_files"] == 1
    assert stats["conservation"]["transcript_rids_outside_capture"] == 1


# --- v1.4: the sub-agent sibling tree -------------------------------------------


def _rec(uuid: str, text: str, ts: str, agent: str | None = None) -> str:
    rec: dict[str, Any] = {
        "type": "user",
        "uuid": uuid,
        "parentUuid": None,
        "isSidechain": bool(agent),
        "timestamp": ts,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }
    if agent:
        rec["agentId"] = agent
    return json.dumps(rec) + "\n"


def test_subagent_tree_ingestion(tmp_path: Path) -> None:
    # Synthetic <sid>/ tree next to the main transcript: one flat sub-agent
    # (with meta/toolUseId), one workflow sub-agent, the workflow run record
    # (with script) and its journal — all land as tagged events.
    from harness_telemetry.normalize import validate

    a, _ = mk_synth_resume_pair(tmp_path)
    t = tmp_path / "t-main.jsonl"
    t.write_text(_rec("u1", "hi", "2026-07-02T23:00:00.000Z"))
    sub = tmp_path / "t-main" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-x1.jsonl").write_text(_rec("s1", "task", "2026-07-02T23:00:01.000Z", "x1"))
    (sub / "agent-x1.meta.json").write_text(
        json.dumps(
            {
                "agentType": "general-purpose",
                "description": "d",
                "toolUseId": "toolu_1",
                "spawnDepth": 1,
            }
        )
    )
    wfd = sub / "workflows" / "wf_1"
    wfd.mkdir(parents=True)
    (wfd / "agent-x2.jsonl").write_text(_rec("s2", "wf task", "2026-07-02T23:00:02.000Z", "x2"))
    (wfd / "agent-x2.meta.json").write_text(
        json.dumps({"agentType": "workflow-subagent", "description": "w", "spawnDepth": 1})
    )
    (wfd / "journal.jsonl").write_text('{"type":"started","agentId":"x2"}\n')
    wfs = tmp_path / "t-main" / "workflows"
    wfs.mkdir()
    (wfs / "wf_1.json").write_text(
        json.dumps(
            {
                "workflowName": "w",
                "script": "export const meta = {}",
                "timestamp": "2026-07-02T23:00:02.500Z",
            }
        )
    )

    doc = normalize(str(a), transcript=str(t))
    msgs = [e for e in doc["events"] if e["type"] == "message"]
    assert any(m.get("agent_id") == "x1" and "workflow_id" not in m for m in msgs)
    assert any(m.get("agent_id") == "x2" and m.get("workflow_id") == "wf_1" for m in msgs)
    metas = {
        e["data"]["agent_id"]: e["data"]
        for e in doc["events"]
        if e.get("subtype") == "subagent_meta"
    }
    assert metas["x1"]["toolUseId"] == "toolu_1"
    assert metas["x2"]["workflow_id"] == "wf_1"
    wfm = next(e for e in doc["events"] if e.get("subtype") == "workflow_meta")
    assert wfm["data"]["run"]["script"] == "export const meta = {}"
    jr = next(e for e in doc["events"] if e.get("subtype") == "workflow_journal")
    assert jr["data"]["record"]["agentId"] == "x2"
    p = tmp_path / "doc.json"
    p.write_text(json.dumps(doc))
    ok, msg = validate.validate(str(p))
    assert ok, msg


# --- pairing: two same-prelude agents in parallel --------------------------
# The pairer must never bind a response to a request just because both sort to
# the same rank by mtime: which successor NAMED the response is the evidence.


@pytest.mark.parametrize(
    ("chain", "method"), [("request-id", "request-id-chain"), ("message-id", "message-id-chain")]
)
def test_parallel_same_prelude_agents_pair_to_their_own_response(
    tmp_path: Path, chain: str, method: str
) -> None:
    cap = mk_synth_parallel_agents(tmp_path, chain=chain)
    doc = normalize(str(cap), transcript=None)
    got = {
        e["correlation"]["client_request_uuid"]: (
            e["correlation"]["server_request_id"],
            e.get("pairing"),
        )
        for e in doc["events"]
        if e["type"] == "inference"
    }
    # alpha was written FIRST but completed LAST: an mtime zip binds it to beta's
    # response. Each root must get the response its own successor named.
    assert got["aaaa0001"] == ("req_ALPHA", method)
    assert got["bbbb0001"] == ("req_BETA", method)
    # the follow-ups have no response in this capture and say so, rather than
    # stealing one
    assert got["aaaa0002"] == (None, None)
    assert got["bbbb0002"] == (None, None)
    # one prelude, two instances: the lineage is the KIND, instance_id the agent
    infs = [e for e in doc["events"] if e["type"] == "inference"]
    assert len({i["lineage_id"] for i in infs}) == 1
    assert len({i["instance_id"] for i in infs}) == 2
    _, problems = check_invariants(doc, str(cap))
    assert problems == []


def test_pairing_declines_rather_than_guessing_when_ambiguous() -> None:
    # Two identical-history requests at one level and a successor that names a
    # response: nothing distinguishes the predecessors, so the chain tier binds
    # nothing and the tail tiers take over (loudly labelled), instead of
    # inventing a 50/50 answer.
    reqs = [
        _req("A.request.json", 0, "lin", 1, prev_msg_id=None),
        _req("B.request.json", 1, "lin", 1, prev_msg_id=None),
        _req("C.request.json", 5, "lin", 3, prev_msg_id="m1"),
    ]
    for r in reqs:
        r["msg_hashes"] = set()
    resps = [_resp("req_1.response.json", 2, msg_id="m1", block_hashes=["h1"])]
    pairs, orphans = pairing.pair(reqs, resps)
    methods = {p[2] for p in pairs if p[1] is not None}
    assert orphans == []
    # bound by a tail tier, never by the chain tier's mtime rank
    assert methods <= {"elimination", "token-affinity"}


# --- emitter truthfulness ---------------------------------------------------


def test_captured_with_raw_bodies_is_derived_not_constant(tmp_path: Path) -> None:
    empty = tmp_path / "empty" / "bodies"
    empty.mkdir(parents=True)
    doc = normalize(str(tmp_path / "empty"), transcript=None)
    assert doc["session"]["captured_with"]["raw_bodies"] is False
    a, _b = mk_synth_resume_pair(tmp_path)
    assert normalize(str(a), transcript=None)["session"]["captured_with"]["raw_bodies"] is True


def test_message_without_uuid_gets_a_deterministic_placeholder(tmp_path: Path) -> None:
    # message.uuid is a REQUIRED string: a record missing one must be neither
    # dropped (content loss) nor emitted as null (an invalid doc).
    t = tmp_path / "t.jsonl"
    t.write_text(
        json.dumps(
            {
                "type": "user",
                "parentUuid": None,
                "timestamp": "T0",
                "message": {"role": "user", "content": "no uuid here"},
            }
        )
        + "\n"
    )
    events, _ = ingest_transcript(str(t), lambda o: sha256_obj(o))
    msg = events[0]
    assert msg["uuid"].startswith("synthetic-") and msg["uuids"] == [msg["uuid"]]
    assert len(msg["block_refs"]) == 1  # content kept, nothing dropped
    again, _ = ingest_transcript(str(t), lambda o: sha256_obj(o))
    assert again[0]["uuid"] == msg["uuid"]  # deterministic across runs


def test_tool_decision_tool_name_falls_back_like_decision(tmp_path: Path) -> None:
    otel = tmp_path / "otel"
    otel.mkdir()
    (otel / "logs-1.json").write_text(
        json.dumps(
            {
                "resourceLogs": [
                    {
                        "scopeLogs": [
                            {
                                "logRecords": [
                                    {
                                        "attributes": [
                                            {
                                                "key": "event.name",
                                                "value": {
                                                    "stringValue": "claude_code.tool_decision"
                                                },
                                            },
                                            {"key": "event.sequence", "value": {"intValue": 1}},
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        )
    )
    _by_req, extra, _m, _bi, rejected = ingest_otel(str(otel))
    assert rejected == []
    ev = next(e for e in extra if e["type"] == "tool_decision")
    assert ev["tool_name"] == "unknown" and ev["decision"] == "unknown"


def test_check_invariants_validates_against_the_schema(tmp_path: Path) -> None:
    a, _b = mk_synth_resume_pair(tmp_path)
    doc = normalize(str(a), transcript=None)
    stats, problems = check_invariants(doc, str(a))
    assert stats["schema"] == "VALID" and problems == []
    # a doc that breaks the contract fails where it is produced
    doc["events"][0]["seq"] = "not-an-integer"
    stats, problems = check_invariants(doc, str(a))
    assert stats["schema"].startswith("INVALID")
    assert any(p.startswith("schema:") for p in problems)


def test_object_keys_must_be_content_hashes(tmp_path: Path) -> None:
    obj = {"kind": "text", "text": "hi"}
    assert is_object_key(sha256_obj(obj))
    assert not is_object_key("../../victim")
    assert not is_object_key(sha256_obj(obj)[:12])  # truncated is not a key
    # a tampered object no longer hashes to its key
    tampered = {"objects": {sha256_obj(obj): {"kind": "text", "text": "TAMPERED"}}, "events": []}
    _, problems = check_invariants(tampered)
    assert len(problems) == 1 and "does not hash to its key" in problems[0]
    traversal = {"objects": {"../../victim": obj}, "events": []}
    _, problems = check_invariants(traversal)
    assert any("not a sha256 hex digest" in p for p in problems)


# --- OTEL ingestion robustness ---------------------------------------------


@pytest.mark.parametrize(
    "blob",
    [
        "{}",
        '{"resourceLogs": null}',
        '{"resourceLogs": [null]}',
        '{"resourceLogs": {"a": 1}}',
        '{"resourceLogs": [{"scopeLogs": [{"logRecords": [1]}]}]}',
        "[]",
        "not json at all",
    ],
)
def test_malformed_otel_file_is_skipped_with_a_reason_never_a_crash(
    tmp_path: Path, blob: str
) -> None:
    otel = tmp_path / "otel"
    otel.mkdir()
    (otel / "logs-1.json").write_text(blob)
    by_req, extra, _m, _bi, rejected = ingest_otel(str(otel))
    assert by_req == {} and extra == []
    assert len(rejected) == 1 and rejected[0][0] == "logs-1.json" and rejected[0][1]


def test_rejected_otel_file_is_counted_by_check_not_swallowed(tmp_path: Path) -> None:
    a, _b = mk_synth_resume_pair(tmp_path)
    otel = a / "otel"
    otel.mkdir()
    (otel / "logs-1.json").write_text('{"resourceLogs": null}')
    doc = normalize(str(a), transcript=None)
    stats, problems = check_invariants(doc, str(a))
    assert stats["conservation"]["otel_files_rejected"] == 1
    assert any("OTLP log file skipped" in p and "logs-1.json" in p for p in problems)


def test_wellformed_but_empty_otel_export_is_not_a_rejection(tmp_path: Path) -> None:
    otel = tmp_path / "otel"
    otel.mkdir()
    (otel / "logs-1.json").write_text('{"resourceLogs": []}')
    _by_req, _extra, _m, _bi, rejected = ingest_otel(str(otel))
    assert rejected == []
    assert otel_log_records({"resourceLogs": []}) == ([], None)


# --- --discount validation --------------------------------------------------


def test_discount_arg_rejects_out_of_range_values() -> None:
    assert discount_arg("0.10") == pytest.approx(0.10)
    assert discount_arg("0") == 0.0
    for bad in ("1", "1.5", "-0.1", "nope", ""):
        with pytest.raises(argparse.ArgumentTypeError):
            discount_arg(bad)


# --- projection safety ------------------------------------------------------


def test_render_refuses_object_keys_that_escape_the_objects_dir(tmp_path: Path) -> None:
    from harness_telemetry.normalize import project

    obj = {"kind": "text", "text": "hi"}
    good = {
        "schema_version": "1.5",
        "session": {
            "session_id": "s",
            "harness": "claude-code",
            "captured_with": {
                "transcript": False,
                "raw_bodies": True,
                "otel_metrics": False,
                "otel_events": False,
            },
        },
        "objects": {sha256_obj(obj): obj},
        "events": [
            {
                "seq": 0,
                "type": "message",
                "source": "transcript",
                "uuid": "u0",
                "role": "user",
                "block_refs": [sha256_obj(obj)],
            },
            {
                "seq": 1,
                "type": "inference",
                "source": "raw_body",
                "index": 0,
                "model": "m",
                "context": [{"ref": sha256_obj(obj), "role": "user"}],
            },
        ],
    }
    project.render(good, str(tmp_path / "ok"))
    written = tmp_path / "ok" / "objects" / f"{sha256_obj(obj)}.json"
    assert written.exists()
    # sessfs/ holds prompts and file contents: private from creation, never a
    # 0644 window (dirs 0700, files 0600 — see collector.sink.open_private).
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    for d in ("", "objects", "log", "inferences", "inferences/0000", "inferences/0000/context"):
        sub = tmp_path / "ok" / d if d else tmp_path / "ok"
        if sub.is_dir():
            assert stat.S_IMODE(sub.stat().st_mode) == 0o700, sub

    assert (tmp_path / "ok" / "inferences" / "0000" / "context").is_dir()

    hostile = json.loads(json.dumps(good))
    hostile["objects"] = {"../../victim": obj}
    hostile["events"][0]["block_refs"] = ["../../victim"]
    hostile["events"][1]["context"] = [{"ref": "../../victim", "role": "user"}]
    with pytest.raises(ValueError, match="sha256 hex digest"):
        project.render(hostile, str(tmp_path / "bad"))
    # refused BEFORE anything was written outside the tree
    assert not (tmp_path / "victim.json").exists()


# --- tier order: evidence before timing -----------------------------------------


def _parallel_chained_records() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Two concurrent same-prelude agents, each follow-up naming its OWN
    predecessor response, with distinguishable histories."""
    reqs = [
        _req("A1.request.json", 0, "L", 1),
        _req("B1.request.json", 1, "L", 1),
        _req("A2.request.json", 20, "L", 3, prev_msg_id="msg_A", assistant_hashes={"ha"}),
        _req("B2.request.json", 21, "L", 3, prev_msg_id="msg_B", assistant_hashes={"hb"}),
    ]
    for r, h in zip(reqs, [{"ua"}, {"ub"}, {"ua", "ha", "ua2"}, {"ub", "hb", "ub2"}], strict=True):
        r["msg_hashes"] = h
    resps = [
        _resp("req_A.response.json", 10, msg_id="msg_A", block_hashes=["ha"]),
        _resp("req_B.response.json", 11, msg_id="msg_B", block_hashes=["hb"]),
    ]
    return reqs, resps


_TIMELESS_INDEX = {
    "A1.request.json": {"sequence": 1},
    "B1.request.json": {"sequence": 2},
    "req_A.response.json": {"sequence": 3},
    "req_B.response.json": {"sequence": 4},
}


def test_chains_are_resolved_before_the_otel_window() -> None:
    # The regression: with the window running FIRST and its OTEL records carrying
    # no call-start time, the LIFO fallback bound A's response to B's request and
    # labelled it "otel-window" — while both agents' previous_message_id chains
    # said, unambiguously, otherwise. Evidence before timing.
    reqs, resps = _parallel_chained_records()
    pairs, orphans = pairing.pair(reqs, resps, _TIMELESS_INDEX, start_time_of={})
    bound = {r["path"]: (resp["path"] if resp else None, m) for r, resp, m in pairs}
    assert bound["A1.request.json"] == ("req_A.response.json", "message-id-chain")
    assert bound["B1.request.json"] == ("req_B.response.json", "message-id-chain")
    assert bound["A2.request.json"] == (None, None)  # tails, no response in this set
    assert bound["B2.request.json"] == (None, None)
    assert orphans == []


def test_otel_window_declines_without_a_discriminating_start_time() -> None:
    # It has exactly one discriminator. Without it, declining costs a label; the
    # old LIFO guess cost correctness, under an exact method's name.
    reqs, resps = _parallel_chained_records()
    assert pairing.pair_by_otel_window(reqs, resps, _TIMELESS_INDEX, {}) == {}


def test_otel_window_binds_only_when_the_start_time_picks_one() -> None:
    reqs = [_req("A.request.json", 100.0, "LA", 1), _req("B.request.json", 200.0, "LB", 1)]
    resps = [_resp("req_1.response.json", 300.0, msg_id="m1")]
    bi = {
        "A.request.json": {"sequence": 1},
        "B.request.json": {"sequence": 2},
        "req_1.response.json": {"sequence": 3},
    }
    # nearest B's mtime -> B
    assert pairing.pair_by_otel_window(reqs, resps, bi, {"req_1": 199.0}) == {
        "B.request.json": (resps[0], "otel-window")
    }
    # exactly between the two open requests -> ambiguous, decline
    assert pairing.pair_by_otel_window(reqs, resps, bi, {"req_1": 150.0}) == {}
    # no timing at all -> decline
    assert pairing.pair_by_otel_window(reqs, resps, bi, {}) == {}


def test_otel_window_still_binds_what_the_chains_could_not() -> None:
    # It is a real tier, not a disabled one: a lineage tail with no chain field
    # and an unambiguous window is still bound, and still says "otel-window".
    reqs = [_req("A.request.json", 100.0, "LA", 1)]
    resps = [_resp("req_1.response.json", 300.0)]
    bi = {"A.request.json": {"sequence": 1}, "req_1.response.json": {"sequence": 2}}
    pairs, orphans = pairing.pair(reqs, resps, bi, start_time_of={"req_1": 100.5})
    assert orphans == []
    assert [(r["path"], resp["path"], m) for r, resp, m in pairs] == [
        ("A.request.json", "req_1.response.json", "otel-window")
    ]


# --- --out never leaves a 0644 file behind --------------------------------------


def test_normalize_out_forces_0600_on_an_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # normalized.json is exactly as sensitive as the capture it came from. Writing
    # over a file the user had already created must not inherit its 0644.
    from harness_telemetry.normalize import claude_code as cc

    a, _b = mk_synth_resume_pair(tmp_path)
    out = tmp_path / "normalized.json"
    out.write_text("{}")
    out.chmod(0o644)
    monkeypatch.setattr(
        cc.sys, "argv", ["harness-normalize", str(a), "--out", str(out), "--no-transcript"]
    )
    cc.main()
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    doc = json.loads(out.read_text())
    assert doc["events"]  # ... and it really was rewritten
    capsys.readouterr()
