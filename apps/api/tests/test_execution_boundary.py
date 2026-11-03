"""Standard-library tests for the signal-only execution boundary."""
import ast
import re
import unittest
from pathlib import Path

from app.security.execution_boundary import (
    ExecutionMode, UnsupportedExecutionMode, require_supported_execution_mode,
)

API_ROOT = Path(__file__).resolve().parents[1] / "app"
FORBIDDEN_PATTERNS = (
    re.compile(r"/api/v3/order", re.I),
    re.compile(r"/fapi/v1/order", re.I),
    re.compile(r"/dapi/v1/order", re.I),
    re.compile(r"\b(?:create_order|futures_create_order|cancel_order|new_order)\b", re.I),
    re.compile(r"\b(?:BINANCE_API_KEY|BINANCE_API_SECRET|BINANCE_SECRET_KEY)\b", re.I),
)


class ExecutionModePolicyTests(unittest.TestCase):
    def test_paper_mode_is_accepted(self):
        self.assertIs(require_supported_execution_mode("paper"), ExecutionMode.PAPER)

    def test_paper_mode_is_normalized(self):
        self.assertIs(require_supported_execution_mode(" PAPER "), ExecutionMode.PAPER)

    def test_enum_value_is_accepted(self):
        self.assertIs(require_supported_execution_mode(ExecutionMode.PAPER), ExecutionMode.PAPER)

    def test_real_and_unknown_modes_fail_closed(self):
        for value in ("real", "live", "exchange", "binance", "production", "unknown", ""):
            with self.subTest(value=value):
                with self.assertRaises(UnsupportedExecutionMode):
                    require_supported_execution_mode(value)

    def test_non_string_modes_fail_closed(self):
        for value in (None, 0, False, [], {}):
            with self.subTest(value=value):
                with self.assertRaises(UnsupportedExecutionMode):
                    require_supported_execution_mode(value)


class ApplicationSourceBoundaryTests(unittest.TestCase):
    def test_no_known_real_order_or_trading_credential_patterns(self):
        violations = []
        for source_file in API_ROOT.rglob("*.py"):
            if "__pycache__" in source_file.parts:
                continue
            source = source_file.read_text(encoding="utf-8-sig")
            for pattern in FORBIDDEN_PATTERNS:
                if pattern.search(source):
                    violations.append(f"{source_file}: {pattern.pattern}")
        self.assertEqual([], violations, "\n".join(violations))

    def test_application_python_files_parse(self):
        failures = []
        for source_file in API_ROOT.rglob("*.py"):
            if "__pycache__" in source_file.parts:
                continue
            try:
                ast.parse(source_file.read_text(encoding="utf-8-sig"), filename=str(source_file))
            except SyntaxError as exc:
                failures.append(f"{source_file}: {exc}")
        self.assertEqual([], failures, "\n".join(failures))


if __name__ == "__main__":
    unittest.main(verbosity=2)
