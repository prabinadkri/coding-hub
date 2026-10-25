# Coding Hub project instructions

## Architecture

- `hub.py`: agent routing, native CLI subprocesses, permissions, terminal chat.
- `dashboard.py`: authenticated loopback HTTP API and task lifecycle.
- `context_engine.py`: SQLite source retrieval, project rules, conversation checkpoints.
- `smart_route.py`: bounded manager/worker orchestration and provider-reported token metrics.
- `free_quota.py`: read-only OpenCode usage metadata, rate-limit observations, and expected UTC reset timing.
- `quota.py`: official Antigravity `/usage` integration and cached limits.
- `accounts.py`: bounded native sign-in sessions, account metadata, and model catalogs.
- `change_review.py`, `workspace_changes.py`: bounded diffs, private source checkpoints, conflict-checked apply/undo.
- `project_checks.py`, `browser_checks.py`: independent command checks, saved browser chat turns and automatic Playwright flows.
- `browser_project.py`, `tools/static_preview.py`: bounded framework discovery, loopback startup, readiness and owned-process cleanup.
- `symbol_index.py`, `model_metrics.py`, `local_resource.py`: Python symbols, observed model results, and cooperative local inference scheduling.
- `desktop_features.py`, `assets/features.js`: on-demand checks, queue and observations interfaces.
- `task_lock.py`: process-lifetime workspace locking and legacy stale-lock recovery.
- `terminal_ui.py`: terminal commands, formatting, and progress presentation.
- `system_diagnostics.py`: bounded, read-only host checks without model requests.
- `message_format.py`: safe Markdown presentation for native and terminal replies.
- `desktop.py`: native GTK 4 application and project/chat navigation.
- `assets/desktop.css`: native GTK theme.
- `tools/check_desktop_ui.py`: isolated native layout and interaction validation.
- `assets/`: browser interface, with no build step or third-party scripts.

## Development commands

Run `python3 tools/check_all.py` for the combined local test workflow, logs, and native screenshots. Use `--no-gui` when GTK or a test display is unavailable.
Run `python3 -m unittest discover -p 'test_*.py' -v` for the test suite.
Run `node --check assets/app.js` and `node --check assets/features.js` after JavaScript changes.
Run `npm run test:browser` for the real browser regression suite after installing optional Playwright development dependencies.
Start the browser interface with `python3 hub.py web`.
Start the native Linux interface with `python3 hub.py app`.

## Constraints

- Keep the CLI and browser backend compatible with Python 3.10+. Core routing uses the standard library; sign-in terminal rendering uses bundled, unmodified libraries in `vendor/`.
- Preserve existing files, unrelated work, model installations, and user agent settings.
- Keep HTTP bound to loopback and require authentication for every API endpoint.
- Never publish credentials, account details, task logs, source indexes, or session tokens.
- Use native provider sign-in and official quota reporting. Do not proxy account tokens.
- Verify free-model prices at execution time and keep credit overages disabled.
- Keep context packets bounded and pinned requirements intact. Report partial index coverage honestly.
- Preserve project and conversation isolation across all interfaces.

## Working method

Inspect the relevant module, implement a focused change, and run meaningful checks.
Validate a real local workflow when changing agent execution or UI behavior.
Report actual test results and remaining limitations. Keep documentation accurate.
