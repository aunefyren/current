"""Shared constants for the CURRENT tests."""

from __future__ import annotations

MOCK_EMAIL = "user@example.com"
MOCK_PASSWORD = "hunter2"

# Match the ids and tokens pinned by dev/make_fixtures.py.
MOCK_CUSTOMER_ID = 1001
MOCK_USER_ID = 2001
MOCK_CHARGE_POINT_ID = 3001
MOCK_BOX_ID = 4001
MOCK_ACCESS_TOKEN = "access-token"
MOCK_REFRESH_TOKEN = "refresh-token"
MOCK_REFRESHED_TOKEN = "refreshed-access-token"

# A second charger, built from the first by tests that need more than one.
SECOND_CHARGE_POINT_ID = 3002
SECOND_BOX_ID = 4002
