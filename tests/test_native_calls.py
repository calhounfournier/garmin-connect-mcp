"""The workout write paths go through garminconnect's own methods, and its
exceptions map onto this server's error types.

garminconnect 0.3.x raises GarminConnectConnectionError (with ``.response``) for
HTTP failures on write calls, GarminConnectNotFoundError for a 404,
GarminConnectTooManyRequestsError for a 429 on decorated reads, and
GarminConnectAuthenticationError for a 401.
"""

from unittest.mock import MagicMock

import pytest
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)

from garmin_connect_mcp.client import (
    GarminAPIError,
    GarminAuthenticationError,
    GarminClientWrapper,
    GarminNotFoundError,
    GarminRateLimitError,
)


def _http_error(status):
    err = GarminConnectConnectionError(f"API Error {status}")
    err.response = MagicMock(status_code=status)
    return err


def _wrapper():
    client = MagicMock()
    return GarminClientWrapper(client), client


def test_schedule_calls_native():
    wrapper, client = _wrapper()
    client.schedule_workout.return_value = {"workoutScheduleId": 1}
    assert wrapper.schedule_workout(11, "2026-06-02") == {"workoutScheduleId": 1}
    client.schedule_workout.assert_called_once_with(11, "2026-06-02")


def test_unschedule_calls_native():
    wrapper, client = _wrapper()
    assert wrapper.unschedule_workout(5001) == {"status": "ok", "schedule_id": 5001}
    client.unschedule_workout.assert_called_once_with(5001)


def test_delete_calls_native():
    wrapper, client = _wrapper()
    assert wrapper.delete_workout(42) == {"status": "ok", "workout_id": 42}
    client.delete_workout.assert_called_once_with(42)


def test_update_calls_native_with_parsed_json():
    wrapper, client = _wrapper()
    client.update_workout.return_value = {"workoutId": 42}
    wrapper.update_workout(42, '{"workoutName": "Legs"}')
    client.update_workout.assert_called_once_with(42, {"workoutName": "Legs"})


def test_update_invalid_json_raises_before_network():
    wrapper, client = _wrapper()
    with pytest.raises(GarminAPIError, match="Invalid workout JSON"):
        wrapper.update_workout(42, "{not json")
    client.update_workout.assert_not_called()


def test_get_workout_calls_native():
    wrapper, client = _wrapper()
    client.get_workout_by_id.return_value = {"workoutId": 42}
    assert wrapper.safe_call("get_workout_by_id", 42) == {"workoutId": 42}


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (_http_error(429), GarminRateLimitError),
        (GarminConnectTooManyRequestsError("Rate limit exceeded"), GarminRateLimitError),
        (_http_error(404), GarminNotFoundError),
        (GarminConnectNotFoundError("API Error 404"), GarminNotFoundError),
        (_http_error(401), GarminAuthenticationError),
        (_http_error(403), GarminAuthenticationError),
        (GarminConnectAuthenticationError("Authentication failed"), GarminAuthenticationError),
        (_http_error(500), GarminAPIError),
    ],
)
@pytest.mark.parametrize(
    "call",
    [
        lambda w: w.schedule_workout(11, "2026-06-02"),
        lambda w: w.unschedule_workout(5001),
        lambda w: w.delete_workout(42),
        lambda w: w.update_workout(42, {"workoutName": "Legs"}),
        lambda w: w.safe_call("get_workout_by_id", 42),
    ],
)
def test_error_mapping(raised, expected, call):
    wrapper, client = _wrapper()
    for name in (
        "schedule_workout",
        "unschedule_workout",
        "delete_workout",
        "update_workout",
        "get_workout_by_id",
    ):
        getattr(client, name).side_effect = raised
    with pytest.raises(expected):
        call(wrapper)


def test_get_scheduled_maps_rate_limit():
    wrapper, client = _wrapper()
    client.get_scheduled_workouts.side_effect = GarminConnectTooManyRequestsError("429")
    with pytest.raises(GarminRateLimitError):
        wrapper.get_scheduled_workouts("2026-06-01", "2026-06-07")


def test_safe_call_unknown_method_raises():
    wrapper = GarminClientWrapper(object())
    with pytest.raises(GarminAPIError, match="not found on Garmin client"):
        wrapper.safe_call("get_activity_social", 1)


def test_client_module_does_not_import_garth():
    import garmin_connect_mcp.client as client_module

    assert "garth" not in client_module.__dict__
    with open(client_module.__file__) as f:
        assert ".garth" not in f.read()
