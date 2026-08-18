"""Cloudflared tunnel control (start/stop/status) from the dashboard."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

from .settings import settings

log = logging.getLogger("analytics.tunnel")
_proc: asyncio.subprocess.Process | None = None
_reader_task: asyncio.Task | None = None

# Кэш детекта внешнего cloudflared-процесса (туннель, запущенный НЕ из
# дашборда — напр. start_services.bat или службой). Опрос процессов ОС
# недешёв (PowerShell ~0.3-1с), поэтому кэшируем на 10с.
_EXT_DETECT_TTL = 10.0
_ext_cache = {"ts": 0.0, "running": False, "pids": []}


def _cf_dir() -> Path:
    return Path.home() / ".cloudflared"


def find_bin() -> str | None:
    if settings.cloudflared_bin:
        return settings.cloudflared_bin
    found = shutil.which("cloudflared")
    if found:
        return found
    cand = _cf_dir() / "cloudflared.exe"
    return str(cand) if cand.exists() else None


def config_path() -> Path | None:
    if settings.tunnel_config:
        return Path(settings.tunnel_config)
    if not settings.tunnel_name:
        return None
    cand = _cf_dir() / f"config-{settings.tunnel_name}.yml"
    return cand if cand.exists() else None


def _detect_external() -> dict:
    """Ищет cloudflared-процесс ЭТОГО туннеля, запущенный вне дашборда.

    В хост-проекте (Агент Норм) туннель поднимается start_services.bat
    (``cloudflared.exe tunnel run standards-parser``) или службой — дашборд
    им не управляет, но должен видеть его статус. Матчим имя туннеля в
    командной строке процесса (как это делает start_services.bat). Результат
    кэшируется на ``_EXT_DETECT_TTL`` сек — опрос процессов ОС недешёв.

    Returns:
        ``{"running": bool, "pids": list[int]}``.
    """
    now = time.monotonic()
    if (now - _ext_cache["ts"]) < _EXT_DETECT_TTL and _ext_cache["ts"] > 0:
        return {"running": _ext_cache["running"], "pids": list(_ext_cache["pids"])}
    result = {"running": False, "pids": []}
    name = (settings.tunnel_name or "").strip()
    if name:
        try:
            if os.name == "nt":
                # Windows: PowerShell CIM по командной строке (tasklist её не отдаёт).
                ps = (
                    "Get-CimInstance Win32_Process -Filter \"Name='cloudflared.exe'\" | "
                    f"Where-Object {{ $_.CommandLine -match '{name}' }} | "
                    "Select-Object -ExpandProperty ProcessId"
                )
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps],
                    capture_output=True, text=True, timeout=3,
                )
            else:
                # Unix: pgrep по полной командной строке.
                out = subprocess.run(
                    ["pgrep", "-f", f"cloudflared.*{name}"],
                    capture_output=True, text=True, timeout=3,
                )
            pids = [int(x) for x in out.stdout.split() if x.isdigit()]
            result = {"running": bool(pids), "pids": pids}
        except FileNotFoundError:
            pass  # powershell/pgrep не найден — только прямое управление дашборда
        except Exception:
            log.debug("tunnel: не удалось опросить процессы ОС", exc_info=True)
    _ext_cache.update(ts=now, running=result["running"], pids=result["pids"])
    return result


def status() -> dict:
    bin_ = find_bin()
    cfg = config_path()
    dashboard_running = _proc is not None and _proc.returncode is None
    external = _detect_external()
    running = dashboard_running or external["running"]
    pid = _proc.pid if dashboard_running else (external["pids"][0] if external["pids"] else None)
    return {"configured": bool(settings.tunnel_name), "installed": bin_ is not None,
            "config_exists": cfg is not None and cfg.exists(), "bin": bin_,
            "config": str(cfg) if cfg else None, "running": running,
            "pid": pid,
            "managed_by_dashboard": dashboard_running,
            "external_pids": external["pids"],
            "tunnel_name": settings.tunnel_name, "public_host": settings.tunnel_public_host}


async def _read_output(proc):
    assert proc.stdout
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            log.info("[tunnel] %s", line.decode(errors="replace").rstrip())
    except Exception:
        pass


async def start() -> dict:
    global _proc, _reader_task
    if _proc is not None and _proc.returncode is None:
        return {"ok": True, "msg": "уже запущен", **status()}
    if not settings.tunnel_name:
        return {"ok": False, "msg": "TUNNEL_NAME не задан", **status()}
    bin_ = find_bin()
    cfg = config_path()
    if not bin_:
        return {"ok": False, "msg": "cloudflared не найден", **status()}
    if not cfg or not cfg.exists():
        return {"ok": False, "msg": "конфиг туннеля не найден", **status()}
    try:
        _proc = await asyncio.create_subprocess_exec(
            bin_, "tunnel", "--config", str(cfg), "run", settings.tunnel_name,
            cwd=str(_cf_dir()), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        _reader_task = asyncio.create_task(_read_output(_proc))
        log.warning("Туннель запущен: pid=%s → https://%s", _proc.pid, settings.tunnel_public_host)
        return {"ok": True, "msg": "запущен", **status()}
    except Exception as e:
        log.exception("start tunnel failed")
        return {"ok": False, "msg": str(e), **status()}


async def stop() -> dict:
    global _proc, _reader_task
    if _proc is None or _proc.returncode is not None:
        _proc = None
        return {"ok": True, "msg": "не был запущен", **status()}
    pid = _proc.pid
    try:
        _proc.terminate()
        try:
            await asyncio.wait_for(_proc.wait(), timeout=8)
        except asyncio.TimeoutError:
            _proc.kill()
        log.warning("Туннель остановлен (pid=%s)", pid)
    except Exception:
        log.exception("stop tunnel failed")
    _proc = None
    return {"ok": True, "msg": "остановлен", **status()}
