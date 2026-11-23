import ast
from pathlib import Path

import pytest

from app.services import paper_futures_execution as service


@pytest.fixture(scope="module")
def execution_ast():
    source_path = Path(service.__file__).resolve()
    source = source_path.read_text(encoding="utf-8")
    module = ast.parse(source)

    function = next(
        node
        for node in module.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "execute_futures_trade_in_transaction"
    )

    return source, function


def call_lines(function, name):
    return [
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and (
            (
                isinstance(node.func, ast.Name)
                and node.func.id == name
            )
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == name
            )
        )
    ]


def branch_for(function, marker):
    for node in ast.walk(function):
        if not isinstance(node, ast.If):
            continue

        test_source = ast.unparse(node.test)

        if marker in test_source:
            return node

    pytest.fail(f"Required idempotency branch missing: {marker}")


def test_existing_request_lookup_precedes_decision_and_quote(execution_ast):
    source, function = execution_ast

    lookup_lines = [
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "existing_request"
            for target in node.targets
        )
    ]

    decision_lines = call_lines(function, "decide_idempotency")
    quote_lines = call_lines(function, "validate_with_context")

    assert lookup_lines, "Existing idempotency request lookup is missing."
    assert decision_lines, "Idempotency decision is missing."
    assert quote_lines, "Trusted quote adapter call is missing."

    lookup_line = min(lookup_lines)
    decision_line = min(decision_lines)
    quote_line = min(quote_lines)

    assert lookup_line < decision_line < quote_line


def test_conflicting_key_is_rejected_before_quote(execution_ast):
    _, function = execution_ast

    branch = branch_for(
        function,
        "IdempotencyDecisionKind.CONFLICT",
    )

    quote_lines = call_lines(function, "validate_with_context")
    assert quote_lines

    raises_conflict = any(
        isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == "PaperIdempotencyConflict"
        for node in ast.walk(branch)
    )

    assert raises_conflict
    assert branch.end_lineno < min(quote_lines)


def test_same_fingerprint_replay_returns_before_quote(execution_ast):
    _, function = execution_ast

    branch = branch_for(
        function,
        "IdempotencyDecisionKind.REPLAY",
    )

    quote_lines = call_lines(function, "validate_with_context")
    assert quote_lines

    returns_saved_response = any(
        isinstance(node, ast.Return)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "FuturesTradeExecutionResult"
        for node in ast.walk(branch)
    )

    assert returns_saved_response
    assert branch.end_lineno < min(quote_lines)


def test_quote_fetch_is_after_idempotency_replay_and_conflict_guards(
    execution_ast,
):
    _, function = execution_ast

    replay = branch_for(
        function,
        "IdempotencyDecisionKind.REPLAY",
    )
    conflict = branch_for(
        function,
        "IdempotencyDecisionKind.CONFLICT",
    )

    quote_lines = call_lines(function, "validate_with_context")
    assert quote_lines

    quote_line = min(quote_lines)

    assert conflict.end_lineno < quote_line
    assert replay.end_lineno < quote_line


def test_ledger_posting_occurs_after_quote_and_position_mutation(
    execution_ast,
):
    _, function = execution_ast

    quote_lines = call_lines(function, "validate_with_context")
    posting_lines = call_lines(
        function,
        "post_paper_trade_journal_in_transaction",
    )

    assert quote_lines, "Trusted quote retrieval is missing."
    assert posting_lines, "Ledger posting integration is missing."

    assert min(quote_lines) < min(posting_lines)