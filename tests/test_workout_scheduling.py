"""Tests for calendar-aware workout scheduling on GarminClientWrapper.

Covers the calendar-service-backed ``get_scheduled_workouts`` (0-indexed month,
date-range filtering, workout-only, multi-month spanning) and the duplicate-proof
``reschedule_workout`` (remove existing matching entries, then schedule).
"""

from unittest.mock import MagicMock

import pytest

from garth.exc import GarthHTTPError
from requests import HTTPError

from garmin_connect_mcp.client import GarminAPIError, GarminClientWrapper


def _resp(payload):
    """A fake garth HTTP response whose .json() returns ``payload``."""
    r = MagicMock()
    r.json.return_value = payload
    return r


def _wrapper_with_months(month_payloads):
    """
    Build a wrapper whose garth.get returns a payload chosen by the month in the
    request URL. ``month_payloads`` maps a URL substring (e.g. "/month/5") to the
    calendar-service payload to return for that month.
    """
    client = MagicMock()

    def get_side(_connectapi, url, api=True):
        for key, payload in month_payloads.items():
            if key in url:
                return _resp(payload)
        return _resp({"calendarItems": []})

    client.garth.get.side_effect = get_side
    return GarminClientWrapper(client), client


def test_get_scheduled_filters_by_type_and_range():
    """Only workout items within the inclusive range are returned, with mapped fields."""
    payload = {
        "calendarItems": [
            {"itemType": "workout", "id": 111, "workoutId": 11, "title": "Push", "date": "2026-06-02"},
            {"itemType": "workout", "id": 222, "workoutId": 22, "title": "Legs", "date": "2026-06-05"},
            # out of range (before start)
            {"itemType": "workout", "id": 333, "workoutId": 33, "title": "Old", "date": "2026-05-30"},
            # wrong type — an activity, not a scheduled workout
            {"itemType": "activity", "id": 444, "workoutId": None, "title": "Swim", "date": "2026-06-03"},
        ]
    }
    wrapper, _ = _wrapper_with_months({"/month/5": payload})

    result = wrapper.get_scheduled_workouts("2026-06-01", "2026-06-07")

    assert result == [
        {"scheduleId": 111, "workoutId": 11, "title": "Push", "date": "2026-06-02"},
        {"scheduleId": 222, "workoutId": 22, "title": "Legs", "date": "2026-06-05"},
    ]


def test_get_scheduled_month_is_zero_indexed():
    """June (month 6) must hit the calendar-service path /month/5."""
    wrapper, client = _wrapper_with_months({"/month/5": {"calendarItems": []}})

    wrapper.get_scheduled_workouts("2026-06-01", "2026-06-07")

    called_urls = [call.args[1] for call in client.garth.get.call_args_list]
    assert called_urls == ["/calendar-service/year/2026/month/5"]


def test_get_scheduled_spans_multiple_months_sorted():
    """A range crossing a month boundary queries both months and merges sorted by date."""
    may = {"calendarItems": [
        {"itemType": "workout", "id": 1, "workoutId": 91, "title": "May end", "date": "2026-05-31"},
    ]}
    june = {"calendarItems": [
        {"itemType": "workout", "id": 2, "workoutId": 92, "title": "June start", "date": "2026-06-01"},
    ]}
    wrapper, client = _wrapper_with_months({"/month/4": may, "/month/5": june})

    result = wrapper.get_scheduled_workouts("2026-05-30", "2026-06-02")

    assert [r["date"] for r in result] == ["2026-05-31", "2026-06-01"]
    called_urls = sorted(call.args[1] for call in client.garth.get.call_args_list)
    assert called_urls == [
        "/calendar-service/year/2026/month/4",
        "/calendar-service/year/2026/month/5",
    ]


def test_get_scheduled_dedupes_grid_overflow():
    """calendar-service month payloads include adjacent-month grid-overflow days,
    so a boundary date appears in two months. The same scheduleId must be returned
    once, not twice."""
    # June 2 appears in BOTH the May (month/4) and June (month/5) payloads.
    may = {"calendarItems": [
        {"itemType": "workout", "id": 700, "workoutId": 70, "title": "May28", "date": "2026-05-28"},
        {"itemType": "workout", "id": 800, "workoutId": 80, "title": "Push", "date": "2026-06-02"},
    ]}
    june = {"calendarItems": [
        {"itemType": "workout", "id": 800, "workoutId": 80, "title": "Push", "date": "2026-06-02"},
    ]}
    wrapper, _ = _wrapper_with_months({"/month/4": may, "/month/5": june})

    result = wrapper.get_scheduled_workouts("2026-05-19", "2026-06-23")

    push_entries = [r for r in result if r["scheduleId"] == 800]
    assert len(push_entries) == 1, f"expected Push once, got {len(push_entries)}"
    assert [r["scheduleId"] for r in result] == [700, 800]


def test_reschedule_grid_overflow_no_double_unschedule():
    """A boundary-date duplicate from grid overflow must not cause the same entry to
    be unscheduled twice (which would 404 the second time and mislabel it phantom)."""
    may = {"calendarItems": [
        {"itemType": "workout", "id": 800, "workoutId": 80, "title": "Push", "date": "2026-06-02"},
    ]}
    june = {"calendarItems": [
        {"itemType": "workout", "id": 800, "workoutId": 80, "title": "Push", "date": "2026-06-02"},
    ]}
    wrapper, client = _wrapper_with_months({"/month/4": may, "/month/5": june})
    client.garth.post.return_value = _resp({"workoutScheduleId": 8080})

    result = wrapper.reschedule_workout(80, "2026-06-02")

    assert client.garth.delete.call_count == 1  # not 2
    assert result["removed_duplicates"] == [{"scheduleId": 800, "date": "2026-06-02"}]
    assert result["stale_phantom_entries"] == []


def test_get_scheduled_handles_empty_calendaritems():
    """A month with no calendarItems key returns nothing, no error."""
    wrapper, _ = _wrapper_with_months({"/month/5": {}})
    assert wrapper.get_scheduled_workouts("2026-06-01", "2026-06-07") == []


def test_reschedule_removes_matching_then_schedules():
    """Reschedule unschedules only the same-workout stale entry, then schedules the target."""
    # Push (workoutId 11) is wrongly on 06-03; Legs (22) is unrelated and must be left alone.
    payload = {"calendarItems": [
        {"itemType": "workout", "id": 5001, "workoutId": 11, "title": "Push", "date": "2026-06-03"},
        {"itemType": "workout", "id": 5002, "workoutId": 22, "title": "Legs", "date": "2026-06-05"},
    ]}
    wrapper, client = _wrapper_with_months({"/month/5": payload})
    client.garth.post.return_value = _resp({"workoutScheduleId": 6001})

    result = wrapper.reschedule_workout(11, "2026-06-02")

    # exactly one unschedule, for the stale Push entry only
    assert client.garth.delete.call_count == 1
    assert "/workout-service/schedule/5001" in client.garth.delete.call_args.args[1]
    # scheduled to the target date
    post_url = client.garth.post.call_args.args[1]
    assert "/workout-service/schedule/11" in post_url
    assert client.garth.post.call_args.kwargs["json"] == {"date": "2026-06-02"}
    # return payload reflects the cleanup + new id
    assert result["scheduled_date"] == "2026-06-02"
    assert result["new_schedule_id"] == 6001
    assert result["removed_duplicates"] == [{"scheduleId": 5001, "date": "2026-06-03"}]


def test_reschedule_no_existing_entries():
    """With nothing on the calendar, reschedule just schedules — no unschedule calls."""
    wrapper, client = _wrapper_with_months({"/month/5": {"calendarItems": []}})
    client.garth.post.return_value = _resp({"workoutScheduleId": 7001})

    result = wrapper.reschedule_workout(11, "2026-06-02")

    assert client.garth.delete.call_count == 0
    assert result["removed_duplicates"] == []
    assert result["new_schedule_id"] == 7001


def test_reschedule_dedups_multiple_stale_entries():
    """If the same workout was scheduled more than once, all stale copies are removed."""
    payload = {"calendarItems": [
        {"itemType": "workout", "id": 8001, "workoutId": 11, "title": "Push", "date": "2026-06-03"},
        {"itemType": "workout", "id": 8002, "workoutId": 11, "title": "Push", "date": "2026-06-04"},
    ]}
    wrapper, client = _wrapper_with_months({"/month/5": payload})
    client.garth.post.return_value = _resp({"workoutScheduleId": 9001})

    result = wrapper.reschedule_workout(11, "2026-06-02")

    assert client.garth.delete.call_count == 2
    assert {r["scheduleId"] for r in result["removed_duplicates"]} == {8001, 8002}


def test_reschedule_tolerates_phantom_entry():
    """A calendar entry that 404s on unschedule (workout-service already dropped it)
    is treated as already-gone — recorded as phantom, not fatal — and the reschedule
    still completes."""
    payload = {"calendarItems": [
        {"itemType": "workout", "id": 4040, "workoutId": 11, "title": "Push", "date": "2026-06-03"},
    ]}
    wrapper, client = _wrapper_with_months({"/month/5": payload})
    # garth raises a 404; unschedule_workout maps it to GarminNotFoundError, which
    # reschedule_workout must swallow as "already gone".
    client.garth.delete.side_effect = GarthHTTPError(
        "Error in request", HTTPError("404 Client Error: Not Found")
    )
    client.garth.post.return_value = _resp({"workoutScheduleId": 4242})

    result = wrapper.reschedule_workout(11, "2026-06-02")

    # schedule still happened despite the unschedule 404
    assert result["new_schedule_id"] == 4242
    assert result["removed_duplicates"] == []
    assert result["stale_phantom_entries"] == [{"scheduleId": 4040, "date": "2026-06-03"}]


def test_reschedule_invalid_date_raises():
    """A malformed target date raises GarminAPIError before any network call."""
    wrapper, client = _wrapper_with_months({})
    with pytest.raises(GarminAPIError):
        wrapper.reschedule_workout(11, "June 2 2026")
    client.garth.get.assert_not_called()
    client.garth.post.assert_not_called()
