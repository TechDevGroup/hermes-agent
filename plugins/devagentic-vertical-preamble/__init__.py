"""devagentic-vertical-preamble plugin entrypoint (G1 / hermes-agent#55).

Closes G1 of the devagentic+hermes+modal fusion rubric
(TechDevGroup/devagentic#203). When hermes boots against a profile
whose name matches a devagentic vertical user_id, this plugin auto-
loads the vertical's context docs (kind:vertical-spec, kind:grafted-
context, kind:worker-guardrails) and injects them as a pre-LLM-call
preamble — once per process — so the worker sees its vertical
manifest without anyone shelling out to ``python api.py`` or reading
``genesis/doc.jsonl`` directly.

Dependency: devagentic exposes ``verticalContext(userId)`` GraphQL
query (devagentic#204 / PR #205).

Failure semantics: any failure (no profile, devagentic unreachable,
no docs for this user_id, GraphQL error) returns None and the turn
proceeds without preamble. The plugin never blocks the turn and never
re-injects the preamble after the first successful load for a given
user_id in this process.

Enable/disable via ``hermes plugin enable|disable
devagentic-vertical-preamble``. Inert when disabled.
"""
from __future__ import annotations

import logging


logger = logging.getLogger(__name__)
