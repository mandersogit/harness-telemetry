"""harness-telemetry — capture + normalize pipeline for agentic-harness session internals.

The package owns the normalized event vocabulary (events.schema.json, shipped inside the
package and mirrored at the repo top level as the path-stable public copy, because it is
the contract for an unreleased companion project (session-fs) — a session filesystem —
that consumes these documents) and the tooling that produces documents in it: per-harness
normalizers (normalize/), the local OTLP capture sink (collector/), and the capture
launcher.
"""

__version__ = "0.1.0"
