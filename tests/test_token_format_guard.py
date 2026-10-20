"""A token store this garth can't read must never trigger a credential login.

Regression: on 2026-10-20 the token store held only `garmin_tokens.json` (written
by a newer garminconnect). `Garmin.login()` raised FileNotFoundError for the
missing `oauth1_token.json`, which `init_garmin_client` read as "no tokens at
all" and answered with a full email/password login — one Garmin MFA passcode per
tool call. Four retries, five passcodes.
"""

from unittest.mock import patch

import pytest

from garmin_connect_mcp.auth import GarminConfig
from garmin_connect_mcp.client import (
    GarminTokenFormatError,
    _login_with_tokens,
    clear_cached_client,
    init_garmin_client,
)


@pytest.fixture(autouse=True)
def _no_cached_client():
    clear_cached_client()
    yield
    clear_cached_client()


@pytest.fixture
def unreadable_store(tmp_path):
    """Non-empty token dir in a layout the installed garth cannot load."""
    (tmp_path / "garmin_tokens.json").write_text('{"di_token": "x"}')
    return tmp_path


def test_unreadable_token_store_raises_format_error(unreadable_store):
    with patch("garmin_connect_mcp.client.Garmin") as mock_garmin:
        mock_garmin.return_value.login.side_effect = FileNotFoundError(
            f"{unreadable_store}/oauth1_token.json"
        )
        with pytest.raises(GarminTokenFormatError):
            _login_with_tokens(str(unreadable_store))


def test_unreadable_token_store_never_credential_logs_in(unreadable_store):
    """The regression itself: tokens present but unreadable => None, no login."""
    config = GarminConfig(garmin_email="a@b.com", garmin_password="pw")

    with (
        patch("garmin_connect_mcp.client.get_token_store", return_value=str(unreadable_store)),
        patch("garmin_connect_mcp.client.Garmin") as mock_garmin,
        patch("garmin_connect_mcp.client._credential_login") as mock_credential,
    ):
        mock_garmin.return_value.login.side_effect = FileNotFoundError(
            f"{unreadable_store}/oauth1_token.json"
        )

        assert init_garmin_client(config) is None
        mock_credential.assert_not_called()


def test_genuinely_empty_store_still_credential_logs_in(tmp_path):
    """The guard must not block a legitimate first-time login."""
    config = GarminConfig(garmin_email="a@b.com", garmin_password="pw")
    sentinel = object()

    with (
        patch("garmin_connect_mcp.client.get_token_store", return_value=str(tmp_path)),
        patch("garmin_connect_mcp.client._credential_login", return_value=sentinel) as mock_cred,
    ):
        assert init_garmin_client(config) is sentinel
        mock_cred.assert_called_once()
