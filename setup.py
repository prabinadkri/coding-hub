#!/usr/bin/env python3
"""Finish local setup without paid accounts or third-party Python packages."""
import argparse
import ast
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import time
import urllib.request
import hub


def configure():
    launcher = Path.home() / ".local/bin/codehub"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    # A shell launcher resolves this installed folder; it also works from other projects.
    import shlex
    launcher.write_text("#!/bin/sh\nexec python3 " + shlex.quote(str(hub.ROOT / "hub.py")) + ' "$@"\n')
    launcher.chmod(0o755)
    print("Installed launcher:", launcher)
    if sys.platform.startswith("linux"):
        icon = Path.home() / ".local/share/icons/hicolor/scalable/apps/coding-hub.svg"
        icon.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(hub.ROOT / "assets/icon.svg", icon)
        app_id = 'io.github.prabinadkri.CodingHub'
        shutil.copy2(hub.ROOT / 'assets/icon.svg', icon.with_name(app_id + '.svg'))
        application = Path.home() / ('.local/share/applications/' + app_id + '.desktop')
        application.parent.mkdir(parents=True, exist_ok=True)
        escaped_path = str(launcher).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
        application.write_text('[Desktop Entry]\nType=Application\nName=Coding Hub\n'
                               'Comment=Cloud coding agents and local Qwen in one launcher\n'
                               f'Exec="{escaped_path}" app\nTerminal=false\nIcon={app_id}\n'
                               'Categories=Development;\nStartupNotify=false\n'
                               'StartupWMClass=io.github.prabinadkri.CodingHub\nActions=Web;Terminal;\n\n'
                               '[Desktop Action Web]\nName=Open in browser\n'
                               f'Exec="{escaped_path}" web\n\n'
                               '[Desktop Action Terminal]\nName=Open terminal menu\n'
                               f'Exec=x-terminal-emulator -e "{escaped_path}"\n')
        application.chmod(0o755)
        legacy = application.with_name('prabin-coding-hub.desktop')
        if legacy.is_file() and 'Name=Coding Hub\n' in legacy.read_text():
            # Keep an old shortcut working while hiding its duplicate menu entry.
            legacy.write_text(application.read_text().replace('[Desktop Entry]\n', '[Desktop Entry]\nNoDisplay=true\n', 1))
            settings_tool = shutil.which('gsettings')
            if settings_tool:
                try:
                    result = subprocess.run([settings_tool, 'get', 'org.gnome.shell', 'favorite-apps'],
                                            capture_output=True, text=True, timeout=5)
                    favorites = ast.literal_eval(result.stdout)
                    if isinstance(favorites, list) and 'prabin-coding-hub.desktop' in favorites:
                        replacement = [app_id + '.desktop' if item == 'prabin-coding-hub.desktop' else item for item in favorites]
                        subprocess.run([settings_tool, 'set', 'org.gnome.shell', 'favorite-apps', repr(replacement)], check=True, timeout=5)
                except (ValueError, SyntaxError, OSError, subprocess.SubprocessError):
                    print('Existing dock favorite could not be updated automatically. Pin the new Coding Hub entry from Applications.')
        updater = shutil.which('update-desktop-database')
        if updater:
            subprocess.run([updater, str(application.parent)], capture_output=True)
        print("Added Coding Hub and its icon to the applications menu.")
        updater = shutil.which("gtk-update-icon-cache")
        if updater:
            subprocess.run([updater, "-f", "-t", str(icon.parents[2])], capture_output=True)
    # Change only our overage toggle; preserve any existing Antigravity preferences.
    settings = Path.home() / ".gemini/antigravity-cli/settings.json"
    if settings.exists():
        try:
            current = json.loads(settings.read_text())
        except (ValueError, OSError):
            print("Existing Antigravity settings are not JSON. Left them unchanged; set AI credit overages to Never.")
        else:
            if not isinstance(current, dict):
                raise ValueError("Antigravity settings must be a JSON object.")
            if current.get("modelProvider") == "gemini":
                print("Antigravity is configured for API-key mode. Switch it to native Google sign-in before using the cloud route.")
            current["useG1Credits"] = False
            backup = settings.with_name("settings.before-coding-hub.json")
            if not backup.exists():
                backup.write_bytes(settings.read_bytes())
                backup.chmod(0o600)
            hub.save_json(settings, current)
    else:
        hub.save_json(settings, {"useG1Credits": False})
    print("Antigravity credit overages disabled in CLI settings.")


def local_setup():
    ollama = hub.executable("ollama")
    if not ollama:
        print("Install Ollama from https://ollama.com/download first.")
        return 2
    try:
        installed = hub.local_models()
    except Exception:
        print("Ollama is not running. Start the Ollama application/service first.")
        return 2
    if hub.LOCAL_MODEL not in installed:
        print("Downloading Qwen3 8B (about 5.2 GB). This may take time on your connection.")
        last_error = None
        for attempt in range(1, 4):
            try:
                request = urllib.request.Request("http://127.0.0.1:11434/api/pull",
                    data=json.dumps({"model": hub.LOCAL_MODEL, "stream": True}).encode(),
                    headers={"Content-Type": "application/json"})
                last_bucket = -1
                complete = False
                with urllib.request.urlopen(request, timeout=90) as response:
                    for line in response:
                        event = json.loads(line)
                        if "error" in event:
                            raise RuntimeError(event["error"])
                        total = event.get("total", 0)
                        downloaded = event.get("completed", 0)
                        progress = int(downloaded * 100 / total) if total else None
                        if progress is not None and progress // 5 != last_bucket:
                            print(f"Qwen download: {progress}% ({downloaded / 1e9:.2f}/{total / 1e9:.2f} GB)", flush=True)
                            last_bucket = progress // 5
                        if event.get("status") == "success":
                            complete = True
                        hub.save_json(hub.STATE / "setup.json", {"status": event.get("status"),
                            "model": hub.LOCAL_MODEL, "percent": progress, "updated_at": time.time()})
                if not complete:
                    raise RuntimeError("Download ended before success; partial data will be resumed.")
                break
            except (OSError, ValueError, RuntimeError) as error:
                last_error = error
                print(f"Download attempt {attempt}/3 stopped: {error}", flush=True)
                if attempt == 3:
                    hub.save_json(hub.STATE / "setup.json", {"status": "failed", "error": str(last_error)})
                    return 1
                time.sleep(2)
    modelfile = hub.ROOT / "Qwen8B.Modelfile"
    result = subprocess.call([ollama, "create", hub.LOCAL_AGENT_MODEL, "-f", str(modelfile)])
    if result:
        return result
    print("Local coding model ready:", hub.LOCAL_AGENT_MODEL)
    hub.save_json(hub.STATE / "setup.json", {"status": "ready", "model": hub.LOCAL_AGENT_MODEL,
                                            "updated_at": time.time()})
    print("16K context uses less memory than a full 64K agent context. Use cloud for large repositories.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true")
    args = parser.parse_args()
    configure()
    return local_setup() if args.local else 0


if __name__ == "__main__":
    sys.exit(main())
