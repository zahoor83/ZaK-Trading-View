import ast
from pathlib import Path

import pytest
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import postgresql

import app.models
from app.database.base import Base
from app.security.permissions import AccountRole, Permission, is_allowed
from app.services.paper_fee_policy import PaperFeePolicy, PaperFeePolicyError
from app.models.paper_fee_policy import PaperFeePolicyHead, PaperFeePolicyVersion


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "versions" / "0008_paper_fee_policy.py"


def test_fee_policy_models_are_registered():
    assert PaperFeePolicyVersion.__tablename__ in Base.metadata.tables
    assert PaperFeePolicyHead.__tablename__ in Base.metadata.tables


def test_fee_policy_versions_have_account_market_version_uniqueness():
    table = PaperFeePolicyVersion.__table__
    constraints = {c.name for c in table.constraints}
    assert "uq_paper_fee_policy_versions_account_market_version" in constraints
    assert "uq_paper_fee_policy_versions_pointer_target" in constraints


def test_fee_policy_head_uses_composite_version_pointer():
    table = PaperFeePolicyHead.__table__
    foreign_keys = {fk.name for fk in table.foreign_key_constraints}
    assert "fk_paper_fee_policy_heads_current_version" in foreign_keys


def test_fee_policy_accepts_valid_exact_decimal_rates_below_one():
    policy = PaperFeePolicy(
        policy_id="policy-family",
        version=1,
        market_type="spot",
        entry_fee_rate=__import__("decimal").Decimal("0.125"),
        exit_fee_rate=__import__("decimal").Decimal("0.25"),
    )
    assert str(policy.entry_fee_rate) == "0.125"
    assert str(policy.exit_fee_rate) == "0.25"


@pytest.mark.parametrize("bad_rate", [True, 0.001, "NaN", "-0.01"])
def test_fee_policy_still_rejects_unsafe_rates(bad_rate):
    with pytest.raises(PaperFeePolicyError):
        PaperFeePolicy(
            policy_id="policy-family",
            version=1,
            market_type="spot",
            entry_fee_rate=bad_rate,
            exit_fee_rate=__import__("decimal").Decimal("0"),
        )


def test_fee_policy_manage_is_owner_admin_only():
    assert is_allowed(AccountRole.OWNER, Permission.FEE_POLICY_MANAGE)
    assert is_allowed(AccountRole.ADMIN, Permission.FEE_POLICY_MANAGE)
    assert not is_allowed(AccountRole.TRADER, Permission.FEE_POLICY_MANAGE)
    assert not is_allowed(AccountRole.VIEWER, Permission.FEE_POLICY_MANAGE)


def test_migration_chain_and_immutability_guards():
    source = MIGRATION.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    assignments = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    }
    assert assignments["revision"] == "0008_paper_fee_policy"
    assert assignments["down_revision"] == "0007_paper_trading_ledger"
    assert "BEFORE UPDATE OR DELETE" in source
    assert "BEFORE TRUNCATE" in source
    assert "Refusing fee-policy downgrade" in source


def test_models_do_not_use_fixed_numeric_typmods():
    dialect = postgresql.dialect()
    for model in (PaperFeePolicyVersion, PaperFeePolicyHead):
        ddl = str(CreateTable(model.__table__).compile(dialect=dialect))
        assert "NUMERIC(" not in ddl.upper()
