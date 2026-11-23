import pytest

from app.security.permissions import (
    AccountRole,
    Permission,
    PermissionDenied,
    ROLE_PERMISSIONS,
    is_allowed,
    require_permission,
)


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (
            AccountRole.OWNER,
            set(Permission),
        ),
        (
            AccountRole.ADMIN,
            set(Permission) - {
                Permission.MEMBERSHIP_MANAGE_ADMINS,
                Permission.OWNERSHIP_TRANSFER,
                Permission.ACCOUNT_DELETE,
            },
        ),
        (
            AccountRole.TRADER,
            {
                Permission.ACCOUNT_READ,
                Permission.PORTFOLIO_READ,
                Permission.PAPER_TRADE,
                Permission.POSITION_MANAGE,
                Permission.AUDIT_READ_OWN,
                Permission.AUDIT_READ_LIMITED,
            },
        ),
        (
            AccountRole.VIEWER,
            {
                Permission.ACCOUNT_READ,
                Permission.PORTFOLIO_READ,
                Permission.AUDIT_READ_LIMITED,
            },
        ),
    ],
)
def test_role_permission_matrix_is_exact(role, expected):
    assert ROLE_PERMISSIONS[role] == frozenset(expected)


@pytest.mark.parametrize("role", list(AccountRole))
@pytest.mark.parametrize("permission", list(Permission))
def test_permission_decisions_match_central_matrix(role, permission):
    expected = permission in ROLE_PERMISSIONS[role]
    assert is_allowed(role, permission) is expected


@pytest.mark.parametrize(
    "role",
    ["unknown", "Owner", "ADMINISTRATOR", "", None, 123, []],
)
def test_unknown_or_invalid_roles_fail_closed(role):
    assert is_allowed(role, Permission.ACCOUNT_READ) is False


@pytest.mark.parametrize(
    "permission",
    [
        "unknown.permission",
        "ACCOUNT_READ",
        "",
        None,
        123,
        [],
    ],
)
def test_unknown_or_invalid_permissions_fail_closed(permission):
    assert is_allowed(AccountRole.OWNER, permission) is False


def test_owner_can_manage_admins():
    assert is_allowed(
        AccountRole.OWNER,
        Permission.MEMBERSHIP_MANAGE_ADMINS,
    )


def test_admin_cannot_manage_admins():
    assert not is_allowed(
        AccountRole.ADMIN,
        Permission.MEMBERSHIP_MANAGE_ADMINS,
    )


def test_admin_cannot_transfer_ownership():
    assert not is_allowed(
        AccountRole.ADMIN,
        Permission.OWNERSHIP_TRANSFER,
    )


def test_admin_cannot_delete_account():
    assert not is_allowed(
        AccountRole.ADMIN,
        Permission.ACCOUNT_DELETE,
    )


def test_trader_cannot_manage_memberships():
    assert not is_allowed(
        AccountRole.TRADER,
        Permission.MEMBERSHIP_MANAGE_TRADER_VIEWER,
    )


def test_viewer_cannot_paper_trade():
    assert not is_allowed(
        AccountRole.VIEWER,
        Permission.PAPER_TRADE,
    )


@pytest.mark.parametrize("role", [AccountRole.TRADER, AccountRole.VIEWER])
def test_trader_and_viewer_cannot_manually_top_up(role):
    assert not is_allowed(role, Permission.MANUAL_TOP_UP)


@pytest.mark.parametrize("role", [AccountRole.OWNER, AccountRole.ADMIN])
def test_owner_and_admin_can_manually_top_up(role):
    assert is_allowed(role, Permission.MANUAL_TOP_UP)


def test_require_permission_accepts_allowed_action():
    assert require_permission(
        AccountRole.TRADER,
        Permission.PAPER_TRADE,
    ) is None


@pytest.mark.parametrize(
    ("role", "permission"),
    [
        (AccountRole.ADMIN, Permission.OWNERSHIP_TRANSFER),
        (AccountRole.TRADER, Permission.MEMBERSHIP_READ),
        (AccountRole.VIEWER, Permission.PAPER_TRADE),
        ("unknown", Permission.ACCOUNT_READ),
        (AccountRole.OWNER, "unknown.permission"),
    ],
)
def test_require_permission_rejects_denied_or_invalid_action(role, permission):
    with pytest.raises(PermissionDenied):
        require_permission(role, permission)


def test_permission_error_does_not_disclose_internal_details():
    with pytest.raises(PermissionDenied) as captured:
        require_permission(
            AccountRole.VIEWER,
            Permission.ACCOUNT_DELETE,
        )

    assert str(captured.value) == "The requested action is not permitted."


def test_role_permission_mapping_is_immutable():
    with pytest.raises(TypeError):
        ROLE_PERMISSIONS[AccountRole.VIEWER] = frozenset()


def test_individual_role_permission_sets_are_immutable():
    with pytest.raises(AttributeError):
        ROLE_PERMISSIONS[AccountRole.VIEWER].add(Permission.PAPER_TRADE)


def test_only_paper_trading_permission_exists():
    assert Permission.PAPER_TRADE.value == "paper_trade.execute"
    assert not any(
        "real_order" in permission.value
        or "exchange_order" in permission.value
        for permission in Permission
    )
