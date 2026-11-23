"""Shared API test environment defaults."""

import os

# Tests run in an explicitly declared development environment by default.
# Individual tests may override or remove APP_ENV to verify fail-closed behavior.
os.environ.setdefault("APP_ENV", "development")