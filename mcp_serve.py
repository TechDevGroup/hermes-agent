"""
Hermes MCP Server — expose messaging conversations as MCP tools.

Starts a stdio MCP server that lets any MCP client (Claude Code, Cursor, Codex,
etc.) list conversations, read message history, send messages, poll for live
events, and manage approval requests across all connected platforms.

Matches OpenClaw's 9-tool MCP channel bridge surface:
  conversations_list, conversation_get, messages_read, attachments_fetch,
  events_poll, events_wait, messages_send, permissions_list_open,
  permissions_respond

Plus: channels_list (Hermes-specific extra)

Usage:
    hermes mcp serve
    hermes mcp serve --verbose

MCP client config (e.g. claude_desktop_config.json):
    {
        "mcpServers": {
            "hermes": {
                "command": "hermes",
                "args": ["mcp", "serve"]
            }
        }
    }
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("hermes.mcp_serve")

# ---------------------------------------------------------------------------
# Lazy MCP SDK import
# ---------------------------------------------------------------------------

_MCP_SERVER_AVAILABLE = False
try:
    from mcp.server.fastmcp import FastMCP

    _MCP_SERVER_AVAILABLE = True
except ImportError:
    FastMCP = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_sessions_dir() -> Path:
    """Return the sessions directory using HERMES_HOME."""
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "sessions"
    except ImportError:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "sessions"


def _get_session_db():
    """Get a SessionDB instance for reading message transcripts."""
    try:
        from hermes_state import SessionDB
        return SessionDB()
    except Exception as e:
        logger.debug("SessionDB unavailable: %s", e)
        return None


def _load_sessions_index() -> dict:
    """Load the gateway sessions.json index directly.

    Returns a dict of session_key -> entry_dict with platform routing info.
    This avoids importing the full SessionStore which needs GatewayConfig.
    """
    sessions_file = _get_sessions_dir() / "sessions.json"
    if not sessions_file.exists():
        return {}
    try:
        with open(sessions_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.debug("Failed to load sessions.json: %s", e)
        return {}


def _load_channel_directory() -> dict:
    """Load the cached channel directory for available targets."""
    try:
        from hermes_constants import get_hermes_home
        directory_file = get_hermes_home() / "channel_directory.json"
    except ImportError:
        directory_file = Path(
            os.environ.get("HERMES_HOME", Path.home() / ".hermes")
        ) / "channel_directory.json"

    if not directory_file.exists():
        return {}
    try:
        with open(directory_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.debug("Failed to load channel_directory.json: %s", e)
        return {}


def _coerce_int(
    value,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Coerce value to int with fallback and clamping.

    Used at MCP tool boundaries to handle invalid types from external clients.
    Returns default if value cannot be converted to int.
    """
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        coerced = default
    return max(minimum, min(coerced, maximum))


def _extract_message_content(msg: dict) -> str:
    """Extract text content from a message, handling multi-part content."""
    content = msg.get("content", "")
    if isinstance(content, list):
        text_parts = [
            p.get("text", "") for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        return "\n".join(text_parts)
    return str(content) if content else ""


def _extract_attachments(msg: dict) -> List[dict]:
    """Extract non-text attachments from a message.

    Finds: multi-part image/file content blocks, MEDIA: tags in text,
    image URLs, and file references.
    """
    attachments = []
    content = msg.get("content", "")

    # Multi-part content blocks (image_url, file, etc.)
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type", "")
            if ptype == "image_url":
                url = part.get("image_url", {}).get("url", "") if isinstance(part.get("image_url"), dict) else ""
                if url:
                    attachments.append({"type": "image", "url": url})
            elif ptype == "image":
                url = part.get("url", part.get("source", {}).get("url", ""))
                if url:
                    attachments.append({"type": "image", "url": url})
            elif ptype not in {"text",}:
                # Unknown non-text content type
                attachments.append({"type": ptype, "data": part})

    # MEDIA: tags in text content
    text = _extract_message_content(msg)
    if text:
        media_pattern = re.compile(r'MEDIA:\s*(\S+)')
        for match in media_pattern.finditer(text):
            path = match.group(1)
            attachments.append({"type": "media", "path": path})

    return attachments


# ---------------------------------------------------------------------------
# Event Bridge — polls SessionDB for new messages, maintains event queue
# ---------------------------------------------------------------------------

QUEUE_LIMIT = 1000
POLL_INTERVAL = 0.2  # seconds between DB polls (200ms)


@dataclass
class QueueEvent:
    """An event in the bridge's in-memory queue."""
    cursor: int
    type: str  # "message", "approval_requested", "approval_resolved"
    session_key: str = ""
    data: dict = field(default_factory=dict)


class EventBridge:
    """Background poller that watches SessionDB for new messages and
    maintains an in-memory event queue with waiter support.

    This is the Hermes equivalent of OpenClaw's WebSocket gateway bridge.
    Instead of WebSocket events, we poll the SQLite database for changes.
    """

    def __init__(self):
        self._queue: List[QueueEvent] = []
        self._cursor = 0
        self._lock = threading.Lock()
        self._new_event = threading.Event()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._last_poll_timestamps: Dict[str, float] = {}  # session_key -> unix timestamp
        # In-memory approval tracking (populated from events)
        self._pending_approvals: Dict[str, dict] = {}
        # mtime cache — skip expensive work when files haven't changed
        self._sessions_json_mtime: float = 0.0
        self._state_db_mtime: float = 0.0
        self._cached_sessions_index: dict = {}

    def start(self):
        """Start the background polling thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logger.debug("EventBridge started")

    def stop(self):
        """Stop the background polling thread."""
        self._running = False
        self._new_event.set()  # Wake any waiters
        if self._thread:
            self._thread.join(timeout=5)
        logger.debug("EventBridge stopped")

    def poll_events(
        self,
        after_cursor: int = 0,
        session_key: Optional[str] = None,
        limit: int = 20,
    ) -> dict:
        """Return events since after_cursor, optionally filtered by session_key."""
        with self._lock:
            events = [
                e for e in self._queue
                if e.cursor > after_cursor
                and (not session_key or e.session_key == session_key)
            ][:limit]

        next_cursor = events[-1].cursor if events else after_cursor
        return {
            "events": [
                {"cursor": e.cursor, "type": e.type,
                 "session_key": e.session_key, **e.data}
                for e in events
            ],
            "next_cursor": next_cursor,
        }

    def wait_for_event(
        self,
        after_cursor: int = 0,
        session_key: Optional[str] = None,
        timeout_ms: int = 30000,
    ) -> Optional[dict]:
        """Block until a matching event arrives or timeout expires."""
        deadline = time.monotonic() + (timeout_ms / 1000.0)

        while time.monotonic() < deadline:
            with self._lock:
                for e in self._queue:
                    if e.cursor > after_cursor and (
                        not session_key or e.session_key == session_key
                    ):
                        return {
                            "cursor": e.cursor, "type": e.type,
                            "session_key": e.session_key, **e.data,
                        }

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._new_event.clear()
            self._new_event.wait(timeout=min(remaining, POLL_INTERVAL))

        return None

    def list_pending_approvals(self) -> List[dict]:
        """List approval requests observed during this bridge session."""
        with self._lock:
            return sorted(
                self._pending_approvals.values(),
                key=lambda a: a.get("created_at", ""),
            )

    def respond_to_approval(self, approval_id: str, decision: str) -> dict:
        """Resolve a pending approval (best-effort without gateway IPC)."""
        with self._lock:
            approval = self._pending_approvals.pop(approval_id, None)

        if not approval:
            return {"error": f"Approval not found: {approval_id}"}

        self._enqueue(QueueEvent(
            cursor=0,  # Will be set by _enqueue
            type="approval_resolved",
            session_key=approval.get("session_key", ""),
            data={"approval_id": approval_id, "decision": decision},
        ))

        return {"resolved": True, "approval_id": approval_id, "decision": decision}

    def _enqueue(self, event: QueueEvent) -> None:
        """Add an event to the queue and wake any waiters."""
        with self._lock:
            self._cursor += 1
            event.cursor = self._cursor
            self._queue.append(event)
            # Trim queue to limit
            while len(self._queue) > QUEUE_LIMIT:
                self._queue.pop(0)
        self._new_event.set()

    def _poll_loop(self):
        """Background loop: poll SessionDB for new messages."""
        db = _get_session_db()
        if not db:
            logger.warning("EventBridge: SessionDB unavailable, event polling disabled")
            return

        while self._running:
            try:
                self._poll_once(db)
            except Exception as e:
                logger.debug("EventBridge poll error: %s", e)
            time.sleep(POLL_INTERVAL)

    def _poll_once(self, db):
        """Check for new messages across all sessions.

        Uses mtime checks on sessions.json and state.db to skip work
        when nothing has changed — makes 200ms polling essentially free.
        """
        # Check if sessions.json has changed (mtime check is ~1μs)
        sessions_file = _get_sessions_dir() / "sessions.json"
        try:
            sj_mtime = sessions_file.stat().st_mtime if sessions_file.exists() else 0.0
        except OSError:
            sj_mtime = 0.0

        if sj_mtime != self._sessions_json_mtime:
            self._sessions_json_mtime = sj_mtime
            self._cached_sessions_index = _load_sessions_index()

        # Check if state.db has changed
        try:
            from hermes_constants import get_hermes_home
            db_file = get_hermes_home() / "state.db"
        except ImportError:
            db_file = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / "state.db"

        try:
            db_mtime = db_file.stat().st_mtime if db_file.exists() else 0.0
        except OSError:
            db_mtime = 0.0

        if db_mtime == self._state_db_mtime and sj_mtime == self._sessions_json_mtime:
            return  # Nothing changed since last poll — skip entirely

        self._state_db_mtime = db_mtime
        entries = self._cached_sessions_index

        for session_key, entry in entries.items():
            session_id = entry.get("session_id", "")
            if not session_id:
                continue

            last_seen = self._last_poll_timestamps.get(session_key, 0.0)

            try:
                messages = db.get_messages(session_id)
            except Exception:
                continue

            if not messages:
                continue

            # Normalize timestamps to float for comparison
            def _ts_float(ts) -> float:
                if isinstance(ts, (int, float)):
                    return float(ts)
                if isinstance(ts, str) and ts:
                    try:
                        return float(ts)
                    except ValueError:
                        # ISO string — parse to epoch
                        try:
                            from datetime import datetime
                            return datetime.fromisoformat(ts).timestamp()
                        except Exception:
                            return 0.0
                return 0.0

            # Find messages newer than our last seen timestamp
            new_messages = []
            for msg in messages:
                ts = _ts_float(msg.get("timestamp", 0))
                role = msg.get("role", "")
                if role not in {"user", "assistant"}:
                    continue
                if ts > last_seen:
                    new_messages.append(msg)

            for msg in new_messages:
                content = _extract_message_content(msg)
                if not content:
                    continue
                self._enqueue(QueueEvent(
                    cursor=0,
                    type="message",
                    session_key=session_key,
                    data={
                        "role": msg.get("role", ""),
                        "content": content[:500],
                        "timestamp": str(msg.get("timestamp", "")),
                        "message_id": str(msg.get("id", "")),
                    },
                ))

            # Update last seen to the most recent message timestamp
            all_ts = [_ts_float(m.get("timestamp", 0)) for m in messages]
            if all_ts:
                latest = max(all_ts)
                if latest > last_seen:
                    self._last_poll_timestamps[session_key] = latest


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

def create_mcp_server(event_bridge: Optional[EventBridge] = None) -> "FastMCP":
    """Create and return the Hermes MCP server with all tools registered."""
    if not _MCP_SERVER_AVAILABLE:
        raise ImportError(
            "MCP server requires the 'mcp' package. "
            f"Install with: {sys.executable} -m pip install 'mcp'"
        )

    mcp = FastMCP(
        "hermes",
        instructions=(
            "Hermes Agent messaging bridge. Use these tools to interact with "
            "conversations across Telegram, Discord, Slack, WhatsApp, Signal, "
            "Matrix, and other connected platforms."
        ),
    )

    bridge = event_bridge or EventBridge()

    # -- conversations_list ------------------------------------------------

    @mcp.tool()
    def conversations_list(
        platform: Optional[str] = None,
        limit: int = 50,
        search: Optional[str] = None,
    ) -> str:
        """List active messaging conversations across connected platforms.

        Returns conversations with their session keys (needed for messages_read),
        platform, chat type, display name, and last activity time.

        Args:
            platform: Filter by platform name (telegram, discord, slack, etc.)
            limit: Maximum number of conversations to return (default 50)
            search: Optional text to filter conversations by name
        """
        limit = _coerce_int(limit, default=50, minimum=1, maximum=200)
        entries = _load_sessions_index()
        conversations = []

        for key, entry in entries.items():
            origin = entry.get("origin", {})
            entry_platform = entry.get("platform") or origin.get("platform", "")

            if platform and entry_platform.lower() != platform.lower():
                continue

            display_name = entry.get("display_name", "")
            chat_name = origin.get("chat_name", "")
            if search:
                search_lower = search.lower()
                if (search_lower not in display_name.lower()
                        and search_lower not in chat_name.lower()
                        and search_lower not in key.lower()):
                    continue

            conversations.append({
                "session_key": key,
                "session_id": entry.get("session_id", ""),
                "platform": entry_platform,
                "chat_type": entry.get("chat_type", origin.get("chat_type", "")),
                "display_name": display_name,
                "chat_name": chat_name,
                "user_name": origin.get("user_name", ""),
                "updated_at": entry.get("updated_at", ""),
            })

        conversations.sort(key=lambda c: c.get("updated_at", ""), reverse=True)
        conversations = conversations[:limit]

        return json.dumps({
            "count": len(conversations),
            "conversations": conversations,
        }, indent=2)

    # -- conversation_get --------------------------------------------------

    @mcp.tool()
    def conversation_get(session_key: str) -> str:
        """Get detailed info about one conversation by its session key.

        Args:
            session_key: The session key from conversations_list
        """
        entries = _load_sessions_index()
        entry = entries.get(session_key)

        if not entry:
            return json.dumps({"error": f"Conversation not found: {session_key}"})

        origin = entry.get("origin", {})
        return json.dumps({
            "session_key": session_key,
            "session_id": entry.get("session_id", ""),
            "platform": entry.get("platform") or origin.get("platform", ""),
            "chat_type": entry.get("chat_type", origin.get("chat_type", "")),
            "display_name": entry.get("display_name", ""),
            "user_name": origin.get("user_name", ""),
            "chat_name": origin.get("chat_name", ""),
            "chat_id": origin.get("chat_id", ""),
            "thread_id": origin.get("thread_id"),
            "updated_at": entry.get("updated_at", ""),
            "created_at": entry.get("created_at", ""),
            "input_tokens": entry.get("input_tokens", 0),
            "output_tokens": entry.get("output_tokens", 0),
            "total_tokens": entry.get("total_tokens", 0),
        }, indent=2)

    # -- messages_read -----------------------------------------------------

    @mcp.tool()
    def messages_read(
        session_key: str,
        limit: int = 50,
    ) -> str:
        """Read recent messages from a conversation.

        Returns the message history in chronological order with role, content,
        and timestamp for each message.

        Args:
            session_key: The session key from conversations_list
            limit: Maximum number of messages to return (default 50, most recent)
        """
        limit = _coerce_int(limit, default=50, minimum=1, maximum=200)
        entries = _load_sessions_index()
        entry = entries.get(session_key)
        if not entry:
            return json.dumps({"error": f"Conversation not found: {session_key}"})

        session_id = entry.get("session_id", "")
        if not session_id:
            return json.dumps({"error": "No session ID for this conversation"})

        db = _get_session_db()
        if not db:
            return json.dumps({"error": "Session database unavailable"})

        try:
            all_messages = db.get_messages(session_id)
        except Exception as e:
            return json.dumps({"error": f"Failed to read messages: {e}"})

        filtered = []
        for msg in all_messages:
            role = msg.get("role", "")
            if role in {"user", "assistant"}:
                content = _extract_message_content(msg)
                if content:
                    filtered.append({
                        "id": str(msg.get("id", "")),
                        "role": role,
                        "content": content[:2000],
                        "timestamp": msg.get("timestamp", ""),
                    })

        messages = filtered[-limit:]

        return json.dumps({
            "session_key": session_key,
            "count": len(messages),
            "total_in_session": len(filtered),
            "messages": messages,
        }, indent=2)

    # -- attachments_fetch -------------------------------------------------

    @mcp.tool()
    def attachments_fetch(
        session_key: str,
        message_id: str,
    ) -> str:
        """List non-text attachments for a message in a conversation.

        Extracts images, media files, and other non-text content blocks
        from the specified message.

        Args:
            session_key: The session key from conversations_list
            message_id: The message ID from messages_read
        """
        entries = _load_sessions_index()
        entry = entries.get(session_key)
        if not entry:
            return json.dumps({"error": f"Conversation not found: {session_key}"})

        session_id = entry.get("session_id", "")
        if not session_id:
            return json.dumps({"error": "No session ID for this conversation"})

        db = _get_session_db()
        if not db:
            return json.dumps({"error": "Session database unavailable"})

        try:
            all_messages = db.get_messages(session_id)
        except Exception as e:
            return json.dumps({"error": f"Failed to read messages: {e}"})

        # Find the target message
        target_msg = None
        for msg in all_messages:
            if str(msg.get("id", "")) == message_id:
                target_msg = msg
                break

        if not target_msg:
            return json.dumps({"error": f"Message not found: {message_id}"})

        attachments = _extract_attachments(target_msg)

        return json.dumps({
            "message_id": message_id,
            "count": len(attachments),
            "attachments": attachments,
        }, indent=2)

    # -- events_poll -------------------------------------------------------

    @mcp.tool()
    def events_poll(
        after_cursor: int = 0,
        session_key: Optional[str] = None,
        limit: int = 20,
    ) -> str:
        """Poll for new conversation events since a cursor position.

        Returns events that have occurred since the given cursor. Use the
        returned next_cursor value for subsequent polls.

        Event types: message, approval_requested, approval_resolved

        Args:
            after_cursor: Return events after this cursor (0 for all)
            session_key: Optional filter to one conversation
            limit: Maximum events to return (default 20)
        """
        after_cursor = _coerce_int(after_cursor, default=0, minimum=0, maximum=10**18)
        limit = _coerce_int(limit, default=20, minimum=1, maximum=200)
        result = bridge.poll_events(
            after_cursor=after_cursor,
            session_key=session_key,
            limit=limit,
        )
        return json.dumps(result, indent=2)

    # -- events_wait -------------------------------------------------------

    @mcp.tool()
    def events_wait(
        after_cursor: int = 0,
        session_key: Optional[str] = None,
        timeout_ms: int = 30000,
    ) -> str:
        """Wait for the next conversation event (long-poll).

        Blocks until a matching event arrives or the timeout expires.
        Use this for near-real-time event delivery without polling.

        Args:
            after_cursor: Wait for events after this cursor
            session_key: Optional filter to one conversation
            timeout_ms: Maximum wait time in milliseconds (default 30000)
        """
        after_cursor = _coerce_int(after_cursor, default=0, minimum=0, maximum=10**18)
        timeout_ms = _coerce_int(
            timeout_ms,
            default=30000,
            minimum=0,
            maximum=300000,
        )  # Cap at 5 minutes
        event = bridge.wait_for_event(
            after_cursor=after_cursor,
            session_key=session_key,
            timeout_ms=timeout_ms,
        )
        if event:
            return json.dumps({"event": event}, indent=2)
        return json.dumps({"event": None, "reason": "timeout"}, indent=2)

    # -- messages_send -----------------------------------------------------

    @mcp.tool()
    def messages_send(
        target: str,
        message: str,
    ) -> str:
        """Send a message to a platform conversation.

        The target format is "platform:chat_id" — same format used by the
        channels_list tool. You can also use human-friendly channel names
        that will be resolved automatically.

        Examples:
            target="telegram:6308981865"
            target="discord:#general"
            target="slack:#engineering"

        Args:
            target: Platform target in "platform:identifier" format
            message: The message text to send
        """
        if not target or not message:
            return json.dumps({"error": "Both target and message are required"})

        try:
            from tools.send_message_tool import send_message_tool
            result_str = send_message_tool(
                {"action": "send", "target": target, "message": message}
            )
            return result_str
        except ImportError:
            return json.dumps({"error": "Send message tool not available"})
        except Exception as e:
            return json.dumps({"error": f"Send failed: {e}"})

    # -- channels_list -----------------------------------------------------

    @mcp.tool()
    def channels_list(platform: Optional[str] = None) -> str:
        """List available messaging channels and targets across platforms.

        Returns channels that you can send messages to. The target strings
        returned here can be used directly with the messages_send tool.

        Args:
            platform: Filter by platform name (telegram, discord, slack, etc.)
        """
        directory = _load_channel_directory()
        if not directory:
            entries = _load_sessions_index()
            targets = []
            seen = set()
            for key, entry in entries.items():
                origin = entry.get("origin", {})
                p = entry.get("platform") or origin.get("platform", "")
                chat_id = origin.get("chat_id", "")
                if not p or not chat_id:
                    continue
                if platform and p.lower() != platform.lower():
                    continue
                target_str = f"{p}:{chat_id}"
                if target_str in seen:
                    continue
                seen.add(target_str)
                targets.append({
                    "target": target_str,
                    "platform": p,
                    "name": entry.get("display_name") or origin.get("chat_name", ""),
                    "chat_type": entry.get("chat_type", origin.get("chat_type", "")),
                })
            return json.dumps({"count": len(targets), "channels": targets}, indent=2)

        channels = []
        for plat, entries_list in directory.get("platforms", {}).items():
            if platform and plat.lower() != platform.lower():
                continue
            if isinstance(entries_list, list):
                for ch in entries_list:
                    if isinstance(ch, dict):
                        chat_id = ch.get("id", ch.get("chat_id", ""))
                        channels.append({
                            "target": f"{plat}:{chat_id}" if chat_id else plat,
                            "platform": plat,
                            "name": ch.get("name", ch.get("display_name", "")),
                            "chat_type": ch.get("type", ""),
                        })

        return json.dumps({"count": len(channels), "channels": channels}, indent=2)

    # -- permissions_list_open ---------------------------------------------

    @mcp.tool()
    def permissions_list_open() -> str:
        """List pending approval requests observed during this bridge session.

        Returns exec and plugin approval requests that the bridge has seen
        since it started. Approvals are live-session only — older approvals
        from before the bridge connected are not included.
        """
        approvals = bridge.list_pending_approvals()
        return json.dumps({
            "count": len(approvals),
            "approvals": approvals,
        }, indent=2)

    # -- permissions_respond -----------------------------------------------

    @mcp.tool()
    def permissions_respond(
        id: str,
        decision: str,
    ) -> str:
        """Respond to a pending approval request.

        Args:
            id: The approval ID from permissions_list_open
            decision: One of "allow-once", "allow-always", or "deny"
        """
        if decision not in {"allow-once", "allow-always", "deny"}:
            return json.dumps({
                "error": f"Invalid decision: {decision}. "
                         f"Must be allow-once, allow-always, or deny"
            })

        result = bridge.respond_to_approval(id, decision)
        return json.dumps(result, indent=2)

    # ---------------------------------------------------------------------
    # Canvas tools (issue #56) — devagentic canvas operations exposed as
    # MCP tools. The devagentic-canvas plugin (#55) is the source of truth
    # for the HTTP client; these tools just adapt its return shape to MCP's
    # one-string-out convention.
    #
    # Auth: each tool calls the plugin client which threads X-User-Id +
    # Authorization through to devagentic. The MCP host's bearer (typically
    # set via the spawning hermes process's env) flows through naturally
    # because we share the same DEVAGENTIC_API_KEY / DEVAGENTIC_USER_ID
    # resolution.
    #
    # All errors from devagentic propagate as `{"error": "<msg>"}` JSON
    # strings — no MCP tool ever raises.
    # ---------------------------------------------------------------------

    # Each registrar is wrapped defensively: if one fails (e.g., a
    # plugin module is malformed in a deployed wheel), the others
    # still register and the failure surfaces in stderr instead of
    # crashing the whole server. Per issue #88.
    _devagentic_registrars = (
        ("canvas", _register_canvas_tools),
        ("docs", _register_docs_tools),
        ("devagentic-mutations", _register_devagentic_mutation_tools),
        ("github", _register_github_tools),
        ("lane-h", _register_lane_h_tools),
    )
    for _label, _registrar in _devagentic_registrars:
        try:
            _registrar(mcp)
        except Exception as exc:
            logger.warning(
                "MCP server: registrar %r failed: %s — peers still register",
                _label, exc,
            )

    # Boot-line summary so deployed-container subprocess stderr exposes
    # exactly which tools are present in the loaded mcp_serve build.
    # Without this, operators have to truncate-enumerate (see #88 false
    # alarm). Per issue #88.
    try:
        _tool_names = sorted(
            getattr(t, "name", "") for t in mcp._tool_manager.list_tools()
        )
        logger.info(
            "MCP server boot: registered %d tools: %s",
            len(_tool_names), _tool_names,
        )
    except Exception as exc:
        logger.debug("MCP server boot: tool enumeration failed: %s", exc)

    return mcp


def _resolve_canvas_client():
    """Load the devagentic-canvas plugin's HTTP client module. The
    plugin's directory is hyphenated (`plugins/devagentic-canvas/`),
    which Python's standard `import plugins.devagentic_canvas` can't
    resolve — so we load by file path. Returns None when the plugin
    isn't present (the MCP tools then return error JSON to the host
    without crashing the server)."""
    try:
        import importlib.util
        from pathlib import Path
        plugin_dir = (Path(__file__).resolve().parent
                      / "plugins" / "devagentic-canvas")
        client_path = plugin_dir / "client.py"
        if not client_path.is_file():
            return None
        spec = importlib.util.spec_from_file_location(
            "_devagentic_canvas_client", client_path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as exc:
        logger.debug("canvas MCP: plugin client unavailable: %s", exc)
        return None


def _register_canvas_tools(mcp: "FastMCP") -> None:
    """Register the devagentic canvas tools on `mcp`. Pulled into its
    own function so the create_mcp_server body stays readable and
    tests can drive the registration in isolation.

    Tools registered (issue #56):
      canvas_list, canvas_open, canvas_add_node, canvas_move_node,
      canvas_update_node, canvas_delete_node, canvas_link_nodes,
      canvas_delete_edge, canvas_search.
    """

    def _err(msg: str) -> str:
        return json.dumps({"error": msg})

    @mcp.tool()
    def canvas_list() -> str:
        """List the authenticated user's devagentic canvases.

        Auth: uses the same X-User-Id resolution as the
        devagentic-canvas plugin — `DEVAGENTIC_USER_ID` env or
        the active hermes profile. Bearer token from
        `DEVAGENTIC_API_KEY`.

        Returns: JSON with `{"count": N, "canvases": [...]}` on
        success or `{"error": ...}` on failure.
        """
        c = _resolve_canvas_client()
        if c is None:
            return _err("canvas plugin not available on this hermes "
                        "install (missing plugins/devagentic-canvas/)")
        canvases = c.list_canvases()
        if canvases is None:
            return _err("devagentic unreachable or auth failed; "
                        "check DEVAGENTIC_BASE_URL + DEVAGENTIC_USER_ID")
        return json.dumps({"count": len(canvases),
                           "canvases": canvases}, indent=2)

    @mcp.tool()
    def canvas_open(canvas_id: str) -> str:
        """Get the full state of a canvas: metadata + nodes + edges.

        Args:
            canvas_id: The canvas id (from canvas_list).
        """
        if not canvas_id:
            return _err("canvas_id is required")
        c = _resolve_canvas_client()
        if c is None:
            return _err("canvas plugin not available")
        state = c.get_canvas(canvas_id)
        if state is None:
            return _err(f"canvas {canvas_id!r} not found or "
                        "devagentic unreachable")
        return json.dumps(state, indent=2)

    @mcp.tool()
    def canvas_add_node(canvas_id: str, node_type: str,
                        position_x: Optional[float] = None,
                        position_y: Optional[float] = None) -> str:
        """Add a node to a canvas.

        Args:
            canvas_id: The canvas id.
            node_type: The node kind label (e.g. `doc`, `assertion`,
                       `decision`).
            position_x: Optional x coordinate.
            position_y: Optional y coordinate.
        """
        if not canvas_id or not node_type:
            return _err("canvas_id and node_type are required")
        c = _resolve_canvas_client()
        if c is None:
            return _err("canvas plugin not available")
        pos = None
        if position_x is not None and position_y is not None:
            pos = {"x": float(position_x), "y": float(position_y)}
        node = c.add_node(canvas_id, node_type, position=pos)
        if node is None:
            return _err("add_node failed (canvas missing or "
                        "devagentic unreachable)")
        return json.dumps(node, indent=2)

    @mcp.tool()
    def canvas_move_node(canvas_id: str, node_id: str,
                         x: float, y: float) -> str:
        """Reposition a node on a canvas.

        Args:
            canvas_id: The canvas id.
            node_id: The node id.
            x: The new x coordinate.
            y: The new y coordinate.
        """
        if not canvas_id or not node_id:
            return _err("canvas_id and node_id are required")
        c = _resolve_canvas_client()
        if c is None:
            return _err("canvas plugin not available")
        out = c.move_node(canvas_id, node_id, x, y)
        if out is None:
            return _err("move_node failed")
        return json.dumps(out, indent=2)

    @mcp.tool()
    def canvas_update_node(canvas_id: str, node_id: str,
                           fields_json: str) -> str:
        """Partially update a node's fields.

        Args:
            canvas_id: The canvas id.
            node_id: The node id.
            fields_json: JSON object of fields to merge into the
                         node (e.g. `{"node_type": "decision"}`,
                         `{"body": {"content": "..."}}`).
        """
        if not canvas_id or not node_id:
            return _err("canvas_id and node_id are required")
        try:
            fields = json.loads(fields_json)
        except json.JSONDecodeError as exc:
            return _err(f"fields_json is not valid JSON: {exc}")
        if not isinstance(fields, dict) or not fields:
            return _err("fields_json must be a non-empty JSON object")
        c = _resolve_canvas_client()
        if c is None:
            return _err("canvas plugin not available")
        out = c.update_node(canvas_id, node_id, fields)
        if out is None:
            return _err("update_node failed")
        return json.dumps(out, indent=2)

    @mcp.tool()
    def canvas_delete_node(canvas_id: str, node_id: str) -> str:
        """Delete a node (and any incident edges, depending on
        devagentic's cascade behavior).

        Args:
            canvas_id: The canvas id.
            node_id: The node id to remove.
        """
        if not canvas_id or not node_id:
            return _err("canvas_id and node_id are required")
        c = _resolve_canvas_client()
        if c is None:
            return _err("canvas plugin not available")
        out = c.delete_node(canvas_id, node_id)
        if out is None:
            return _err("delete_node failed")
        return json.dumps(out, indent=2)

    @mcp.tool()
    def canvas_link_nodes(canvas_id: str, source_id: str,
                          target_id: str,
                          edge_type: str = "links") -> str:
        """Author an edge between two nodes.

        Args:
            canvas_id: The canvas id.
            source_id: Source node id.
            target_id: Target node id.
            edge_type: Optional edge label (default: `links`).
        """
        if not canvas_id or not source_id or not target_id:
            return _err("canvas_id, source_id, target_id all required")
        c = _resolve_canvas_client()
        if c is None:
            return _err("canvas plugin not available")
        out = c.link_nodes(canvas_id, source_id, target_id,
                           edge_type=edge_type)
        if out is None:
            return _err("link_nodes failed")
        return json.dumps(out, indent=2)

    @mcp.tool()
    def canvas_delete_edge(canvas_id: str, edge_id: str) -> str:
        """Delete an edge from a canvas.

        Args:
            canvas_id: The canvas id.
            edge_id: The edge id to remove.
        """
        if not canvas_id or not edge_id:
            return _err("canvas_id and edge_id are required")
        c = _resolve_canvas_client()
        if c is None:
            return _err("canvas plugin not available")
        out = c.delete_edge(canvas_id, edge_id)
        if out is None:
            return _err("delete_edge failed")
        return json.dumps(out, indent=2)

    @mcp.tool()
    def canvas_search(canvas_id: str, query: str) -> str:
        """Search a canvas's nodes by keyword (case-insensitive
        substring match against node body / name / node_type).

        v0 — client-side filter over the full canvas state.
        Server-side search is deferred (devagentic doesn't ship
        a `/search` endpoint yet).

        Args:
            canvas_id: The canvas id.
            query: Substring to search for.
        """
        if not canvas_id or not query:
            return _err("canvas_id and query are required")
        c = _resolve_canvas_client()
        if c is None:
            return _err("canvas plugin not available")
        matches = c.search_canvas(canvas_id, query)
        if matches is None:
            return _err("canvas_search failed (canvas missing or "
                        "devagentic unreachable)")
        return json.dumps({"count": len(matches),
                           "matches": matches}, indent=2)


def _resolve_docs_client():
    """Load the devagentic-docs plugin's HTTP client module. Same
    file-path import pattern as `_resolve_canvas_client` (the
    hyphenated `plugins/devagentic-docs/` directory can't be
    addressed via `import plugins.devagentic_docs`).

    Returns None when the plugin isn't present; tools then surface
    `{"error": "docs plugin not available …"}` without crashing
    the MCP server.
    """
    try:
        import importlib.util
        from pathlib import Path
        plugin_dir = (Path(__file__).resolve().parent
                      / "plugins" / "devagentic-docs")
        client_path = plugin_dir / "client.py"
        if not client_path.is_file():
            return None
        spec = importlib.util.spec_from_file_location(
            "_devagentic_docs_client", client_path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as exc:
        logger.debug("docs MCP: plugin client unavailable: %s", exc)
        return None


def _register_docs_tools(mcp: "FastMCP") -> None:
    """Register the devagentic-docs + fork tools on `mcp`. Mirrors
    `_register_canvas_tools` shape (#56). Each tool is a thin
    adapter around the devagentic-docs plugin client; failures
    return `{"error": "<msg>"}` JSON strings without raising.

    Tools registered (issue #24):
      doc_search, doc_write, doc_show, fork_open, fork_decorate,
      fork_get, fork_render.

    Not registered (out-of-scope per #24): fork_close + fork_pin
    depend on the local `$HERMES_HOME/docs-fork-active` marker,
    which is session-local hermes state and doesn't translate to
    MCP's stateless tool model.

    Transport caveat: the underlying client talks to
    `<DEVAGENTIC_BASE_URL>/graphql`, which not all devagentic
    deployments expose over HTTP. See #21 — tools then surface
    `{"error": "not found at <url>/graphql ..."}` via the
    plugin's `last_error_text()`.
    """

    def _err(msg: str) -> str:
        return json.dumps({"error": msg})

    def _reason(c) -> str:
        """Pull the client's last_error_text if available; empty
        string otherwise so callers can append `f"{msg}{_reason(c)}"`
        unconditionally."""
        try:
            t = c.last_error_text()
        except Exception:  # noqa: BLE001
            return ""
        return f" ({t})" if t else ""

    @mcp.tool()
    def doc_search(query: str = "", limit: int = 10,
                   tag: Optional[str] = None) -> str:
        """Search the user's devagentic doc graph.

        Args:
            query: Free-text query. With `tag` set, applied as a
                   client-side substring filter over the tag scope.
                   Required when `tag` is empty.
            limit: Max hits to return (clamped 1..100).
            tag: Tag to scope to (routes to `Query.docs(tags:[t])`
                 instead of `searchDocs(query, k)`).

        Returns: JSON `{"count": N, "hits": [{id, content, tags,
                 source, ts}, ...]}` or `{"error": ...}`.
        """
        c = _resolve_docs_client()
        if c is None:
            return _err("docs plugin not available on this hermes "
                        "install (missing plugins/devagentic-docs/)")
        if not query and not tag:
            return _err("query or tag is required")
        hits = c.search_docs(
            query=query, limit=max(1, min(100, int(limit))),
            tag=tag)
        if hits is None:
            return _err("doc_search failed" + _reason(c))
        return json.dumps({"count": len(hits), "hits": hits},
                          indent=2)

    @mcp.tool()
    def doc_write(content: str, tags: Optional[List[str]] = None,
                  source: Optional[str] = None) -> str:
        """Write a doc to the devagentic doc graph via writeDoc.

        Args:
            content: Doc body. Required.
            tags: Optional list of tag strings. Tools that expect
                  to be identifiable should include something like
                  `source:<agent-name>` so doc-graph queries can
                  filter authored finds by origin.
            source: Optional canonical source label. Maps to the
                    `source` argument on writeDoc.

        Returns: JSON `{"id": "<doc-id>", ...}` or `{"error": ...}`.
        """
        c = _resolve_docs_client()
        if c is None:
            return _err("docs plugin not available")
        if not content:
            return _err("content is required")
        doc = c.write_doc(
            content=content, tags=list(tags or []), source=source)
        if doc is None:
            return _err("doc_write failed" + _reason(c))
        return json.dumps(doc, indent=2)

    @mcp.tool()
    def doc_show(doc_id: str) -> str:
        """Fetch a single doc by id (top-1 identity-verified
        searchDocs match).

        Args:
            doc_id: The doc id.

        Returns: JSON of the doc or `{"error": ...}`.
        """
        c = _resolve_docs_client()
        if c is None:
            return _err("docs plugin not available")
        if not doc_id:
            return _err("doc_id is required")
        doc = c.get_doc(doc_id)
        if doc is None:
            return _err("doc_show failed" + _reason(c))
        return json.dumps(doc, indent=2)

    @mcp.tool()
    def fork_open(parent_id: str, goal: Optional[str] = None,
                  tags: Optional[List[str]] = None) -> str:
        """Fork a devagentic context from a parent doc/context.

        Wraps `forkContext(parentId, tags, annotations)`. The
        `goal` argument (if set) is stored as a `goal` annotation;
        the parent is auto-pinned as a `pinned-doc` annotation
        (matches the `/fork open` slash-command contract).

        Args:
            parent_id: The id of the doc or context to fork from.
            goal: Optional human-readable description of the fork's
                  intent.
            tags: Optional additional tags on the new context.

        Returns: JSON of the new Context or `{"error": ...}`.
        """
        c = _resolve_docs_client()
        if c is None:
            return _err("docs plugin not available")
        if not parent_id:
            return _err("parent_id is required")
        annotations: list[dict] = []
        if goal:
            annotations.append(
                {"key": "goal", "value": goal, "weight": 1.0})
        annotations.append(
            {"key": "pinned-doc", "value": parent_id, "weight": 1.0})
        ctx = c.fork_context(
            parent_id=parent_id,
            tags=list(tags or []) + ["source:hermes-mcp"],
            annotations=annotations)
        if ctx is None:
            return _err("fork_open failed" + _reason(c))
        return json.dumps(ctx, indent=2)

    @mcp.tool()
    def fork_decorate(ctx_id: str, key: str, value: str,
                      weight: float = 1.0) -> str:
        """Append an annotation to an existing context via
        `decorateContext`.

        Args:
            ctx_id: The context id to decorate.
            key: Annotation key (e.g. `pinned-doc`, `goal`,
                 free-form labels).
            value: Annotation value.
            weight: Optional relative weight (defaults to 1.0).

        Returns: JSON of the updated Context or `{"error": ...}`.
        """
        c = _resolve_docs_client()
        if c is None:
            return _err("docs plugin not available")
        if not ctx_id or not key:
            return _err("ctx_id and key are required")
        updated = c.decorate_context(
            ctx_id=ctx_id, key=key, value=value,
            weight=float(weight))
        if updated is None:
            return _err("fork_decorate failed" + _reason(c))
        return json.dumps(updated, indent=2)

    @mcp.tool()
    def fork_get(ctx_id: str) -> str:
        """Fetch a context's id + tags + annotations via
        `Query.context(id)`.

        Args:
            ctx_id: The context id.

        Returns: JSON of the Context or `{"error": ...}`.
        """
        c = _resolve_docs_client()
        if c is None:
            return _err("docs plugin not available")
        if not ctx_id:
            return _err("ctx_id is required")
        ctx = c.get_context(ctx_id)
        if ctx is None:
            return _err("fork_get failed" + _reason(c))
        return json.dumps(ctx, indent=2)

    @mcp.tool()
    def fork_render(ctx_id: str) -> str:
        """Call devagentic's `renderContext(ctxId)` to materialize
        the context as a flat string. Useful for feeding pinned-doc
        content into another agent's prompt.

        Args:
            ctx_id: The context id.

        Returns: JSON `{"ctx_id": "...", "rendered": "..."}` or
                 `{"error": ...}`.
        """
        c = _resolve_docs_client()
        if c is None:
            return _err("docs plugin not available")
        if not ctx_id:
            return _err("ctx_id is required")
        rendered = c.render_context(ctx_id)
        if rendered is None:
            return _err("fork_render failed" + _reason(c))
        return json.dumps({"ctx_id": ctx_id, "rendered": rendered},
                          indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_mcp_server(verbose: bool = False) -> None:
    """Start the Hermes MCP server on stdio."""
    if not _MCP_SERVER_AVAILABLE:
        print(
            "Error: MCP server requires the 'mcp' package.\n"
            f"Install with: {sys.executable} -m pip install 'mcp'",
            file=sys.stderr,
        )
        sys.exit(1)

    if verbose:
        logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)
    else:
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)

    bridge = EventBridge()
    bridge.start()

    server = create_mcp_server(event_bridge=bridge)

    import asyncio

    async def _run():
        try:
            await server.run_stdio_async()
        finally:
            bridge.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        bridge.stop()


def _resolve_mutations_client():
    """Load the devagentic-mutations plugin's HTTP client module.
    Same file-path import pattern as ``_resolve_canvas_client`` /
    ``_resolve_docs_client`` — the hyphenated
    ``plugins/devagentic-mutations/`` directory can't be addressed
    via ``import plugins.devagentic_mutations``.

    Returns ``None`` when the plugin isn't present; tools then
    surface ``{"error": "mutations plugin not available …"}``
    without crashing the MCP server.
    """
    try:
        import importlib.util
        from pathlib import Path
        plugin_dir = (Path(__file__).resolve().parent
                      / "plugins" / "devagentic-mutations")
        client_path = plugin_dir / "client.py"
        if not client_path.is_file():
            return None
        spec = importlib.util.spec_from_file_location(
            "_devagentic_mutations_client", client_path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as exc:  # noqa: BLE001
        logger.debug("mutations MCP: plugin client unavailable: %s", exc)
        return None


def _register_devagentic_mutation_tools(mcp: "FastMCP") -> None:
    """Register the devagentic-mutations MCP tools on ``mcp``.
    Closes G2 (hermes-agent#56) of devagentic#203. Same shape as
    ``_register_canvas_tools`` / ``_register_docs_tools``: each
    tool is a thin adapter around the plugin client; failures
    return ``{"error": "<msg>"}`` JSON strings without raising.

    Tools registered:
      * ``silo_query`` — wraps devagentic's ``querySilo`` GraphQL field
      * ``confer_run`` — wraps devagentic's ``runConferLoop`` mutation
      * ``assert_output`` — wraps devagentic's ``assertOutput`` mutation
        (closes hermes-agent#60 / G2b)

    Not registered here (already in ``_register_docs_tools``):
      ``doc_write`` (writeDoc), ``fork_*`` (forkContext family).

    Follow-up tools tracked under #56:
      ``patch_artifact``, ``read_artifact``, ``fetch_url``.
    """

    def _err(msg: str) -> str:
        return json.dumps({"error": msg})

    def _reason(c) -> str:
        """Pull the client's last_error_text if available; empty
        string otherwise so callers can append ``f"{msg}{_reason(c)}"``
        unconditionally."""
        try:
            t = c.last_error_text()
        except Exception:  # noqa: BLE001
            return ""
        return f" ({t})" if t else ""

    @mcp.tool()
    def silo_query(name: str, prompt: str,
                   role_override: Optional[str] = None) -> str:
        """Query a named devagentic silo with a one-shot prompt.

        Wraps devagentic's ``querySilo`` GraphQL field. Auto-scoped
        to the active X-User-Id (env override or hermes profile).

        Args:
            name: Silo name (e.g. ``silo-gemini-flash``).
            prompt: The user prompt to send to the silo.
            role_override: Optional role override (e.g. ``aider``,
                ``reviewer``); defaults to the silo's configured role.

        Returns: JSON ``{"siloId": ..., "siloName": ..., "text": ...,
        "cachedTokenCount": N, "promptTokenCount": N,
        "totalTokenCount": N}`` on success, or ``{"error": ...}``.
        """
        c = _resolve_mutations_client()
        if c is None:
            return _err("devagentic-mutations plugin not available "
                        "(missing plugins/devagentic-mutations/)")
        if not name or not prompt:
            return _err("name and prompt are required")
        reply = c.query_silo(
            name=name, prompt=prompt, role_override=role_override)
        if reply is None:
            return _err("silo_query failed" + _reason(c))
        return json.dumps(reply, indent=2)

    @mcp.tool()
    def confer_run(user_id: str, candidate_id: str) -> str:
        """Run a confer-loop on a finding-candidate.

        Wraps devagentic's ``runConferLoop`` mutation. Polls 3-4
        free-tier silos for a single-line ``{action, reason}``
        verdict per silo, then rolls up by majority + confidence.
        Writes one ``kind:confer-result`` doc + N
        ``kind:silo-bench-response`` docs into the graph.

        Args:
            user_id: User scope (typically the active profile name).
                The MCP layer passes the worker's X-User-Id binding;
                you usually want to pass the same value here.
            candidate_id: The ``kind:finding-candidate`` doc id to
                confer on.

        Returns: JSON rollup ``{confer_result_id, candidate_id,
        consensus_action, confidence, recommendation,
        silos_consulted, per_silo_response_ids,
        disagreement_points}`` on success, or ``{"error": ...}``.
        """
        c = _resolve_mutations_client()
        if c is None:
            return _err("devagentic-mutations plugin not available")
        if not user_id or not candidate_id:
            return _err("user_id and candidate_id are required")
        rollup = c.run_confer_loop(
            user_id=user_id, candidate_id=candidate_id)
        if rollup is None:
            return _err("confer_run failed" + _reason(c))
        return json.dumps(rollup, indent=2)

    @mcp.tool()
    def assert_output(call_id: str, fragment: str,
                      predicate: Optional[str] = None) -> str:
        """Vet a prior tool_call against a predicate (closes #60).

        Wraps devagentic's ``assertOutput`` mutation. Looks up the
        ``tool_call`` node by id, builds a subject from its body,
        and evaluates ``ExpectationInput {fragment, predicate?}``
        against it. Devagentic writes a ``Verdict`` node + (if the
        predicate was None/empty) a ``kind:predicate-coerced``
        telemetry doc.

        Args:
            call_id: The ``tool_call`` node id to vet (typically
                captured from a prior tool invocation's audit
                trail).
            fragment: A subject path into the tool_call body
                (e.g., ``"content"``, ``"args.path"``). Required.
            predicate: Optional predicate string. When omitted /
                empty, the devagentic-side ``_stiffen_predicate``
                substitutes a tool-specific floor (so the verdict
                is meaningful even on no-predicate emissions).

        Returns: JSON ``{id, ts, callId, passed, violations: [...]}``
        on success, or ``{"error": ...}``.
        """
        c = _resolve_mutations_client()
        if c is None:
            return _err("devagentic-mutations plugin not available")
        if not call_id or not fragment:
            return _err("call_id and fragment are required")
        verdict = c.assert_output(
            call_id=call_id, fragment=fragment, predicate=predicate)
        if verdict is None:
            return _err("assert_output failed" + _reason(c))
        return json.dumps(verdict, indent=2)


def _resolve_github_client():
    """Load the hermes-github plugin's client module. Same file-path
    import pattern as the other resolvers. Returns ``None`` when the
    plugin isn't present; tool then surfaces
    ``{"error": "hermes-github plugin not available …"}`` without
    crashing the MCP server."""
    try:
        import importlib.util
        from pathlib import Path
        plugin_dir = (Path(__file__).resolve().parent
                      / "plugins" / "hermes-github")
        client_path = plugin_dir / "client.py"
        if not client_path.is_file():
            return None
        spec = importlib.util.spec_from_file_location(
            "_hermes_github_client", client_path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as exc:  # noqa: BLE001
        logger.debug("github MCP: plugin client unavailable: %s", exc)
        return None


def _register_github_tools(mcp: "FastMCP") -> None:
    """Register the hermes-github MCP tool on ``mcp``. Closes G3
    (hermes-agent#57) of devagentic#203.

    Tools registered:
      * ``file_issue`` — open an issue on TechDevGroup/devagentic
        or TechDevGroup/hermes-agent. Restricted by design to those
        two stack repos. Auth token resolved on the hermes host
        (env or ``gh auth token``); the worker conversation never
        sees credentials.
    """

    def _err(msg: str) -> str:
        return json.dumps({"error": msg})

    def _reason(c) -> str:
        try:
            t = c.last_error_text()
        except Exception:  # noqa: BLE001
            return ""
        return f" ({t})" if t else ""

    @mcp.tool()
    def file_issue(repo: str, title: str, body: str,
                   labels: Optional[List[str]] = None) -> str:
        """Open a GitHub issue on a TechDevGroup stack repo.

        Use this when you discover a stack gap (missing capability,
        broken behavior, bug) in devagentic or hermes-agent — per
        devagentic#203 §3.2, this is the ONLY way to report stack
        issues. NEVER edit stack source from a worker session.

        Args:
            repo: ``"devagentic"`` or ``"hermes-agent"`` — anything
                else is rejected.
            title: Short issue title.
            body: Issue body (markdown). Include enough context for
                the stack maintainer to reproduce + understand the
                gap.
            labels: Optional list of label names (e.g.
                ``["bug", "needs-triage"]``).

        Returns: JSON ``{"number": int, "url": str, "html_url": str,
        "title": str, "state": str}`` on success, or
        ``{"error": ...}``.
        """
        c = _resolve_github_client()
        if c is None:
            return _err("hermes-github plugin not available "
                        "(missing plugins/hermes-github/)")
        if not repo:
            allowed = sorted(c.allowed_repos()) if hasattr(c, "allowed_repos") else []
            return _err(f"repo is required (allowed: {allowed})")
        if not title or not title.strip():
            return _err("title is required (non-empty)")
        if not body or not body.strip():
            return _err("body is required (non-empty)")
        result = c.file_issue(
            repo=repo, title=title, body=body,
            labels=list(labels) if labels else None,
        )
        if result is None:
            return _err("file_issue failed" + _reason(c))
        return json.dumps(result, indent=2)


def _resolve_lane_h_client():
    """Load the devagentic-lane-h plugin's client module. Same
    file-path import pattern as the other devagentic-adjacent
    plugin resolvers."""
    try:
        import importlib.util
        from pathlib import Path
        plugin_dir = (Path(__file__).resolve().parent
                      / "plugins" / "devagentic-lane-h")
        client_path = plugin_dir / "client.py"
        if not client_path.is_file():
            return None
        spec = importlib.util.spec_from_file_location(
            "_devagentic_lane_h_client", client_path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as exc:  # noqa: BLE001
        logger.debug("lane-h MCP: plugin client unavailable: %s", exc)
        return None


def _register_lane_h_tools(mcp: "FastMCP") -> None:
    """Register the devagentic-lane-h MCP tools on ``mcp``. Closes
    G4 (hermes-agent#58) of devagentic#203.

    Tools registered:
      * ``lane_h_list`` — list a user's recent Lane H
        reasoning-graft-candidate docs.
      * ``lane_h_fetch`` — fetch a single Lane H doc by id with
        kind validation.

    Depends on devagentic#207 (reasoningGraftCandidates query).
    No ``request_reasoning_graft`` tool — Lane H is auto-triggered
    by the cycle_tick when conferring produces a high-confidence
    result. Workers trigger the underlying flow via ``confer_run``
    (G2, already shipped)."""

    def _err(msg: str) -> str:
        return json.dumps({"error": msg})

    def _reason(c) -> str:
        try:
            t = c.last_error_text()
        except Exception:  # noqa: BLE001
            return ""
        return f" ({t})" if t else ""

    @mcp.tool()
    def lane_h_list(user_id: Optional[str] = None, limit: int = 20) -> str:
        """List recent Lane H reasoning-graft-candidate docs.

        Lane H docs are emitted automatically by devagentic's
        cycle_tick when a confer-result clears the auto-trigger
        confidence threshold (#174). Workers read them as
        authoritative preamble for subsequent moves.

        Args:
            user_id: User scope. Omit to use the active hermes
                profile's resolved id (the usual case).
            limit: Cap on rows returned (default 20). Newest-first.

        Returns: JSON ``{"count": N, "grafts": [...]}`` on success
        or ``{"error": ...}``.
        """
        c = _resolve_lane_h_client()
        if c is None:
            return _err("devagentic-lane-h plugin not available "
                        "(missing plugins/devagentic-lane-h/)")
        rows = c.list_reasoning_grafts(
            user_id=user_id, limit=max(0, int(limit)))
        if rows is None:
            return _err("lane_h_list failed" + _reason(c))
        return json.dumps({"count": len(rows), "grafts": rows}, indent=2)

    @mcp.tool()
    def lane_h_fetch(graft_id: str) -> str:
        """Fetch one Lane H reasoning-graft-candidate doc by id.

        Validates the doc has the ``kind:reasoning-graft-candidate``
        tag — returns an error if the id resolves to a different
        kind of doc (which would indicate the worker passed the
        wrong id).

        Args:
            graft_id: The doc id (from a prior ``lane_h_list``).

        Returns: JSON ``{id, content, tags, source, ts}`` on
        success or ``{"error": ...}``.
        """
        c = _resolve_lane_h_client()
        if c is None:
            return _err("devagentic-lane-h plugin not available")
        if not graft_id:
            return _err("graft_id is required")
        doc = c.fetch_reasoning_graft(graft_id)
        if doc is None:
            return _err("lane_h_fetch failed" + _reason(c))
        return json.dumps(doc, indent=2)

    @mcp.tool()
    def grafted_context_fetch(graft_id: str,
                              user_id: Optional[str] = None) -> str:
        """Fetch one kind:grafted-context doc by id, scoped to user_id
        (hermes-agent#71).

        Use this when the vertical preamble index showed you a graft
        you want to read in full. The preamble (post-#71 G1) renders
        only ``[graft_id | source | path | 1-line abstract]`` per
        graft to keep context cost bounded; fetch on demand.

        Args:
            graft_id: The doc id from the preamble index.
            user_id: Omit to use the active hermes profile (typical).

        Returns: JSON ``{id, userId, source, ref, sha, path, content,
        ts}`` on success, or ``{"error": ...}``.
        """
        c = _resolve_lane_h_client()
        if c is None:
            return _err("devagentic-lane-h plugin not available")
        if not graft_id:
            return _err("graft_id is required")
        doc = c.fetch_grafted_context(
            graft_id=graft_id, user_id=user_id)
        if doc is None:
            return _err("grafted_context_fetch failed" + _reason(c))
        return json.dumps(doc, indent=2)
if __name__ == "__main__":
    # hermes-agent#82 — enables `python -m mcp_serve` as a stdio MCP
    # server, which `ensure_internal_mcp_server()` (hermes_cli/mcp_autowire.py)
    # registers under `mcp_servers.hermes-internal` so worker sessions
    # auto-discover the hermes-internal MCP tool surface (G2 mutations,
    # G3 file_issue, G4 lane-h + grafted-context) without any operator
    # config. Honors a single optional ``--verbose`` flag for debugging
    # the subprocess from outside.
    import sys as _sys
    _verbose = "--verbose" in _sys.argv[1:] or "-v" in _sys.argv[1:]
    run_mcp_server(verbose=_verbose)
