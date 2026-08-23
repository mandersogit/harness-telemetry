"""The schema is a shipped artifact, not a repo-layout accident.

Two things have to stay true at once: the canonical file lives INSIDE the package
(so `pip install .` carries it and harness-validate works from anywhere), and the
repo-root path `schema/events.schema.json` — which the $id and consuming projects
name — keeps resolving to that same file.
"""

import json
from pathlib import Path

from harness_telemetry.normalize import pricing, validate

REPO = Path(__file__).resolve().parents[1]
PUBLIC = REPO / "schema" / "events.schema.json"
PACKAGED = REPO / "src" / "harness_telemetry" / "schema" / "events.schema.json"


def test_public_path_is_a_symlink_to_the_packaged_schema() -> None:
    assert PUBLIC.is_symlink(), "schema/events.schema.json must stay a path-stable link"
    assert PUBLIC.resolve() == PACKAGED.resolve()
    assert not PUBLIC.readlink().is_absolute(), "the link must be relative to survive a clone"


def test_both_paths_parse_and_agree() -> None:
    public = json.loads(PUBLIC.read_text())
    packaged = json.loads(PACKAGED.read_text())
    assert public == packaged
    assert public["$id"].endswith("/schema/events.schema.json")


def test_validate_loads_the_schema_from_the_package_not_the_repo_root() -> None:
    # importlib.resources, so it resolves under a wheel/sdist install too
    assert Path(str(validate.SCHEMA)).resolve() == PACKAGED.resolve()
    loaded = validate.load_schema()
    assert loaded["title"] and loaded["$defs"]


def test_pairing_enum_matches_the_methods_the_pairer_emits() -> None:
    # The contract's vocabulary drifted from the code once already; pin it — and
    # pin the ORDER too, which the description calls "the order the emitter tries
    # them": the exact chain/content tiers first, the timing-derived window after.
    schema = validate.load_schema()
    inference = next(b for b in schema["$defs"]["event"]["oneOf"] if b.get("title") == "inference")
    assert inference["properties"]["pairing"]["enum"] == [
        "request-id-chain",
        "message-id-chain",
        "successor-embedding",
        "otel-window",
        "token-affinity",
        "elimination",
        None,
    ]


def test_schema_version_enum_only_lists_documented_versions() -> None:
    schema = validate.load_schema()
    versions = schema["properties"]["schema_version"]["enum"]
    changelog = schema["properties"]["schema_version"]["description"]
    for v in versions:
        assert f"{v} (" in changelog, f"schema_version {v} has no changelog entry"


def test_discount_rule_is_shared_by_env_and_cli() -> None:
    # one rule, three doors: parse_discount is the only gate
    assert pricing.parse_discount("0.10") == 0.10
    for bad in ("1.0", "1.5", "-0.5", "abc"):
        try:
            pricing.parse_discount(bad)
        except ValueError:
            continue
        raise AssertionError(f"parse_discount accepted {bad!r}")
