"""Garmin Connect API client wrapper with error handling."""

import json as _json
import sys
import threading
from pathlib import Path
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)

from .auth import GarminConfig, get_token_store

# garminconnect 0.3.x keeps its whole session in this one file inside the token
# store. The garth-era layout (oauth1_token.json + oauth2_token.json) is legacy
# and unreadable by this version.
TOKEN_FILE = "garmin_tokens.json"


class GarminAPIError(Exception):
    """Custom exception for Garmin API errors."""

    def __init__(self, message: str, original_error: Exception | None = None):
        self.message = message
        self.original_error = original_error
        super().__init__(self.message)


class GarminRateLimitError(GarminAPIError):
    """Exception raised when rate limit is exceeded (HTTP 429)."""

    def __init__(self, original_error: Exception | None = None):
        super().__init__(
            "Rate limit exceeded. Please wait a few minutes before trying again.",
            original_error=original_error,
        )


class GarminNotFoundError(GarminAPIError):
    """Exception raised when resource is not found (HTTP 404)."""

    def __init__(self, resource: str = "Resource", original_error: Exception | None = None):
        super().__init__(
            f"{resource} not found. Please check the ID or date and try again.",
            original_error=original_error,
        )


class GarminTokenFormatError(GarminAPIError):
    """Token store is non-empty but holds no token file this garminconnect can read.

    Tokens exist, so a credential login is the wrong response: it would send
    Charles an MFA passcode on every tool call. Surfaced as retryable instead.
    """

    def __init__(self, tokenstore: str, original_error: Exception | None = None):
        super().__init__(
            f"Token store {tokenstore} has no {TOKEN_FILE} (legacy or unknown "
            "format). Not attempting a credential login. Re-authenticate with "
            "'garmin-connect-mcp-auth' in a terminal.",
            original_error=original_error,
        )


class GarminMFARequiredError(GarminConnectAuthenticationError):
    """Garmin asked for an MFA code during a credential login.

    Subclasses garminconnect's authentication error on purpose: its login loop
    stops on that type and moves to the next strategy on anything else, and every
    strategy that reaches MFA mails Charles another passcode.
    """


class GarminAuthenticationError(GarminAPIError):
    """Exception raised when authentication fails (HTTP 401/403)."""

    def __init__(self, original_error: Exception | None = None):
        super().__init__(
            "Authentication failed. Please run 'garmin-connect-mcp-auth' to re-authenticate.",
            original_error=original_error,
        )


# Module-level cache: the authenticated client is built once and reused across
# every tool call. garminconnect refreshes the short-lived DI access token with
# the refresh token and writes the rotated pair back to the token store, so a
# cached client keeps working without re-login (and without ever triggering MFA).
_cached_client: Garmin | None = None
_client_lock = threading.Lock()


def clear_cached_client() -> None:
    """Drop the cached client so the next call re-authenticates from scratch."""
    global _cached_client
    with _client_lock:
        _cached_client = None


def _status_code(err: Exception) -> int | None:
    """HTTP status carried by a garminconnect error, if any."""
    status = getattr(getattr(err, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _translate_error(err: Exception, resource: str = "Resource") -> GarminAPIError:
    """Map a garminconnect exception onto this server's error types."""
    if isinstance(err, GarminAPIError):
        return err
    status = _status_code(err)
    text = str(err)
    if isinstance(err, GarminConnectTooManyRequestsError) or status == 429:
        return GarminRateLimitError(original_error=err)
    if isinstance(err, GarminConnectNotFoundError) or status == 404:
        return GarminNotFoundError(resource, original_error=err)
    if isinstance(err, GarminConnectAuthenticationError) or status in (401, 403):
        return GarminAuthenticationError(original_error=err)
    if isinstance(err, GarminConnectConnectionError):
        return GarminAPIError(f"Garmin API error: {text}", original_error=err)
    return GarminAPIError(f"Unexpected error: {text}", original_error=err)


def _login_with_tokens(tokenstore: str) -> Garmin:
    """Resume a session from the persisted token file. Raises on failure.

    Garmin() is built without email/password, so garminconnect cannot fall back
    to a credential login inside login(): a missing or rejected token raises
    GarminConnectAuthenticationError instead of mailing an MFA passcode.
    """
    token_path = Path(tokenstore)
    if not (token_path.exists() and any(token_path.iterdir())):
        raise FileNotFoundError("No tokens found")
    if not (token_path / TOKEN_FILE).is_file():
        # Files exist, so tokens were provisioned, just not in a layout this
        # version reads. Distinct from "no tokens", which may credential-login.
        raise GarminTokenFormatError(tokenstore)
    garmin = Garmin()
    garmin.login(tokenstore)
    return garmin


def _refuse_mfa() -> str:
    raise GarminMFARequiredError(
        "Garmin requires an MFA code, which cannot be entered from the background "
        "server. Run 'garmin-connect-mcp-auth' in a terminal to re-authenticate, "
        "then retry."
    )


def _credential_login(config: GarminConfig, tokenstore: str) -> Garmin:
    """Full email/password login, used only on a positively empty token store.

    MFA cannot be completed from a background MCP server (no stdin), so the MFA
    prompt raises instead of blocking on input(). garminconnect writes the new
    tokens into the token store itself.
    """
    garmin = Garmin(config.garmin_email, config.garmin_password, prompt_mfa=_refuse_mfa)
    garmin.login(tokenstore)
    print(f"OAuth tokens saved to directory: {tokenstore}", file=sys.stderr)
    return garmin


def init_garmin_client(config: GarminConfig, force_refresh: bool = False) -> Garmin | None:
    """
    Return an authenticated Garmin client, reusing a cached session when possible.

    Strategy:
    1. Reuse the cached client unless force_refresh is set (garminconnect
       refreshes the access token itself, so no re-login/MFA is needed).
    2. Otherwise resume from the token file on disk.
    3. Credential login happens only when the token store is empty. Tokens that
       are unreadable or rejected, and transient errors (429/network/5xx), all
       return None so the caller surfaces a retryable error — none of them can
       trigger an MFA passcode.

    Args:
        config: Garmin configuration with credentials
        force_refresh: Rebuild the client even if one is cached

    Returns:
        Authenticated Garmin client, or None on transient/auth failure
    """
    global _cached_client

    with _client_lock:
        if _cached_client is not None and not force_refresh:
            return _cached_client

        tokenstore = get_token_store()

        # Resume from saved tokens (the common path).
        try:
            garmin = _login_with_tokens(tokenstore)
            print("Logged in using token data from directory.", file=sys.stderr)
            _cached_client = garmin
            return garmin
        except GarminTokenFormatError as e:
            # Tokens exist but this version can't read them. A credential login
            # here fires an MFA passcode on every single tool call.
            print(f"{e.message}", file=sys.stderr)
            return None
        except FileNotFoundError as e:
            # No tokens at all → first-time credential login is legitimate.
            print(f"No saved tokens: {e}. Attempting credential login...", file=sys.stderr)
        except GarminConnectAuthenticationError as e:
            # Tokens present but rejected. Re-authenticating means MFA, which
            # only a person at a terminal can complete.
            print(
                f"Saved tokens rejected ({e}). Not re-authenticating from the "
                "background server; run 'garmin-connect-mcp-auth' in a terminal.",
                file=sys.stderr,
            )
            return None
        except Exception as e:
            # Transient (429/network/5xx): the saved tokens are almost certainly
            # still valid. Do NOT re-auth (that's the MFA-storm path).
            print(f"Transient token-login error (not re-authing): {e}", file=sys.stderr)
            return None

        # Genuine credential login: first run on an empty store.
        try:
            garmin = _credential_login(config, tokenstore)
            _cached_client = garmin
            return garmin
        except GarminConnectAuthenticationError as err:
            print(f"Authentication error: {err}", file=sys.stderr)
            return None
        except Exception as err:
            print(f"Unexpected error during login: {err}", file=sys.stderr)
            import traceback

            traceback.print_exc(file=sys.stderr)
            return None


class GarminClientWrapper:
    """Wrapper around Garmin client for consistent error handling."""

    def __init__(self, client: Garmin):
        self.client = client

    def safe_call(self, method_name: str, *args, **kwargs) -> Any:
        """
        Safely call a Garmin client method with error handling.

        This method uses `Any` as the return type because it dynamically proxies calls
        to the external garminconnect library. The actual return type depends on which
        Garmin API method is called.

        Args:
            method_name: Name of the Garmin client method to call
            *args: Positional arguments for the method
            **kwargs: Keyword arguments for the method

        Returns:
            Method result or raises GarminAPIError

        Raises:
            GarminAuthenticationError: Authentication failed (401/403)
            GarminNotFoundError: Resource not found (404)
            GarminRateLimitError: Rate limit exceeded (429)
            GarminAPIError: Other API errors
        """
        try:
            method = getattr(self.client, method_name)
        except AttributeError as e:
            raise GarminAPIError(
                f"Method '{method_name}' not found on Garmin client", original_error=e
            ) from e
        try:
            return method(*args, **kwargs)
        except Exception as e:
            raise _translate_error(e) from e

    def schedule_workout(self, workout_id: int, date: str) -> Any:
        """
        Schedule a workout to a specific date.

        Scheduling puts it on the Garmin Connect calendar, which triggers
        auto-sync to the watch (workouts for the next 15 days sync automatically).
        """
        try:
            return self.client.schedule_workout(workout_id, date)
        except Exception as e:
            raise _translate_error(e, "Workout") from e

    def unschedule_workout(self, schedule_id: int) -> Any:
        """
        Remove a scheduled workout from the Garmin calendar.

        Args:
            schedule_id: The workoutScheduleId returned when the workout was scheduled.
        """
        try:
            self.client.unschedule_workout(schedule_id)
            return {"status": "ok", "schedule_id": schedule_id}
        except Exception as e:
            raise _translate_error(e, "Workout schedule") from e

    def delete_workout(self, workout_id: int) -> Any:
        """
        Permanently delete a workout from the Garmin library.

        Distinct from unschedule_workout: this removes the workout itself from the
        library, not merely a calendar instance. If the workout is currently
        scheduled, unschedule it first (the calendar entry references the workout).

        Args:
            workout_id: The library workout id (from get_workouts / the 'list' action).
        """
        try:
            self.client.delete_workout(workout_id)
            return {"status": "ok", "workout_id": workout_id}
        except Exception as e:
            raise _translate_error(e, "Workout") from e

    def get_scheduled_workouts(self, start_date: str, end_date: str) -> list[dict]:
        """
        List workouts scheduled on the Garmin calendar within a date range.

        garminconnect reads the calendar one month at a time (1-indexed month; it
        converts to the calendar service's 0-indexed month itself) and returns
        ``calendarItems``; for workout items the ``id`` field IS the
        workoutScheduleId needed to unschedule, and ``workoutId`` is the library
        workout id.

        Args:
            start_date: Range start, YYYY-MM-DD (inclusive).
            end_date: Range end, YYYY-MM-DD (inclusive).

        Returns:
            List of {scheduleId, workoutId, title, date} dicts sorted by date.
        """
        from datetime import date as _date

        try:
            sy, sm, sd = (int(p) for p in start_date.split("-"))
            ey, em, ed = (int(p) for p in end_date.split("-"))
            start = _date(sy, sm, sd)
            end = _date(ey, em, ed)
        except (ValueError, TypeError) as e:
            raise GarminAPIError(
                f"Invalid date (expected YYYY-MM-DD): {str(e)}", original_error=e
            ) from e
        if end < start:
            start, end = end, start

        start_iso, end_iso = start.isoformat(), end.isoformat()
        results: list[dict] = []
        # Month payloads include adjacent-month grid-overflow days, so a date near
        # a month boundary appears in two months' payloads. Dedupe by scheduleId
        # so each scheduled workout is returned once.
        seen_ids: set = set()
        year, month = start.year, start.month
        try:
            while (year, month) <= (end.year, end.month):
                data = self.client.get_scheduled_workouts(year, month) or {}
                for item in data.get("calendarItems", []) or []:
                    if item.get("itemType") != "workout":
                        continue
                    item_date = item.get("date")
                    schedule_id = item.get("id")
                    if (
                        item_date
                        and start_iso <= item_date <= end_iso
                        and schedule_id not in seen_ids
                    ):
                        seen_ids.add(schedule_id)
                        results.append(
                            {
                                "scheduleId": schedule_id,
                                "workoutId": item.get("workoutId"),
                                "title": item.get("title"),
                                "date": item_date,
                            }
                        )
                month = 1 if month == 12 else month + 1
                if month == 1:
                    year += 1
        except Exception as e:
            raise _translate_error(e, "Calendar month") from e

        results.sort(key=lambda r: (r.get("date") or "", r.get("title") or ""))
        return results

    def reschedule_workout(
        self,
        workout_id: int,
        date: str,
        search_start: str | None = None,
        search_end: str | None = None,
    ) -> dict:
        """
        Move a workout to a target date, removing any existing scheduled entries
        for the same workout first — so a reschedule can't leave a duplicate.

        Garmin's ``schedule`` action only ever ADDS a calendar entry; it never
        replaces one. Rescheduling naively therefore leaves the old entry behind.
        This method closes that gap: it lists scheduled workouts in a window
        around the target date, unschedules every entry whose ``workoutId``
        matches, then schedules the workout to ``date``. Net result: exactly one
        entry, on the right day.

        Args:
            workout_id: Library workout id to (re)schedule.
            date: Target date, YYYY-MM-DD.
            search_start: Optional window start (YYYY-MM-DD). Defaults to 14 days
                before the target date.
            search_end: Optional window end (YYYY-MM-DD). Defaults to 21 days
                after the target date.

        Returns:
            {workout_id, scheduled_date, new_schedule_id, removed_duplicates, schedule_result}
        """
        from datetime import date as _date
        from datetime import timedelta

        try:
            ty, tm, td = (int(p) for p in date.split("-"))
            target = _date(ty, tm, td)
        except (ValueError, TypeError) as e:
            raise GarminAPIError(
                f"Invalid date (expected YYYY-MM-DD): {str(e)}", original_error=e
            ) from e

        window_start = search_start or (target - timedelta(days=14)).isoformat()
        window_end = search_end or (target + timedelta(days=21)).isoformat()

        existing = self.get_scheduled_workouts(window_start, window_end)
        removed: list[dict] = []
        phantom: list[dict] = []
        for entry in existing:
            if entry.get("workoutId") == workout_id and entry.get("scheduleId") is not None:
                schedule_id = entry["scheduleId"]
                try:
                    self.unschedule_workout(schedule_id)
                    removed.append({"scheduleId": schedule_id, "date": entry.get("date")})
                except GarminNotFoundError:
                    # calendar-service can list an entry that workout-service has
                    # already dropped (eventual-consistency phantom after a delete).
                    # A 404 means the entry is already gone — the desired end state —
                    # so record it and carry on rather than aborting the reschedule.
                    phantom.append({"scheduleId": schedule_id, "date": entry.get("date")})

        result = self.schedule_workout(workout_id, date)
        new_schedule_id = None
        if isinstance(result, dict):
            new_schedule_id = result.get("workoutScheduleId") or result.get("id")

        return {
            "workout_id": workout_id,
            "scheduled_date": date,
            "new_schedule_id": new_schedule_id,
            "removed_duplicates": removed,
            "stale_phantom_entries": phantom,
            "schedule_result": result,
        }

    def update_workout(self, workout_id: int, workout_data: str | dict) -> Any:
        """
        Replace an existing workout in place, keeping its id.

        Garmin's workout endpoint replaces the whole workout, so ``workout_data``
        must be the complete structure (as returned by ``get_workout_by_id``).
        Calendar schedules pointing at the workout stay valid.
        """
        try:
            payload = _json.loads(workout_data) if isinstance(workout_data, str) else workout_data
        except _json.JSONDecodeError as e:
            raise GarminAPIError(f"Invalid workout JSON: {str(e)}", original_error=e) from e
        try:
            result = self.client.update_workout(workout_id, payload)
            return result or {"status": "ok", "workout_id": workout_id}
        except Exception as e:
            raise _translate_error(e, "Workout") from e
