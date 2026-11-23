import pytest

from app.security.paper_authorization import (
    PaperAuthorizationError,
    require_paper_permission,
)


@pytest.mark.parametrize(
    ("role", "operation"),
    [
        ("owner", "read"),
        ("owner", "trade"),
        ("owner", "manual_top_up"),
        ("owner", "reset"),
        ("admin", "read"),
        ("admin", "trade"),
        ("admin", "manual_top_up"),
        ("admin", "reset"),
        ("trader", "read"),
        ("trader", "trade"),
        ("viewer", "read"),
    ],
)
def test_allowed_role_operation_pairs(role, operation):
    assert require_paper_permission(role, operation) is None


@pytest.mark.parametrize(
    ("role", "operation"),
    [
        ("trader", "manual_top_up"),
        ("trader", "reset"),
        ("viewer", "trade"),
        ("viewer", "manual_top_up"),
        ("viewer", "reset"),
    ],
)
def test_restricted_role_operation_pairs_are_denied(role, operation):
    with pytest.raises(PaperAuthorizationError):
        require_paper_permission(role, operation)


@pytest.mark.parametrize(
    ("role", "operation"),
    [
        ("superadmin", "trade"),
        ("OWNER", "trade"),
        ("", "read"),
        ("owner", "delete"),
        ("owner", ""),
    ],
)
def test_unknown_roles_and_operations_are_denied(role, operation):
    with pytest.raises(PaperAuthorizationError):
        require_paper_permission(role, operation)


@pytest.mark.parametrize(
    ("role", "operation"),
    [
        (None, "trade"),
        ("owner", None),
        (123, "read"),
        ("viewer", True),
    ],
)
def test_non_string_inputs_are_denied(role, operation):
    with pytest.raises(PaperAuthorizationError):
        require_paper_permission(role, operation)
