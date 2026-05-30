"""Unit tests for the devagentic-docs plugin (hermes #12).
Mirrors the structure of test_devagentic_canvas_plugin.py — same
synthetic-package fixture, same stubbing pattern."""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

import pytest
import yaml


PLUGIN_DIR = (Path(__file__).resolve().parents[1]
              / "plugins" / "devagentic-docs")


@pytest.fixture
def plugin_pkg(tmp_path, monkeypatch):
    """Load the devagentic-docs plugin modules as a synthetic
    package so relative imports resolve.

    hermes-agent#167 — preset a non-empty DEVAGENTIC_BASE_URL so tests
    that focus on OTHER error paths (auth, unreachable, GraphQL errors)
    don't all short-circuit on the new "BASE_URL not set" guard. Tests
    that specifically exercise the unset case override this in-test.
    """
    monkeypatch.setenv("DEVAGENTIC_BASE_URL", "http://test:6070")
    pkg_name = "_devagentic_docs_under_test"
    spec = importlib.util.spec_from_file_location(
        pkg_name, PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    pkg = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = pkg

    def _load(name):
        sub_spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.{name}", PLUGIN_DIR / f"{name}.py")
        mod = importlib.util.module_from_spec(sub_spec)
        sys.modules[f"{pkg_name}.{name}"] = mod
        sub_spec.loader.exec_module(mod)
        return mod

    client = _load("client")
    commands = _load("commands")
    preamble = _load("preamble")
    # Pin the submodules as attributes on the package so the
    # `from . import …` chain inside __init__.py picks up the
    # SAME instances we just loaded (rather than re-importing
    # them via fresh specs that would shadow our monkeypatches).
    pkg.client = client
    pkg.commands = commands
    pkg.preamble = preamble

    assert spec.loader is not None
    spec.loader.exec_module(pkg)

    from types import SimpleNamespace
    return SimpleNamespace(
        pkg=pkg, client=client, commands=commands, preamble=preamble)


# ─── Manifest ───────────────────────────────────────────────

def test_manifest_parses_and_declares_expected_fields():
    manifest = yaml.safe_load(
        (PLUGIN_DIR / "plugin.yaml").read_text())
    assert manifest["name"] == "devagentic-docs"
    assert "version" in manifest
    assert "description" in manifest
    assert manifest.get("kind") == "standalone"
    # v0.2 wires pre_llm_call for fork preamble injection.
    assert "pre_llm_call" in (manifest.get("hooks") or [])


def test_skill_md_parses_and_has_expected_name():
    """`devagentic-docs:docs` reference skill must be parseable
    by hermes' skill loader (same frontmatter shape canvas uses)."""
    skill_path = PLUGIN_DIR / "skills" / "docs" / "SKILL.md"
    assert skill_path.is_file(), skill_path
    text = skill_path.read_text(encoding="utf-8")
    # frontmatter block parses
    assert text.startswith("---\n")
    _, frontmatter, body = text.split("---", 2)
    fm = yaml.safe_load(frontmatter)
    assert fm["name"] == "docs"
    assert "version" in fm
    assert "description" in fm
    # body has the surface tables
    assert "/doc search" in body
    assert "/fork open" in body
    # Transport caveat is documented (links #21).
    assert "#21" in body


def test_register_skill_invoked_with_docs(plugin_pkg, tmp_path):
    """Plugin's register() should call ctx.register_skill(name="docs")
    when SKILL.md is on disk."""
    calls: list[dict] = []

    class _Ctx:
        def register_command(self, **kw):
            calls.append({"kind": "command", **kw})

        def register_hook(self, name, handler):
            calls.append({"kind": "hook", "name": name})

        def register_skill(self, **kw):
            calls.append({"kind": "skill", **kw})

    ctx = _Ctx()
    plugin_pkg.pkg.register(ctx)
    skills = [c for c in calls if c["kind"] == "skill"]
    assert len(skills) == 1, calls
    assert skills[0]["name"] == "docs"
    assert skills[0]["path"].name == "SKILL.md"


# ─── Base URL normalization ─────────────────────────────────

def test_base_url_strips_v1_for_graphql(plugin_pkg, monkeypatch):
    """The /v1 suffix in DEVAGENTIC_BASE_URL is stripped because
    GraphQL lives at <root>/graphql, not <root>/v1/graphql."""
    monkeypatch.setenv("DEVAGENTIC_BASE_URL", "http://devbox:6070/v1")
    assert plugin_pkg.client._base_url() == "http://devbox:6070"


def test_base_url_passthrough_when_no_v1(plugin_pkg, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_BASE_URL", "http://devbox:6070")
    assert plugin_pkg.client._base_url() == "http://devbox:6070"


# ─── Client failure-loudness (mirrors canvas #15) ───────────

def test_last_error_unresolved_user_id(plugin_pkg, monkeypatch):
    monkeypatch.delenv("DEVAGENTIC_USER_ID", raising=False)
    fake = type("F", (), {"get_active_profile_name": staticmethod(
        lambda: None)})()
    monkeypatch.setitem(sys.modules, "hermes_cli.profiles", fake)
    assert plugin_pkg.client.search_docs("anything") is None
    assert "DEVAGENTIC_USER_ID" in (
        plugin_pkg.client.last_error_text() or "")


def test_last_error_auth_failed(plugin_pkg, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")

    def _raise(*a, **k):
        raise urllib.error.HTTPError(
            "http://x/graphql", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(plugin_pkg.client.urllib.request,
                        "urlopen", _raise)
    assert plugin_pkg.client.search_docs("anything") is None
    err = plugin_pkg.client.last_error_text() or ""
    assert "authentication failed" in err
    assert "DEVAGENTIC_API_KEY" in err


def test_last_error_unreachable(plugin_pkg, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")

    def _raise(*a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(plugin_pkg.client.urllib.request,
                        "urlopen", _raise)
    assert plugin_pkg.client.write_doc("body") is None
    err = plugin_pkg.client.last_error_text() or ""
    assert "unreachable at" in err


def test_last_error_graphql_errors(plugin_pkg, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(
                {"errors": [{"message": "schema mismatch"}]}).encode("utf-8")

    monkeypatch.setattr(plugin_pkg.client.urllib.request,
                        "urlopen", lambda *a, **k: _Resp())
    assert plugin_pkg.client.search_docs("x") is None
    assert "schema mismatch" in (plugin_pkg.client.last_error_text() or "")


# ─── search_docs ────────────────────────────────────────────

def test_search_docs_freetext_uses_searchDocs(plugin_pkg, monkeypatch):
    """No --tag set → schema-correct searchDocs(query, k) path."""
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    captured: dict = {}
    hits = [{"id": "doc-1", "content": "hello", "tags": ["a"],
             "source": "s", "ts": "now"}]

    def _stub(query, variables, **k):
        captured["query"] = query
        captured["variables"] = variables
        return {"searchDocs": hits}

    monkeypatch.setattr(plugin_pkg.client, "_post_graphql", _stub)
    out = plugin_pkg.client.search_docs("hi", limit=5)
    assert out == hits
    assert "$k:Int" in captured["query"]
    assert captured["variables"] == {"q": "hi", "k": 5}
    assert "limit" not in captured["query"]
    assert "score" not in captured["query"]


def test_search_docs_tag_uses_docs_query(plugin_pkg, monkeypatch):
    """--tag set → schema-correct docs(tags:[t]) path; sliced
    client-side."""
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    captured: dict = {}
    raw_hits = [
        {"id": f"doc-{i}", "content": f"line {i}", "tags": ["k"],
         "source": "s", "ts": "t"}
        for i in range(5)
    ]

    def _stub(query, variables, **k):
        captured["query"] = query
        captured["variables"] = variables
        return {"docs": raw_hits}

    monkeypatch.setattr(plugin_pkg.client, "_post_graphql", _stub)
    out = plugin_pkg.client.search_docs("line", limit=3, tag="k")
    assert len(out) == 3
    assert captured["variables"] == {"t": ["k"]}
    assert "docs(tags:$t)" in captured["query"]


def test_search_docs_tag_filters_by_query_substring(
        plugin_pkg, monkeypatch):
    """With --tag, the query string is a substring filter applied
    client-side over the tag-scoped result set."""
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    raw_hits = [
        {"id": "doc-a", "content": "apple", "tags": ["k"]},
        {"id": "doc-b", "content": "banana", "tags": ["k"]},
        {"id": "doc-c", "content": "apricot", "tags": ["k"]},
    ]
    monkeypatch.setattr(
        plugin_pkg.client, "_post_graphql",
        lambda q, v, **k: {"docs": raw_hits})
    out = plugin_pkg.client.search_docs("ap", limit=10, tag="k")
    assert [h["id"] for h in out] == ["doc-a", "doc-c"]


def test_search_docs_empty_query_short_circuits(plugin_pkg):
    assert plugin_pkg.client.search_docs("") is None


# ─── write_doc ──────────────────────────────────────────────

def test_write_doc_returns_id(plugin_pkg, monkeypatch):
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "alice")
    monkeypatch.setattr(
        plugin_pkg.client, "_post_graphql",
        lambda q, v, **k: {"writeDoc": {"id": "doc-abc"}})
    out = plugin_pkg.client.write_doc("body", tags=["x"])
    assert out == {"id": "doc-abc"}


def test_write_doc_empty_body_short_circuits(plugin_pkg):
    assert plugin_pkg.client.write_doc("") is None


# ─── /doc search command ────────────────────────────────────

def test_handle_search_usage_when_empty(plugin_pkg):
    out = plugin_pkg.commands._handle_search("")
    assert "Usage:" in out


def test_handle_search_renders_hits(plugin_pkg, monkeypatch):
    monkeypatch.setattr(
        plugin_pkg.client, "search_docs",
        lambda **k: [{"id": "doc-1", "content": "line 1\nline 2"},
                     {"id": "doc-2", "content": "another"}])
    out = plugin_pkg.commands._handle_search("hello")
    assert "doc-1" in out and "doc-2" in out
    assert "Top 2" in out
    # First line only of multi-line content is shown.
    assert "line 1" in out and "line 2" not in out


def test_handle_search_appends_failure_detail(plugin_pkg, monkeypatch):
    monkeypatch.setattr(plugin_pkg.client, "search_docs",
                        lambda **k: None)
    monkeypatch.setattr(plugin_pkg.client, "last_error_text",
                        lambda: "auth failed")
    out = plugin_pkg.commands._handle_search("hello")
    assert "Reason: auth failed" in out


def test_handle_search_no_hits(plugin_pkg, monkeypatch):
    monkeypatch.setattr(plugin_pkg.client, "search_docs",
                        lambda **k: [])
    out = plugin_pkg.commands._handle_search("hello --tag k")
    assert "No docs matched" in out
    assert "tag=`k`" in out


def test_parse_search_args_extracts_flags(plugin_pkg):
    q, limit, tag = plugin_pkg.commands._parse_search_args(
        "find something --tag k:foo --limit 25")
    assert q == "find something"
    assert limit == 25
    assert tag == "k:foo"


def test_parse_search_args_limit_clamped(plugin_pkg):
    _, limit, _ = plugin_pkg.commands._parse_search_args(
        "x --limit 999999")
    assert limit == 100
    _, limit, _ = plugin_pkg.commands._parse_search_args(
        "x --limit 0")
    assert limit == 1


# ─── /doc write command ────────────────────────────────────

def test_handle_write_usage_when_empty(plugin_pkg):
    out = plugin_pkg.commands._handle_write("")
    assert "Usage:" in out


def test_handle_write_auto_tags_source(plugin_pkg, monkeypatch):
    captured: dict = {}

    def _stub(content, tags, source, **k):
        captured["content"] = content
        captured["tags"] = list(tags or [])
        captured["source"] = source
        return {"id": "doc-x"}

    monkeypatch.setattr(plugin_pkg.client, "write_doc", _stub)
    out = plugin_pkg.commands._handle_write(
        "hello world --tags k:test,user:duplex")
    assert "doc-x" in out
    assert "source:hermes-cli" in captured["tags"]
    assert "k:test" in captured["tags"]
    assert "user:duplex" in captured["tags"]
    assert captured["source"] == "hermes-cli"
    assert captured["content"] == "hello world"


def test_handle_write_appends_failure_detail(plugin_pkg, monkeypatch):
    monkeypatch.setattr(plugin_pkg.client, "write_doc",
                        lambda **k: None)
    monkeypatch.setattr(plugin_pkg.client, "last_error_text",
                        lambda: "unreachable at http://x/graphql")
    out = plugin_pkg.commands._handle_write("a body")
    assert "Reason:" in out
    assert "unreachable" in out


# ─── /doc show command ──────────────────────────────────────

def test_handle_show_usage_when_empty(plugin_pkg):
    out = plugin_pkg.commands._handle_show("")
    assert "Usage:" in out


def test_handle_show_renders_doc(plugin_pkg, monkeypatch):
    monkeypatch.setattr(
        plugin_pkg.client, "get_doc",
        lambda doc_id, **k: {"id": doc_id, "content": "body",
                              "tags": ["a", "b"]})
    out = plugin_pkg.commands._handle_show("doc-abc")
    assert "doc-abc" in out
    assert "body" in out
    assert "`a`" in out and "`b`" in out


# ─── dispatcher ────────────────────────────────────────────

def test_doc_command_dispatcher_usage(plugin_pkg):
    out = plugin_pkg.commands.doc_command("")
    assert "Usage:" in out


def test_doc_command_dispatcher_unknown_sub(plugin_pkg):
    out = plugin_pkg.commands.doc_command("nope hi")
    assert "Unknown" in out


def test_doc_command_dispatcher_routes_search(plugin_pkg, monkeypatch):
    monkeypatch.setattr(plugin_pkg.client, "search_docs",
                        lambda **k: [])
    out = plugin_pkg.commands.doc_command("search anything")
    assert "No docs matched" in out


# ─── /fork command surface ──────────────────────────────────

@pytest.fixture
def fork_marker(plugin_pkg, tmp_path, monkeypatch):
    """Re-point HERMES_HOME at tmp_path so the marker file goes
    somewhere isolated, regardless of the plugin_pkg fixture's
    earlier setenv (fixture order is pytest-dependent)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path / "docs-fork-active"


def test_fork_open_writes_marker_and_calls_forkContext(
        plugin_pkg, fork_marker, monkeypatch):
    captured: dict = {}

    def _stub(parent_id, tags, annotations, **k):
        captured["parent_id"] = parent_id
        captured["tags"] = list(tags or [])
        captured["annotations"] = list(annotations or [])
        return {"id": "ctx-7"}

    monkeypatch.setattr(plugin_pkg.client, "fork_context", _stub)
    out = plugin_pkg.commands._handle_fork_open(
        'doc-parent --goal "trace the bug"')
    assert "ctx-7" in out
    assert "doc-parent" in out
    assert "trace the bug" in out
    assert captured["parent_id"] == "doc-parent"
    assert "source:hermes-cli" in captured["tags"]
    keys = [a["key"] for a in captured["annotations"]]
    assert "goal" in keys
    assert "pinned-doc" in keys
    assert fork_marker.read_text(encoding="utf-8") == "ctx-7"


def test_fork_open_without_parent_returns_usage(plugin_pkg):
    out = plugin_pkg.commands._handle_fork_open("")
    assert "Usage:" in out


def test_fork_open_failure_surfaces_reason(plugin_pkg, monkeypatch):
    monkeypatch.setattr(plugin_pkg.client, "fork_context",
                        lambda **k: None)
    monkeypatch.setattr(plugin_pkg.client, "last_error_text",
                        lambda: "auth failed — set DEVAGENTIC_API_KEY")
    out = plugin_pkg.commands._handle_fork_open("doc-x")
    assert "Reason:" in out
    assert "auth failed" in out


def test_fork_close_clears_marker(plugin_pkg, fork_marker):
    fork_marker.parent.mkdir(parents=True, exist_ok=True)
    fork_marker.write_text("ctx-9", encoding="utf-8")
    out = plugin_pkg.commands._handle_fork_close("")
    assert "ctx-9" in out
    assert not fork_marker.exists()


def test_fork_close_when_none_active(plugin_pkg, fork_marker):
    out = plugin_pkg.commands._handle_fork_close("")
    assert "No fork was active" in out


def test_fork_show_renders_active(plugin_pkg, fork_marker, monkeypatch):
    fork_marker.parent.mkdir(parents=True, exist_ok=True)
    fork_marker.write_text("ctx-5", encoding="utf-8")
    monkeypatch.setattr(
        plugin_pkg.client, "get_context",
        lambda ctx_id, **k: {
            "id": ctx_id,
            "ts": "now",
            "tags": ["source:hermes-cli", "k:foo"],
            "annotations": [
                {"key": "goal", "value": "investigate", "weight": 1.0},
                {"key": "pinned-doc", "value": "doc-1", "weight": 1.0},
                {"key": "pinned-doc", "value": "doc-2", "weight": 0.5},
            ],
        })
    out = plugin_pkg.commands._handle_fork_show("")
    assert "ctx-5" in out
    assert "investigate" in out
    assert "doc-1" in out and "doc-2" in out
    assert "k:foo" in out


def test_fork_show_when_none_active(plugin_pkg, fork_marker):
    out = plugin_pkg.commands._handle_fork_show("")
    assert "No fork is active" in out


def test_fork_pin_calls_decorateContext(
        plugin_pkg, fork_marker, monkeypatch):
    fork_marker.parent.mkdir(parents=True, exist_ok=True)
    fork_marker.write_text("ctx-1", encoding="utf-8")
    captured: dict = {}

    def _stub(ctx_id, key, value, weight=1.0, **k):
        captured["ctx_id"] = ctx_id
        captured["key"] = key
        captured["value"] = value
        return {"id": ctx_id, "annotations": []}

    monkeypatch.setattr(plugin_pkg.client, "decorate_context", _stub)
    out = plugin_pkg.commands._handle_fork_pin("doc-42")
    assert "doc-42" in out
    assert captured == {"ctx_id": "ctx-1", "key": "pinned-doc",
                        "value": "doc-42"}


def test_fork_pin_requires_active_fork(plugin_pkg, fork_marker):
    out = plugin_pkg.commands._handle_fork_pin("doc-42")
    assert "No fork is active" in out


def test_fork_render_returns_rendered_text(
        plugin_pkg, fork_marker, monkeypatch):
    fork_marker.parent.mkdir(parents=True, exist_ok=True)
    fork_marker.write_text("ctx-1", encoding="utf-8")
    monkeypatch.setattr(plugin_pkg.client, "render_context",
                        lambda ctx_id, **k: "rendered body here")
    out = plugin_pkg.commands._handle_fork_render("")
    assert "ctx-1" in out
    assert "rendered body here" in out


def test_fork_render_empty_string(plugin_pkg, fork_marker, monkeypatch):
    fork_marker.parent.mkdir(parents=True, exist_ok=True)
    fork_marker.write_text("ctx-1", encoding="utf-8")
    monkeypatch.setattr(plugin_pkg.client, "render_context",
                        lambda ctx_id, **k: "")
    out = plugin_pkg.commands._handle_fork_render("")
    assert "empty string" in out


def test_fork_dispatcher_usage(plugin_pkg):
    out = plugin_pkg.commands.fork_command("")
    assert "Usage:" in out


def test_fork_dispatcher_unknown_sub(plugin_pkg):
    out = plugin_pkg.commands.fork_command("nope")
    assert "Unknown" in out


# ─── pre_llm_call preamble ──────────────────────────────────


def _load_preamble(plugin_pkg):
    """The plugin_pkg fixture now eagerly pre-loads preamble.py so
    monkeypatches on plugin_pkg.client reach the preamble's
    `docs_client` reference (same module instance)."""
    return plugin_pkg.preamble


def test_preamble_returns_none_when_no_active_fork(
        plugin_pkg, fork_marker):
    preamble = _load_preamble(plugin_pkg)
    assert preamble.on_pre_llm_call() is None


def test_preamble_returns_context_when_fork_active(
        plugin_pkg, fork_marker, monkeypatch):
    fork_marker.parent.mkdir(parents=True, exist_ok=True)
    fork_marker.write_text("ctx-42", encoding="utf-8")
    preamble = _load_preamble(plugin_pkg)
    monkeypatch.setattr(plugin_pkg.client, "render_context",
                        lambda ctx_id, **k: "rendered fork state")
    out = preamble.on_pre_llm_call()
    assert isinstance(out, dict)
    assert "context" in out
    assert "ctx-42" in out["context"]
    assert "rendered fork state" in out["context"]


def test_preamble_returns_none_on_empty_render(
        plugin_pkg, fork_marker, monkeypatch):
    fork_marker.parent.mkdir(parents=True, exist_ok=True)
    fork_marker.write_text("ctx-42", encoding="utf-8")
    preamble = _load_preamble(plugin_pkg)
    monkeypatch.setattr(plugin_pkg.client, "render_context",
                        lambda ctx_id, **k: "   ")
    assert preamble.on_pre_llm_call() is None


def test_preamble_caps_long_renders(
        plugin_pkg, fork_marker, monkeypatch):
    fork_marker.parent.mkdir(parents=True, exist_ok=True)
    fork_marker.write_text("ctx-42", encoding="utf-8")
    preamble = _load_preamble(plugin_pkg)
    monkeypatch.setattr(plugin_pkg.client, "render_context",
                        lambda ctx_id, **k: "X" * 20000)
    out = preamble.on_pre_llm_call()
    assert out is not None
    assert "truncated" in out["context"]
    assert len(out["context"]) < 9000


def test_preamble_returns_none_on_render_failure(
        plugin_pkg, fork_marker, monkeypatch):
    fork_marker.parent.mkdir(parents=True, exist_ok=True)
    fork_marker.write_text("ctx-42", encoding="utf-8")
    preamble = _load_preamble(plugin_pkg)
    monkeypatch.setattr(plugin_pkg.client, "render_context",
                        lambda ctx_id, **k: None)
    assert preamble.on_pre_llm_call() is None


# ── hermes-agent#161: shadow-immune _classify_http_error ─────────────


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://x", code, "boom", hdrs={}, fp=None)


def test_classify_http_error_auth(plugin_pkg):
    """401/403 → 'auth' (matches utils.classify_http_error semantics)."""
    assert plugin_pkg.client._classify_http_error(_http_error(401)) == "auth"
    assert plugin_pkg.client._classify_http_error(_http_error(403)) == "auth"


def test_classify_http_error_not_found(plugin_pkg):
    assert plugin_pkg.client._classify_http_error(_http_error(404)) == "not_found"


def test_classify_http_error_generic_http(plugin_pkg):
    for code in (500, 502, 418):
        assert plugin_pkg.client._classify_http_error(_http_error(code)) == "http"


def test_classify_http_error_unreachable(plugin_pkg):
    """URLError / OSError / TimeoutError all collapse to 'unreachable'."""
    for exc in (urllib.error.URLError("dns"),
                OSError("conn refused"),
                TimeoutError("deadline")):
        assert plugin_pkg.client._classify_http_error(exc) == "unreachable"


def test_classify_http_error_unknown_for_unrelated(plugin_pkg):
    assert plugin_pkg.client._classify_http_error(ValueError("nope")) == "unknown"


def test_doc_client_does_not_lazy_import_utils():
    """hermes-agent#161 regression: a system-installed `utils` package
    shadowed hermes's top-level utils.py and broke the lazy
    `from utils import classify_http_error` inside the HTTP-error
    except block, crashing doc_write at 23:34/23:44 on poly-explorer.
    The fix replaces the cross-module import with a local helper;
    re-introducing the import brings the shadow bug back."""
    src = (PLUGIN_DIR / "client.py").read_text()
    # Line-level check so the comment quoting the error string doesn't
    # trip the assertion — only real import lines count.
    bad = [
        ln for ln in src.splitlines()
        if ln.lstrip().startswith("from utils import")
    ]
    assert not bad, (
        "hermes-agent#161: do not re-introduce a real `from utils import` "
        "line in this client — keep the http-error path shadow-immune "
        f"via the local _classify_http_error helper. Found: {bad}"
    )
    assert "_classify_http_error" in src
    assert "hermes-agent#161" in src


# ── hermes-agent#167 — fail loud on unset DEVAGENTIC_BASE_URL ──


def test_base_url_returns_none_when_env_unset(plugin_pkg, monkeypatch):
    """hermes-agent#167: ``_base_url()`` must return None when
    DEVAGENTIC_BASE_URL is unset rather than silently defaulting to
    ``http://127.0.0.1:6071``. The silent default disguised a missing
    env-propagation as a transient ECONNREFUSED for ~5 hours on poly."""
    monkeypatch.delenv("DEVAGENTIC_BASE_URL", raising=False)
    assert plugin_pkg.client._base_url() is None


def test_base_url_returns_none_when_env_empty_or_whitespace(plugin_pkg, monkeypatch):
    """Empty / whitespace env values count as unset — they previously
    fell through to the localhost default just like the unset case."""
    monkeypatch.setenv("DEVAGENTIC_BASE_URL", "   ")
    assert plugin_pkg.client._base_url() is None
    monkeypatch.setenv("DEVAGENTIC_BASE_URL", "")
    assert plugin_pkg.client._base_url() is None


def test_post_graphql_records_clear_error_when_base_url_unset(
    plugin_pkg, monkeypatch
):
    """The slash-command surface reads ``last_error_text()`` to enrich
    user-facing errors. When DEVAGENTIC_BASE_URL is unset, that slot
    must hold a clear actionable message — not a misleading 127.0.0.1
    connection-refused string."""
    monkeypatch.delenv("DEVAGENTIC_BASE_URL", raising=False)
    monkeypatch.setenv("DEVAGENTIC_USER_ID", "test-user")
    result = plugin_pkg.client._post_graphql("query Q { x }", {})
    assert result is None
    err = plugin_pkg.client.last_error_text()
    assert err is not None
    assert "DEVAGENTIC_BASE_URL not set" in err
    # And no misleading 127.0.0.1 reference — the whole point of the fix.
    assert "127.0.0.1" not in err
