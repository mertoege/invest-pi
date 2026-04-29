#!/usr/bin/env python3
"""
hardware_check.py — Pi-Hardware-Monitor mit Telegram-Push.

Schwellen (anpassbar via config.yaml):
  CPU >= 75°C
  Disk >= 90%
  Mem >= 90%

Bei Ueberschreitung: Telegram-Info + Lockfile in /tmp damit max 1 Alert
pro Stunde rausgeht (Spam-Schutz). Lockfile wird nach 1h ueberschrieben.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# .env laden
env_path = Path(__file__).resolve().parents[1] / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from src.alerts.notifier import send_info, is_configured


THRESHOLDS = {
    "cpu_temp_c": 75.0,
    "disk_pct":   90.0,
    "mem_pct":    90.0,
}
LOCKFILE_DIR = Path("/tmp/invest-pi-hw-locks")
LOCKFILE_DIR.mkdir(exist_ok=True)
LOCK_HOURS = 1


def _read_cpu_temp() -> float | None:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return int(f.read().strip()) / 1000.0
    except Exception:
        return None


def _read_disk_pct() -> float | None:
    try:
        out = subprocess.check_output(
            ["df", "/", "--output=pcent"], text=True
        ).strip().splitlines()[-1].strip().rstrip("%")
        return float(out)
    except Exception:
        return None


def _read_mem_pct() -> float | None:
    try:
        out = subprocess.check_output(["free"], text=True).splitlines()
        # Mem-Zeile: Mem: total used free shared buff_cache available
        for line in out:
            if line.startswith("Mem:"):
                parts = line.split()
                total = int(parts[1])
                used  = int(parts[2])
                return (used / total) * 100 if total > 0 else None
    except Exception:
        return None
    return None


def _lock_active(metric: str) -> bool:
    lock = LOCKFILE_DIR / f"{metric}.lock"
    if not lock.exists():
        return False
    age_sec = time.time() - lock.stat().st_mtime
    return age_sec < LOCK_HOURS * 3600


def _set_lock(metric: str) -> None:
    lock = LOCKFILE_DIR / f"{metric}.lock"
    lock.write_text(str(time.time()))


def main() -> int:
    if not is_configured():
        print("telegram nicht konfiguriert, hardware_check no-op")
        return 0

    metrics = {
        "cpu_temp_c": _read_cpu_temp(),
        "disk_pct":   _read_disk_pct(),
        "mem_pct":    _read_mem_pct(),
    }

    alerts = []
    for metric, value in metrics.items():
        if value is None:
            continue
        threshold = THRESHOLDS[metric]
        if value >= threshold and not _lock_active(metric):
            alerts.append((metric, value, threshold))
            _set_lock(metric)

    if not alerts:
        return 0

    hostname = subprocess.check_output(["hostname"], text=True).strip()
    parts = ["⚠️ <b>Hardware-Alert · " + hostname + "</b>"]
    for metric, value, threshold in alerts:
        label = {"cpu_temp_c": "CPU", "disk_pct": "Disk", "mem_pct": "Mem"}[metric]
        unit  = {"cpu_temp_c": "°C", "disk_pct": "%", "mem_pct": "%"}[metric]
        parts.append(f"  {label}: <b>{value:.1f}{unit}</b> (Schwelle {threshold:.0f}{unit})")
    parts.append("\n<i>Cooldown: 1h pro Metrik bevor Re-Alert.</i>")

    text = "\n".join(parts)
    ok = send_info(text, label="hardware")
    print(f"hardware-alert sent: {len(alerts)} metrics, telegram-ok={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
