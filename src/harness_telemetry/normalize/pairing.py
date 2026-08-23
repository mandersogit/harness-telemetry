#!/usr/bin/env python3
"""
pairing.py — bind request bodies to response bodies without a shared key.

The request file carries no server id and the response file no client uuid, so
pairing has to be inferred. Pairing by mtime alternation — the obvious first
approach — silently DROPS calls whenever two lineages interleave in time
(measured: whole inferences vanished from sessions with a sub-agent or a
background call). This replaces it with the measured structure of real sessions
(see docs/claude-code.md, Channel 1):

  - Calls are SEQUENTIAL within a lineage (each request embeds the prior response);
    concurrency exists only ACROSS lineages (main vs sub-agent vs background).
  - A lineage = the requests sharing one prelude hash (content-only system + tools
    + params + model) — measured byte-stable per lineage on both fixtures.
  - A response's content blocks reappear (content-hashed) in the SUCCESSOR request
    of its lineage — the object store makes "which response fed this history" an
    exact set test. Thinking blocks match across the redaction boundary because
    both sides normalize to persisted-none + the same signature.

Every tier below resolves PER SUCCESSOR: the successor names (or embeds) the
response that fed it, and the predecessor request is then identified by history
containment. Binding by set membership plus an mtime zip — which is what an
earlier cut did — silently cross-pairs two same-prelude agents whose calls
complete out of order, producing a plausible, schema-valid, WRONG document.

Tiers run STRONGEST FIRST, and "strongest" means what the session itself states,
not what a clock suggests. Evidence beats timing: the chain fields are the
harness's own statement of which response fed which request, so they are resolved
before anything derived from event order or file mtimes. (This order is load-
bearing. Running the OTEL window first — as an earlier cut did — cross-paired two
concurrent same-prelude sub-agents whose OTEL records carried no call-start time:
their unambiguous cc_prev_req chains were never consulted, and the LIFO fallback
bound A's response to B's request under the confident label "otel-window".)

  0. request-id-chain (exact; discovered live 2026-07-02) — CC >= 2.1.198 embeds
     cc_prev_req=<the previous call's req_... id> in the successor's system[0]
     billing header, and req_ ids ARE response filenames: a direct file-level bind.
  1. message-id-chain (exact; discovered 2026-07-02) — requests carry
     diagnostics.previous_message_id = the msg_... id of the lineage's previous
     response. A response whose id appears among the next level's
     previous_message_ids is bound natively.
  2. successor-embedding — a response pairs with request r iff its block hashes all
     reappear among the assistant-block hashes of ONE successor of r, and r is the
     request whose history that successor continues. Same-level attempt siblings
     (retries/branches share a message count) are resolved the same way, one
     successor at a time.
     (0-2 are per lineage, requests ordered by message count then mtime.)
  3. otel-window — GLOBAL and lineage-free, for what the chains could not bind:
     replaying the OTEL exporter's own event.sequence pairs request FILES with
     response FILES. Only binds when the response's computed call-start time
     UNIQUELY picks one open request; an ambiguous or timing-less window is left
     to the tiers below rather than guessed at.
  4. lineage tails have no successor: remaining responses match remaining requests
     within the same model — unique candidate = "elimination"; several = rank by
     size (request byte length vs response input-token total, which are strongly
     correlated) = "token-affinity". A final 1:1 leftover pairs regardless of the
     model field (the GLM fixture's response reports a different model string than
     the request asked for).
  5. anything still unmatched stays unmatched, LOUDLY: unpaired requests become
     response-less inference events; orphan responses are reported by --check.

Pure algorithm over plain records; no I/O, no hashing (callers precompute).
"""

from collections.abc import Callable
from typing import Any

Record = dict[str, Any]

# Request record keys:  path, mtime, prelude_hash, nmsg, model, prev_msg_id,
#                       prev_req_id, assistant_hashes (set[str]),
#                       msg_hashes (set[str], all roles), size (bytes)
# Response record keys: path, mtime, block_hashes (list[str]), model, msg_id,
#                       input_total (input+cache_read+cache_creation tokens)


def _levels(lineage: list[Record]) -> list[list[Record]]:
    """Group a lineage's requests into message-count levels, both sorted ascending."""
    by_nmsg: dict[int, list[Record]] = {}
    for r in lineage:
        by_nmsg.setdefault(r["nmsg"], []).append(r)
    out = []
    for nmsg in sorted(by_nmsg):
        out.append(sorted(by_nmsg[nmsg], key=lambda r: r["mtime"]))
    return out


def _resp_file_id(R: Record) -> str:
    """The response file's id — for Anthropic first-party this IS the req_... id."""
    path: str = R["path"]
    return path.rsplit("/", 1)[-1][: -len(".response.json")]


def _hashes(r: Record) -> set[str]:
    """A request's full assembled-history content hashes (all roles), falling back
    to the assistant-only view for records that predate msg_hashes."""
    h = r.get("msg_hashes")
    if isinstance(h, set):
        return h
    a = r.get("assistant_hashes")
    return a if isinstance(a, set) else set()


def _predecessor_of(
    succ: Record, level: list[Record], resp: Record, taken: set[str]
) -> Record | None:
    """Which request at the previous level does `succ` continue?

    Only the successor knows: its assembled history IS the predecessor's history
    plus the predecessor's response (`resp`) plus the new turn. So the predecessor
    is the request whose every content hash still appears in the successor's, once
    the response's blocks are accounted for. Sorting both sides by mtime and
    zipping — the old shortcut — throws that evidence away and cross-pairs two
    same-prelude agents whose calls complete out of order.

    Returns None rather than guessing when the level is genuinely ambiguous; the
    unbound requests fall through to the tail tiers, which say so in `pairing`.
    """
    cands = [p for p in level if p["path"] not in taken]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]  # a lineage one call wide: no ambiguity
    hs = _hashes(succ) | set(resp["block_hashes"])
    contained = [p for p in cands if _hashes(p) <= hs]
    if len(contained) == 1:
        return contained[0]
    if len(contained) > 1:
        # Several histories survive in the successor: the nearest predecessor is
        # the one that contributed the MOST of them — but only if it is strictly
        # the most, else two candidates are indistinguishable and we decline.
        ranked = sorted(contained, key=lambda p: len(_hashes(p)), reverse=True)
        if len(_hashes(ranked[0])) > len(_hashes(ranked[1])):
            return ranked[0]
    return None


def _bind_by_chain(
    level: list[Record],
    nxt_level: list[Record],
    unpaired: list[Record],
    succ_field: str,
    resp_id: Callable[[Record], str | None],
) -> list[tuple[Record, Record]]:
    """Resolve chain edges ONE SUCCESSOR AT A TIME.

    Each successor names the id of the response that fed it (`succ_field`:
    cc_prev_req from the billing header, or diagnostics.previous_message_id).
    Resolve that id to the response record, then attach it to the predecessor
    request the successor actually continues — never by set membership plus an
    mtime zip, which loses which successor named which response.
    """
    by_id: dict[str, Record] = {}
    for R in unpaired:
        rid = resp_id(R)
        if rid:
            by_id.setdefault(rid, R)
    out: list[tuple[Record, Record]] = []
    taken_reqs: set[str] = set()
    taken_resps: set[int] = set()
    for succ in sorted(nxt_level, key=lambda r: r["mtime"]):
        named = succ.get(succ_field)
        if not named:
            continue
        resp = by_id.get(named)
        if resp is None or id(resp) in taken_resps:
            continue
        pred = _predecessor_of(succ, level, resp, taken_reqs)
        if pred is None:
            continue
        out.append((pred, resp))
        taken_reqs.add(pred["path"])
        taken_resps.add(id(resp))
    return out


def _bind_by_embedding(
    level: list[Record],
    nxt_level: list[Record],
    unpaired: list[Record],
) -> list[tuple[Record, Record]]:
    """Content pairing, also per successor: a response pairs with the request
    whose successor REPLAYS its blocks. Same discipline as the chain tiers — the
    successor that embeds the response chooses it, and the predecessor is then
    identified by history containment, not by mtime rank."""
    model = level[0]["model"]
    out: list[tuple[Record, Record]] = []
    taken_reqs: set[str] = set()
    taken_resps: set[int] = set()
    for succ in sorted(nxt_level, key=lambda r: r["mtime"]):
        cands = [
            R
            for R in unpaired
            if id(R) not in taken_resps
            and R["model"] == model
            and set(R["block_hashes"]) <= succ["assistant_hashes"]
        ]
        if not cands:
            continue
        # Several unpaired responses can be embedded in one successor (earlier
        # turns of the same thread); the one this level produced is the latest.
        resp = max(cands, key=lambda R: R["mtime"])
        pred = _predecessor_of(succ, level, resp, taken_reqs)
        if pred is None:
            continue
        out.append((pred, resp))
        taken_reqs.add(pred["path"])
        taken_resps.add(id(resp))
    return out


def pair_by_otel_window(
    requests: list[Record],
    responses: list[Record],
    body_index: dict[str, Any],
    start_time_of: dict[str, float] | None = None,
) -> dict[str, tuple[Record, str]]:
    """Global exact binds from the OTEL exporter's own event order (2026-07-02,
    from the first interactive capture): api_request_body/api_response_body carry
    a monotonic event.sequence, so replaying them binds request FILES to response
    FILES with no lineage knowledge at all.

    When several same-model requests are open (a retry pile-up, or streaming
    overlap — a sub-agent's first request opens before the spawning call's stream
    closes), the winner is chosen by CALL-START TIME: OTEL's api_request event
    carries duration_ms, so start = event ts - duration, and the request file's
    mtime marks when it was written — bind the response to the open request whose
    mtime is nearest its computed start (start_time_of maps response file id ->
    epoch start).

    That start time is the ONLY discriminator this tier has, so without one — or
    with two open requests equally near it — nothing is bound. The old LIFO
    fallback ("retry semantics") was a guess wearing an exact method's name: on
    two concurrent same-prelude agents it silently cross-paired them. Declining
    costs only the label; the request stays open here and reaches the tail tiers,
    which say what they are (token-affinity / elimination) or leave it unpaired.
    """
    timeline: list[tuple[int, int, Record]] = []
    for r in requests:
        b = body_index.get(r["path"].rsplit("/", 1)[-1])
        if b and b.get("sequence") is not None:
            timeline.append((b["sequence"], 0, r))
    for R in responses:
        b = body_index.get(R["path"].rsplit("/", 1)[-1])
        if b and b.get("sequence") is not None:
            timeline.append((b["sequence"], 1, R))
    timeline.sort(key=lambda t: (t[0], t[1]))

    open_reqs: list[Record] = []
    bound: dict[str, tuple[Record, str]] = {}
    for _seq, kind, rec in timeline:
        if kind == 0:
            open_reqs.append(rec)
            continue
        cands = [r for r in open_reqs if r["model"] == rec["model"]] or list(open_reqs)
        if not cands:
            continue
        rid = rec["path"].rsplit("/", 1)[-1][: -len(".response.json")]
        start = (start_time_of or {}).get(rid)
        if start is None:
            continue  # no timing evidence: leave it for the tail tiers
        ranked = sorted(cands, key=lambda r: abs(r["mtime"] - start))
        if len(ranked) > 1 and abs(ranked[0]["mtime"] - start) == abs(ranked[1]["mtime"] - start):
            continue  # two windows equally near: ambiguous, decline
        winner = ranked[0]
        open_reqs.remove(winner)
        bound[winner["path"]] = (rec, "otel-window")
    return bound


def pair(
    requests: list[Record],
    responses: list[Record],
    body_index: dict[str, Any] | None = None,
    start_time_of: dict[str, float] | None = None,
) -> tuple[list[tuple[Record, Record | None, str | None]], list[Record]]:
    """Bind requests to responses.

    Returns (pairs, orphan_responses) where pairs is one entry PER REQUEST —
    (request, response-or-None, method-or-None) — so no request can be dropped
    silently, and orphan_responses are responses no request claimed.
    """
    unpaired = list(responses)
    paired: dict[str, tuple[Record, str]] = {}  # request path -> (response, method)
    tails: list[Record] = []

    lineages: dict[str, list[Record]] = {}
    for r in requests:
        lineages.setdefault(r["prelude_hash"], []).append(r)

    for lineage in lineages.values():
        levels = _levels(lineage)
        for i, level in enumerate(levels):
            if i + 1 >= len(levels):
                tails.extend(level)
                continue
            nxt_level = levels[i + 1]
            # Tier 0 — exact: CC >= 2.1.198 puts the previous SERVER REQUEST id in
            # the successor's billing header, and req_ ids ARE response filenames.
            bound = _bind_by_chain(level, nxt_level, unpaired, "prev_req_id", _resp_file_id)
            method = "request-id-chain"
            if not bound:
                # Tier 1 — exact: the next level's previous_message_ids name the
                # responses that fed it.
                bound = _bind_by_chain(
                    level, nxt_level, unpaired, "prev_msg_id", lambda R: R.get("msg_id")
                )
                method = "message-id-chain"
            if not bound:
                # Tier 2 — content: the response's blocks reappear in the successor.
                bound = _bind_by_embedding(level, nxt_level, unpaired)
                method = "successor-embedding"
            for req, resp in bound:
                paired[req["path"]] = (resp, method)
                unpaired.remove(resp)
            # requests this level could not bind (attempt siblings with no
            # surviving response, or an ambiguity we declined to guess) fall
            # through to the tail tiers
            claimed = {req["path"] for req, _ in bound}
            tails.extend(p for p in level if p["path"] not in claimed)

    # Tier 3 — global, lineage-free: the OTEL exporter's own event order, for the
    # requests the chains could not bind. AFTER the exact tiers, never before: a
    # window is timing evidence, and timing must not overrule a chain that states
    # the answer outright.
    if body_index:
        rest = [r for r in tails if r["path"] not in paired]
        for req_path, (resp, method) in pair_by_otel_window(
            rest, unpaired, body_index, start_time_of
        ).items():
            paired[req_path] = (resp, method)
            unpaired.remove(resp)

    # Tails: match remaining responses within each model group.
    by_model_req: dict[str, list[Record]] = {}
    for r in tails:
        if r["path"] in paired:
            continue
        by_model_req.setdefault(r["model"], []).append(r)
    for model, reqs in by_model_req.items():
        cands = [R for R in unpaired if R["model"] == model]
        if len(reqs) == 1 and len(cands) == 1:
            paired[reqs[0]["path"]] = (cands[0], "elimination")
            unpaired.remove(cands[0])
        elif cands:
            # Rank affinity: bigger request (bytes) <-> bigger response input total.
            reqs_ranked = sorted(reqs, key=lambda r: -r["size"])
            cands_ranked = sorted(cands, key=lambda R: -R["input_total"])
            for req, resp in zip(reqs_ranked, cands_ranked, strict=False):
                paired[req["path"]] = (resp, "token-affinity")
                unpaired.remove(resp)

    # Final 1:1 leftover: pair regardless of the model field (a gateway response
    # may report a different model string than the request asked for).
    still_unpaired_reqs = [r for r in tails if r["path"] not in paired]
    if len(still_unpaired_reqs) == 1 and len(unpaired) == 1:
        paired[still_unpaired_reqs[0]["path"]] = (unpaired[0], "elimination")
        unpaired.clear()

    out: list[tuple[Record, Record | None, str | None]] = []
    for r in sorted(requests, key=lambda r: (r["mtime"], r["path"])):
        entry = paired.get(r["path"])
        if entry is None:
            out.append((r, None, None))
        else:
            out.append((r, entry[0], entry[1]))
    return out, unpaired
