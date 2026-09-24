#!/usr/bin/env python3
"""
status_dashboard.py — lightweight live terminal status for the self-hosted
deployment. Covers exactly five things, each a real, verified data source,
not a guess:

  - archer.service       systemctl is-active / ActiveEnterTimestamp
  - AI chain              the most recent "[AI] <provider> ..." line in
                          archer.service's own journal — which provider
                          actually answered the last real request, not
                          which one is merely configured
  - caddy                 systemctl is-active / ActiveEnterTimestamp
  - disk space            shutil.disk_usage("/") — stdlib, no subprocess
  - Restic                the latest real snapshot's own timestamp, via
                          `restic snapshots --latest 1`, not just "the
                          backup service last exited 0"

Run as the `archer` user — needs no sudo. Journal read access comes from
archer already being in the `adm` group; the Restic repo and password
file (/home/archer/backup-repo, /etc/archer/restic-password) are already
owned by/readable by this user, since restic-backup.service runs as
archer too; systemctl status queries and disk usage need no privilege at
all.

    python3 self-host/status_dashboard.py            # live, refreshes every 5s
    python3 self-host/status_dashboard.py --once     # one frame, then exit

Ctrl+C to exit the live view.
"""
import argparse
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone

from rich.console import Console
from rich.live import Live
from rich.table import Table

RESTIC_REPOSITORY    = "/home/archer/backup-repo"
RESTIC_PASSWORD_FILE = "/etc/archer/restic-password"
REFRESH_SECS = 5


def _run(cmd, timeout=8, extra_env=None):
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.stdout.strip(), r.returncode
    except Exception:
        return "", 1


def _ago(dt):
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 0:
        secs = 0
    if secs < 60:
        return f"{secs}s ago"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 48:
        return f"{hours}h {mins % 60}m ago"
    days = hours // 24
    return f"{days}d {hours % 24}h ago"


def _service_state(name):
    """(state_text, since_text) for a systemd unit — real systemctl
    queries, no privilege needed to read them."""
    active, _ = _run(["systemctl", "is-active", name])
    ts, _ = _run(["systemctl", "show", name, "-p", "ActiveEnterTimestamp", "--value"])
    since = "—"
    if ts and ts != "n/a":
        try:
            dt = datetime.strptime(ts, "%a %Y-%m-%d %H:%M:%S %Z").replace(tzinfo=timezone.utc)
            since = _ago(dt)
        except ValueError:
            since = ts
    return active, since


def _last_ai_provider():
    """The real last-used AI provider, parsed from archer.service's own
    journal — not the AI chain's config, the actual last answer."""
    out, rc = _run([
        "journalctl", "-u", "archer.service", "-o", "short-iso", "--no-pager",
        "-g", r"^\[AI\]", "-n", "1", "--reverse",
    ], timeout=10)
    if rc != 0 or not out:
        return "no data yet", ""
    try:
        ts_str, rest = out.split(" ", 1)
        line = rest.split(": ", 1)[1] if ": " in rest else rest
        text = line[len("[AI] "):] if line.startswith("[AI] ") else line
        dt = datetime.fromisoformat(ts_str).astimezone(timezone.utc)
        return text, _ago(dt)
    except Exception:
        return out, ""


def _disk_usage(path="/"):
    total, used, _free = shutil.disk_usage(path)
    gb = 1024 ** 3
    pct = used / total * 100 if total else 0
    return f"{used/gb:.0f}G / {total/gb:.0f}G ({pct:.0f}%)"


def _restic_last_backup():
    """The latest snapshot's own recorded time — proof a backup actually
    completed, not just that the timer fired."""
    out, rc = _run(
        ["restic", "snapshots", "--latest", "1", "--json"],
        timeout=15,
        extra_env={"RESTIC_REPOSITORY": RESTIC_REPOSITORY,
                   "RESTIC_PASSWORD_FILE": RESTIC_PASSWORD_FILE},
    )
    if rc != 0 or not out:
        return "no snapshot found", ""
    try:
        import json
        snaps = json.loads(out)
        if not snaps:
            return "no snapshot found", ""
        dt = datetime.fromisoformat(snaps[-1]["time"]).astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC"), _ago(dt)
    except Exception:
        return "restic error", ""


def _dot(is_active):
    return "[green]●[/green]" if is_active == "active" else "[red]●[/red]"


def render():
    table = Table(title="Archer — self-hosted status", title_style="bold cyan", expand=False)
    table.add_column("Component", style="bold")
    table.add_column("Status")
    table.add_column("Detail")

    archer_state, archer_since = _service_state("archer.service")
    table.add_row("archer.service", f"{_dot(archer_state)} {archer_state}", f"since {archer_since}")

    provider, provider_age = _last_ai_provider()
    detail = f"{provider_age}" if provider_age else ""
    table.add_row("AI chain (last used)", provider, detail)

    caddy_state, caddy_since = _service_state("caddy")
    table.add_row("caddy", f"{_dot(caddy_state)} {caddy_state}", f"since {caddy_since}")

    table.add_row("disk (/)", _disk_usage("/"), "")

    restic_when, restic_age = _restic_last_backup()
    table.add_row("Restic last backup", restic_when, restic_age)

    return table


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Render one frame and exit (for scripting/testing).")
    parser.add_argument("--interval", type=float, default=REFRESH_SECS, help=f"Refresh interval in seconds (default {REFRESH_SECS}).")
    args = parser.parse_args()

    console = Console()
    if args.once:
        console.print(render())
        return

    with Live(render(), console=console, refresh_per_second=1, screen=False) as live:
        try:
            while True:
                time.sleep(args.interval)
                live.update(render())
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
