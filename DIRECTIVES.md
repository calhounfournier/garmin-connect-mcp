# DIRECTIVES — garmin-connect-mcp

> Bridge file between memoire (strategy/host) and this dev repo (execution). Dev-Claude reads this at session start. Pending is what matters. See memoire `.claude/rules/bridge-directives.md` for the processing convention (move Pending → Processed with Outcome + deviations, drop the original body).

---

## Pending

_(none)_

---

## Processed

### D-001 — Add a `delete` action to `manage_workouts`

**Processed:** 2026-06-14

**Outcome:** Added `delete_workout(workout_id)` to `client.py` (authenticated garth DELETE on `/workout-service/workout/{id}`, mirroring `unschedule_workout` including the 429/404/401-403/else error mapping) and wired a `delete` action into `manage_workouts` in `tools/workouts.py` (Annotated action list, docstring entry, validating branch requiring `workout_id`, and the invalid-action message). Committed as `4484dab` on branch `feat/workout-reschedule` (source files only). Verified end-to-end after an MCP reconnect: a no-arg `delete` returned the new "Workout ID required" validation (proving the new code path was live, not "invalid action"), then the 6 `TEST` enum-probe workouts were deleted by id (each `status: ok`) and a follow-up `list` confirmed **zero** `TEST` workouts remain while the real `PPL - Legs (06-2026)` (1587652781) stayed intact. All acceptance criteria met.

**Notable deviations / surprises:**
- Implemented directly in the working tree, not handed off — this is a local MCP repo, not a remote/devcontainer project, so there is no separate "dev session." (Correction to an earlier wrong assumption that MCP changes route through a dev session.)
- The garmin MCP runs as a **stdio server Claude Code spawns per session** (`uv run --directory … garmin-connect-mcp`). Editing the source does NOT hot-reload the running process; the action only went live after the MCP was reconnected (`/mcp` → garmin → reconnect). Future MCP code changes need the same reconnect before they're callable in-session.
- `manage_workouts(action="list")` returns ~100 KB (capped at 100 workouts) — exceeds the tool-output token limit and spills to a file. Parsing is done off the saved file. A future `list` enhancement (name filter / pagination / lean projection) would help, but is out of scope here.
- Not committed: the `DIRECTIVES.md` bridge doc and a pre-existing unrelated `uv.lock` change.

_Original directive body removed after processing — full text in git history of `DIRECTIVES.md` and `session-log/log-2026-06-14.md`._
