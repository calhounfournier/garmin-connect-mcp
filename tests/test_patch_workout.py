"""Edit a workout in place: fetch the card, change only the named fields, save once.

Garmin's workout endpoint replaces the whole card, so hand-writing the full JSON
to change one weight is how other steps get lost. ``patch_workout`` addresses
steps by ``stepOrder`` (unique across the card, repeat-group children included),
validates every requested change before writing anything, and reports each
field's before/after value.
"""

import copy
import json
from unittest.mock import MagicMock

import pytest

from garmin_connect_mcp.client import GarminAPIError, GarminClientWrapper
from garmin_connect_mcp.tools.workouts import manage_workouts

# Trimmed live shape of a strength card (Pull, Block Oct 12), probed 2026-10-20.
CARD = {
    "workoutId": 77,
    "workoutName": "PPL - Pull (Block Oct 12)",
    "description": "Pull day",
    "workoutSegments": [
        {
            "segmentOrder": 1,
            "workoutSteps": [
                {
                    "stepOrder": 1,
                    "type": "ExecutableStepDTO",
                    "exerciseName": "PULL_UP",
                    "weightValue": 0.0,
                    "endConditionValue": 0.0,
                    "description": "Strict pull-up",
                },
                {
                    "stepOrder": 2,
                    "type": "ExecutableStepDTO",
                    "exerciseName": None,
                    "weightValue": 0.0,
                    "endConditionValue": 180.0,
                    "description": None,
                },
                {
                    "stepOrder": 3,
                    "type": "RepeatGroupDTO",
                    "numberOfIterations": 4,
                    "endConditionValue": 4.0,
                    "workoutSteps": [
                        {
                            "stepOrder": 4,
                            "type": "ExecutableStepDTO",
                            "exerciseName": "WEIGHTED_PULL_UP",
                            "weightValue": 74.8,
                            "endConditionValue": 8.0,
                            "description": "Assisted pull-up",
                        },
                        {
                            "stepOrder": 5,
                            "type": "ExecutableStepDTO",
                            "exerciseName": None,
                            "weightValue": 0.0,
                            "endConditionValue": 120.0,
                            "description": None,
                        },
                    ],
                },
            ],
        }
    ],
}


def _wrapper():
    client = MagicMock()
    client.get_workout_by_id.return_value = copy.deepcopy(CARD)
    client.update_workout.return_value = {"workoutId": 77}
    return GarminClientWrapper(client), client


def _sent(client):
    return client.update_workout.call_args.args[1]


def test_patch_nested_step_changes_only_named_fields():
    wrapper, client = _wrapper()

    result = wrapper.patch_workout(77, step_updates=[{"stepOrder": 4, "weightValue": 72.5}])

    client.update_workout.assert_called_once()
    sent = _sent(client)
    expected = copy.deepcopy(CARD)
    expected["workoutSegments"][0]["workoutSteps"][2]["workoutSteps"][0]["weightValue"] = 72.5
    assert sent == expected
    assert result["changes"] == [
        {"stepOrder": 4, "field": "weightValue", "before": 74.8, "after": 72.5}
    ]


def test_patch_top_level_and_several_steps():
    wrapper, client = _wrapper()

    result = wrapper.patch_workout(
        77,
        workout_updates={"workoutName": "PPL - Pull (deload)"},
        step_updates=[
            {"stepOrder": 3, "numberOfIterations": 3, "endConditionValue": 3.0},
            {"stepOrder": 5, "endConditionValue": 90.0},
        ],
    )

    sent = _sent(client)
    assert sent["workoutName"] == "PPL - Pull (deload)"
    group = sent["workoutSegments"][0]["workoutSteps"][2]
    assert group["numberOfIterations"] == 3
    assert group["workoutSteps"][1]["endConditionValue"] == 90.0
    # untouched step identical
    assert (
        sent["workoutSegments"][0]["workoutSteps"][0]
        == CARD["workoutSegments"][0]["workoutSteps"][0]
    )
    assert {(c["stepOrder"], c["field"]) for c in result["changes"]} == {
        (None, "workoutName"),
        (3, "numberOfIterations"),
        (3, "endConditionValue"),
        (5, "endConditionValue"),
    }


def test_patch_unknown_step_writes_nothing():
    wrapper, client = _wrapper()
    with pytest.raises(GarminAPIError, match="stepOrder 9"):
        wrapper.patch_workout(
            77,
            step_updates=[
                {"stepOrder": 4, "weightValue": 70.0},
                {"stepOrder": 9, "weightValue": 1.0},
            ],
        )
    client.update_workout.assert_not_called()


@pytest.mark.parametrize(
    "updates",
    [
        [{"weightValue": 70.0}],  # no stepOrder
        [{"stepOrder": 4, "weightValue": 70.0}, {"stepOrder": 4, "weightValue": 71.0}],  # duplicate
        [{"stepOrder": 4, "workoutSteps": []}],  # structural field
        [{"stepOrder": 4}],  # nothing to change
    ],
)
def test_patch_rejects_bad_step_updates(updates):
    wrapper, client = _wrapper()
    with pytest.raises(GarminAPIError):
        wrapper.patch_workout(77, step_updates=updates)
    client.update_workout.assert_not_called()


@pytest.mark.parametrize("key", ["workoutId", "workoutSegments"])
def test_patch_rejects_structural_top_level_fields(key):
    wrapper, client = _wrapper()
    with pytest.raises(GarminAPIError, match=key):
        wrapper.patch_workout(77, workout_updates={key: 1})
    client.update_workout.assert_not_called()


def test_patch_with_no_effective_change_skips_write():
    wrapper, client = _wrapper()
    result = wrapper.patch_workout(77, step_updates=[{"stepOrder": 4, "weightValue": 74.8}])
    client.update_workout.assert_not_called()
    assert result["changes"] == []
    assert result["written"] is False


def test_patch_requires_some_update():
    wrapper, client = _wrapper()
    with pytest.raises(GarminAPIError):
        wrapper.patch_workout(77)
    client.get_workout_by_id.assert_not_called()


# --- tool layer -------------------------------------------------------------


def _ctx(client):
    ctx = MagicMock()
    ctx.get_state.return_value = client
    return ctx


@pytest.mark.asyncio
async def test_tool_patch_parses_json_and_calls_client():
    client = MagicMock()
    client.patch_workout.return_value = {"workout_id": 77, "changes": [], "written": False}

    out = json.loads(
        await manage_workouts(
            "patch",
            workout_id=77,
            step_updates='[{"stepOrder": 4, "weightValue": 72.5}]',
            workout_updates='{"workoutName": "X"}',
            ctx=_ctx(client),
        )
    )

    assert "error" not in out
    client.patch_workout.assert_called_once_with(
        77,
        workout_updates={"workoutName": "X"},
        step_updates=[{"stepOrder": 4, "weightValue": 72.5}],
    )


@pytest.mark.asyncio
async def test_tool_patch_requires_workout_id():
    client = MagicMock()
    out = json.loads(await manage_workouts("patch", step_updates="[]", ctx=_ctx(client)))
    assert "Workout ID required" in out["error"]["message"]
    client.patch_workout.assert_not_called()


@pytest.mark.asyncio
async def test_tool_patch_rejects_invalid_json():
    client = MagicMock()
    out = json.loads(
        await manage_workouts("patch", workout_id=77, step_updates="[{bad", ctx=_ctx(client))
    )
    assert "error" in out
    client.patch_workout.assert_not_called()
