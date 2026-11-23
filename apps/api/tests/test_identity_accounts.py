from sqlalchemy import CheckConstraint, UniqueConstraint

from app.database.base import Base
import app.models  # noqa: F401


def test_identity_and_account_tables_are_registered():
    expected = {
        "users",
        "auth_identities",
        "user_sessions",
        "trading_accounts",
        "account_memberships",
        "account_invitations",
    }
    assert expected.issubset(set(Base.metadata.tables))


def test_oidc_identity_has_unique_issuer_subject():
    table = Base.metadata.tables["auth_identities"]
    constraints = [
        c for c in table.constraints
        if isinstance(c, UniqueConstraint)
    ]
    assert any(
        {column.name for column in c.columns} == {"issuer", "subject"}
        for c in constraints
    )


def test_membership_has_approved_role_constraint():
    table = Base.metadata.tables["account_memberships"]
    checks = [
        str(c.sqltext) for c in table.constraints
        if isinstance(c, CheckConstraint)
    ]
    assert any(
        all(role in check for role in ("owner", "admin", "trader", "viewer"))
        for check in checks
    )


def test_membership_has_unique_account_user_pair():
    table = Base.metadata.tables["account_memberships"]
    constraints = [
        c for c in table.constraints
        if isinstance(c, UniqueConstraint)
    ]
    assert any(
        {column.name for column in c.columns} == {"account_id", "user_id"}
        for c in constraints
    )


def test_invitations_cannot_grant_owner_or_admin():
    table = Base.metadata.tables["account_invitations"]
    checks = [
        str(c.sqltext) for c in table.constraints
        if isinstance(c, CheckConstraint)
    ]
    assert any(
        "trader" in check and "viewer" in check
        and "owner" not in check and "admin" not in check
        for check in checks
    )


def test_session_stores_hash_not_raw_token_field():
    table = Base.metadata.tables["user_sessions"]
    assert "token_hash" in table.columns
    assert "token" not in table.columns


def test_auth_identity_and_session_belong_to_user():
    for name in ("auth_identities", "user_sessions"):
        table = Base.metadata.tables[name]
        assert any(
            fk.target_fullname == "users.id"
            for column in table.columns
            for fk in column.foreign_keys
        )


def test_account_has_owner_user_reference():
    table = Base.metadata.tables["trading_accounts"]
    assert any(
        fk.target_fullname == "users.id"
        for fk in table.c.owner_user_id.foreign_keys
    )