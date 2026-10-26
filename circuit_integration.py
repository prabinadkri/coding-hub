"""Project-scoped Circuit Studio CLI integration; no changes to agent permissions."""
import json
from pathlib import Path
import subprocess
import sys

CONFIG = 'circuit-studio.json'
GUIDE = '''# Circuit design with Coding Hub

This project is connected to Circuit Studio. Enable **Allow edits & commands**
when asking an agent to create or edit circuits; analysis-only mode cannot run tools.

Use `codehub circuit catalog --project .` to discover real component types.
Read the configured Studio's `docs/AUTOMATION.md` for the version 1 JSON plan format.
Create a plan JSON in this project, then run:

```
codehub circuit new --project . --file divider.circuit --plan plan.json
codehub circuit inspect --project . --file divider.circuit
codehub circuit apply --project . --file divider.circuit --plan edit.json --expected-revision SHA256_FROM_INSPECT
codehub circuit simulate --project . --file divider.circuit --steps 1000
codehub circuit check --project . --file divider.circuit
codehub circuit svg --project . --file divider.circuit --output divider.svg
```

Pins use zero-based engine indices, not physical package numbers. Inspect before
wiring or changing properties. Use exact catalog types and report model limits.
Plans are atomic; never bypass revision checks or hand-edit the XML schematic.
Run electrical checks and verify expected voltages/currents after each change.
Do not claim MCU firmware was simulated by the analog `simulate` command.
Open the resulting .circuit in Circuit Studio to edit graphically. Save and close
an editor's unsaved work before changing its file, then reopen the generated result.
Do not overwrite unrelated projects, existing exports or the integration config.
'''


def inside(project, name):
    path = Path(name)
    if path.is_absolute():
        raise ValueError('Circuit paths must be relative to the project')
    path = project / path
    if path.is_symlink() or not path.resolve().is_relative_to(project):
        raise ValueError('Circuit path leaves the project or is a symlink')
    return path


def initialize(project, studio):
    project = Path(project).expanduser().resolve(strict=True)
    studio = Path(studio).expanduser().resolve(strict=True)
    if not project.is_dir() or not (studio / 'circuitctl.py').is_file() or not (studio / 'docs/AUTOMATION.md').is_file():
        raise ValueError('Choose an existing project and a Circuit Studio installation with automation support')
    config = project / CONFIG
    guide = project / 'CIRCUITS.md'
    rules = project / 'CODING_HUB.md'
    if config.exists() or config.is_symlink() or guide.exists() or guide.is_symlink() or rules.is_symlink():
        raise ValueError('Integration files already exist or guidance is a symlink; existing files were preserved')
    prior = rules.read_text() if rules.exists() else '# Project instructions\n'
    addition = '\n\n## Circuit Studio\nFor circuit design tasks, read CIRCUITS.md and circuit-studio.json. Use the structured circuit commands, inspect revisions before editing, and report validation results.\n'
    # Explicit init is the only operation that installs project guidance.
    config.write_text(json.dumps({'version': 1, 'studio': str(studio)}, indent=2) + '\n')
    guide.write_text(GUIDE)
    rules.write_text(prior + addition)
    return {'project': str(project), 'studio': str(studio), 'created': [CONFIG, 'CIRCUITS.md'], 'guidance': 'CODING_HUB.md'}


def command(project, operation, file=None, plan=None, expected_revision=None, output=None, steps=1000, time_step=5e-6):
    project = Path(project).expanduser().resolve(strict=True)
    config = inside(project, CONFIG)
    data = json.loads(config.read_text())
    if data.get('version') != 1 or not isinstance(data.get('studio'), str):
        raise ValueError('Unsupported Circuit Studio configuration')
    studio = Path(data['studio']).expanduser().resolve(strict=True)
    if not (studio / 'circuitctl.py').is_file():
        raise ValueError('Circuit Studio installation is unavailable; update circuit-studio.json')
    if operation not in ('catalog', 'new', 'apply', 'inspect', 'check', 'simulate', 'svg', 'kicad'):
        raise ValueError('Unsupported circuit operation')
    args = [sys.executable, str(studio / 'circuitctl.py'), operation]
    if operation != 'catalog':
        if not file:
            raise ValueError('--file is required')
        args.extend(['--project', str(inside(project, file))])
    if plan:
        args.extend(['--plan', str(inside(project, plan))])
    if output:
        args.extend(['--output', str(inside(project, output))])
    if expected_revision:
        args.extend(['--expected-revision', expected_revision])
    if operation == 'simulate':
        args.extend(['--steps', str(steps), '--time-step', str(time_step)])
    return args


def run(args):
    try:
        if args.operation == 'init':
            if not args.studio:
                raise ValueError('init requires --studio /path/to/circuit-studio')
            print(json.dumps(initialize(args.project, args.studio), indent=2))
            return 0
        cmd = command(args.project, args.operation, args.file, args.plan, args.expected_revision, args.output, args.steps, args.time_step)
        return subprocess.run(cmd, cwd=Path(args.project).expanduser().resolve(), timeout=90, check=False).returncode
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired) as error:
        print(json.dumps({'error': str(error)}), file=sys.stderr)
        return 2
