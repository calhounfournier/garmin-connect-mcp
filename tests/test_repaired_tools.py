"""Tools that proxied garminconnect methods which never existed.

``get_training_effect``, ``get_activity_social`` and ``get_fitness_age`` are in no
garminconnect release, so those code paths failed at runtime while the mocked suite
stayed green. Training effect now reads the activity summary, fitness age uses
``get_fitnessage_data``, and the social tool is removed (no endpoint to back it).
"""

import json
from unittest.mock import MagicMock

import garminconnect
import pytest

from garmin_connect_mcp.client import GarminClientWrapper
from garmin_connect_mcp.tools.training import get_performance_metrics, get_training_effect

# Live shape of get_activity(...)["summaryDTO"], probed 2026-10-20 on activity 24789856395.
SUMMARY = {
    "trainingEffect": 1.4,
    "anaerobicTrainingEffect": 1.6,
    "aerobicTrainingEffectMessage": "MINOR_AEROBIC_BENEFIT_6",
    "anaerobicTrainingEffectMessage": "MINOR_ANAEROBIC_BENEFIT_15",
    "trainingEffectLabel": "ANAEROBIC_CAPACITY",
    "activityTrainingLoad": 34.77,
    "distance": 0.0,
}


def _ctx(client):
    ctx = MagicMock()
    ctx.get_state.return_value = client
    return ctx


def _real_spec_client():
    """A mock that only answers methods the installed garminconnect actually has."""
    return MagicMock(spec=garminconnect.Garmin)


@pytest.mark.asyncio
async def test_training_effect_reads_activity_summary():
    client = _real_spec_client()
    client.get_activity.return_value = {"activityId": 9, "summaryDTO": SUMMARY}

    out = json.loads(
        await get_training_effect(activity_id=9, ctx=_ctx(GarminClientWrapper(client)))
    )

    client.get_activity.assert_called_once_with(9)
    effect = out["data"]["training_effect"]
    assert effect == {
        "aerobic": 1.4,
        "anaerobic": 1.6,
        "aerobic_message": "MINOR_AEROBIC_BENEFIT_6",
        "anaerobic_message": "MINOR_ANAEROBIC_BENEFIT_15",
        "label": "ANAEROBIC_CAPACITY",
        "training_load": 34.77,
    }


@pytest.mark.asyncio
async def test_fitness_age_uses_fitnessage_data():
    client = _real_spec_client()
    client.get_fitnessage_data.return_value = {"fitnessAge": 38.2, "chronologicalAge": 46}

    out = json.loads(
        await get_performance_metrics(
            date="2026-10-20",
            include_vo2_max=False,
            include_hill_score=False,
            include_endurance_score=False,
            include_hrv=False,
            include_fitness_age=True,
            ctx=_ctx(GarminClientWrapper(client)),
        )
    )

    client.get_fitnessage_data.assert_called_once_with("2026-10-20")
    assert out["data"]["fitness_age"] == {"fitnessAge": 38.2, "chronologicalAge": 46}


def test_activity_social_tool_is_not_registered():
    import garmin_connect_mcp.tools.activities as activities

    assert not hasattr(activities, "get_activity_social")


def test_every_proxied_method_exists_on_installed_garminconnect():
    """Guard the whole class of bug: every name passed to safe_call must exist."""
    import pathlib
    import re

    src = pathlib.Path(__file__).parent.parent / "src" / "garmin_connect_mcp"
    names = set()
    for path in src.rglob("*.py"):
        names |= set(re.findall(r'safe_call\(\s*"([a-z_]+)"', path.read_text()))
    assert names, "no safe_call sites found — pattern drifted"
    missing = sorted(n for n in names if not hasattr(garminconnect.Garmin, n))
    assert missing == []
