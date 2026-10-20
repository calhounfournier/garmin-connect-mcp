"""Tests for calendar-aware workout scheduling on GarminClientWrapper.

Covers ``get_scheduled_workouts`` (built on garminconnect's month-based
``get_scheduled_workouts(year, month)``: date-range filtering, workout-only,
multi-month spanning, grid-overflow dedupe) and the duplicate-proof
``reschedule_workout`` (remove existing matching entries, then schedule).
"""

from unittest.mock import MagicMock

import pytest
from garminconnect import GarminConnectNotFoundError

from garmin_connect_mcp.client import GarminAPIError, GarminClientWrapper


def _wrapper_with_months(month_payloads):
    """
    Build a wrapper whose client.get_scheduled_workouts returns a payload chosen
    by (year, month). ``month_payloads`` maps a 1-indexed (year, month) tuple to the
    calendar payload for that month.
    """
    client = MagicMock()

    def get_side(year, month):
        return month_payloads.get((year, month), {"calendarItems": []})

    client.get_scheduled_workouts.side_effect = get_side
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
    wrapper, _ = _wrapper_with_months({(2026, 6): payload})

    result = wrapper.get_scheduled_workouts("2026-06-01", "2026-06-07")

    assert result == [
        {"scheduleId": 111, "workoutId": 11, "title": "Push", "date": "2026-06-02"},
        {"scheduleId": 222, "workoutId": 22, "title": "Legs", "date": "2026-06-05"},
    ]


def test_get_scheduled_passes_one_indexed_month():
    """garminconnect does the 0-index shift itself; June must be requested as month 6."""
    wrapper, client = _wrapper_with_months({})

    wrapper.get_scheduled_workouts("2026-06-01", "2026-06-07")

    assert [c.args for c in client.get_scheduled_workouts.call_args_list] == [(2026, 6)]


def test_get_scheduled_spans_multiple_months_sorted():
    """A range crossing a month boundary queries both months and merges sorted by date."""
    may = {"calendarItems": [
        {"itemType": "workout", "id": 1, "workoutId": 91, "title": "May end", "date": "2026-05-31"},
    ]}
    june = {"calendarItems": [
        {"itemType": "workout", "id": 2, "workoutId": 92, "title": "June start", "date": "2026-06-01"},
    ]}
    wrapper, client = _wrapper_with_months({(2026, 5): may, (2026, 6): june})

    result = wrapper.get_scheduled_workouts("2026-05-30", "2026-06-02")

    assert [r["date"] for r in result] == ["2026-05-31", "2026-06-01"]
    assert sorted(c.args for c in client.get_scheduled_workouts.call_args_list) == [
        (2026, 5),
        (2026, 6),
    ]


def test_get_scheduled_spans_year_boundary():
    """December → January rolls the year forward."""
    wrapper, client = _wrapper_with_months({})

    wrapper.get_scheduled_workouts("2026-12-28", "2027-01-03")

    assert [c.args for c in client.get_scheduled_workouts.call_args_list] == [
        (2026, 12),
        (2027, 1),
    ]


def test_get_scheduled_dedupes_grid_overflow():
    """Month payloads include adjacent-month grid-overflow days, so a boundary date
    appears in two months. The same scheduleId must be returned once, not twice."""
    may = {"calendarItems": [
        {"itemType": "workout", "id": 700, "workoutId": 70, "title": "May28", "date": "2026-05-28"},
        {"itemType": "workout", "id": 800, "workoutId": 80, "title": "Push", "date": "2026-06-02"},
    ]}
    june = {"calendarItems": [
        {"itemType": "workout", "id": 800, "workoutId": 80, "title": "Push", "date": "2026-06-02"},
    ]}
    wrapper, _ = _wrapper_with_months({(2026, 5): may, (2026, 6): june})

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
    wrapper, client = _wrapper_with_months({(2026, 5): may, (2026, 6): june})
    client.schedule_workout.return_value = {"workoutScheduleId": 8080}

    result = wrapper.reschedule_workout(80, "2026-06-02")

    assert client.unschedule_workout.call_count == 1  # not 2
    assert result["removed_duplicates"] == [{"scheduleId": 800, "date": "2026-06-02"}]
    assert result["stale_phantom_entries"] == []


def test_get_scheduled_handles_empty_calendaritems():
    """A month with no calendarItems key returns nothing, no error."""
    wrapper, _ = _wrapper_with_months({(2026, 6): {}})
    assert wrapper.get_scheduled_workouts("2026-06-01", "2026-06-07") == []


def test_reschedule_removes_matching_then_schedules():
    """Reschedule unschedules only the same-workout stale entry, then schedules the target."""
    # Push (workoutId 11) is wrongly on 06-03; Legs (22) is unrelated and must be left alone.
    payload = {"calendarItems": [
        {"itemType": "workout", "id": 5001, "workoutId": 11, "title": "Push", "date": "2026-06-03"},
        {"itemType": "workout", "id": 5002, "workoutId": 22, "title": "Legs", "date": "2026-06-05"},
    ]}
    wrapper, client = _wrapper_with_months({(2026, 6): payload})
    client.schedule_workout.return_value = {"workoutScheduleId": 6001}

    result = wrapper.reschedule_workout(11, "2026-06-02")

    # exactly one unschedule, for the stale Push entry only
    client.unschedule_workout.assert_called_once_with(5001)
    # scheduled to the target date
    client.schedule_workout.assert_called_once_with(11, "2026-06-02")
    # return payload reflects the cleanup + new id
    assert result["scheduled_date"] == "2026-06-02"
    assert result["new_schedule_id"] == 6001
    assert result["removed_duplicates"] == [{"scheduleId": 5001, "date": "2026-06-03"}]


def test_reschedule_no_existing_entries():
    """With nothing on the calendar, reschedule just schedules — no unschedule calls."""
    wrapper, client = _wrapper_with_months({})
    client.schedule_workout.return_value = {"workoutScheduleId": 7001}

    result = wrapper.reschedule_workout(11, "2026-06-02")

    client.unschedule_workout.assert_not_called()
    assert result["removed_duplicates"] == []
    assert result["new_schedule_id"] == 7001


def test_reschedule_dedups_multiple_stale_entries():
    """If the same workout was scheduled more than once, all stale copies are removed."""
    payload = {"calendarItems": [
        {"itemType": "workout", "id": 8001, "workoutId": 11, "title": "Push", "date": "2026-06-03"},
        {"itemType": "workout", "id": 8002, "workoutId": 11, "title": "Push", "date": "2026-06-04"},
    ]}
    wrapper, client = _wrapper_with_months({(2026, 6): payload})
    client.schedule_workout.return_value = {"workoutScheduleId": 9001}

    result = wrapper.reschedule_workout(11, "2026-06-02")

    assert client.unschedule_workout.call_count == 2
    assert {r["scheduleId"] for r in result["removed_duplicates"]} == {8001, 8002}


def test_reschedule_tolerates_phantom_entry():
    """A calendar entry that 404s on unschedule (workout-service already dropped it)
    is treated as already-gone — recorded as phantom, not fatal — and the reschedule
    still completes."""
    payload = {"calendarItems": [
        {"itemType": "workout", "id": 4040, "workoutId": 11, "title": "Push", "date": "2026-06-03"},
    ]}
    wrapper, client = _wrapper_with_months({(2026, 6): payload})
    # garminconnect raises a 404 as GarminConnectNotFoundError; unschedule_workout
    # maps it to GarminNotFoundError, which reschedule_workout swallows as "already gone".
    client.unschedule_workout.side_effect = GarminConnectNotFoundError("API Error 404")
    client.schedule_workout.return_value = {"workoutScheduleId": 4242}

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
    client.get_scheduled_workouts.assert_not_called()
    client.schedule_workout.assert_not_called()
