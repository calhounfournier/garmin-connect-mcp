# DIRECTIVES — garmin-connect-mcp

> Bridge file between memoire (strategy/host) and this dev repo (execution). Dev-Claude reads this at session start. Pending is what matters. See memoire `.claude/rules/bridge-directives.md` for the processing convention (move Pending → Processed with Outcome + deviations, drop the original body).

---

## Pending

_(none)_

---

## Processed

### D-002 — An unreadable token store must never trigger a credential login

**Processed:** 2026-10-20

**Source:** Live incident. Charles asked to move leg day; every `manage_workouts` call returned "Garmin client unavailable." Four retries sent him five Garmin MFA passcode emails at 06:17–06:18, which he caught via a screenshot of his inbox.

**Scope:** `src/garmin_connect_mcp/client.py`, `tests/test_token_format_guard.py`.

**Outcome:** Root cause was a version skew, not a dead token. The 2026-10-19 18:07 morning-brief fix re-authenticated with garminconnect 0.3.19, which writes a single `garmin_tokens.json` (`di_token`/`di_refresh_token`/`di_client_id`) into `~/.garminconnect`. This repo's venv pins garminconnect 0.2.38 / garth 0.5.21, which still expect `oauth1_token.json` + `oauth2_token.json`. `_login_with_tokens` passed its own non-empty-directory check, then `Garmin.login()` raised `FileNotFoundError` for the missing `oauth1_token.json` — and `init_garmin_client` caught that as "no tokens at all → first-time credential login is legitimate." So every tool call did a full email/password login and Garmin mailed a passcode.

Added `GarminTokenFormatError`. `_login_with_tokens` now wraps the inner `FileNotFoundError` in it, distinguishing "directory has token files this garth can't parse" from "directory is empty." `init_garmin_client` catches it ahead of the `FileNotFoundError` branch and returns `None` (retryable) without ever reaching `_credential_login`. A genuinely empty store still credential-logs in.

Also pinned this server's token store via `env.GARMINTOKENS` → `/Users/admin/.garminconnect-mcp` in `~/.claude.json` (backup `.claude.json.bak-2026-10-20-garmin`), seeded from `~/.garminconnect.bak-2026-10-19`. Those oauth1/oauth2 tokens are from 2026-06-07 and still authenticate. The brief scripts keep using `~/.garminconnect` on the new format, so both work.

**Verification:** 3 new tests; full suite 79 passed. RED proven precisely by reverting only the `return None` guard (keeping the exception class, so the failure is behavioural not a collection error) — `mock_credential.assert_not_called()` failed, confirming the credential login fired without the guard. Live probe against the real broken store with credentials blanked returns `None` and logs the format-mismatch message. End-to-end login against the pinned store returns an authenticated client.

**Notable deviations / surprises:**
- This is the second MFA-spam incident in this repo — `9855787` ("stop transient errors triggering MFA") hardened the 429/network path. That fix guarded transient HTTP errors but not a token-format mismatch, which arrives as `FileNotFoundError`. The guard now covers both shapes.
- Both failures share one design flaw: credential login is the default fallback for *any* unexplained resume failure. The safer inversion is to credential-login only on a positively-identified empty store. Partially done here; worth finishing.
- ~~The deeper skew is unresolved.~~ **Resolved same morning after Charles pushed on the deferral.** The true root cause was an **unpinned dependency**: `gather-brief-data.py`, `health-snapshot.py` and `schedule-swimming.py` all declared bare `garminconnect`, so `uv run` silently took the 10-15/16 release. Nobody chose 0.3.19. Going forward to it is a *port*: it replaces the `garth` transport with `curl_cffi` and `Garmin.garth` ceases to exist, breaking all 7 `.garth.*` sites here — and `schedule-swimming.py`, which uses the same pattern, was **already broken and silently so** (manual, not cron). Fix went backward instead: all three scripts pinned to `garminconnect==0.2.38`, old-format tokens restored into the shared `~/.garminconnect` beside the new `garmin_tokens.json`, `GARMINTOKENS` override removed. Four consumers verified live.
- **A claim in this entry's first draft was wrong:** I stated 0.3.19 dropped `get_activity_social`, `get_fitness_age` and `get_training_effect`. They exist in **neither** version — never were in garminconnect. I had probed only the new version. **Standing bug this exposed:** the `get_training_effect` and `get_activity_social` tools `safe_call` methods that do not exist, so they fail at runtime and always have. Dynamic string dispatch means the mocked suite stays green. Unfixed — worth a directive.
- Credentials sit in the repo's plaintext `.env`, which is what makes a stray credential login possible at all.

---

### D-001 — Add a `delete` action to `manage_workouts`

**Processed:** 2026-06-14

**Outcome:** Added `delete_workout(workout_id)` to `client.py` (authenticated garth DELETE on `/workout-service/workout/{id}`, mirroring `unschedule_workout` including the 429/404/401-403/else error mapping) and wired a `delete` action into `manage_workouts` in `tools/workouts.py` (Annotated action list, docstring entry, validating branch requiring `workout_id`, and the invalid-action message). Committed as `4484dab` on branch `feat/workout-reschedule` (source files only). Verified end-to-end after an MCP reconnect: a no-arg `delete` returned the new "Workout ID required" validation (proving the new code path was live, not "invalid action"), then the 6 `TEST` enum-probe workouts were deleted by id (each `status: ok`) and a follow-up `list` confirmed **zero** `TEST` workouts remain while the real `PPL - Legs (06-2026)` (1587652781) stayed intact. All acceptance criteria met.

**Notable deviations / surprises:**
- Implemented directly in the working tree, not handed off — this is a local MCP repo, not a remote/devcontainer project, so there is no separate "dev session." (Correction to an earlier wrong assumption that MCP changes route through a dev session.)
- The garmin MCP runs as a **stdio server Claude Code spawns per session** (`uv run --directory … garmin-connect-mcp`). Editing the source does NOT hot-reload the running process; the action only went live after the MCP was reconnected (`/mcp` → garmin → reconnect). Future MCP code changes need the same reconnect before they're callable in-session.
- `manage_workouts(action="list")` returns ~100 KB (capped at 100 workouts) — exceeds the tool-output token limit and spills to a file. Parsing is done off the saved file. A future `list` enhancement (name filter / pagination / lean projection) would help, but is out of scope here.
- Not committed: the `DIRECTIVES.md` bridge doc and a pre-existing unrelated `uv.lock` change.

_Original directive body removed after processing — full text in git history of `DIRECTIVES.md` and `session-log/log-2026-06-14.md`._
