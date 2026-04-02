"""Workout management tools for Garmin Connect MCP server."""

from typing import Annotated

from fastmcp import Context

from ..client import GarminAPIError
from ..response_builder import ResponseBuilder


async def manage_workouts(
    action: Annotated[str, "Action: 'list', 'get', 'download', 'upload', 'update', 'schedule', 'unschedule'"],
    workout_id: Annotated[int | None, "Workout ID (for get/download/update/schedule actions)"] = None,
    workout_data: Annotated[str | None, "Workout data (for upload/update actions)"] = None,
    schedule_date: Annotated[str | None, "Date to schedule workout (YYYY-MM-DD format, for schedule action)"] = None,
    schedule_id: Annotated[int | None, "Workout schedule ID (for unschedule action — returned by schedule action as workoutScheduleId)"] = None,
    ctx: Context | None = None,
) -> str:
    """
    Manage structured workouts.

    Actions:
    - list: Get all workouts
    - get: Get specific workout by ID
    - download: Download workout file
    - upload: Upload a new workout
    - update: Update an existing workout (provide workout_id and workout_data)
    - schedule: Schedule a workout to a date (syncs to watch automatically)
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
                ["Valid actions: 'list', 'get', 'download', 'upload', 'update', 'schedule', 'unschedule'"],
            )

    except GarminAPIError as e:
        return ResponseBuilder.build_error_response(e.message, "api_error")
    except Exception as e:
        return ResponseBuilder.build_error_response(str(e), "internal_error")
