"""The background server must never send Charles an MFA passcode on its own.

Regression: on 2026-10-20 the token store held only `garmin_tokens.json` (written
by a newer garminconnect) while this server ran an older one. The resume failed,
`init_garmin_client` read it as "no tokens at all" and answered with a full
email/password login — one Garmin MFA passcode per tool call. Four retries, five
passcodes.

On garminconnect 0.3.x the roles flip: `garmin_tokens.json` is the current format
and `oauth1_token.json`/`oauth2_token.json` are the legacy one. The rule is the
same: a credential login happens only on a positively empty store. Tokens that are
present but unreadable, or present but rejected, surface as a retryable failure.
"""

from unittest.mock import MagicMock, patch

import pytest
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from garmin_connect_mcp.auth import GarminConfig
from garmin_connect_mcp.client import (
    GarminMFARequiredError,
    GarminTokenFormatError,
    _credential_login,
    _login_with_tokens,
    clear_cached_client,
    init_garmin_client,
)

CONFIG = GarminConfig(garmin_email="a@b.com", garmin_password="pw")


@pytest.fixture(autouse=True)
def _no_cached_client():
    clear_cached_client()
    yield
    clear_cached_client()


@pytest.fixture
def legacy_store(tmp_path):
    """Token dir holding only the garth-era oauth1/oauth2 files."""
    (tmp_path / "oauth1_token.json").write_text('{"oauth_token": "x"}')
    (tmp_path / "oauth2_token.json").write_text('{"access_token": "y"}')
    return tmp_path


@pytest.fixture
def current_store(tmp_path):
    """Token dir in the garminconnect 0.3.x format."""
    (tmp_path / "garmin_tokens.json").write_text(
        '{"di_token": "x", "di_refresh_token": "y", "di_client_id": "z"}'
    )
    return tmp_path


def _init_with_store(store, login_side_effect=None):
    """Run init_garmin_client against ``store`` with Garmin and _credential_login mocked."""
    with (
        patch("garmin_connect_mcp.client.get_token_store", return_value=str(store)),
        patch("garmin_connect_mcp.client.Garmin") as mock_garmin,
        patch("garmin_connect_mcp.client._credential_login") as mock_credential,
    ):
        if login_side_effect is not None:
            mock_garmin.return_value.login.side_effect = login_side_effect
        result = init_garmin_client(CONFIG)
        return result, mock_garmin, mock_credential


def test_legacy_only_store_raises_format_error(legacy_store):
    with patch("garmin_connect_mcp.client.Garmin") as mock_garmin:
        with pytest.raises(GarminTokenFormatError):
            _login_with_tokens(str(legacy_store))
        mock_garmin.assert_not_called()


def test_legacy_only_store_never_credential_logs_in(legacy_store):
    """The 2026-10-20 regression, mirrored: tokens present but unreadable => None."""
    result, _, mock_credential = _init_with_store(legacy_store)
    assert result is None
    mock_credential.assert_not_called()


def test_token_resume_uses_no_credentials(current_store):
    """The resume path builds Garmin() without email/password, so garminconnect
    cannot fall back to a credential login inside login()."""
    result, mock_garmin, mock_credential = _init_with_store(current_store)
    assert result is mock_garmin.return_value
    mock_garmin.assert_called_once_with()
    mock_garmin.return_value.login.assert_called_once_with(str(current_store))
    mock_credential.assert_not_called()


def test_rejected_tokens_never_credential_log_in(current_store):
    """Tokens present but rejected by Garmin: retryable None, never an MFA login."""
    result, _, mock_credential = _init_with_store(
        current_store, GarminConnectAuthenticationError("Authentication failed (401)")
    )
    assert result is None
    mock_credential.assert_not_called()


@pytest.mark.parametrize(
    "transient",
    [
        GarminConnectConnectionError("API Error 503"),
        GarminConnectTooManyRequestsError("Rate limit exceeded"),
    ],
)
def test_transient_errors_never_credential_log_in(current_store, transient):
    result, _, mock_credential = _init_with_store(current_store, transient)
    assert result is None
    mock_credential.assert_not_called()


def test_genuinely_empty_store_still_credential_logs_in(tmp_path):
    """The guard must not block a legitimate first-time login."""
    sentinel = object()

    with (
        patch("garmin_connect_mcp.client.get_token_store", return_value=str(tmp_path)),
        patch("garmin_connect_mcp.client._credential_login", return_value=sentinel) as mock_cred,
    ):
        assert init_garmin_client(CONFIG) is sentinel
        mock_cred.assert_called_once()


def test_credential_login_refuses_interactive_mfa(tmp_path):
    """A background server has no stdin: the MFA prompt must raise, not block on input()."""
    with patch("garmin_connect_mcp.client.Garmin") as mock_garmin:
        _credential_login(CONFIG, str(tmp_path))

        prompt_mfa = mock_garmin.call_args.kwargs["prompt_mfa"]
        with pytest.raises(GarminMFARequiredError):
            prompt_mfa()


def test_mfa_required_is_an_authentication_error():
    """garminconnect stops its strategy loop on GarminConnectAuthenticationError and
    retries the next strategy on anything else. One strategy = one passcode email,
    so the refusal must be an authentication error."""
    assert issubclass(GarminMFARequiredError, GarminConnectAuthenticationError)


def test_credential_login_persists_tokens_to_store(tmp_path):
    with patch("garmin_connect_mcp.client.Garmin") as mock_garmin:
        garmin = _credential_login(CONFIG, str(tmp_path))

    mock_garmin.return_value.login.assert_called_once_with(str(tmp_path))
    assert garmin is mock_garmin.return_value


def test_empty_store_mfa_demand_returns_none(tmp_path):
    """If Garmin demands MFA on a first-time login, the call fails cleanly."""
    with (
        patch("garmin_connect_mcp.client.get_token_store", return_value=str(tmp_path)),
        patch(
            "garmin_connect_mcp.client._credential_login",
            side_effect=GarminMFARequiredError("MFA"),
        ),
    ):
        assert init_garmin_client(CONFIG) is None


def test_cached_client_is_reused(current_store):
    with (
        patch("garmin_connect_mcp.client.get_token_store", return_value=str(current_store)),
        patch("garmin_connect_mcp.client.Garmin", return_value=MagicMock()) as mock_garmin,
    ):
        first = init_garmin_client(CONFIG)
        second = init_garmin_client(CONFIG)
    assert first is second
    assert mock_garmin.call_count == 1
