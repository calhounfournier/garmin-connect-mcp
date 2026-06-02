"""Tests for the manage_workouts tool dispatch (param validation + wiring).

These cover the new ``list_scheduled`` and ``reschedule`` actions at the tool
layer: required-parameter guards and that the right client method is invoked.
"""

import json
from unittest.mock import MagicMock

import pytest

from garmin_connect_mcp.tools.workouts import manage_workouts


def _ctx(client):
    ctx = MagicMock()
    ctx.get_state.return_value = client
    return ctx


@pytest.mark.asyncio
async def test_reschedule_requires_workout_id():
    client = MagicMock()
    out = json.loads(await manage_workouts("reschedule", schedule_date="2026-06-02", ctx=_ctx(client)))
    assert "error" in out
    assert "Workout ID required" in out["error"]["message"]
    client.reschedule_workout.assert_not_called()


@pytest.mark.asyncio
async def test_reschedule_requires_date():
    client = MagicMock()
    out = json.loads(await manage_workouts("reschedule", workout_id=11, ctx=_ctx(client)))
    assert "error" in out
    assert "Date required" in out["error"]["message"]
    client.reschedule_workout.assert_not_called()


@pytest.mark.asyncio
async def test_reschedule_happy_path_calls_client_and_reports_removed():
    client = MagicMock()
    client.reschedule_workout.return_value = {
        "workout_id": 11,
        "scheduled_date": "2026-06-02",
        "new_schedule_id": 6001,
        "removed_duplicates": [{"scheduleId": 5001, "date": "2026-06-03"}],
        "schedule_result": {"workoutScheduleId": 6001},
    }
    out = json.loads(await manage_workouts("reschedule", workout_id=11, schedule_date="2026-06-02", ctx=_ctx(client)))

    client.reschedule_workout.assert_called_once_with(11, "2026-06-02")
    assert "error" not in out
    assert out["data"]["result"]["new_schedule_id"] == 6001
    insight = out["analysis"]["insights"][0]
    assert "removed 1" in insight and "2026-06-03" in insight


@pytest.mark.asyncio
async def test_reschedule_happy_path_no_prior_entries_insight():
    client = MagicMock()
    client.reschedule_workout.return_value = {
        "workout_id": 11,
        "scheduled_date": "2026-06-02",
        "new_schedule_id": 7001,
        "removed_duplicates": [],
        "schedule_result": {"workoutScheduleId": 7001},
    }
    out = json.loads(await manage_workouts("reschedule", workout_id=11, schedule_date="2026-06-02", ctx=_ctx(client)))
    assert "no prior entries found" in out["analysis"]["insights"][0]


@pytest.mark.asyncio
async def test_list_scheduled_requires_both_dates():
    client = MagicMock()
    out = json.loads(await manage_workouts("list_scheduled", start_date="2026-06-01", ctx=_ctx(client)))
    assert "error" in out
    assert "start_date and end_date required" in out["error"]["message"]
    client.get_scheduled_workouts.assert_not_called()


@pytest.mark.asyncio
async def test_list_scheduled_happy_path():
    client = MagicMock()
    client.get_scheduled_workouts.return_value = [
        {"scheduleId": 111, "workoutId": 11, "title": "Push", "date": "2026-06-02"},
        {"scheduleId": 222, "workoutId": 22, "title": "Legs", "date": "2026-06-05"},
    ]
    out = json.loads(
        await manage_workouts("list_scheduled", start_date="2026-06-01", end_date="2026-06-07", ctx=_ctx(client))
    )
    client.get_scheduled_workouts.assert_called_once_with("2026-06-01", "2026-06-07")
    assert out["data"]["count"] == 2
    assert out["data"]["scheduled_workouts"][0]["scheduleId"] == 111


@pytest.mark.asyncio
async def test_invalid_action_lists_new_actions():
    client = MagicMock()
    out = json.loads(await manage_workouts("bogus", ctx=_ctx(client)))
    assert "error" in out
    # the suggestion list should advertise the new actions
    suggestions = " ".join(out["error"].get("suggestions", []))
    assert "reschedule" in suggestions and "list_scheduled" in suggestions
