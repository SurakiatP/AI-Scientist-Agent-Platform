from pathlib import Path

import pytest

from scilab.db import (
    execute_in_lab,
    load_a2a_peer_credential,
    load_api_key_credential,
    load_membership,
)
from scilab.identity import Identity, credential_digest
from scilab.tenancy import AuthorizationError, require_lab, require_scope


class FakeCursor:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        self.calls.append(("cursor.enter",))
        return self

    def __exit__(self, *_):
        self.calls.append(("cursor.exit",))

    def execute(self, sql, params=()):
        self.calls.append(("execute", sql, params))

    def fetchall(self):
        return [("ok",)]


class FakeTransaction:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        self.calls.append(("transaction.enter",))
        return self

    def __exit__(self, *_):
        self.calls.append(("transaction.exit",))


class FakeConnection:
    def __init__(self):
        self.calls = []

    def transaction(self):
        self.calls.append(("transaction",))
        return FakeTransaction(self.calls)

    def cursor(self):
        self.calls.append(("cursor",))
        return FakeCursor(self.calls)


def test_scope_and_lab_guards_are_explicit():
    identity = Identity("lab-a", "user:alice", frozenset({"runs:read"}))
    require_scope(identity, "runs:read")
    require_lab(identity, "lab-a")

    with pytest.raises(AuthorizationError):
        require_scope(identity, "runs:write")
    with pytest.raises(AuthorizationError):
        require_scope(identity, "unknown:scope")
    with pytest.raises(AuthorizationError):
        require_lab(identity, "lab-b")


def test_cross_lab_denial_happens_before_any_database_call():
    connection = FakeConnection()
    identity = Identity("lab-a", "user:alice", frozenset({"runs:read"}))

    with pytest.raises(AuthorizationError):
        execute_in_lab(connection, identity, "lab-b", "SELECT 1")

    assert connection.calls == []


def test_same_lab_sets_parameterized_tenant_context_before_query():
    connection = FakeConnection()
    identity = Identity("lab-a", "user:alice", frozenset({"runs:read"}))

    result = execute_in_lab(
        connection,
        identity,
        "lab-a",
        "SELECT run_id FROM runs WHERE run_id = %s",
        ("run-1",),
    )

    assert result == [("ok",)]
    execute_calls = [call for call in connection.calls if call[0] == "execute"]
    assert execute_calls[0][1] == "SELECT set_config(%s, %s, true)"
    assert execute_calls[0][2] == ("scilab.current_lab_id", "lab-a")
    assert execute_calls[1][1].startswith("SELECT run_id")
    assert connection.calls.index(("transaction.enter",)) < connection.calls.index(execute_calls[0])
    assert connection.calls.index(execute_calls[0]) < connection.calls.index(execute_calls[1])


def test_migration_has_tenant_tables_hash_only_credentials_and_forced_rls():
    migration = Path("services/control-plane/migrations/001_identity_and_labs.sql").read_text()
    normalized = migration.lower()
    compact = " ".join(normalized.split())

    assert "alter table labs enable row level security" in compact
    assert "alter table labs force row level security" in compact
    assert "create policy labs_tenant on labs" in compact
    assert (
        "using (id = current_setting('scilab.current_lab_id', true))"
        in compact
    )
    assert (
        "with check (id = current_setting('scilab.current_lab_id', true))"
        in compact
    )

    for table in ("labs", "lab_memberships", "api_credentials", "a2a_peers"):
        assert f"create table if not exists {table}" in normalized
    assert "secret_hash" in normalized
    assert "secret text" not in normalized
    assert "secret varchar" not in normalized
    for table in ("lab_memberships", "api_credentials", "a2a_peers"):
        assert f"alter table {table} force row level security" in normalized
        assert f"create policy {table}_tenant" in normalized

    assert (
        "create policy lab_memberships_bootstrap on lab_memberships for select using"
        in compact
    )
    assert "subject = current_setting('scilab.bootstrap_subject', true)" in compact
    assert "lab_id = current_setting('scilab.bootstrap_lab_id', true)" in compact
    assert (
        "create policy api_credentials_bootstrap on api_credentials for select using"
        in compact
    )
    assert (
        "create policy a2a_peers_bootstrap on a2a_peers for select using" in compact
    )
    assert "secret_hash = decode(" in compact
    assert "current_setting('scilab.bootstrap_secret_digest', true)" in compact
    assert "'hex'" in compact
    assert "create policy lab_memberships_bootstrap on lab_memberships using" not in compact
    assert "create policy api_credentials_bootstrap on api_credentials using" not in compact
    assert "create policy a2a_peers_bootstrap on a2a_peers using" not in compact
    assert "create policy lab_memberships_bootstrap on lab_memberships for all" not in compact
    assert "create policy api_credentials_bootstrap on api_credentials for all" not in compact
    assert "create policy a2a_peers_bootstrap on a2a_peers for all" not in compact


class QueryCursor:
    def __init__(self, calls, row):
        self.calls = calls
        self.row = row

    def __enter__(self):
        self.calls.append(("cursor.enter",))
        return self

    def __exit__(self, *_):
        self.calls.append(("cursor.exit",))

    def execute(self, sql, params=()):
        self.calls.append(("execute", sql, params))

    def fetchone(self):
        self.calls.append(("fetchone",))
        return self.row


class QueryConnection:
    def __init__(self, row):
        self.calls = []
        self.row = row

    def transaction(self):
        self.calls.append(("transaction",))
        return FakeTransaction(self.calls)

    def cursor(self):
        self.calls.append(("cursor",))
        return QueryCursor(self.calls, self.row)


def test_load_membership_binds_subject_and_selected_lab_before_returning_record():
    row = {"subject": "alice-sub", "lab_id": "lab-a", "role": "researcher"}
    connection = QueryConnection(row)

    assert load_membership(connection, "alice-sub", "lab-a") == row
    execute_calls = [call for call in connection.calls if call[0] == "execute"]
    assert connection.calls.index(("transaction.enter",)) < connection.calls.index(
        execute_calls[0]
    ) < connection.calls.index(execute_calls[-1]) < connection.calls.index(
        ("transaction.exit",)
    )
    assert execute_calls[0][1] == "SELECT set_config(%s, %s, true)"
    assert execute_calls[0][2] == (
        "scilab.bootstrap_subject",
        "alice-sub",
    )
    assert execute_calls[1][1] == "SELECT set_config(%s, %s, true)"
    assert execute_calls[1][2] == (
        "scilab.bootstrap_lab_id",
        "lab-a",
    )
    _, sql, params = execute_calls[2]
    assert "subject = %s" in sql
    assert "lab_id = %s" in sql
    assert params == ("alice-sub", "lab-a")


@pytest.mark.parametrize(
    "loader",
    [load_api_key_credential, load_a2a_peer_credential],
)
def test_credential_loaders_hash_secret_before_parameterized_sql(loader):
    secret = "super-secret"
    row = {"lab_id": "lab-a", "scopes": ["runs:read"]}
    connection = QueryConnection(row)

    assert loader(connection, secret) == row
    execute_calls = [call for call in connection.calls if call[0] == "execute"]
    _, sql, params = execute_calls[-1]
    assert "%s" in sql
    assert params == (credential_digest(secret),)
    assert secret.encode() not in params
    assert execute_calls[0][1] == "SELECT set_config(%s, %s, true)"
    assert execute_calls[0][2] == (
        "scilab.bootstrap_secret_digest",
        credential_digest(secret).hex(),
    )


def test_bootstrap_transaction_exits_after_cursor_on_lookup_error():
    class FailingConnection(QueryConnection):
        def cursor(self):
            self.calls.append(("cursor",))
            return FailingQueryCursor(self.calls, self.row)

    class FailingQueryCursor(QueryCursor):
        def execute(self, sql, params=()):
            self.calls.append(("execute", sql, params))
            if sql.lstrip().startswith("SELECT subject"):
                raise RuntimeError("database failure")

    connection = FailingConnection(None)
    with pytest.raises(RuntimeError):
        load_membership(connection, "alice-sub", "lab-a")

    assert connection.calls[-2:] == [("cursor.exit",), ("transaction.exit",)]
