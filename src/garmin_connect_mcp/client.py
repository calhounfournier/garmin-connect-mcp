"""Garmin Connect API client wrapper with error handling."""

import json as _json
import sys
import threading
from pathlib import Path
from typing import Any

from garminconnect import Garmin, GarminConnectAuthenticationError
from garth.exc import GarthHTTPError

from .auth import GarminConfig, get_token_base64_path, get_token_store


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


class GarminAuthenticationError(GarminAPIError):
    """Exception raised when authentication fails (HTTP 401/403)."""

    def __init__(self, original_error: Exception | None = None):
        super().__init__(
            "Authentication failed. Please run 'garmin-connect-mcp-auth' to re-authenticate.",
            original_error=original_error,
        )


# Module-level cache: the authenticated client is built once and reused across
# every tool call. garth transparently refreshes the short-lived OAuth2 access
# token using the long-lived refresh token, so a cached client keeps working for
# days without re-login (and without ever triggering an MFA SMS).
_cached_client: Garmin | None = None
_client_lock = threading.Lock()


def clear_cached_client() -> None:
    """Drop the cached client so the next call re-authenticates from scratch."""
    global _cached_client
    with _client_lock:
        _cached_client = None


def _is_auth_failure(err: Exception) -> bool:
    """True only for genuine auth rejections (token truly dead), not transient errors.

    A 429 rate-limit or a network/5xx blip must NOT be treated as an auth failure —
    doing so is what cascaded into full credential logins and MFA-passcode spam.
    """
    s = str(err)
    return any(marker in s for marker in ("401", "403", "Unauthorized", "Forbidden"))


def _login_with_tokens(tokenstore: str) -> Garmin:
    """Resume a session from persisted OAuth tokens. Raises on failure."""
    token_path = Path(tokenstore)
    if not (token_path.exists() and any(token_path.iterdir())):
        raise FileNotFoundError("No tokens found")
    garmin = Garmin()
    garmin.login(tokenstore)
    return garmin


def _credential_login(config: GarminConfig, tokenstore: str) -> Garmin:
    """Full email/password login, used only when saved tokens are truly dead.

    MFA cannot be completed from a background MCP server (no stdin), so if Garmin
    demands an MFA code we fail with a clear instruction instead of silently
    triggering a passcode SMS and then blocking on input().
    """
    garmin = Garmin(config.garmin_email, config.garmin_password)
    result = garmin.login()

    if result and len(result) >= 2:
        oauth1_token, _ = result
        mfa_token = getattr(oauth1_token, "mfa_token", None)
        if mfa_token:
            raise GarminConnectAuthenticationError(
                "Garmin requires an MFA code, which cannot be entered from the "
                "background server. Run 'garmin-connect-mcp-auth' in a terminal to "
                "re-authenticate, then retry."
            )

    garmin.garth.dump(tokenstore)
    print(f"OAuth tokens saved to directory: {tokenstore}", file=sys.stderr)
    Path(get_token_base64_path()).write_text(garmin.garth.dumps())
    return garmin


def init_garmin_client(config: GarminConfig, force_refresh: bool = False) -> Garmin | None:
    """
    Return an authenticated Garmin client, reusing a cached session when possible.

    Strategy:
    1. Reuse the cached client unless force_refresh is set (garth auto-refreshes
       the access token via the refresh token, so no re-login/MFA is needed).
    2. Otherwise resume from persisted OAuth tokens on disk.
    3. Only fall back to a full credential login when the saved tokens are
       genuinely rejected (401/403). Transient errors (429/network/5xx) return
       None so the caller can surface a retryable error — they never trigger MFA.

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
        except FileNotFoundError as e:
            # No tokens at all → first-time credential login is legitimate.
            print(f"No saved tokens: {e}. Attempting credential login...", file=sys.stderr)
        except (GarthHTTPError, GarminConnectAuthenticationError) as e:
            if not _is_auth_failure(e):
                # Transient: the saved tokens are almost certainly still valid.
                # Do NOT re-auth (that's the MFA-storm path). Fail retryably.
                print(
                    f"Transient token-login error (not re-authing): {e}", file=sys.stderr
                )
                return None
            print(
                f"Saved tokens rejected ({e}). Attempting credential login...",
                file=sys.stderr,
            )

        # Genuine credential login: first run, or saved tokens truly dead.
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
        to the external garminconnect library, which doesn't have type stubs. The actual
        return type depends on which Garmin API method is called.

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
            return method(*args, **kwargs)
        except AttributeError as e:
            raise GarminAPIError(
                f"Method '{method_name}' not found on Garmin client", original_error=e
            ) from e
        except GarminConnectAuthenticationError as e:
            raise GarminAuthenticationError(original_error=e) from e
        except GarthHTTPError as e:
            # Parse HTTP status code from error
            error_str = str(e)
            if "429" in error_str or "Too Many Requests" in error_str:
                raise GarminRateLimitError(original_error=e) from e
            elif "404" in error_str or "Not Found" in error_str:
                raise GarminNotFoundError(original_error=e) from e
            elif "401" in error_str or "403" in error_str or "Unauthorized" in error_str:
                raise GarminAuthenticationError(original_error=e) from e
            else:
                raise GarminAPIError(f"Garmin API error: {str(e)}", original_error=e) from e
        except Exception as e:
            raise GarminAPIError(f"Unexpected error: {str(e)}", original_error=e) from e

    def schedule_workout(self, workout_id: int, date: str) -> Any:
        """
        Schedule a workout to a specific date via POST request.

        Scheduling puts it on the Garmin Connect calendar, which triggers
        auto-sync to the watch (workouts for the next 15 days sync automatically).
        """
        try:
            url = f"/workout-service/schedule/{workout_id}"
            payload = {"date": date}
            response = self.client.garth.post("connectapi", url, json=payload, api=True)
            try:
                return response.json()
            except Exception:
                return {"status": "ok", "workout_id": workout_id, "date": date}
        except GarthHTTPError as e:
            error_str = str(e)
            if "429" in error_str:
                raise GarminRateLimitError(original_error=e) from e
            elif "404" in error_str:
                raise GarminNotFoundError("Workout", original_error=e) from e
            elif "401" in error_str or "403" in error_str:
                raise GarminAuthenticationError(original_error=e) from e
            else:
                raise GarminAPIError(f"Garmin API error: {str(e)}", original_error=e) from e
        except Exception as e:
            raise GarminAPIError(f"Unexpected error: {str(e)}", original_error=e) from e

    def unschedule_workout(self, schedule_id: int) -> Any:
        """
        Remove a scheduled workout from the Garmin calendar via DELETE request.

        Args:
            schedule_id: The workoutScheduleId returned when the workout was scheduled.
        """
        try:
            url = f"/workout-service/schedule/{schedule_id}"
            response = self.client.garth.delete("connectapi", url, api=True)
            return {"status": "ok", "schedule_id": schedule_id}
        except GarthHTTPError as e:
            error_str = str(e)
            if "429" in error_str:
                raise GarminRateLimitError(original_error=e) from e
            elif "404" in error_str:
                raise GarminNotFoundError("Workout schedule", original_error=e) from e
            elif "401" in error_str or "403" in error_str:
                raise GarminAuthenticationError(original_error=e) from e
            else:
                raise GarminAPIError(f"Garmin API error: {str(e)}", original_error=e) from e
        except Exception as e:
            raise GarminAPIError(f"Unexpected error: {str(e)}", original_error=e) from e

    def delete_workout(self, workout_id: int) -> Any:
        """
        Permanently delete a workout from the Garmin library via DELETE request.

        Distinct from unschedule_workout: this removes the workout itself from the
        library, not merely a calendar instance. If the workout is currently
        scheduled, unschedule it first (the calendar entry references the workout).

        Args:
            workout_id: The library workout id (from get_workouts / the 'list' action).
        """
        try:
            url = f"/workout-service/workout/{workout_id}"
            self.client.garth.delete("connectapi", url, api=True)
            return {"status": "ok", "workout_id": workout_id}
        except GarthHTTPError as e:
            error_str = str(e)
            if "429" in error_str:
                raise GarminRateLimitError(original_error=e) from e
            elif "404" in error_str:
                raise GarminNotFoundError("Workout", original_error=e) from e
            elif "401" in error_str or "403" in error_str:
                raise GarminAuthenticationError(original_error=e) from e
            else:
                raise GarminAPIError(f"Garmin API error: {str(e)}", original_error=e) from e
        except Exception as e:
            raise GarminAPIError(f"Unexpected error: {str(e)}", original_error=e) from e

    def get_scheduled_workouts(self, start_date: str, end_date: str) -> list[dict]:
        """
        List workouts scheduled on the Garmin calendar within a date range.

        The garminconnect library doesn't expose the calendar service, so we call
        garth directly. The calendar-service month endpoint uses a 0-indexed month
        (0 = January) and returns ``calendarItems``; for workout items the ``id``
        field IS the workoutScheduleId needed to unschedule, and ``workoutId`` is
        the library workout id.

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
            if end < start:
                start, end = end, start

            start_iso, end_iso = start.isoformat(), end.isoformat()
            results: list[dict] = []
            # calendar-service month payloads include adjacent-month grid-overflow
            # days, so a date near a month boundary appears in two months' payloads.
            # Dedupe by scheduleId so each scheduled workout is returned once.
            seen_ids: set = set()
            year, month = start.year, start.month
            while (year, month) <= (end.year, end.month):
                # calendar-service month is 0-indexed (0 = January)
                url = f"/calendar-service/year/{year}/month/{month - 1}"
                response = self.client.garth.get("connectapi", url, api=True)
                data = response.json()
                for item in data.get("calendarItems", []) or []:
                    if item.get("itemType") != "workout":
                        continue
                    item_date = item.get("date")
                    schedule_id = item.get("id")
                    if item_date and start_iso <= item_date <= end_iso and schedule_id not in seen_ids:
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

            results.sort(key=lambda r: (r.get("date") or "", r.get("title") or ""))
            return results
        except GarthHTTPError as e:
            error_str = str(e)
            if "429" in error_str:
                raise GarminRateLimitError(original_error=e) from e
            elif "401" in error_str or "403" in error_str:
                raise GarminAuthenticationError(original_error=e) from e
            else:
                raise GarminAPIError(f"Garmin API error: {str(e)}", original_error=e) from e
        except (ValueError, TypeError) as e:
            raise GarminAPIError(f"Invalid date (expected YYYY-MM-DD): {str(e)}", original_error=e) from e
        except Exception as e:
            raise GarminAPIError(f"Unexpected error: {str(e)}", original_error=e) from e

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
        from datetime import date as _date, timedelta

        try:
            ty, tm, td = (int(p) for p in date.split("-"))
            target = _date(ty, tm, td)
        except (ValueError, TypeError) as e:
            raise GarminAPIError(f"Invalid date (expected YYYY-MM-DD): {str(e)}", original_error=e) from e

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
        Update an existing workout by ID via PUT request.

        The garminconnect library doesn't expose this, so we call garth directly.
        """
        try:
            if isinstance(workout_data, str):
                payload = _json.loads(workout_data)
            else:
                payload = workout_data

            url = f"/workout-service/workout/{workout_id}"
            response = self.client.garth.put("connectapi", url, json=payload, api=True)
            try:
                return response.json()
            except Exception:
                return {"status": "ok", "workout_id": workout_id}
        except GarthHTTPError as e:
            error_str = str(e)
            if "429" in error_str:
                raise GarminRateLimitError(original_error=e) from e
            elif "404" in error_str:
                raise GarminNotFoundError("Workout", original_error=e) from e
            elif "401" in error_str or "403" in error_str:
                raise GarminAuthenticationError(original_error=e) from e
            else:
                raise GarminAPIError(f"Garmin API error: {str(e)}", original_error=e) from e
        except _json.JSONDecodeError as e:
            raise GarminAPIError(f"Invalid workout JSON: {str(e)}", original_error=e) from e
        except Exception as e:
            raise GarminAPIError(f"Unexpected error: {str(e)}", original_error=e) from e
