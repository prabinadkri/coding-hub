"""Bounded, read-only local diagnostics. No root, network probes, or model calls."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
from terminal_ui import safe_text

CHECKS = {
    'gpu': ('nvidia-smi','--query-gpu=name,driver_version,memory.used,memory.total,utilization.gpu','--format=csv,noheader,nounits'),
    'clock': ('timedatectl','show','--property=NTP','--property=NTPSynchronized','--property=Timezone'),
    'services': ('systemctl','--failed','--no-legend','--plain','--no-pager'),
    'user_services': ('systemctl','--user','--failed','--no-legend','--plain','--no-pager'),
    'secure_boot': ('mokutil','--sb-state'),
}


def command(args):
    if not shutil.which(args[0]): return {'ok':False,'text':'Command not installed'}
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=4,
                                env=dict(os.environ, LC_ALL='C', SYSTEMD_COLORS='0'))
        return {'ok':result.returncode == 0,'text':safe_text(result.stdout or result.stderr).strip()[:2500]}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {'ok':False,'text':type(error).__name__}


def report():
    checks = []
    def add(name, state, detail): checks.append({'name':name,'state':state,'detail':safe_text(detail)[:2500]})
    add('System','info',platform.system()+' '+platform.release()+' · '+platform.machine())
    for label, path in (('System disk','/'),('Home disk',str(Path.home()))):
        try:
            disk=shutil.disk_usage(path); percent=100*disk.used/max(1,disk.total)
            add(label,'attention' if percent >= 90 else 'ok',f'{disk.free/1024**3:.1f} GB free of {disk.total/1024**3:.1f} GB · {percent:.0f}% used')
        except OSError: add(label,'unavailable','Could not read disk usage')
    try:
        values={line.split(':')[0]:int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith(('MemTotal:','MemAvailable:','SwapTotal:','SwapFree:'))}
        available,total=values['MemAvailable'],values['MemTotal']
        add('Memory','attention' if available/max(1,total)<.08 else 'ok',f'{available/1024**2:.1f} GB available of {total/1024**2:.1f} GB · swap used {(values.get("SwapTotal",0)-values.get("SwapFree",0))/1024**2:.1f} GB')
    except (OSError,ValueError,KeyError): add('Memory','unavailable','Linux memory counters unavailable')
    if hasattr(os,'getloadavg'):
        loads=os.getloadavg();cpus=os.cpu_count() or 1
        add('CPU load','attention' if loads[0]>cpus*2 else 'info',f'{loads[0]:.2f}, {loads[1]:.2f}, {loads[2]:.2f} over 1/5/15 minutes · {cpus} logical CPUs')
    if platform.system() == 'Linux':
        with ThreadPoolExecutor(max_workers=len(CHECKS)) as pool:
            results=dict(zip(CHECKS,pool.map(command,CHECKS.values())))
        gpu=results['gpu']
        add('NVIDIA GPU','ok' if gpu['ok'] else 'unavailable',(gpu['text']+'\nFields: name, driver, used MB, total MB, utilization %') if gpu['ok'] else gpu['text'])
        clock=results['clock']
        add('Clock synchronization','ok' if clock['ok'] and 'NTPSynchronized=yes' in clock['text'] else 'attention' if clock['ok'] else 'unavailable',clock['text'])
        for key,title in (('services','System services'),('user_services','User services')):
            item=results[key]
            add(title,('attention' if item['text'] else 'ok') if item['ok'] else 'unavailable', item['text'] or 'No failed units reported')
        boot=results['secure_boot'];add('Secure Boot','info' if boot['ok'] else 'unavailable',boot['text'])
    else:
        add('Linux checks','unavailable','GPU, systemd, and Secure Boot checks apply to a Linux host')
    stamp=time.time()
    text='System check · '+datetime.fromtimestamp(stamp).astimezone().strftime('%Y-%m-%d %H:%M %Z')+'\nRead-only snapshot; no settings changed.\n\n'
    text+='\n\n'.join(f"[{item['state'].upper()}] {item['name']}\n{item['detail']}" for item in checks)
    text+='\n\nUnavailable checks are not diagnoses. Describe the symptoms to investigate further.'
    return {'checked_at':stamp,'checks':checks,'text':text}


def prompt(text):
    text = text.encode('utf-8')[:4800].decode('utf-8', errors='ignore')
    return 'Help me diagnose my Linux laptop. Explain these observations and ask about symptoms when needed. Start with read-only checks; propose exact fixes for review before changing the system.\n\n'+text
