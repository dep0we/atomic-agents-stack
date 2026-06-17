"""Postgres-specific tests for ``PostgresLogBackend``.

Module-level comment:
    Mock-cursor tests in this file are NON-CONFORMANCE — they test the
    backend's internal SQL generation, credential redaction, schema logic,
    and registry wiring, NOT Protocol compliance. Protocol compliance is
    verified via BACKEND_FACTORIES in test_log_protocol_conformance.py
    using a real Postgres service container (ATOMIC_AGENTS_TEST_POSTGRES_URL).

    Tests decorated with ``@requires_postgres`` (a ``skipif`` on the
    ``ATOMIC_AGENTS_TEST_POSTGRES_URL`` env var) require a real Postgres
    instance. They run in CI (the service container sets that env var) and
    skip locally when it is absent. There is no ``pytest.mark.postgres``
    marker — ``@requires_postgres`` is the actual gate.

    All other tests in this file use mocking and run unconditionally.
"""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ─────────────────────────────────────────────────────────────────
# Postgres availability gate

_POSTGRES_URL = os.environ.get("ATOMIC_AGENTS_TEST_POSTGRES_URL")
_POSTGRES_AVAILABLE = False

if _POSTGRES_URL:
    try:
        import psycopg as _psycopg_check  # noqa: F401

        _POSTGRES_AVAILABLE = True
    except ImportError:
        pass

requires_postgres = pytest.mark.skipif(
    not _POSTGRES_AVAILABLE,
    reason=(
        "Requires ATOMIC_AGENTS_TEST_POSTGRES_URL env var and psycopg installed. "
        "Set ATOMIC_AGENTS_TEST_POSTGRES_URL=postgresql://... to run Postgres tests."
    ),
)


# ─────────────────────────────────────────────────────────────────
# Helpers


def _ts(year: int, month: int, day: int, hour: int = 12) -> str:
    return datetime(year, month, day, hour, tzinfo=timezone.utc).isoformat()


def _make_record(
    *,
    ts: str | None = None,
    run_id: str = "r1",
    primitive: str = "agent_call",
    status: str = "ok",
    summary: str = "t",
    model: str = "m",
    input_tokens: int = 0,
    output_tokens: int = 0,
    **extras,
):
    from atomic_agents.logs import RunRecord

    if ts is None:
        ts = datetime.now(timezone.utc).isoformat()
    return RunRecord(
        ts=ts,
        run_id=run_id,
        primitive=primitive,
        status=status,
        summary=summary,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        **extras,
    )


# ─────────────────────────────────────────────────────────────────
# NON-CONFORMANCE: URL parsing and credential redaction
# These tests do NOT require a real Postgres instance.


def test_redact_dsn_strips_password():
    """_redact_dsn must remove the password from a postgresql:// URL."""
    from atomic_agents.logs.postgres import _redact_dsn

    url = "postgresql://alice:secretpassword@db.example.com:5432/mydb"
    redacted = _redact_dsn(url)
    # Exact-match pins the full redacted form (password -> ***, username/host/db/port
    # all preserved). Stronger than substring `in` checks, and avoids the
    # py/incomplete-url-substring-sanitization pattern CodeQL flags on `host in url`.
    assert redacted == "postgresql://alice:***@db.example.com:5432/mydb"
    assert "secretpassword" not in redacted


def test_redact_dsn_url_without_password_unchanged():
    """_redact_dsn on a URL without credentials returns a safe representation."""
    from atomic_agents.logs.postgres import _redact_dsn

    url = "postgresql://localhost:5432/mydb"
    redacted = _redact_dsn(url)
    # No password to strip — URL returned unchanged. Exact-match (not a host
    # substring check) avoids the py/incomplete-url-substring-sanitization pattern.
    assert redacted == "postgresql://localhost:5432/mydb"


def test_redact_dsn_fallback_for_malformed():
    """_redact_dsn never crashes on malformed input."""
    from atomic_agents.logs.postgres import _redact_dsn

    result = _redact_dsn("not_a_url")
    assert isinstance(result, str)


def test_redact_dsn_strips_query_string_password():
    """_redact_dsn must strip credentials in the query string, not just the netloc.

    Postgres DSNs may carry credentials as query parameters:
      postgresql://host/db?password=topsecret
      postgresql://host/db?sslpassword=keysecret
      postgresql://user:netloc@host/db?sslpassword=keysecret

    The netloc-only implementation returned these unredacted, giving false
    confidence when the netloc password was masked.
    """
    from atomic_agents.logs.postgres import _redact_dsn

    # Query-string-only password (no netloc password).
    url_qs_pw = "postgresql://host/db?password=topsecret"
    redacted = _redact_dsn(url_qs_pw)
    assert "topsecret" not in redacted, (
        "query-string 'password' must be redacted even when netloc has no password"
    )

    # sslpassword in query string.
    url_qs_ssl = "postgresql://host/db?sslpassword=keysecret"
    redacted_ssl = _redact_dsn(url_qs_ssl)
    assert "keysecret" not in redacted_ssl, (
        "query-string 'sslpassword' must be redacted"
    )

    # Both netloc and query-string credentials.
    url_both = "postgresql://user:netloc@host/db?sslpassword=keysecret"
    redacted_both = _redact_dsn(url_both)
    assert "netloc" not in redacted_both, "netloc password must still be redacted"
    assert "keysecret" not in redacted_both, (
        "query-string sslpassword must also be redacted"
    )
    assert "user" in redacted_both, "username must be preserved"


def test_redact_dsn_preserves_non_credential_query_params():
    """_redact_dsn must preserve non-credential query parameters verbatim."""
    from atomic_agents.logs.postgres import _redact_dsn

    url = "postgresql://host/db?sslmode=require&connect_timeout=10"
    redacted = _redact_dsn(url)
    assert "sslmode=require" in redacted
    assert "connect_timeout=10" in redacted


def test_redact_dsn_query_string_uses_literal_asterisks():
    """_redact_dsn must produce the literal '***' marker in both netloc and
    query-string positions — NOT the percent-encoded '%2A%2A%2A' form.

    urlencode() would percent-encode '*' by default, making 'password=***'
    appear as 'password=%2A%2A%2A' in error messages and logs.  The
    ``safe='*'`` argument to urlencode preserves the asterisks as-is.
    """
    from atomic_agents.logs.postgres import _redact_dsn

    # Query-string password: must read as '***', not '%2A%2A%2A'.
    url = "postgresql://host/db?password=topsecret"
    redacted = _redact_dsn(url)
    assert "topsecret" not in redacted, "secret must be removed"
    assert "***" in redacted, (
        f"redacted marker must be '***', not '%2A%2A%2A'. Got: {redacted!r}"
    )
    assert "%2A" not in redacted, (
        f"asterisks must not be percent-encoded. Got: {redacted!r}"
    )


def test_redact_dsn_preserves_path_valued_params_unencoded():
    """_redact_dsn must keep preserved TLS path values readable — NOT percent-
    encode the slashes into '%2F' mush.

    sslkey/sslcert/sslrootcert are deliberately excluded from redaction so an
    operator debugging a TLS failure can read the path. Running the preserved
    value through ``urlencode`` with too-narrow a safe set re-mangles the
    slashes, defeating the exact diagnostic intent the exclusion exists to
    serve (same urlencode-mangling class as the '***' marker, on a different
    character). ``safe='*/'`` preserves both.
    """
    from atomic_agents.logs.postgres import _redact_dsn

    url = (
        "postgresql://user:topsecret@host/db"
        "?sslmode=require&sslkey=/etc/ssl/client.key&password=anothersecret"
    )
    redacted = _redact_dsn(url)
    # Preserved path reads verbatim — no percent-encoding of slashes.
    assert "sslkey=/etc/ssl/client.key" in redacted, redacted
    assert "%2F" not in redacted, f"path slashes must not be encoded: {redacted!r}"
    # Credentials are still redacted in both positions.
    assert "topsecret" not in redacted, "netloc password must be redacted"
    assert "anothersecret" not in redacted, "query-string password must be redacted"
    assert redacted.count("***") >= 2, f"both credentials must show '***': {redacted!r}"


def test_redact_dsn_unencoded_slash_in_password():
    """_redact_dsn must NOT leak a password that contains a raw (un-percent-
    encoded) '/'.

    Regression guard: ``urlparse`` follows RFC-3986 and treats the first
    unencoded '/' as the start of the path, so ``parsed.password`` silently
    becomes None and a netloc-password-only redaction would emit the credential
    verbatim. The textual userinfo redaction (split authority on the last '@',
    mask everything after the first ':' in the userinfo) catches this regardless
    of percent-encoding.

    Covers both the scheme-error path (non-postgres scheme reaches _redact_dsn
    via the friendly error) and the would-be connect path.
    """
    from atomic_agents.logs.postgres import _redact_dsn

    # Raw '/' in the password — the exact urlparse mis-parse case.
    url = "mysql://user:p/a$$w@host/db"
    redacted = _redact_dsn(url)
    assert "p/a$$w" not in redacted, (
        f"raw-slash password leaked into redacted output: {redacted!r}"
    )
    assert "***" in redacted, f"redaction marker missing: {redacted!r}"
    assert "user" in redacted, f"username should be preserved: {redacted!r}"

    # Postgres scheme, raw '/' in password, with a port present — the variant
    # the SHORTCUT finding showed could raise an uncaught 'Port could not be
    # cast' from urlparse.port. _redact_dsn must still not leak and not crash.
    url_port = "postgresql://user:p/a$$w@host:5432/db"
    redacted_port = _redact_dsn(url_port)
    assert "p/a$$w" not in redacted_port, (
        f"raw-slash password leaked with port present: {redacted_port!r}"
    )
    assert "***" in redacted_port


def test_init_rejects_unencoded_slash_password_with_redacted_error():
    """Constructing with a raw '/' in the password must raise the friendly,
    REDACTED malformed-URL ValueError — never an uncaught 'Port could not be
    cast to integer value', and never with the credential in the message.

    Without the construction-time guard, urlparse drops the password (None) and
    the backend would silently connect with an empty password → confusing auth
    failure later. We refuse loudly instead.
    """
    from atomic_agents.logs.postgres import PostgresLogBackend

    with pytest.raises(ValueError) as excinfo:
        PostgresLogBackend("postgresql://user:p/a$$w@host:5432/db")
    msg = str(excinfo.value)
    assert "p/a$$w" not in msg, f"credential leaked into error message: {msg!r}"
    assert "percent-encode" in msg.lower() or "malformed" in msg.lower(), msg


def test_redact_dsn_unencoded_hash_or_question_in_password():
    """_redact_dsn must NOT leak a password that contains a raw '#' or '?'.

    Round-3 regression guard: the round-2 fix handled a raw '/' in the password
    but the netloc redaction still skipped redaction entirely when a '?' or '#'
    appeared before the '@' (it was assumed to be a query/fragment delimiter).
    A '#' or '?' INSIDE the password is before the '@', so the credential leaked
    verbatim. ``urlparse`` mis-isolates all three characters identically; the
    textual userinfo split (last '@' after '://', mask after the first ':') must
    catch every one of them.
    """
    from atomic_agents.logs.postgres import _redact_dsn

    for pw in ("pa#ss", "pa?ss", "Xy9#Kq2mZ!vL", "S3cr?tWord"):
        url = f"postgresql://user:{pw}@host:5432/db"
        redacted = _redact_dsn(url)
        assert pw not in redacted, (
            f"password with special char leaked: pw={pw!r} redacted={redacted!r}"
        )
        assert "***" in redacted, f"redaction marker missing: {redacted!r}"

    # A PORT-LESS query-string '@' with no userinfo password is left intact:
    # the textual userinfo split sees no ':' before the '@', so nothing is
    # masked. NOTE: this is NOT a general "over-redaction only fires when a
    # password is present" invariant — add an explicit port (host:5432) and the
    # ':' in 'host:5432' DOES sit before the query '@', so _redact_dsn
    # over-masks it ('postgresql://host:***@b'). That over-redaction of a
    # non-secret is the documented security-first stance and is harmless; the
    # functional line we must NOT cross is __init__ REFUSING such a valid URL
    # (covered by test_init_accepts_credentialless_url_with_port_and_query_at).
    safe = _redact_dsn("postgresql://host/db?application_name=a@b")
    assert "application_name" in safe, safe


def test_init_rejects_hash_or_question_password_without_fragment_leak():
    """Constructing with a raw '#'/'?' in the password must raise the friendly,
    REDACTED ValueError — and must NOT echo even a FRAGMENT of the password.

    Round-3 regression guard: before the fix, '#'/'?' passwords fell through the
    credential-isolation check (which clamped the authority on '?'/'#') into the
    ``parsed.port`` path, whose stdlib ValueError text echoes the leading run of
    the password (e.g. "Port could not be cast … as 'Xy9'"). Interpolating that
    exc into the message partially leaked the credential. The construction guard
    must now fire first and the port-error message must never include exc text.
    """
    from atomic_agents.logs.postgres import PostgresLogBackend

    for pw in ("pa#ss", "Xy9#Kq2mZ", "S3cr?tWord", "pre?post"):
        with pytest.raises(ValueError) as excinfo:
            PostgresLogBackend(f"postgresql://user:{pw}@host:5432/db")
        msg = str(excinfo.value)
        assert pw not in msg, f"full credential leaked: pw={pw!r} msg={msg!r}"
        # No password fragment up to the first special char may survive either.
        leading = pw.split("#")[0].split("?")[0]
        # The fragment must not appear in the port-error tail. The friendly
        # rejection message contains no exc text at all, so any occurrence would
        # be a leak (guard against the leading fragment ≥ 3 chars to avoid
        # incidental matches like 'a' inside 'password').
        if len(leading) >= 3:
            assert leading not in msg, (
                f"password fragment leaked into error: frag={leading!r} msg={msg!r}"
            )
        assert "percent-encode" in msg.lower() or "malformed" in msg.lower(), msg


def test_init_accepts_credentialless_url_with_port_and_query_at():
    """A VALID credential-less DSN that has both an explicit port and an '@' in a
    query value must CONSTRUCT successfully — it is not a malformed password.

    Regression guard: the prior password-detection heuristic fired on
    ``parsed.password is None`` + ``rfind('@')`` finding a ':' before the last
    '@'. With an explicit port the ':' in 'host:5432' sits before the query '@',
    so a legitimate URL like
    ``postgresql://host:5432/db?application_name=a@b`` was wrongly REFUSED. The
    fix gates detection on ``parsed.port`` actually raising (the only signal that
    urlparse genuinely mis-isolated the authority); a clean port parse means the
    None password is the truth, not a parse failure.

    Both the ported and port-less forms must construct, and the parsed
    components must reflect the real (credential-less) authority — the query '@'
    must not have been mis-read as a userinfo separator.
    """
    from atomic_agents.logs.postgres import PostgresLogBackend

    for url, expected_port in (
        ("postgresql://host:5432/db?application_name=a@b", 5432),
        ("postgresql://host/db?application_name=a@b", 5432),  # default
    ):
        backend = PostgresLogBackend(url)
        assert backend._host == "host", url
        assert backend._port == expected_port, url
        assert backend._dbname == "db", url
        assert backend._user == "", url
        assert backend._password == "", url


def test_init_still_rejects_special_char_password_with_port():
    """The targeted percent-encode message must still fire for a real password
    that contains an unencoded special char, EVEN with an explicit port present —
    the port-gated restructure must not weaken the leak guard for actual
    credentials. Mirrors the no-leak assertions of the round-3 guard.
    """
    from atomic_agents.logs.postgres import PostgresLogBackend

    for pw in ("pa/ss", "pa?ss", "pa#ss", "Xy9#Kq2mZ"):
        with pytest.raises(ValueError) as excinfo:
            PostgresLogBackend(f"postgresql://user:{pw}@host:5432/db")
        msg = str(excinfo.value)
        assert pw not in msg, f"credential leaked: pw={pw!r} msg={msg!r}"
        assert "malformed" in msg.lower() or "percent-encode" in msg.lower(), msg


def test_init_rejects_digit_leading_special_char_password_no_silent_construct():
    """Round-5 P1 regression: a password that STARTS WITH DIGITS followed by an
    unencoded '/' makes ``parsed.port`` cast the digit prefix as a VALID port and
    NOT raise — so the round-4 port-gated detection never fired and the backend
    SILENTLY CONSTRUCTED with garbage components (username as host, password
    prefix as port, credentials dropped). The fix adds a second detection arm:
    after a clean port parse, an unencoded '@' in ``parsed.path`` means urlparse
    mis-isolated a special-char password.

    These URLs MUST refuse (not silently construct), with the actionable
    percent-encode message and no credential leak. Covers the digit-leading
    shape AND the double-'@' shape (``user:p@ss/word@host/db``) where the first
    '@' is consumed by urlparse into a partial userinfo.
    """
    from atomic_agents.logs.postgres import PostgresLogBackend

    # (url, distinctive secret fragment that must NOT survive redaction). The
    # fragments are deliberately not substrings of the static message text
    # ("password", "percent-encode", "encoded", etc.) to avoid false positives.
    cases = (
        ("postgresql://user:5432/myS3cretPw@dbhost:5432/mydb", "myS3cretPw"),
        ("postgresql://user:12345/h1ddenCreds@host/db", "h1ddenCreds"),
        ("postgresql://user:p@ssZZ/qqWord@host:5432/db", "qqWord"),
    )
    for url, secret in cases:
        with pytest.raises(ValueError) as excinfo:
            PostgresLogBackend(url)
        msg = str(excinfo.value)
        assert secret not in msg, f"credential leaked: {secret!r} in {msg!r}"
        assert "malformed" in msg.lower() or "percent-encode" in msg.lower(), msg


def test_backend_rejects_non_postgres_scheme():
    """PostgresLogBackend must reject non-postgresql:// schemes immediately."""
    from atomic_agents.logs.postgres import PostgresLogBackend

    with pytest.raises(ValueError, match="postgresql"):
        PostgresLogBackend("sqlite:///foo.db")


def test_backend_rejects_sqlite_url():
    """Explicit sqlite:// URL must raise ValueError at construction time."""
    from atomic_agents.logs.postgres import PostgresLogBackend

    with pytest.raises(ValueError):
        PostgresLogBackend("sqlite:///foo.db")


def test_connection_failure_raises_value_error_not_psycopg_error():
    """Connection failure must surface as ValueError with redacted URL,
    NOT as a raw psycopg OperationalError that may embed credentials.

    Layer A+B credential redaction: the ValueError message must NOT contain
    the password from the connection URL.

    psycopg_mock.Error must be set to the SAME exception class that
    fail_connect raises — the production code catches ``psycopg.Error``
    (base class), so only exceptions that ARE-A psycopg.Error trigger the
    redaction path.  Using a different class for OperationalError vs Error
    would cause the mock ``except psycopg.Error`` clause to resolve to a
    MagicMock, which raises TypeError instead of exercising redaction.
    """
    psycopg_mock = MagicMock()
    # Error must be the class that fail_connect raises so the production
    # ``except psycopg.Error`` branch actually runs.
    psycopg_mock.Error = Exception
    psycopg_mock.rows = MagicMock()
    psycopg_mock.rows.dict_row = MagicMock()

    def fail_connect(**kwargs):
        raise Exception(
            "connection to server at '...' (password 'secretpassword') failed"
        )

    psycopg_mock.connect = fail_connect

    with patch.dict(sys.modules, {"psycopg": psycopg_mock}):
        from atomic_agents.logs.postgres import PostgresLogBackend

        backend = PostgresLogBackend("postgresql://user:secretpassword@localhost/db")
        # Must raise ValueError (not raw psycopg exception) with redacted URL.
        with pytest.raises(ValueError) as exc_info:
            backend._get_conn()
        # The password must not appear in any raised exception message.
        assert "secretpassword" not in str(exc_info.value)


def test_broken_connection_triggers_reconnect():
    """After a connection goes broken (server restart / network blip),
    the next append() must succeed (reconnect) rather than raising forever.

    This pins the P1 broken-connection-caching fix: _get_conn() must treat
    a cached-but-dead connection as absent and rebuild it, instead of
    re-using the broken conn which raises on every subsequent write.
    """
    call_count = 0

    def make_mock_conn(is_broken: bool):
        conn = MagicMock()
        conn.closed = 1 if is_broken else 0
        conn.broken = is_broken
        conn.execute.return_value = MagicMock()
        return conn

    good_conn = make_mock_conn(is_broken=False)

    psycopg_mock = MagicMock()
    psycopg_mock.Error = Exception

    def connect_side_effect(**kwargs):
        nonlocal call_count
        call_count += 1
        return good_conn

    psycopg_mock.connect.side_effect = connect_side_effect
    psycopg_mock.rows = MagicMock()
    psycopg_mock.rows.dict_row = MagicMock()

    from atomic_agents.logs.postgres import PostgresLogBackend

    backend = PostgresLogBackend.__new__(PostgresLogBackend)
    backend._host = "localhost"
    backend._port = 5432
    backend._dbname = "test"
    backend._user = "u"
    backend._password = "p"
    backend._safe_url = "postgresql://u:***@localhost/test"
    backend._tls = threading.local()

    # Pre-load a broken connection into the thread-local cache.
    broken_conn = make_mock_conn(is_broken=True)
    backend._tls.conn = broken_conn

    # _get_conn must detect the broken conn, discard it, and reconnect.
    with patch.dict(sys.modules, {"psycopg": psycopg_mock}):
        conn = backend._get_conn()

    # Must have reconnected (called connect() once), NOT re-used the broken conn.
    assert psycopg_mock.connect.called, "_get_conn must reconnect when conn is broken"
    assert conn is good_conn, "_get_conn must return the fresh connection"
    assert backend._tls.conn is good_conn, "Thread-local must be updated to new conn"


def _bare_backend():
    """Build a PostgresLogBackend with stubbed connection params, no real connect."""
    from atomic_agents.logs.postgres import PostgresLogBackend

    backend = PostgresLogBackend.__new__(PostgresLogBackend)
    backend._host = "localhost"
    backend._port = 5432
    backend._dbname = "test"
    backend._user = "u"
    backend._password = "p"
    backend._safe_url = "postgresql://u:***@localhost/test"
    backend._tls = threading.local()
    return backend


def test_append_transparent_reconnect_on_not_yet_flagged_drop():
    """P1 regression: the FIRST write after a server-side termination (not yet
    reflected in conn.closed/broken) must NOT abort the agent run.

    The framework's _log() path calls append() WITHOUT a try/except, so a raw
    connection-level error propagating out of append() takes down the whole
    agent.call(). _run_with_reconnect() must catch the connection-level error,
    rebuild the connection, and retry the INSERT once — so a single transient
    drop costs zero records and zero failed runs.

    A genuine statement-level error must still propagate (no retry), and a
    persistent connection failure (both attempts fail) must re-raise.
    """
    psycopg_mock = MagicMock()

    class OperationalError(Exception):
        pass

    psycopg_mock.OperationalError = OperationalError
    psycopg_mock.Error = Exception

    # append() does `import psycopg.types.json as _pj` and calls _pj.Jsonb(...).
    # Provide a stub submodule so the import resolves and Jsonb is callable.
    psycopg_types_mock = MagicMock()
    psycopg_json_mock = MagicMock()
    psycopg_json_mock.Jsonb = lambda v: v
    psycopg_types_mock.json = psycopg_json_mock

    module_patch = {
        "psycopg": psycopg_mock,
        "psycopg.types": psycopg_types_mock,
        "psycopg.types.json": psycopg_json_mock,
    }
    with patch.dict(sys.modules, module_patch):
        # ── Case 1: connection-level drop on first execute → retry succeeds ──
        backend = _bare_backend()
        dead_conn = MagicMock()
        dead_conn.closed = 0  # NOT flagged dead — the exact P1 scenario.
        dead_conn.broken = False
        dead_conn.execute.side_effect = OperationalError(
            "terminating connection due to administrator command"
        )
        fresh_conn = MagicMock()
        fresh_conn.closed = 0
        fresh_conn.broken = False
        fresh_conn.execute.return_value = MagicMock()

        conns = iter([dead_conn, fresh_conn])
        backend._get_conn = lambda: next(conns)  # type: ignore[assignment]

        # Must NOT raise — the retry against fresh_conn succeeds.
        backend.append(_make_record(run_id="reconnect_ok"))
        assert fresh_conn.execute.called, "retry must run the INSERT on a fresh conn"
        assert fresh_conn.commit.called, "retry must commit the INSERT"

        # ── Case 2: statement-level error must NOT be retried ──
        backend2 = _bare_backend()
        only_conn = MagicMock()
        only_conn.closed = 0
        only_conn.broken = False

        class ProgrammingError(Exception):  # not a connection error
            pass

        psycopg_mock.OperationalError = OperationalError
        only_conn.execute.side_effect = ProgrammingError("bad column")
        get_calls = {"n": 0}

        def _one_conn():
            get_calls["n"] += 1
            return only_conn

        backend2._get_conn = _one_conn  # type: ignore[assignment]
        with pytest.raises(ProgrammingError):
            backend2.append(_make_record(run_id="stmt_err"))
        # _get_conn called once by append's pre-check + once inside the wrapper;
        # the key assertion is that NO retry happened (no third call).
        assert get_calls["n"] <= 2, "statement-level error must not trigger a retry"

        # ── Case 3: persistent connection failure → re-raise after one retry ──
        backend3 = _bare_backend()

        def _always_dead():
            c = MagicMock()
            c.closed = 0
            c.broken = False
            c.execute.side_effect = OperationalError("server still down")
            return c

        backend3._get_conn = _always_dead  # type: ignore[assignment]
        with pytest.raises(OperationalError):
            backend3.append(_make_record(run_id="persistent_down"))


def test_append_does_not_retry_on_commit_phase_drop():
    """P1 audit-integrity: a connection-level drop at commit() must NOT be
    retried — the INSERT may already be committed server-side and only the ack
    lost on the wire (restart/failover/blip during commit). Retrying would
    issue a SECOND INSERT and silently DOUBLE the audit row (run_records has no
    uniqueness column) and double-count cost.

    Contract: when execute() SUCCEEDS but commit() raises a connection-level
    error, append() must (a) issue the INSERT exactly once — no second execute
    against a fresh connection — and (b) re-raise the original connection error
    (the row's persistence is unknown; the caller must not assume it landed).

    Contrast with test_append_transparent_reconnect_on_not_yet_flagged_drop's
    Case 1, where the drop happens at execute() (row provably NOT persisted) and
    a one-shot retry IS the correct, safe behavior.
    """
    psycopg_mock = MagicMock()

    class OperationalError(Exception):
        pass

    psycopg_mock.OperationalError = OperationalError
    psycopg_mock.Error = Exception

    psycopg_types_mock = MagicMock()
    psycopg_json_mock = MagicMock()
    psycopg_json_mock.Jsonb = lambda v: v
    psycopg_types_mock.json = psycopg_json_mock

    module_patch = {
        "psycopg": psycopg_mock,
        "psycopg.types": psycopg_types_mock,
        "psycopg.types.json": psycopg_json_mock,
    }
    with patch.dict(sys.modules, module_patch):
        backend = _bare_backend()

        # First connection: execute() SUCCEEDS, commit() raises a
        # connection-level error (the lost-commit-ack scenario).
        committed_conn = MagicMock()
        committed_conn.closed = 0
        committed_conn.broken = False
        committed_conn.execute.return_value = MagicMock()
        committed_conn.commit.side_effect = OperationalError(
            "server closed the connection unexpectedly (ack lost after commit)"
        )

        # A would-be fresh connection — must NEVER be used: if the wrapper
        # retried, it would INSERT a second, duplicate row here.
        forbidden_conn = MagicMock()
        forbidden_conn.closed = 0
        forbidden_conn.broken = False

        # append() calls _get_conn() twice on the no-drop path: once in its
        # pre-check (to confirm psycopg is importable) and once inside
        # _run_with_reconnect. Both must return the SAME committed_conn so the
        # INSERT-then-failing-commit happens on the connection under test. Any
        # THIRD call would be a retry — it must hit forbidden_conn, which we
        # assert is never executed against.
        get_calls = {"n": 0}

        def _conn_provider():
            get_calls["n"] += 1
            return committed_conn if get_calls["n"] <= 2 else forbidden_conn

        backend._get_conn = _conn_provider  # type: ignore[assignment]

        # The commit-level connection error must propagate (the row's fate is
        # unknown; the caller must not silently swallow it) and must NOT trigger
        # a retry.
        with pytest.raises(OperationalError):
            backend.append(_make_record(run_id="commit_ack_lost"))

        assert committed_conn.execute.call_count == 1, (
            "INSERT must be issued exactly once — no retry past the commit point"
        )
        assert not forbidden_conn.execute.called, (
            "a commit-phase drop must NOT retry the INSERT on a fresh "
            "connection — that would DOUBLE the audit row"
        )


def test_close_is_idempotent():
    """close() must be safe to call multiple times (spec/22 MUST 7 lifecycle).

    First call closes the cached connection and nulls the thread-local; every
    subsequent call is a no-op that neither raises nor re-closes — and a close()
    on a backend that never opened a connection is also a no-op.
    """
    backend = _bare_backend()

    # close() with no connection ever opened — must not raise.
    backend.close()
    assert getattr(backend._tls, "conn", None) is None

    # Seed a connection, close once, then close again twice more.
    conn = MagicMock()
    backend._tls.conn = conn
    backend.close()
    assert conn.close.call_count == 1, "first close() must close the live conn once"
    assert getattr(backend._tls, "conn", None) is None

    backend.close()
    backend.close()
    assert conn.close.call_count == 1, (
        "subsequent close() calls must be no-ops (idempotent), not re-close"
    )


def test_non_operational_error_at_connect_is_redacted():
    """Non-OperationalError psycopg exceptions at connect time must also
    be caught and redacted.  Previously only OperationalError was caught;
    ProgrammingError / SSL errors could propagate with raw DSN text.
    """
    psycopg_mock = MagicMock()
    # Use a custom exception that is a subclass of the mock base Error.
    psycopg_mock.Error = RuntimeError  # broad base — catch-all

    def fail_connect(**kwargs):
        raise RuntimeError(
            "connection to server at '...' (password 'secretpassword') "
            "FATAL: database 'wrongdb' does not exist"
        )

    psycopg_mock.connect = fail_connect
    psycopg_mock.rows = MagicMock()
    psycopg_mock.rows.dict_row = MagicMock()

    with patch.dict(sys.modules, {"psycopg": psycopg_mock}):
        from atomic_agents.logs.postgres import PostgresLogBackend

        backend = PostgresLogBackend(
            "postgresql://user:secretpassword@localhost/wrongdb"
        )
        with pytest.raises((ValueError, Exception)) as exc_info:
            backend._get_conn()
        assert "secretpassword" not in str(exc_info.value), (
            "Non-OperationalError connect failures must also be redacted"
        )


def test_read_error_discards_connection_prevents_aborted_tx_trap():
    """After a read (query/tail/aggregate/stats) raises an exception, the
    connection must be discarded so the NEXT read gets a fresh connection
    rather than hitting 'current transaction is aborted' on the ABORTED-state
    connection.

    Without the read-path try/except+discard, autocommit=False leaves an
    implicit transaction open.  If the read errors, the transaction is left
    ABORTED and every subsequent execute() on that thread raises
    InFailedSqlTransaction until the process restarts — silent permanent
    audit loss after a transient failure.

    This test pins the FULL recovery loop:
      1. failed read discards the connection (tls.conn → None)
      2. the NEXT read calls _get_conn(), which calls psycopg.connect() to
         rebuild a fresh connection — the wired second-connection branch is
         load-bearing, NOT bypassed by manual injection.
    """
    import threading
    from unittest.mock import MagicMock, patch
    from atomic_agents.logs.postgres import PostgresLogBackend, _SCHEMA_VERSION
    from atomic_agents.logs import LogQuery

    # First connection: execute raises immediately (simulates a transient SQL
    # error leaving the transaction in ABORTED state). The error must be a
    # psycopg.Error subclass: the read-failure wrap now catches psycopg.Error
    # NARROWLY (not bare Exception), so a non-psycopg error would propagate as a
    # code defect rather than being wrapped into LogBackendReadError. We use a
    # dedicated subclass so the cause assertion below is type-precise.
    class _FakePsycopgError(Exception):
        """Stands in for a psycopg.Error subclass (the mock aliases
        ``psycopg.Error = _FakePsycopgError`` below)."""

    bad_conn = MagicMock()
    bad_conn.closed = 0
    bad_conn.broken = False
    bad_conn.execute.side_effect = _FakePsycopgError("simulated SQL error")

    # Second connection: healthy.
    # _ensure_schema calls execute(advisory_lock), execute(CREATE TABLE×N),
    # execute(INSERT meta), execute(SELECT version) → fetchone() must return a
    # valid schema_version row.  query() then calls execute(SELECT ...) →
    # fetchall() → [].
    good_cursor = MagicMock()
    good_cursor.fetchone.return_value = {"value": str(_SCHEMA_VERSION)}
    good_cursor.fetchall.return_value = []
    good_conn = MagicMock()
    good_conn.closed = 0
    good_conn.broken = False
    good_conn.execute.return_value = good_cursor

    connect_calls = []

    def connect_side_effect(**kwargs):
        # bad_conn is pre-injected directly into _tls.conn — it never goes
        # through psycopg.connect().  Every psycopg.connect() call therefore
        # returns good_conn (the rebuilt connection after the discard).
        connect_calls.append(good_conn)
        return good_conn

    psycopg_mock = MagicMock()
    # The read-failure wrap resolves the catch class via psycopg.Error; alias it
    # to our fake subclass so the narrow catch actually wraps the injected error.
    psycopg_mock.Error = _FakePsycopgError
    psycopg_mock.connect.side_effect = connect_side_effect
    psycopg_mock.rows = MagicMock()
    psycopg_mock.rows.dict_row = MagicMock()

    backend = PostgresLogBackend.__new__(PostgresLogBackend)
    backend._host = "localhost"
    backend._port = 5432
    backend._dbname = "test"
    backend._user = "u"
    backend._password = "p"
    backend._safe_url = "postgresql://u:***@localhost/test"
    backend._tls = threading.local()

    with patch.dict(__import__("sys").modules, {"psycopg": psycopg_mock}):
        # Prime the thread-local with the bad connection (simulates in-flight
        # reuse of a connection whose transaction went ABORTED).
        backend._tls.conn = bad_conn

        # First query must raise LogBackendReadError (spec/22 addendum: the
        # postgres backend wraps a psycopg.Error surviving the one-shot
        # reconnect into LogBackendReadError). The original psycopg error is
        # the __cause__.
        from atomic_agents import LogBackendReadError

        with pytest.raises(LogBackendReadError) as exc_info:
            backend.query(LogQuery(run_id=None))
        assert isinstance(exc_info.value.__cause__, _FakePsycopgError)
        assert "simulated SQL error" in str(exc_info.value.__cause__)

        # After the error, the connection must have been discarded.
        assert backend._tls.conn is None, (
            "Failed read must discard the thread-local connection to prevent "
            "the aborted-transaction trap on the next call"
        )

        # The next read must:
        #   (a) call psycopg.connect() to rebuild (NOT use manually injected conn)
        #   (b) succeed and return an empty list
        # Do NOT manually set backend._tls.conn — let _get_conn() rebuild it.
        result = backend.query(LogQuery(run_id=None))
        assert result == [], "Second read on fresh connection must succeed"

        # Exactly one psycopg.connect() call proves the rebuild path was
        # exercised: bad_conn was pre-injected (no connect call), and the
        # second query triggered _get_conn() → psycopg.connect() → good_conn.
        assert len(connect_calls) == 1, (
            "_get_conn() must have called psycopg.connect() once to rebuild "
            "the fresh connection on the second query"
        )


def test_tail_and_aggregate_wrap_psycopg_error_as_log_backend_read_error():
    """tail() and aggregate() MUST wrap a psycopg.Error into LogBackendReadError,
    symmetric with the query() path.

    spec/22 read-failure addendum (#497). SCOPE NOTE (review #497): every
    connection this mock builds raises ``psycopg.Error`` on execute(), so the
    failure surfaces inside ``_get_conn_for_read()`` (the connect-time
    schema-read establishment) — which is the REALISTIC corruption surface the
    addendum's boundary table names, and is itself a read-failure wrap that
    tail()/aggregate() must honor. It does NOT exercise the post-connect
    ``_run_with_reconnect`` wrap (that would need a connection whose schema
    establishment succeeds but whose data query then fails); that shared wrap is
    already proven for query() by
    ``test_read_error_discards_connection_prevents_aborted_tx_trap`` (pre-seeded
    ``_tls.conn``). The conformance read-error tests cover tail/aggregate
    end-to-end ONLY against a live server (skipped without
    ATOMIC_AGENTS_TEST_POSTGRES_URL); this pins the off-server connect-time wrap
    for the other two read methods so they are not silently uncovered.
    """
    import threading
    from unittest.mock import MagicMock, patch

    from atomic_agents import LogBackendReadError
    from atomic_agents.logs import LogAggregate, LogQuery
    from atomic_agents.logs.postgres import PostgresLogBackend

    # A psycopg.Error subclass — the wrap catches psycopg.Error NARROWLY, so a
    # non-psycopg error would propagate as a code defect rather than be wrapped.
    class _FakePsycopgError(Exception):
        pass

    def _make_failing_conn():
        # Every connection's execute raises — both the initial connection and
        # any one-shot reconnect attempt — so the read definitively fails (not a
        # transient disconnect that recovers on retry).
        c = MagicMock()
        c.closed = 0
        c.broken = False
        c.execute.side_effect = _FakePsycopgError("simulated unrecoverable read")
        return c

    psycopg_mock = MagicMock()
    psycopg_mock.Error = _FakePsycopgError
    psycopg_mock.connect.side_effect = lambda **kw: _make_failing_conn()
    psycopg_mock.rows = MagicMock()
    psycopg_mock.rows.dict_row = MagicMock()

    def _fresh_backend():
        b = PostgresLogBackend.__new__(PostgresLogBackend)
        b._host = "localhost"
        b._port = 5432
        b._dbname = "test"
        b._user = "u"
        b._password = "p"
        b._safe_url = "postgresql://u:***@localhost/test"
        b._tls = threading.local()
        return b

    with patch.dict(__import__("sys").modules, {"psycopg": psycopg_mock}):
        with pytest.raises(LogBackendReadError) as ei_tail:
            _fresh_backend().tail(5)
        assert isinstance(ei_tail.value.__cause__, _FakePsycopgError)

        with pytest.raises(LogBackendReadError) as ei_agg:
            _fresh_backend().aggregate(
                LogQuery(),
                LogAggregate(group_by=("primitive",), metric="count"),
            )
        assert isinstance(ei_agg.value.__cause__, _FakePsycopgError)


# ─────────────────────────────────────────────────────────────────
# NON-CONFORMANCE: SQL generation (no real DB needed)


def test_insert_sql_uses_percent_s_not_question_mark():
    """_INSERT_SQL must use %s placeholders (psycopg3 paramstyle='pyformat'),
    NOT ? (sqlite3's qmark paramstyle). Mixing them produces a silent
    runtime error — this test prevents accidental SQLite→Postgres porting.
    """
    from atomic_agents.logs.postgres import _INSERT_SQL

    assert "?" not in _INSERT_SQL
    assert "%s" in _INSERT_SQL


def test_insert_sql_column_count_matches_values():
    """The INSERT statement must have one %s per column."""
    from atomic_agents.logs.postgres import _INSERT_COLUMNS, _INSERT_SQL

    placeholder_count = _INSERT_SQL.count("%s")
    assert placeholder_count == len(_INSERT_COLUMNS), (
        f"INSERT SQL has {placeholder_count} placeholders but "
        f"_INSERT_COLUMNS has {len(_INSERT_COLUMNS)} entries"
    )


def test_canonical_columns_derived_from_run_record():
    """_CANONICAL_COLUMNS must be derived from RunRecord.__dataclass_fields__,
    not hand-coded (prevents drift when new fields are added to RunRecord).
    """
    from atomic_agents.logs.postgres import _CANONICAL_COLUMNS
    from atomic_agents.logs import RunRecord

    expected = frozenset(
        name for name in RunRecord.__dataclass_fields__ if name != "extra"
    )
    assert _CANONICAL_COLUMNS == expected


def test_build_query_sql_uses_percent_s():
    """_build_query_sql must produce %s placeholders, never ?."""
    from atomic_agents.logs.postgres import PostgresLogBackend
    from atomic_agents.logs import LogQuery

    # Construct without connecting — URL parsing only.
    backend = PostgresLogBackend.__new__(PostgresLogBackend)
    backend._host = "localhost"
    backend._port = 5432
    backend._dbname = "test"
    backend._user = "u"
    backend._password = "p"
    backend._safe_url = "postgresql://u:***@localhost/test"
    backend._tls = threading.local()

    sql, params = backend._build_query_sql(
        LogQuery(run_id="r1", primitive="agent_call"),
        select="*",
        order_limit=True,
    )
    assert "?" not in sql
    assert "%s" in sql
    assert "r1" in params
    assert "agent_call" in params


def test_build_query_sql_any_for_tuple_primitive():
    """Tuple primitive filter must use ANY(%s) with a list param,
    not N individual ? or %s placeholders.
    """
    from atomic_agents.logs.postgres import PostgresLogBackend
    from atomic_agents.logs import LogQuery

    backend = PostgresLogBackend.__new__(PostgresLogBackend)
    backend._host = "localhost"
    backend._port = 5432
    backend._dbname = "test"
    backend._user = "u"
    backend._password = "p"
    backend._safe_url = "postgresql://u:***@localhost/test"
    backend._tls = threading.local()

    sql, params = backend._build_query_sql(
        LogQuery(primitive=("agent_call", "helper")),
        select="*",
        order_limit=False,
    )
    assert "ANY(%s)" in sql
    # The list of primitives should be a single list parameter.
    assert any(isinstance(p, list) for p in params)
    list_param = next(p for p in params if isinstance(p, list))
    assert set(list_param) == {"agent_call", "helper"}


# ──────────────────────────────────────────────────────────────────
# spec/45 PR2 / spec/22 addendum — idempotency_key parity (static, no DB).
# These verify the Postgres backend honors the spec/22 versioned normative
# addendum point 4 (LogQuery.idempotency_key MUST be an AND-predicate on
# conforming backends) without requiring a live Postgres connection.


def test_postgres_schema_has_idempotency_columns_and_index():
    """The Postgres CREATE TABLE + index set MUST include the idempotency audit
    columns + idx_idempotency_key (spec/22 addendum point 4). Without these,
    LogQuery(idempotency_key=...) silently returns wrong results and the two
    RunRecord audit fields are dropped on write."""
    from atomic_agents.logs.postgres import (
        _CREATE_INDEXES,
        _CREATE_RUN_RECORDS,
        _INSERT_COLUMNS,
        _SCHEMA_VERSION,
    )

    assert "idempotency_key" in _CREATE_RUN_RECORDS, (
        "Postgres run_records MUST have an idempotency_key column"
    )
    assert "replayed_run_id" in _CREATE_RUN_RECORDS, (
        "Postgres run_records MUST have a replayed_run_id column"
    )
    assert "idempotency_key" in _INSERT_COLUMNS
    assert "replayed_run_id" in _INSERT_COLUMNS
    assert any("idx_idempotency_key" in stmt for stmt in _CREATE_INDEXES), (
        "Postgres MUST create idx_idempotency_key (spec/22 addendum)"
    )
    # Schema bumped to v2 with the migration ladder.
    assert _SCHEMA_VERSION == 2


def test_postgres_build_query_sql_includes_idempotency_key_predicate():
    """LogQuery(idempotency_key=...) MUST add an AND-predicate clause on the
    Postgres backend (spec/22 addendum point 4). Without this clause the filter
    is silently ignored and ALL records are returned."""
    from atomic_agents.logs import LogQuery
    from atomic_agents.logs.postgres import PostgresLogBackend

    backend = PostgresLogBackend.__new__(PostgresLogBackend)
    backend._host = "localhost"
    backend._port = 5432
    backend._dbname = "test"
    backend._user = "u"
    backend._password = "p"
    backend._safe_url = "postgresql://u:***@localhost/test"
    backend._tls = threading.local()

    sql, params = backend._build_query_sql(
        LogQuery(idempotency_key="my-key-123"),
        select="*",
        order_limit=False,
    )
    assert "idempotency_key = %s" in sql, (
        "Postgres _build_query_sql MUST emit an idempotency_key AND-predicate"
    )
    assert "my-key-123" in params


def test_postgres_build_query_sql_no_idempotency_predicate_when_absent():
    """NEGATIVE control: LogQuery with NO idempotency_key MUST NOT add the
    idempotency_key clause (so unfiltered queries return all records)."""
    from atomic_agents.logs import LogQuery
    from atomic_agents.logs.postgres import PostgresLogBackend

    backend = PostgresLogBackend.__new__(PostgresLogBackend)
    backend._host = "localhost"
    backend._port = 5432
    backend._dbname = "test"
    backend._user = "u"
    backend._password = "p"
    backend._safe_url = "postgresql://u:***@localhost/test"
    backend._tls = threading.local()

    sql, _ = backend._build_query_sql(
        LogQuery(run_id="r1"), select="*", order_limit=False
    )
    assert "idempotency_key" not in sql, (
        "no idempotency_key filter -> no idempotency_key clause"
    )


def test_postgres_ensure_schema_v1_to_v2_migration_ladder_order():
    """BEHAVIORAL coverage of the Postgres v1→v2 migration (not string-presence
    on the constants): drive _ensure_schema() with a mock conn pre-seeded to
    schema_version='1' and assert it issues BOTH idempotency ADD-COLUMN ALTERs
    and the meta UPDATE BEFORE any idx_idempotency_key CREATE INDEX.

    The ordering is load-bearing: idx_idempotency_key references the
    idempotency_key column, so it MUST be created AFTER the ALTER that adds it.
    No live Postgres required — mirrors the SQLite on-disk migration test for
    cross-backend parity (the CHANGELOG/CLAUDE.md 'Postgres v1→v2 migration'
    claim is otherwise unverified; static column-name string checks do not
    exercise the ALTER/UPDATE ladder)."""
    from atomic_agents.logs.postgres import PostgresLogBackend

    backend = PostgresLogBackend.__new__(PostgresLogBackend)

    executed: list[str] = []

    def _execute(sql, params=None):
        executed.append(sql)
        cur = MagicMock()
        # The meta SELECT inside _ensure_schema must report v1 so the migration
        # ladder runs. Any other execute() returns a benign cursor.
        if "SELECT value FROM meta" in sql:
            cur.fetchone.return_value = {"value": "1"}
        else:
            cur.fetchone.return_value = None
        return cur

    conn = MagicMock()
    conn.execute.side_effect = _execute

    backend._ensure_schema(conn)

    joined = "\n".join(executed)
    # Both new columns added via idempotent ADD COLUMN IF NOT EXISTS.
    assert any(
        "ADD COLUMN IF NOT EXISTS" in s and "idempotency_key" in s for s in executed
    ), "v1→v2 migration MUST ALTER ... ADD COLUMN IF NOT EXISTS idempotency_key"
    assert any(
        "ADD COLUMN IF NOT EXISTS" in s and "replayed_run_id" in s for s in executed
    ), "v1→v2 migration MUST ALTER ... ADD COLUMN IF NOT EXISTS replayed_run_id"
    # meta bumped to '2'.
    assert any("UPDATE meta SET value = '2'" in s for s in executed), (
        "v1→v2 migration MUST bump meta.schema_version to 2"
    )

    # Ordering invariant: the idempotency_key ALTER MUST precede the
    # idx_idempotency_key CREATE INDEX (the index references the new column).
    alter_idx = next(
        i
        for i, s in enumerate(executed)
        if "ADD COLUMN IF NOT EXISTS" in s and "idempotency_key" in s
    )
    create_index_idx = next(
        i for i, s in enumerate(executed) if "idx_idempotency_key" in s
    )
    assert alter_idx < create_index_idx, (
        "ADD COLUMN idempotency_key MUST run BEFORE CREATE INDEX "
        "idx_idempotency_key (the index references the column)"
    )
    # And the UPDATE meta MUST land before the index creation too (the ladder
    # bumps the version, then creates indexes).
    update_idx = next(
        i for i, s in enumerate(executed) if "UPDATE meta SET value = '2'" in s
    )
    assert update_idx < create_index_idx
    assert "idx_idempotency_key" in joined


def test_postgres_ensure_schema_v2_db_skips_migration_ladder():
    """NEGATIVE control: when the meta row already reports v2, _ensure_schema
    MUST NOT issue any ADD COLUMN ALTER (the ladder is gated on existing==1).
    Without this gate a warm DB would re-run the migration on every connect."""
    from atomic_agents.logs.postgres import PostgresLogBackend, _SCHEMA_VERSION

    backend = PostgresLogBackend.__new__(PostgresLogBackend)
    executed: list[str] = []

    def _execute(sql, params=None):
        executed.append(sql)
        cur = MagicMock()
        if "SELECT value FROM meta" in sql:
            cur.fetchone.return_value = {"value": str(_SCHEMA_VERSION)}
        else:
            cur.fetchone.return_value = None
        return cur

    conn = MagicMock()
    conn.execute.side_effect = _execute

    backend._ensure_schema(conn)

    assert not any("ADD COLUMN" in s for s in executed), (
        "a v2 DB MUST NOT re-run the ADD COLUMN migration ladder"
    )
    # Index creation still runs (idempotent CREATE INDEX IF NOT EXISTS).
    assert any("idx_idempotency_key" in s for s in executed)


def test_aggregate_injection_guard_raises_on_bad_identifier():
    """The SQL injection guard in aggregate() must reject malicious group_by
    field names before any SQL is executed. Mirrors test_aggregate_group_by_
    invalid_identifier_raises in test_log_sqlite_backend.py.
    """
    from atomic_agents.logs.postgres import PostgresLogBackend
    from atomic_agents.logs import LogAggregate, LogQuery

    backend = PostgresLogBackend.__new__(PostgresLogBackend)
    backend._host = "localhost"
    backend._port = 5432
    backend._dbname = "test"
    backend._user = "u"
    backend._password = "p"
    backend._safe_url = "postgresql://u:***@localhost/test"
    backend._tls = threading.local()

    # This mock ensures no real DB call happens — the guard must fire first.
    with pytest.raises(ValueError, match="not a valid identifier"):
        backend.aggregate(
            LogQuery(),
            LogAggregate(
                group_by=("'; DROP TABLE run_records; --",),
                metric="count",
            ),
        )


def test_aggregate_extra_field_uses_jsonb_operator():
    """aggregate() must use JSONB ->> operator for extra-field group_by,
    NOT SQLite's json_extract(extra, '$.field') syntax.
    """
    from atomic_agents.logs.postgres import PostgresLogBackend
    from atomic_agents.logs import LogAggregate, LogQuery

    backend = PostgresLogBackend.__new__(PostgresLogBackend)
    backend._host = "localhost"
    backend._port = 5432
    backend._dbname = "test"
    backend._user = "u"
    backend._password = "p"
    backend._safe_url = "postgresql://u:***@localhost/test"
    backend._tls = threading.local()

    # We need a mock connection to capture the SQL that would be executed.
    mock_conn = MagicMock()
    # Set psycopg connection-health attributes to "open" so _get_conn's
    # dead-connection guard does not discard the mock and try to reconnect.
    mock_conn.closed = 0
    mock_conn.broken = False
    mock_cursor = MagicMock()
    mock_cursor.fetchall.return_value = []
    mock_conn.execute.return_value = mock_cursor
    backend._tls.conn = mock_conn

    backend.aggregate(
        LogQuery(),
        LogAggregate(group_by=("iteration",), metric="count"),
    )

    # Verify the SQL uses ->>' not json_extract
    called_sql = mock_conn.execute.call_args[0][0]
    assert "->>" in called_sql
    assert "json_extract" not in called_sql


def test_delete_older_than_rejects_naive_datetime():
    """delete_older_than must raise ValueError on naive datetimes (spec/22 MUST 5).
    This guard must fire before any DB interaction.
    """
    from atomic_agents.logs.postgres import PostgresLogBackend

    backend = PostgresLogBackend.__new__(PostgresLogBackend)
    backend._host = "localhost"
    backend._port = 5432
    backend._dbname = "test"
    backend._user = "u"
    backend._password = "p"
    backend._safe_url = "postgresql://u:***@localhost/test"
    backend._tls = threading.local()

    with pytest.raises(ValueError, match="tz-aware"):
        backend.delete_older_than(datetime(2026, 5, 15, 12))  # naive


# ─────────────────────────────────────────────────────────────────
# NON-CONFORMANCE: Registry and lazy-import wiring


def test_postgres_not_imported_at_module_load():
    """Importing atomic_agents.logs must NOT import psycopg.
    The postgres backend must be lazy — only loaded when selected.

    This pins the 'no optional-dep at startup' guarantee: an operator
    who hasn't installed psycopg[binary] must not get an ImportError
    just by importing the framework.
    """
    # Simulate psycopg not being installed.
    original = sys.modules.get("psycopg")
    sys.modules["psycopg"] = None  # type: ignore[assignment]
    try:
        # Re-importing the logs package must not raise ImportError.
        import importlib
        import atomic_agents.logs as logs_mod

        importlib.reload(logs_mod)
        # No ImportError means the lazy-import contract holds.
    except ImportError as exc:
        pytest.fail(
            f"Importing atomic_agents.logs raised ImportError when psycopg "
            f"is not installed: {exc}. The backend must be lazily imported."
        )
    finally:
        if original is None:
            sys.modules.pop("psycopg", None)
        else:
            sys.modules["psycopg"] = original


def test_get_default_log_backend_postgres_no_url_raises(monkeypatch):
    """ATOMIC_AGENTS_LOG_BACKEND=postgres without ATOMIC_AGENTS_LOG_BACKEND_URL
    must raise ValueError with a clear message, NOT a cryptic psycopg error.
    """
    from atomic_agents.logs import get_default_log_backend

    monkeypatch.setenv("ATOMIC_AGENTS_LOG_BACKEND", "postgres")
    monkeypatch.delenv("ATOMIC_AGENTS_LOG_BACKEND_URL", raising=False)

    with pytest.raises(ValueError, match="ATOMIC_AGENTS_LOG_BACKEND_URL"):
        get_default_log_backend(Path("/tmp/test_scope"))


def test_get_default_log_backend_typo_includes_postgres_in_known_ids(monkeypatch):
    """A typo like 'postgre' must show 'postgres' in the Available list.
    The lazy backend must appear in error messages even before first use.
    Mirrors the Redis arc's Step-11-adversarial-P0-3 fix.
    """
    from atomic_agents.exceptions import BackendNotRegistered
    from atomic_agents.logs import get_default_log_backend

    monkeypatch.setenv("ATOMIC_AGENTS_LOG_BACKEND", "postgre")
    monkeypatch.delenv("ATOMIC_AGENTS_LOG_BACKEND_URL", raising=False)

    with pytest.raises(BackendNotRegistered) as exc_info:
        get_default_log_backend(Path("/tmp/test_scope"))

    assert "postgres" in str(exc_info.value), (
        "Error message must include 'postgres' in the Available list "
        "even before the backend is lazily registered."
    )


def test_get_log_backend_typo_includes_postgres_in_known_ids():
    """get_log_backend() with a typo must include 'postgres' in error message."""
    from atomic_agents.exceptions import BackendNotRegistered
    from atomic_agents.logs import get_log_backend

    with pytest.raises(BackendNotRegistered) as exc_info:
        get_log_backend("postgre")

    assert "postgres" in str(exc_info.value)


def test_doctor_check_log_backend_postgres_known_id(monkeypatch, tmp_path):
    """doctor.check_log_backend must recognise 'postgres' as a known id,
    not fail with 'not a known backend' before attempting construction.
    Mirrors test_doctor_log_backend_sqlite_forward_pointer pattern.
    """
    from atomic_agents.doctor import check_log_backend

    monkeypatch.setenv("ATOMIC_AGENTS_LOG_BACKEND", "postgres")
    monkeypatch.setenv(
        "ATOMIC_AGENTS_LOG_BACKEND_URL",
        "postgresql://user:pass@localhost:5432/db",
    )

    result = check_log_backend(tmp_path)
    # Must NOT fail with "not a known backend" — it may fail on connection
    # (backend unreachable) but must attempt construction, not reject id.
    assert "not a known backend" not in result.message
    assert "not a known backend" not in (result.fix_hint or "")


# ─────────────────────────────────────────────────────────────────
# NON-CONFORMANCE: _row_to_record type handling


def test_row_to_record_handles_jsonb_dict():
    """_row_to_record must handle JSONB column returning a Python dict directly
    (psycopg 3 auto-deserializes JSONB). Must NOT call json.loads on a dict.
    """
    from atomic_agents.logs.postgres import PostgresLogBackend

    backend = PostgresLogBackend.__new__(PostgresLogBackend)
    backend._host = "localhost"
    backend._port = 5432
    backend._dbname = "test"
    backend._user = "u"
    backend._password = "p"
    backend._safe_url = "postgresql://u:***@localhost/test"
    backend._tls = threading.local()

    row = {
        "ts": _ts(2026, 5, 15, 10),
        "run_id": "r1",
        "primitive": "agent_call",
        "status": "ok",
        "summary": "test",
        "model": "m",
        "input_tokens": 10,
        "output_tokens": 5,
        "cost_usd": None,
        "cost_source": None,
        "latency_ms": None,
        "cache_hit_tokens": None,
        "cache_miss_tokens": None,
        "mandate_id": None,
        "parent_run_id": None,
        "parent_agent": None,
        "trigger": None,
        "agent_name": None,
        "fallback": None,
        "critical": None,
        # spec/45 PR2: post-migration these columns ALWAYS exist (NULL on old rows).
        "idempotency_key": None,
        "replayed_run_id": None,
        # JSONB returns a Python dict directly — must NOT json.loads this.
        "extra": {"iteration": 3, "nested": [1, 2]},
    }

    record = backend._row_to_record(row)
    assert record.extra == {"iteration": 3, "nested": [1, 2]}


def test_row_to_record_fallback_none_preserved():
    """fallback=None and critical=None must round-trip as None, not False.
    bool(None) = False would silently corrupt unset boolean fields.
    """
    from atomic_agents.logs.postgres import PostgresLogBackend

    backend = PostgresLogBackend.__new__(PostgresLogBackend)
    backend._host = "localhost"
    backend._port = 5432
    backend._dbname = "test"
    backend._user = "u"
    backend._password = "p"
    backend._safe_url = "postgresql://u:***@localhost/test"
    backend._tls = threading.local()

    row = {
        "ts": _ts(2026, 5, 15),
        "run_id": "r1",
        "primitive": "agent_call",
        "status": "ok",
        "summary": "t",
        "model": "m",
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": None,
        "cost_source": None,
        "latency_ms": None,
        "cache_hit_tokens": None,
        "cache_miss_tokens": None,
        "mandate_id": None,
        "parent_run_id": None,
        "parent_agent": None,
        "trigger": None,
        "agent_name": None,
        "fallback": None,  # Must stay None, not become False
        "critical": None,  # Must stay None, not become False
        # spec/45 PR2: post-migration these columns ALWAYS exist (NULL on old rows).
        "idempotency_key": None,
        "replayed_run_id": None,
        "extra": {},
    }

    record = backend._row_to_record(row)
    assert record.fallback is None, "fallback=None must round-trip as None, not False"
    assert record.critical is None, "critical=None must round-trip as None, not False"


def test_row_to_record_boolean_true_false():
    """BOOLEAN True/False from Postgres must round-trip correctly."""
    from atomic_agents.logs.postgres import PostgresLogBackend

    backend = PostgresLogBackend.__new__(PostgresLogBackend)
    backend._host = "localhost"
    backend._port = 5432
    backend._dbname = "test"
    backend._user = "u"
    backend._password = "p"
    backend._safe_url = "postgresql://u:***@localhost/test"
    backend._tls = threading.local()

    row = {
        "ts": _ts(2026, 5, 15),
        "run_id": "r1",
        "primitive": "agent_call",
        "status": "ok",
        "summary": "t",
        "model": "m",
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": None,
        "cost_source": None,
        "latency_ms": None,
        "cache_hit_tokens": None,
        "cache_miss_tokens": None,
        "mandate_id": None,
        "parent_run_id": None,
        "parent_agent": None,
        "trigger": None,
        "agent_name": None,
        "fallback": True,
        "critical": False,
        # spec/45 PR2: post-migration these columns ALWAYS exist (NULL on old rows).
        "idempotency_key": None,
        "replayed_run_id": None,
        "extra": {},
    }

    record = backend._row_to_record(row)
    assert record.fallback is True
    assert record.critical is False


# ─────────────────────────────────────────────────────────────────
# CONFORMANCE: Real Postgres tests (require ATOMIC_AGENTS_TEST_POSTGRES_URL)
# These run in CI against the postgres:16-alpine service container.


@pytest.fixture
def pg_backend():
    """Yield a PostgresLogBackend for real-Postgres tests; close on teardown.

    close() is idempotent and always runs via finally, releasing the
    server-side Postgres backend process promptly after each test.  This
    mirrors the close-on-teardown fix applied to the conformance suite's
    ``backend`` fixture, and dogfoods the spec/22 MUST 7 contract that this
    test file is responsible for demonstrating.

    Skipped automatically when ATOMIC_AGENTS_TEST_POSTGRES_URL is not set
    (requires_postgres guard on each consuming test handles the skipif).
    """
    if not _POSTGRES_AVAILABLE:
        pytest.skip(
            "Requires ATOMIC_AGENTS_TEST_POSTGRES_URL env var and psycopg installed."
        )
    from atomic_agents.logs.postgres import PostgresLogBackend

    backend = PostgresLogBackend(_POSTGRES_URL)
    try:
        yield backend
    finally:
        backend.close()


@requires_postgres
def test_postgres_backend_id(pg_backend):
    assert pg_backend.backend_id == "postgres"


@requires_postgres
def test_postgres_schema_created_on_first_append(pg_backend):
    """Schema (run_records + meta + indexes) must be created on first use."""
    import psycopg

    pg_backend.append(_make_record(run_id="schema_test"))

    # Verify tables exist via a direct connection.
    conn = psycopg.connect(_POSTGRES_URL, autocommit=True)
    cur = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name IN ('run_records', 'meta')"
    )
    tables = {row[0] for row in cur.fetchall()}
    conn.close()
    assert "run_records" in tables
    assert "meta" in tables


@requires_postgres
def test_postgres_schema_version_recorded(pg_backend):
    """schema_version=1 must be written to the meta table on first init."""
    import psycopg

    pg_backend.append(_make_record(run_id="version_test"))

    conn = psycopg.connect(_POSTGRES_URL, autocommit=True)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    conn.close()
    assert row is not None
    assert int(row[0]) == 1


@requires_postgres
def test_postgres_indexes_created(pg_backend):
    """Six B-tree indexes matching the SQLite reference set must be created."""
    import psycopg

    pg_backend.append(_make_record(run_id="index_test"))

    conn = psycopg.connect(_POSTGRES_URL, autocommit=True)
    rows = conn.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename = 'run_records'"
    ).fetchall()
    conn.close()

    index_names = {row[0] for row in rows}
    assert "idx_ts" in index_names
    assert "idx_run_id" in index_names
    assert "idx_primitive" in index_names
    assert "idx_parent_run_id" in index_names
    assert "idx_cost_source" in index_names
    assert "idx_mandate_id" in index_names


@requires_postgres
def test_postgres_append_persist_before_returning(pg_backend):
    """spec/22 MUST 2: record must be visible in a new connection immediately
    after append() returns (commit semantics, not deferred flush).
    """
    import psycopg

    run_id = "persist_test_" + datetime.now(timezone.utc).isoformat()
    pg_backend.append(_make_record(run_id=run_id))

    # Check from a DIFFERENT connection — proves the commit landed.
    conn = psycopg.connect(_POSTGRES_URL, autocommit=True)
    row = conn.execute(
        "SELECT run_id FROM run_records WHERE run_id = %s", (run_id,)
    ).fetchone()
    conn.close()
    assert row is not None, (
        "Record not found in a fresh connection immediately after append(). "
        "Likely missing conn.commit() in append() — violates spec/22 MUST 2."
    )


@requires_postgres
def test_postgres_round_trip_all_fields(pg_backend):
    """Full RunRecord round-trip: append then query back, verify every field."""
    from atomic_agents.logs import LogQuery, RunRecord

    run_id = "roundtrip_" + datetime.now(timezone.utc).isoformat()
    rec = RunRecord(
        ts=_ts(2026, 5, 15, 10),
        run_id=run_id,
        primitive="agent_call",
        status="ok",
        summary="round-trip",
        model="claude-opus-4-7",
        input_tokens=100,
        output_tokens=50,
        cost_usd=0.001,
        cost_source="actor",
        latency_ms=1234.5,
        cache_hit_tokens=20,
        cache_miss_tokens=80,
        mandate_id="m-1",
        parent_run_id="parent",
        parent_agent="boss",
        trigger="agent_call",
        agent_name="alice",
        fallback=True,
        critical=False,
        extra={"iteration": 3, "tool_calls": [{"tool_name": "search"}]},
    )
    pg_backend.append(rec)
    out = pg_backend.query(LogQuery(run_id=run_id))
    assert len(out) == 1
    got = out[0]
    assert got.run_id == run_id
    assert got.cost_source == "actor"
    assert got.mandate_id == "m-1"
    assert got.parent_run_id == "parent"
    assert got.fallback is True
    assert got.critical is False
    assert got.extra["iteration"] == 3
    assert got.extra["tool_calls"][0]["tool_name"] == "search"


@requires_postgres
def test_postgres_fallback_none_round_trip(pg_backend):
    """fallback=None and critical=None must survive the Postgres BOOLEAN round-trip."""
    from atomic_agents.logs import LogQuery

    run_id = "bool_none_" + datetime.now(timezone.utc).isoformat()
    pg_backend.append(_make_record(run_id=run_id, fallback=None, critical=None))
    out = pg_backend.query(LogQuery(run_id=run_id))
    assert len(out) == 1
    assert out[0].fallback is None, (
        "fallback=None must not become False after Postgres round-trip"
    )
    assert out[0].critical is None, (
        "critical=None must not become False after Postgres round-trip"
    )


@requires_postgres
def test_postgres_delete_older_than_returns_correct_count(pg_backend):
    """delete_older_than must return the integer count of deleted rows, not -1.
    rowcount must be read BEFORE conn.commit() (psycopg3 may reset after commit).
    """
    prefix = "del_count_" + datetime.now(timezone.utc).isoformat()
    pg_backend.append(_make_record(run_id=f"{prefix}_jan", ts=_ts(2020, 1, 15)))
    pg_backend.append(_make_record(run_id=f"{prefix}_mar", ts=_ts(2020, 3, 15)))
    pg_backend.append(_make_record(run_id=f"{prefix}_may", ts=_ts(2020, 5, 15)))

    deleted = pg_backend.delete_older_than(datetime(2020, 4, 1, tzinfo=timezone.utc))
    assert deleted == 2, (
        f"delete_older_than returned {deleted!r} instead of 2. "
        "Likely rowcount was read after commit() — psycopg3 resets it."
    )


@requires_postgres
def test_postgres_same_ts_insertion_order_preserved(pg_backend):
    """spec/22 MUST 3: two appends with identical ts values must appear in
    insertion order from both query() and tail(). BIGSERIAL id tiebreaker.
    """
    from atomic_agents.logs import LogQuery

    same_ts = _ts(2026, 5, 15, 12)
    prefix = "order_" + datetime.now(timezone.utc).isoformat()
    pg_backend.append(_make_record(run_id=f"{prefix}_first", ts=same_ts))
    pg_backend.append(_make_record(run_id=f"{prefix}_second", ts=same_ts))

    out = pg_backend.query(LogQuery(run_id=None))
    # Find our two records in order.
    our = [r for r in out if r.run_id.startswith(prefix)]
    assert len(our) >= 2
    our_ids = [r.run_id for r in our if r.run_id.startswith(prefix)]
    # The first appended must appear before the second.
    idx_first = next(i for i, rid in enumerate(our_ids) if rid == f"{prefix}_first")
    idx_second = next(i for i, rid in enumerate(our_ids) if rid == f"{prefix}_second")
    assert idx_first < idx_second, (
        "Insertion order for same-ts records not preserved. "
        "BIGSERIAL tiebreaker in ORDER BY ts ASC, id ASC must fix this."
    )


@requires_postgres
def test_postgres_stats_size_bytes_is_none(pg_backend):
    """stats().size_bytes must always be None for Postgres.
    Postgres stores data remotely; there is no local file to stat.
    """
    pg_backend.append(_make_record())
    s = pg_backend.stats()
    assert s.size_bytes is None, (
        "PostgresLogBackend.stats().size_bytes must be None "
        "(no local file; spec/22 §LogStats allows None for remote backends)."
    )


@requires_postgres
def test_postgres_cold_start_concurrent_schema_init():
    """Concurrent _ensure_schema() calls from N threads must converge without
    deadlock or UNIQUE violation. Validates the pg_advisory_xact_lock + ON
    CONFLICT DO NOTHING cold-start safety mechanism.

    Each worker constructs its own backend (one per thread, as in production)
    and closes it on completion — mirroring the close-on-teardown contract.
    """
    from atomic_agents.logs.postgres import PostgresLogBackend

    errors = []

    def worker(i: int):
        b = PostgresLogBackend(_POSTGRES_URL)
        try:
            b.append(_make_record(run_id=f"concurrent_{i}"))
        except Exception as exc:
            errors.append(exc)
        finally:
            b.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, (
        f"Concurrent cold-start schema init raised exceptions: {errors}. "
        "pg_advisory_xact_lock + ON CONFLICT DO NOTHING must prevent this."
    )


@requires_postgres
def test_postgres_capabilities(pg_backend):
    """PostgresLogBackend must advertise the correct capabilities."""
    caps = pg_backend.capabilities()
    assert caps.supports_aggregation_pushdown is True
    assert caps.supports_streaming is False
    assert caps.supports_retention is True
    assert caps.durable is True


@requires_postgres
def test_postgres_aggregate_extra_field_via_jsonb(pg_backend):
    """JSONB ->> operator powers extra-field group_by in Postgres.
    Equivalent to SQLite's json_extract(extra, '$.field') test.
    """
    from atomic_agents.logs import LogAggregate, LogQuery

    prefix = "agg_extra_" + datetime.now(timezone.utc).isoformat()
    pg_backend.append(
        _make_record(
            run_id=f"{prefix}_r1",
            primitive="outcome_iteration",
            ts=_ts(2026, 5, 15, 10),
            extra={"iteration": 0},
        )
    )
    pg_backend.append(
        _make_record(
            run_id=f"{prefix}_r2",
            primitive="outcome_iteration",
            ts=_ts(2026, 5, 15, 11),
            extra={"iteration": 1},
        )
    )
    pg_backend.append(
        _make_record(
            run_id=f"{prefix}_r3",
            primitive="outcome_iteration",
            ts=_ts(2026, 5, 15, 12),
            extra={"iteration": 1},
        )
    )
    result = pg_backend.aggregate(
        LogQuery(run_id=None),
        LogAggregate(group_by=("iteration",), metric="count"),
    )
    # Postgres ->> always returns TEXT, so keys are ("0",) and ("1",).
    # Other records may exist in a shared DB, so assert >= rather than ==.
    assert ("0",) in result, f"expected ('0',) key in {result}"
    assert result[("1",)] >= 2, f"expected >=2 records for bucket ('1',), got {result}"
    assert result[("0",)] >= 1, f"expected >=1 record for bucket ('0',), got {result}"


@requires_postgres
def test_postgres_schema_version_mismatch_raises_runtime_error():
    """_ensure_schema must refuse to connect when meta.schema_version != _SCHEMA_VERSION.

    This pins the version-validation code path in _ensure_schema() against a
    real Postgres instance — the conformance suite (which truncates only
    run_records) exercises the warm validation read on every test, but this
    test explicitly pre-seeds a wrong version and asserts the RuntimeError,
    mirroring the equivalent SQLite backend test.

    The refusal path: _ensure_schema SELECTs schema_version from meta; if it
    doesn't match _SCHEMA_VERSION it raises RuntimeError with a migration hint.
    Without this test, the schema-mismatch refusal branch is dead code against
    real Postgres.

    Constructs the backend directly (not via pg_backend fixture) so we can
    control the exact construction + _get_conn() sequence that triggers the
    schema-mismatch refusal without a clean fixture interfering.  close() is
    called in a finally block to honour the teardown contract.
    """
    import psycopg
    from atomic_agents.logs.postgres import PostgresLogBackend, _SCHEMA_VERSION

    # Seed the meta table with a bogus future version so the validation trips.
    wrong_version = _SCHEMA_VERSION + 99
    conn = psycopg.connect(_POSTGRES_URL, autocommit=True)
    try:
        # Ensure meta table exists (backend may not have been constructed yet).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (str(wrong_version),),
        )
    finally:
        conn.close()

    backend = PostgresLogBackend(_POSTGRES_URL)
    try:
        with pytest.raises(RuntimeError, match="schema version mismatch"):
            # Construction itself is fine; schema validation runs on first use.
            backend._get_conn()
    finally:
        backend.close()

    # Restore correct version so subsequent tests are not broken.
    conn = psycopg.connect(_POSTGRES_URL, autocommit=True)
    try:
        conn.execute(
            "UPDATE meta SET value = %s WHERE key = 'schema_version'",
            (str(_SCHEMA_VERSION),),
        )
    finally:
        conn.close()


@requires_postgres
def test_postgres_cost_usd_double_precision_round_trip(pg_backend):
    """cost_usd and latency_ms use DOUBLE PRECISION (8-byte float), not REAL
    (4-byte float).  A cost value with >7 significant digits must survive the
    round-trip without single-precision truncation.

    Postgres REAL is float4 (~7 sig digits); DOUBLE PRECISION is float8
    (~15-16 sig digits, matching SQLite REAL).  This test catches any future
    DDL regression that silently reverts DOUBLE PRECISION to REAL.
    """
    from atomic_agents.logs import LogQuery

    # 0.00123456789 has 9 significant digits. Two related single-precision
    # error figures, both far above the 1e-12 tolerance below:
    #   * ~4.94e-11 — the pure float4 round-trip error in Python
    #     (struct.unpack('f', struct.pack('f', 0.00123456789))).
    #   * ~9e-11    — the OBSERVED end-to-end Postgres REAL round-trip error
    #     (binary REAL column -> text -> Python float8), measured against a live
    #     Postgres 16; ~2x the struct figure because the actual path it
    #     documents involves the REAL column representation, not just the cast.
    # The 1e-12 tolerance sits safely below BOTH, so the guard holds regardless
    # of which figure is "true" for the path. Double precision (float8 / DOUBLE
    # PRECISION) round-trips this value with ~1e-17 absolute error.
    cost = 0.00123456789
    latency = 9876543.21  # 9 sig digits; single precision rounds to 9876543.0
    run_id = "precision_" + datetime.now(timezone.utc).isoformat()

    pg_backend.append(_make_record(run_id=run_id, cost_usd=cost, latency_ms=latency))
    out = pg_backend.query(LogQuery(run_id=run_id))
    assert len(out) == 1
    got = out[0]
    # Tolerance 1e-12 sits BELOW the single-precision error (~4.94e-11 struct /
    # ~9e-11 end-to-end Postgres REAL) and far ABOVE the double-precision error
    # (~1e-17): this assertion FAILS if the column is REAL (float4) and PASSES
    # only if it is DOUBLE PRECISION (float8). A looser tolerance (e.g. 1e-10 >
    # 9e-11) would vacuously pass even on REAL, providing no guard for the cost
    # ledger column — the exact silent-truncation gap this test exists to prevent.
    assert abs(got.cost_usd - cost) < 1e-12, (
        f"cost_usd round-trip precision loss: stored {cost}, got {got.cost_usd}. "
        "DDL must use DOUBLE PRECISION, not REAL (which is single-precision in Postgres)."
    )
    assert abs(got.latency_ms - latency) < 1e-5, (
        f"latency_ms round-trip precision loss: stored {latency}, got {got.latency_ms}. "
        "DDL must use DOUBLE PRECISION, not REAL."
    )
