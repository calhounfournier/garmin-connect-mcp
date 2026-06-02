"""Workout management tools for Garmin Connect MCP server."""

from typing import Annotated

from fastmcp import Context

from ..client import GarminAPIError
from ..response_builder import ResponseBuilder


async def manage_workouts(
    action: Annotated[str, "Action: 'list', 'list_scheduled', 'get', 'download', 'upload', 'update', 'schedule', 'reschedule', 'unschedule'"],
    workout_id: Annotated[int | None, "Workout ID (for get/download/update/schedule actions)"] = None,
    workout_data: Annotated[str | None, "Workout data (for upload/update actions)"] = None,
    schedule_date: Annotated[str | None, "Date to schedule workout (YYYY-MM-DD format, for schedule action)"] = None,
    schedule_id: Annotated[int | None, "Workout schedule ID (for unschedule action — returned by schedule action as workoutScheduleId)"] = None,
    start_date: Annotated[str | None, "Range start YYYY-MM-DD (for list_scheduled action)"] = None,
    end_date: Annotated[str | None, "Range end YYYY-MM-DD (for list_scheduled action)"] = None,
    ctx: Context | None = None,
) -> str:
    """
    Manage structured workouts.

    Actions:
    - list: Get all workouts
    - list_scheduled: List workouts scheduled on the calendar in a date range
      (provide start_date and end_date) — returns each scheduleId + workoutId +
      date, so a wrong-date entry can be unscheduled by id without the watch UI
    - get: Get specific workout by ID
    - download: Download workout file
    - upload: Upload a new workout
    - update: Update an existing workout (provide workout_id and workout_data)
    - schedule: Schedule a workout to a date (syncs to watch automatically)
    - reschedule: Move a workout to a date, first removing any existing entry for
      that workout in the window (duplicate-proof — prefer this over schedule when
      a workout may already be on the calendar). Provide workout_id and schedule_date.
    - unschedule: Remove a scheduled workout from the calendar (provide schedule_id)
    """
    assert ctx is not None
    try:
        client = ctx.get_state("client")

        if action == "list":
            workouts = client.safe_call("get_workouts")
            return ResponseBuilder.build_response(
                data={
                    "workouts": workouts,
                    "count": len(workouts) if isinstance(workouts, list) else 0,
                },
                metadata={"action": "list"},
            )

        elif action == "list_scheduled":
            if not start_date or not end_date:
                return ResponseBuilder.build_error_response(
                    "start_date and end_date required for list_scheduled action (YYYY-MM-DD)",
                    "invalid_parameters",
                    ["Provide both start_date and end_date parameters"],
                )

            scheduled = client.get_scheduled_workouts(start_date, end_date)
            return ResponseBuilder.build_response(
                data={"scheduled_workouts": scheduled, "count": len(scheduled)},
                analysis={
                    "insights": [
                        "Use each entry's scheduleId with the 'unschedule' action to remove a wrong-date workout"
                    ]
                },
                metadata={"action": "list_scheduled", "start_date": start_date, "end_date": end_date},
            )

        elif action == "get":
            if workout_id is None:
                return ResponseBuilder.build_error_response(
                    "Workout ID required for get action",
                    "invalid_parameters",
                    ["Provide workout_id parameter"],
                )

            workout = client.safe_call("get_workout_by_id", workout_id)
            return ResponseBuilder.build_response(
                data={"workout": workout},
                metadata={"action": "get", "workout_id": workout_id},
            )

        elif action == "download":
            if workout_id is None:
                return ResponseBuilder.build_error_response(
                    "Workout ID required for download action",
                    "invalid_parameters",
                    ["Provide workout_id parameter"],
                )

            download_info = client.safe_call("download_workout", workout_id)
            return ResponseBuilder.build_response(
                data={"download_info": download_info},
                metadata={"action": "download", "workout_id": workout_id},
            )

        elif action == "upload":
            if not workout_data:
                return ResponseBuilder.build_error_response(
                    "Workout data required for upload action",
                    "invalid_parameters",
                    ["Provide workout_data parameter"],
                )

            result = client.safe_call("upload_workout", workout_data)
            return ResponseBuilder.build_response(
                data={"result": result},
                analysis={"insights": ["Workout uploaded successfully"]},
                metadata={"action": "upload"},
            )

        elif action == "update":
            if workout_id is None:
                return ResponseBuilder.build_error_response(
                    "Workout ID required for update action",
                    "invalid_parameters",
                    ["Provide workout_id parameter"],
                )
            if not workout_data:
                return ResponseBuilder.build_error_response(
                    "Workout data required for update action",
                    "invalid_parameters",
                    ["Provide workout_data parameter with the full workout JSON"],
                )

            result = client.update_workout(workout_id, workout_data)
            return ResponseBuilder.build_response(
                data={"result": result},
                analysis={"insights": ["Workout updated successfully"]},
                metadata={"action": "update", "workout_id": workout_id},
            )

        elif action == "schedule":
            if workout_id is None:
                return ResponseBuilder.build_error_response(
                    "Workout ID required for schedule action",
                    "invalid_parameters",
                    ["Provide workout_id parameter"],
                )
            if not schedule_date:
                return ResponseBuilder.build_error_response(
                    "Date required for schedule action (YYYY-MM-DD format)",
                    "invalid_parameters",
                    ["Provide schedule_date parameter"],
                )

            result = client.schedule_workout(workout_id, schedule_date)
            return ResponseBuilder.build_response(
                data={"result": result},
                analysis={"insights": [f"Workout scheduled for {schedule_date} — will sync to watch automatically"]},
                metadata={"action": "schedule", "workout_id": workout_id, "date": schedule_date},
            )

        elif action == "reschedule":
            if workout_id is None:
                return ResponseBuilder.build_error_response(
                    "Workout ID required for reschedule action",
                    "invalid_parameters",
                    ["Provide workout_id parameter"],
                )
            if not schedule_date:
                return ResponseBuilder.build_error_response(
                    "Date required for reschedule action (YYYY-MM-DD format)",
                    "invalid_parameters",
                    ["Provide schedule_date parameter"],
                )

            result = client.reschedule_workout(workout_id, schedule_date)
            removed = result.get("removed_duplicates", [])
            phantom = result.get("stale_phantom_entries", [])
            insight = f"Workout rescheduled to {schedule_date}"
            if removed:
                insight += f" — removed {len(removed)} stale entry(ies) on {', '.join(r.get('date') or '?' for r in removed)}"
            else:
                insight += " — no prior entries found in window"
            if phantom:
                insight += f"; skipped {len(phantom)} phantom entry(ies) already gone from workout-service"
            return ResponseBuilder.build_response(
                data={"result": result},
                analysis={"insights": [insight]},
                metadata={"action": "reschedule", "workout_id": workout_id, "date": schedule_date},
            )

        elif action == "unschedule":
            if schedule_id is None:
                return ResponseBuilder.build_error_response(
                    "Schedule ID required for unschedule action (workoutScheduleId from schedule response)",
                    "invalid_parameters",
                    ["Provide schedule_id parameter"],
                )

            result = client.unschedule_workout(schedule_id)
            return ResponseBuilder.build_response(
                data={"result": result},
                analysis={"insights": ["Workout removed from calendar"]},
                metadata={"action": "unschedule", "schedule_id": schedule_id},
            )

        else:
            return ResponseBuilder.build_error_response(
                f"Invalid action: {action}",
                "invalid_parameters",
                ["Valid actions: 'list', 'list_scheduled', 'get', 'download', 'upload', 'update', 'schedule', 'reschedule', 'unschedule'"],
            )

    except GarminAPIError as e:
        return ResponseBuilder.build_error_response(e.message, "api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(str(e), "internal_error")
