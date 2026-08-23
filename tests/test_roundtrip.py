"""Round-trip: reconstruct every inference from the doc ALONE; diff vs raw bodies.

Unit-level coverage of the reconstruction primitives on synthetic inputs. The
correctness oracle for the enriched transcript is: any semantic diff = a
capture gap. The end-to-end version against real captures is part of the
maintainer's local suite and is not distributed; see CONTRIBUTING.md.
"""

import json
from pathlib import Path
from typing import Any

from tests.conftest import mk_synth_resume_pair

from harness_telemetry.normalize import reconstruct, validate
from harness_telemetry.normalize.claude_code import normalize


def test_object_to_block_inverts_with_annotations_and_extras() -> None:
    obj = {"kind": "text", "text": "hi", "citations": None}
    block = reconstruct.object_to_block(obj, {"cache_control": {"type": "ephemeral"}})
    assert block == {
        "type": "text",
        "text": "hi",
        "citations": None,
        "cache_control": {"type": "ephemeral"},
    }
    obj = {"kind": "tool_use", "id": "t", "name": "B", "input": {}, "caller": {"type": "direct"}}
    assert reconstruct.object_to_block(obj)["caller"] == {"type": "direct"}
    obj = {"kind": "opaque", "block_type": "image", "raw": {"type": "image", "source": {}}}
    assert reconstruct.object_to_block(obj) == {"type": "image", "source": {}}


def test_semantic_diff_treats_withheld_thinking_as_equal() -> None:
    recon = {
        "model": "m",
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "<WITHHELD>", "signature": "s"}],
            }
        ],
    }
    raw = json.dumps(
        {
            "model": "m",
            "metadata": {"user_id": "x"},
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "thinking", "thinking": "<REDACTED>", "signature": "s"}],
                }
            ],
        }
    ).encode()
    assert reconstruct.semantic_diff_request(recon, raw) == []


def test_semantic_diff_catches_real_gaps() -> None:
    recon = {
        "model": "m",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hello"}]}],
    }
    raw = json.dumps(
        {
            "model": "m",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "DIFFERENT"}]}],
        }
    ).encode()
    diffs = reconstruct.semantic_diff_request(recon, raw)
    assert diffs and "text" in diffs[0]


def test_nested_metadata_is_content_and_is_compared() -> None:
    # The request's TOP-LEVEL metadata is the documented exemption (it carries
    # user_id and is deliberately not in the prelude) ...
    recon = {
        "model": "m",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
    }
    raw = json.dumps(
        {
            "model": "m",
            "metadata": {"user_id": "x"},
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        }
    ).encode()
    assert reconstruct.semantic_diff_request(recon, raw) == []
    # ... but a `metadata` key NESTED inside tool input is ordinary content: a
    # doc that lost it must not pass as a faithful reconstruction.
    recon = {
        "model": "m",
        "messages": [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t", "name": "B", "input": {}}],
            }
        ],
    }
    raw = json.dumps(
        {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "t",
                            "name": "B",
                            "input": {"metadata": {"k": 1}},
                        }
                    ],
                }
            ],
        }
    ).encode()
    diffs = reconstruct.semantic_diff_request(recon, raw)
    assert diffs and "metadata" in diffs[0]


def _raw_response(**over: Any) -> bytes:
    body = {
        "id": "msg_1",
        "model": "claude-test-1",
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 5, "output_tokens": 5},
    }
    body.update(over)
    return json.dumps(body).encode()


def _doc_response(**over: Any) -> dict[str, Any]:
    rec = {
        "id": "msg_1",
        "model": "claude-test-1",
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 5, "output_tokens": 5},
        "content": [{"type": "text", "text": "ok"}],
    }
    rec.update(over)
    return rec


def test_response_diff_covers_more_than_content() -> None:
    assert reconstruct.semantic_diff_response(_doc_response(), _raw_response()) == []
    for field, wrong in (
        ("id", "msg_OTHER"),
        ("stop_reason", "max_tokens"),
        ("stop_sequence", "STOP"),
    ):
        diffs = reconstruct.semantic_diff_response(_doc_response(**{field: wrong}), _raw_response())
        assert diffs and field in diffs[0], (field, diffs)
    # token counts are part of the claim, too
    diffs = reconstruct.semantic_diff_response(
        _doc_response(usage={"input_tokens": 5, "output_tokens": 99}), _raw_response()
    )
    assert diffs and "usage" in diffs[0]


def test_response_model_tolerates_alias_expansion_only() -> None:
    # the server resolves an alias to a dated id — documented exception
    assert (
        reconstruct.semantic_diff_response(
            _doc_response(model="claude-haiku-4-5"),
            _raw_response(model="claude-haiku-4-5-20251001"),
        )
        == []
    )
    # a different model is a real gap
    diffs = reconstruct.semantic_diff_response(
        _doc_response(model="claude-haiku-4-5"), _raw_response(model="claude-opus-4-8-20260101")
    )
    assert diffs and "model" in diffs[0]


def _write_doc(tmp_path: Path, doc: dict[str, Any]) -> str:
    p = tmp_path / "normalized.json"
    p.write_text(json.dumps(doc))
    return str(p)


def test_roundtrip_is_green_on_a_synthetic_capture(tmp_path: Path) -> None:
    a, _b = mk_synth_resume_pair(tmp_path)
    doc = normalize(str(a), transcript=None)
    ok, msg = validate.roundtrip(_write_doc(tmp_path, doc), str(a))
    assert ok, msg


def test_roundtrip_fails_instead_of_skipping_a_missing_body_ref(tmp_path: Path) -> None:
    a, _b = mk_synth_resume_pair(tmp_path)
    doc = normalize(str(a), transcript=None)
    inf = next(e for e in doc["events"] if e["type"] == "inference")
    inf["response_body_ref"] = None  # still claims a pairing method
    ok, msg = validate.roundtrip(_write_doc(tmp_path, doc), str(a))
    assert not ok and "no response_body_ref" in msg


def test_roundtrip_fails_on_a_ref_that_is_not_in_raw_bodies(tmp_path: Path) -> None:
    a, _b = mk_synth_resume_pair(tmp_path)
    doc = normalize(str(a), transcript=None)
    inf = next(e for e in doc["events"] if e["type"] == "inference")
    inf["request_body_ref"] = "0" * 64
    ok, msg = validate.roundtrip(_write_doc(tmp_path, doc), str(a))
    assert not ok and "not in raw_bodies" in msg


def test_roundtrip_refuses_to_certify_when_it_checked_nothing(tmp_path: Path) -> None:
    a, _b = mk_synth_resume_pair(tmp_path)
    doc = normalize(str(a), transcript=None)
    doc["raw_bodies"] = {}
    doc["session"]["captured_with"]["raw_bodies"] = False  # nothing to check ...
    for e in doc["events"]:
        if e["type"] == "inference":
            e["request_body_ref"] = e["response_body_ref"] = None
    ok, msg = validate.roundtrip(_write_doc(tmp_path, doc), str(a))
    assert not ok and "checked 0 of" in msg  # ... so it must not pass


# --- the oracle reads files the DOCUMENT names, so it distrusts them -------------


def _one_ref(doc: dict[str, Any]) -> tuple[dict[str, Any], str]:
    inf = next(e for e in doc["events"] if e["type"] == "inference")
    return inf, inf["request_body_ref"]


def test_roundtrip_refuses_an_absolute_body_path(tmp_path: Path) -> None:
    a, _b = mk_synth_resume_pair(tmp_path)
    doc = normalize(str(a), transcript=None)
    _inf, ref = _one_ref(doc)
    secret = tmp_path / "secret.json"
    secret.write_text('{"model": "x"}')
    doc["raw_bodies"][ref]["path"] = str(secret)
    ok, msg = validate.roundtrip(_write_doc(tmp_path, doc), str(a))
    assert not ok and "refusing to read it" in msg and str(secret) in msg


def test_roundtrip_refuses_a_dot_dot_body_path(tmp_path: Path) -> None:
    a, _b = mk_synth_resume_pair(tmp_path)
    doc = normalize(str(a), transcript=None)
    _inf, ref = _one_ref(doc)
    doc["raw_bodies"][ref]["path"] = "bodies/../../../etc/passwd"
    ok, msg = validate.roundtrip(_write_doc(tmp_path, doc), str(a))
    assert not ok and "refusing to read it" in msg


def test_roundtrip_refuses_a_symlink_that_escapes_the_capture(tmp_path: Path) -> None:
    # A lexical check cannot see this one: the path has no "..", and the escape
    # only exists on disk.
    a, _b = mk_synth_resume_pair(tmp_path)
    doc = normalize(str(a), transcript=None)
    _inf, ref = _one_ref(doc)
    outside = tmp_path / "outside.request.json"
    outside.write_text('{"model": "x"}')
    (a / "bodies" / "escape.request.json").symlink_to(outside)
    doc["raw_bodies"][ref]["path"] = "bodies/escape.request.json"
    ok, msg = validate.roundtrip(_write_doc(tmp_path, doc), str(a))
    assert not ok and "refusing to read it" in msg


def test_roundtrip_refuses_a_path_of_the_wrong_shape(tmp_path: Path) -> None:
    a, _b = mk_synth_resume_pair(tmp_path)
    doc = normalize(str(a), transcript=None)
    _inf, ref = _one_ref(doc)
    for bad in ("otel/logs-0001.json", "bodies/nested/x.request.json", "bodies/x.json", ""):
        doc["raw_bodies"][ref]["path"] = bad
        ok, msg = validate.roundtrip(_write_doc(tmp_path, doc), str(a))
        assert not ok and "refusing to read it" in msg, bad


def test_roundtrip_verifies_the_body_against_the_sha_it_is_keyed_by(tmp_path: Path) -> None:
    # raw_bodies is keyed BY the sha of the raw body, so "the doc names a file"
    # and "the doc names THIS content" are one check apart.
    a, _b = mk_synth_resume_pair(tmp_path)
    doc = normalize(str(a), transcript=None)
    inf, _ref = _one_ref(doc)
    body = a / "bodies" / doc["raw_bodies"][inf["request_body_ref"]]["path"].split("/")[-1]
    body.write_text(body.read_text() + " ")  # one byte, semantically identical
    ok, msg = validate.roundtrip(_write_doc(tmp_path, doc), str(a))
    assert not ok and "not the raw_bodies key" in msg


def test_roundtrip_does_not_trust_the_documents_own_captured_with_flag(tmp_path: Path) -> None:
    # captured_with.raw_bodies is a claim the document makes about itself. Clearing
    # it must not talk the oracle out of checking bodies that are demonstrably there.
    a, _b = mk_synth_resume_pair(tmp_path)
    doc = normalize(str(a), transcript=None)
    doc["session"]["captured_with"]["raw_bodies"] = False
    inf, _ref = _one_ref(doc)
    inf["request_body_ref"] = None
    inf["response_body_ref"] = None
    ok, msg = validate.roundtrip(_write_doc(tmp_path, doc), str(a))
    assert not ok
    assert "no request_body_ref" in msg
    assert f"nothing was verified for inference(s) {inf['index']}" in msg


def test_roundtrip_names_every_inference_it_could_not_check(tmp_path: Path) -> None:
    a, _b = mk_synth_resume_pair(tmp_path)
    doc = normalize(str(a), transcript=None)
    inf, _ref = _one_ref(doc)
    # a second inference the oracle can never reach
    ghost = dict(inf)
    ghost["index"] = 1
    ghost["request_body_ref"] = None
    ghost["response_body_ref"] = None
    ghost["pairing"] = None
    doc["events"].append(ghost)
    ok, msg = validate.roundtrip(_write_doc(tmp_path, doc), str(a))
    assert not ok and "nothing was verified for inference(s) 1" in msg


# --- harness-validate certifies content, not just shape -------------------------


def test_validate_rejects_a_tampered_object(tmp_path: Path) -> None:
    # The store is content-addressed: an edited object is still schema-valid, and
    # certifying it VALID would certify the wrong property.
    a, _b = mk_synth_resume_pair(tmp_path)
    doc = normalize(str(a), transcript=None)
    key = next(k for k, o in doc["objects"].items() if o.get("kind") == "text")
    doc["objects"][key] = {**doc["objects"][key], "text": "tampered"}
    ok, msg = validate.validate(_write_doc(tmp_path, doc))
    assert not ok and "does not hash to its key" in msg


def test_validate_rejects_dangling_prelude_and_response_refs(tmp_path: Path) -> None:
    a, _b = mk_synth_resume_pair(tmp_path)
    doc = normalize(str(a), transcript=None)
    inf = next(e for e in doc["events"] if e["type"] == "inference")
    inf["response_block_refs"] = ["b" * 64]
    inf["prelude"]["system_refs"] = ["c" * 64]
    inf["prelude"]["tool_refs"] = ["d" * 64]
    ok, msg = validate.validate(_write_doc(tmp_path, doc))
    assert not ok
    assert "dangling response block ref" in msg
    assert "dangling prelude system ref" in msg
    assert "dangling prelude tool ref" in msg


def test_validate_rejects_a_body_ref_that_is_not_in_the_audit_ring(tmp_path: Path) -> None:
    a, _b = mk_synth_resume_pair(tmp_path)
    doc = normalize(str(a), transcript=None)
    inf = next(e for e in doc["events"] if e["type"] == "inference")
    inf["request_body_ref"] = "e" * 64
    ok, msg = validate.validate(_write_doc(tmp_path, doc))
    assert not ok and "request_body_ref" in msg and "not in raw_bodies" in msg


def test_validate_is_green_on_an_untouched_doc(tmp_path: Path) -> None:
    a, _b = mk_synth_resume_pair(tmp_path)
    doc = normalize(str(a), transcript=None)
    ok, msg = validate.validate(_write_doc(tmp_path, doc))
    assert ok, msg
    assert "VALID against events.schema.json" in msg and "semantic invariants OK" in msg
