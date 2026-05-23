"""Tests for ``utils.classify_http_error`` (#38).

Confirms the urllib-error classifier returns the right kind for
each of the five categories. Callers (canvas client, docs client,
hermes_cli.doctor) dispatch on this string to choose their own
user-facing message text.
"""
from __future__ import annotations

import socket
import urllib.error

import pytest

from utils import (
    classify_http_error,
    HTTP_ERROR_AUTH,
    HTTP_ERROR_NOT_FOUND,
    HTTP_ERROR_HTTP,
    HTTP_ERROR_UNREACHABLE,
    HTTP_ERROR_UNKNOWN,
)


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://example.test/", code, f"status {code}", {}, None)


# ── auth ──

def test_401_classifies_as_auth():
    assert classify_http_error(_http_error(401)) == HTTP_ERROR_AUTH


def test_403_classifies_as_auth():
    assert classify_http_error(_http_error(403)) == HTTP_ERROR_AUTH


# ── not_found ──

def test_404_classifies_as_not_found():
    assert classify_http_error(_http_error(404)) == HTTP_ERROR_NOT_FOUND


# ── generic http ──

@pytest.mark.parametrize("code", [400, 422, 429, 500, 502, 503, 504])
def test_other_http_status_classifies_as_http(code):
    assert classify_http_error(_http_error(code)) == HTTP_ERROR_HTTP


# ── unreachable ──

def test_url_error_classifies_as_unreachable():
    exc = urllib.error.URLError("connection refused")
    assert classify_http_error(exc) == HTTP_ERROR_UNREACHABLE


def test_os_error_classifies_as_unreachable():
    assert classify_http_error(OSError(111, "Connection refused")) == \
        HTTP_ERROR_UNREACHABLE


def test_timeout_error_classifies_as_unreachable():
    assert classify_http_error(TimeoutError("operation timed out")) == \
        HTTP_ERROR_UNREACHABLE


def test_socket_timeout_classifies_as_unreachable():
    """socket.timeout is a subclass of OSError, so it should fall
    through to the same branch."""
    assert classify_http_error(socket.timeout("timeout")) == \
        HTTP_ERROR_UNREACHABLE


# ── unknown ──

def test_unrelated_exception_classifies_as_unknown():
    assert classify_http_error(ValueError("not an http error")) == \
        HTTP_ERROR_UNKNOWN


def test_runtime_error_classifies_as_unknown():
    """RuntimeError is the kind of exception SDKs sometimes raise
    in lieu of HTTPError. It SHOULD NOT be classified as auth or
    network — leaving it as "unknown" tells the caller "this isn't
    one of the standard urllib failure modes" so they can fall
    through to message-substring heuristics if they need to."""
    assert classify_http_error(RuntimeError("401 Unauthorized")) == \
        HTTP_ERROR_UNKNOWN


# ── HTTPError-is-URLError subclass quirk ──

def test_http_error_takes_precedence_over_url_error_branch():
    """urllib.error.HTTPError inherits from URLError. The classifier
    must check HTTPError FIRST so a 401 isn't misread as just
    'unreachable'."""
    exc = _http_error(401)
    assert isinstance(exc, urllib.error.URLError)
    assert classify_http_error(exc) == HTTP_ERROR_AUTH
