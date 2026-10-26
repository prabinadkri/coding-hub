"""Project-scoped Circuit Studio CLI integration; no changes to agent permissions."""
import json
import os
import tempfile
import webbrowser
from urllib.parse import quote
from pathlib import Path
import subprocess
import sys

CONFIG = 'circuit-studio.json'
GUIDE = '''# Circuit design with Coding Hub

This project is connected to Circuit Studio. Enable **Allow edits & commands**
when asking an agent to create or edit circuits; analysis-only mode cannot run tools.

Use `codehub circuit catalog --project .` to discover real component types.
Read this project's `CIRCUIT_API.md` for the version 1 JSON plan format.
Start from `circuit-example.json` when that example is supplied by the installation.
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
When design.circuit already exists, inspect it and apply edits with its expected revision; preserve its project ID.
When designing from the circuit assistant, keep the finished project in design.circuit so the Open circuit and Check for result buttons can find it.
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
    api_doc = project / 'CIRCUIT_API.md'
    example = project / 'circuit-example.json'
    if any(p.exists() or p.is_symlink() for p in (config, guide, api_doc, example)) or rules.is_symlink():
        raise ValueError('Integration files already exist or guidance is a symlink; existing files were preserved')
    prior = rules.read_text() if rules.exists() else '# Project instructions\n'
    addition = '\n\n## Circuit Studio\nFor circuit design tasks, read CIRCUITS.md and circuit-studio.json. Use the structured circuit commands, inspect revisions before editing, and report validation results.\n'
    # Explicit init is the only operation that installs project guidance.
    config.write_text(json.dumps({'version': 1, 'studio': str(studio)}, indent=2) + '\n')
    guide.write_text(GUIDE)
    api_doc.write_text((studio / 'docs/AUTOMATION.md').read_text())
    sample = studio / 'examples/automation/voltage-divider.json'
    if sample.is_file():
        example.write_text(sample.read_text())
    rules.write_text(prior + addition)
    return {'project': str(project), 'studio': str(studio), 'created': [CONFIG, 'CIRCUITS.md', 'CIRCUIT_API.md'] + (['circuit-example.json'] if example.exists() else []), 'guidance': 'CODING_HUB.md'}


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


def discover_studio():
    candidate = Path(os.environ.get('CIRCUIT_STUDIO_HOME', str(Path(os.environ.get('XDG_DATA_HOME', str(Path.home()/'.local/share'))) / 'circuit-studio/current'))).expanduser()
    if not (candidate / 'circuitctl.py').is_file() or not (candidate / 'launch.py').is_file():
        raise ValueError('Install Circuit Studio first, then try New circuit again.')
    return candidate.resolve()


def workspace(project=None, studio=None):
    """Create an isolated, ready-to-chat lab; never overwrite a user's circuit."""
    studio = Path(studio).expanduser().resolve(strict=True) if studio else discover_studio()
    if project is None:
        root = Path(os.environ.get('XDG_DATA_HOME', str(Path.home()/'.local/share'))) / 'coding-hub/circuits'
        root.mkdir(parents=True, exist_ok=True)
        project = Path(tempfile.mkdtemp(prefix='Circuit-', dir=root))
    project = Path(project).expanduser().resolve(strict=True)
    if not (project / CONFIG).exists():
        initialize(project, studio)
    return workspace_info(project)


def workspace_info(project):
    project = Path(project).expanduser().resolve(strict=True)
    command(project, 'catalog')  # Validate the integration and its installation.
    has_circuit = inside(project, 'design.circuit').is_file()
    return {'project': str(project), 'hasCircuit': has_circuit,
            'prompt': ('Improve this circuit: ' if has_circuit else 'Create a circuit: '),
            'hint': ('Describe the changes you want. Return to Circuit Studio and click Check for result when finished.' if has_circuit else 'Describe your circuit. Use Open circuit when the assistant finishes.')}


def open_workspace(project):
    from desktop import ensure_server
    url = ensure_server()
    if not webbrowser.open(url + '&circuit=' + quote(str(Path(project).resolve()), safe='')):
        raise ValueError('The browser could not open. Open Coding Hub and choose this circuit workspace.')


def open_circuit(project):
    project = Path(project).expanduser().resolve(strict=True)
    # Opening a data file must not execute a launcher selected by project content.
    studio = discover_studio()
    command(project, 'inspect', 'design.circuit')
    circuit = inside(project, 'design.circuit')
    if not circuit.is_file():
        raise ValueError('No design.circuit yet. Ask the assistant to create your circuit first.')
    if circuit.stat().st_size > 10_000_000:
        raise ValueError('Circuit is larger than 10 MB')
    value = json.loads(circuit.read_text())
    if not isinstance(value, dict) or value.get('format') != 'circuit-studio' or value.get('version') != 2:
        raise ValueError('Choose a valid Circuit Studio v2 project')
    import hub
    directory = hub.private_dir(hub.STATE / 'circuit-launch')
    with (directory / 'launcher.log').open('a') as log:
        subprocess.Popen([sys.executable, str(studio/'launch.py'), '--port', '0', '--project', str(circuit)], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    return {'opened': True}


def run(args):
    try:
        if args.operation == 'workspace':
            result = workspace(args.project, args.studio)
            if args.open:
                open_workspace(result['project'])
            print(json.dumps(result))
            return 0
        if args.operation == 'init':
            if not args.studio:
                raise ValueError('init requires --studio /path/to/circuit-studio')
            print(json.dumps(initialize(args.project or os.getcwd(), args.studio), indent=2))
            return 0
        cmd = command(args.project or os.getcwd(), args.operation, args.file, args.plan, args.expected_revision, args.output, args.steps, args.time_step)
        return subprocess.run(cmd, cwd=Path(args.project or os.getcwd()).expanduser().resolve(), timeout=90, check=False).returncode
    except (OSError, ValueError, TypeError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(json.dumps({'error': str(error)}), file=sys.stderr)
        return 2
