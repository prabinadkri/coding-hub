# Coding Hub project instructions

## Architecture

- `hub.py`: agent routing, native CLI subprocesses, permissions, terminal chat.
- `dashboard.py`: authenticated loopback HTTP API and task lifecycle.
- `context_engine.py`: SQLite source retrieval, project rules, conversation checkpoints.
- `smart_route.py`: bounded manager/worker orchestration and provider-reported token metrics.
- `quota.py`: official Antigravity `/usage` integration and cached limits.
- `desktop.py`: native GTK 4 application and project/chat navigation.
- `assets/`: browser interface, with no build step or third-party scripts.

## Development commands

Run `python3 -m unittest discover -p 'test_*.py' -v` for the test suite.
Run `node --check assets/app.js` after JavaScript changes.
Start the browser interface with `python3 hub.py web`.
Start the native Linux interface with `python3 hub.py app`.

## Constraints

- Keep the CLI and browser backend compatible with Python 3.10+ and the standard library.
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
