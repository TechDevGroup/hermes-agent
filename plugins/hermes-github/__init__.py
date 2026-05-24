"""hermes-github plugin entrypoint (G3 of devagentic#203 /
hermes-agent#57).

Exposes a hermes-native ``file_issue`` MCP tool so worker sessions
can surface stack gaps as GitHub issues on TechDevGroup/devagentic
or TechDevGroup/hermes-agent without ever editing stack source
(#203 §3.2).

The plugin holds NO credentials of its own — token resolution
happens on the hermes host (env vars or ``gh auth token``) so the
worker conversation never sees the token. Restricted by design to
the two TechDevGroup stack repos to keep blast radius bounded.

Enable/disable via ``hermes plugin enable|disable hermes-github``.
Inert when disabled.
"""
from __future__ import annotations

import logging


logger = logging.getLogger(__name__)
