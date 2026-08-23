"""Shared test helpers.

Everything the distributed suite needs is SYNTHETIC: built inline or under tmp_path,
committable, and safe to run anywhere.

capture_dir() is the one exception. It resolves a named capture under captures/ —
real session data, sensitive and gitignored — and exists for the maintainer-local
suite in tests/local/ (the `fixtures` marker), which is not distributed. It skips
the test when the named capture is not on this machine.
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
CAPTURES = REPO / "captures"


def capture_dir(name: str) -> Path:
    """The named local capture, or skip if this machine doesn't have it.

    Used only by the maintainer-local suite under tests/local/ (see the module
    docstring); the distributed tests never touch captures/.
    """
    d = CAPTURES / name
    if not d.is_dir():
        pytest.skip(f"local capture fixture {name} not present")
    return d


# --- synthetic captures (committable; no real data) ---------------------------

SYNTH_SYS = [{"type": "text", "text": "You are T."}]


# The system[0] billing header Claude Code >= 2.1.198 sends. cc_prev_req rides
# inside it; the normalizer strips that token before hashing the lineage, so a
# root request and its successors share one prelude — which is exactly what makes
# two parallel same-prelude agents indistinguishable without the chain fields.
SYNTH_BILLING = "x-anthropic-billing-header: v=1;"


def synth_uid(sid: str) -> dict[str, str]:
    return {"user_id": json.dumps({"session_id": sid, "account_uuid": "acc-1"})}


def write_synth_call(
    bodies: Path,
    *,
    sid: str,
    req_name: str,
    resp_rid: str,
    msg_id: str,
    messages: list[dict[str, Any]],
    prev_msg_id: str | None,
    t0: int,
    answer: str = "ok",
    prev_req_id: str | None = None,
    billing: bool = False,
    resp_dt: int = 1,
    write_response: bool = True,
) -> None:
    """One request/response body pair that load_request/load_response parse,
    with deterministic mtimes (req at t0, resp at t0 + resp_dt).

    billing=True adds the system[0] billing-header block (carrying cc_prev_req
    when prev_req_id is given); resp_dt lets a test make responses complete in a
    different order than the requests were written; write_response=False leaves
    the call response-less.
    """
    system = list(SYNTH_SYS)
    if billing:
        text = SYNTH_BILLING
        if prev_req_id:
            text += f" cc_prev_req={prev_req_id};"
        system.append({"type": "text", "text": text})
    req: dict[str, Any] = {
        "model": "claude-test-1",
        "max_tokens": 100,
        "system": system,
        "messages": messages,
        "metadata": synth_uid(sid),
    }
    if prev_msg_id:
        req["diagnostics"] = {"previous_message_id": prev_msg_id}
    rq = bodies / f"{req_name}.request.json"
    rq.write_text(json.dumps(req))
    os.utime(rq, (t0, t0))
    if not write_response:
        return
    resp = {
        "id": msg_id,
        "model": "claude-test-1",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": answer}],
        "usage": {"input_tokens": 5, "output_tokens": 5},
    }
    rp = bodies / f"{resp_rid}.response.json"
    rp.write_text(json.dumps(resp))
    os.utime(rp, (t0 + resp_dt, t0 + resp_dt))


def mk_synth_capture(
    root: Path,
    name: str,
    sid: str,
    *,
    req_name: str,
    resp_rid: str,
    msg_id: str,
    messages: list[dict[str, Any]],
    prev_msg_id: str | None,
    t0: int,
    answer: str = "ok",
) -> Path:
    """A minimal one-call capture dir."""
    bodies = root / name / "bodies"
    bodies.mkdir(parents=True)
    write_synth_call(
        bodies,
        sid=sid,
        req_name=req_name,
        resp_rid=resp_rid,
        msg_id=msg_id,
        messages=messages,
        prev_msg_id=prev_msg_id,
        t0=t0,
        answer=answer,
    )
    return root / name


def mk_synth_resume_pair(root: Path, sid_b: str = "sid-merge") -> tuple[Path, Path]:
    """capA (one call) + capB (one call chaining to capA's response via
    previous_message_id) — the synthetic resume-lifecycle pair."""
    a = mk_synth_capture(
        root,
        "capA",
        "sid-merge",
        req_name="aaaa0001",
        resp_rid="req_A1",
        msg_id="msg_A1",
        messages=[{"role": "user", "content": [{"type": "text", "text": "one"}]}],
        prev_msg_id=None,
        t0=1_751_400_000,
        answer="answer from capA",
    )
    b = mk_synth_capture(
        root,
        "capB",
        sid_b,
        req_name="bbbb0001",
        resp_rid="req_B1",
        msg_id="msg_B1",
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "one"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "answer from capA"}]},
            {"role": "user", "content": [{"type": "text", "text": "two"}]},
        ],
        prev_msg_id="msg_A1",
        t0=1_751_400_100,
        answer="answer from capB",
    )
    return a, b


def mk_synth_parallel_agents(
    root: Path, name: str = "parallel", *, chain: str = "request-id"
) -> Path:
    """Two same-prelude agents running in PARALLEL, completing OUT OF ORDER.

    alpha's request is written first but its response lands last; each agent's
    follow-up names its OWN predecessor response (cc_prev_req when
    chain="request-id", diagnostics.previous_message_id when chain="message-id").
    Binding by id-set membership plus an mtime zip cross-pairs the two agents
    here; per-successor resolution does not.
    """
    bodies = root / name / "bodies"
    bodies.mkdir(parents=True)
    billing = chain == "request-id"
    t0 = 1_751_500_000
    # roots: alpha written first (t0), beta second (t0+1) ...
    write_synth_call(
        bodies,
        sid="sid-par",
        req_name="aaaa0001",
        resp_rid="req_ALPHA",
        msg_id="msg_ALPHA",
        prev_msg_id=None,
        t0=t0,
        billing=billing,
        messages=[{"role": "user", "content": [{"type": "text", "text": "alpha one"}]}],
        answer="alpha answer",
        resp_dt=3,
    )  # ... but completes LAST
    write_synth_call(
        bodies,
        sid="sid-par",
        req_name="bbbb0001",
        resp_rid="req_BETA",
        msg_id="msg_BETA",
        prev_msg_id=None,
        t0=t0 + 1,
        billing=billing,
        messages=[{"role": "user", "content": [{"type": "text", "text": "beta one"}]}],
        answer="beta answer",
        resp_dt=1,
    )  # ... and completes FIRST
    for who, req_name, prev_rid, prev_mid in (
        ("alpha", "aaaa0002", "req_ALPHA", "msg_ALPHA"),
        ("beta", "bbbb0002", "req_BETA", "msg_BETA"),
    ):
        write_synth_call(
            bodies,
            sid="sid-par",
            req_name=req_name,
            resp_rid=f"req_{who}2",
            msg_id=f"msg_{who}2",
            t0=t0 + 10,
            billing=billing,
            prev_req_id=prev_rid if billing else None,
            prev_msg_id=prev_mid,
            messages=[
                {"role": "user", "content": [{"type": "text", "text": f"{who} one"}]},
                {"role": "assistant", "content": [{"type": "text", "text": f"{who} answer"}]},
                {"role": "user", "content": [{"type": "text", "text": f"{who} two"}]},
            ],
            write_response=False,
        )
    return root / name
