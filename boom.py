#!/usr/bin/env python3
"""BooM: painel local de sistema para Linux, com GTK e modo de terminal."""

from __future__ import annotations

import argparse
import configparser
import curses
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
__version__ = "0.1.0"
UNAVAILABLE = "Indisponível"
TRAY_STARTUP_RETRY_INTERVAL_MS = 500
TRAY_STARTUP_MAX_ATTEMPTS = 20
GTK_CSS = b"""
* { font-family: system-ui, 'Ubuntu', sans-serif; }
window, .boom-root { background: #0b0d11; color: #f3f5f7; }
.sidebar { background: #101319; border-right: 1px solid #292f3a; }
.brand { color: #b9f66a; font-size: 27px; font-weight: 800; letter-spacing: 1px; }
.eyebrow { color: #929aa8; font-size: 10px; font-weight: 700; letter-spacing: 1.3px; }
.nav-button { background: transparent; color: #aeb5c0; border: 0; border-radius: 8px; padding: 11px 13px; }
.nav-button:hover, .nav-button.selected { background: #1b2029; color: #f3f5f7; }
.nav-button.selected { box-shadow: inset 3px 0 #b9f66a; }
.title { color: #f3f5f7; font-size: 25px; font-weight: 700; }
.subtitle { color: #929aa8; font-size: 12px; }
.card { background: #141820; border: 1px solid #292f3a; border-radius: 12px; padding: 18px; }
.card-title { color: #aeb5c0; font-size: 11px; font-weight: 700; letter-spacing: .8px; }
.metric { color: #f3f5f7; font-size: 25px; font-weight: 700; }
.body-copy { color: #e2e5ea; font-size: 13px; }
.muted { color: #929aa8; font-size: 12px; }
.accent { color: #b9f66a; }
treeview { background: #101319; color: #e2e5ea; }
treeview:selected { background: #28331d; color: #f3f5f7; }
treeview header button { background: #1b2029; color: #aeb5c0; border-color: #292f3a; }
entry { background: #101319; color: #f3f5f7; border: 1px solid #292f3a; border-radius: 8px; padding: 8px; }
button.action { background: #b9f66a; color: #101319; border: 0; border-radius: 8px; padding: 9px 14px; font-weight: 700; }
button.action:hover { background: #c9ff7c; }
progressbar trough { background: #292f3a; border-radius: 5px; min-height: 8px; }
progressbar progress { background: #b9f66a; border-radius: 5px; }
scrollbar slider { background: #3b424e; }
"""


def read_text(path: str | Path) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return None


def run_command(args: list[str], timeout: float = 3.0) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def cpu_model() -> str:
    text = read_text("/proc/cpuinfo") or ""
    for line in text.splitlines():
        if line.lower().startswith(("model name", "hardware", "processor")) and ":" in line:
            value = line.split(":", 1)[1].strip()
            if value and not value.isdigit():
                return value
    return UNAVAILABLE


def cpu_usage() -> float | None:
    def totals() -> tuple[int, int] | None:
        text = read_text("/proc/stat")
        if not text:
            return None
        first = text.splitlines()[0].split()
        if not first or first[0] != "cpu":
            return None
        values = [int(value) for value in first[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle

    before = totals()
    if before is None:
        return None
    time.sleep(0.14)
    after = totals()
    if after is None:
        return None
    total_delta = after[0] - before[0]
    idle_delta = after[1] - before[1]
    if total_delta <= 0:
        return None
    return max(0.0, min(100.0, (total_delta - idle_delta) * 100 / total_delta))


def memory_info() -> dict[str, int] | None:
    text = read_text("/proc/meminfo")
    if not text:
        return None
    values: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"^(\w+):\s+(\d+)", line)
        if match:
            values[match.group(1)] = int(match.group(2)) * 1024
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None or available is None:
        return None
    return {"total": total, "available": available, "used": total - available}


def os_info() -> dict[str, str]:
    values: dict[str, str] = {}
    text = read_text("/etc/os-release") or ""
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    return {
        "name": values.get("PRETTY_NAME", platform.system()),
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "hostname": socket.gethostname(),
    }


def uptime_seconds() -> float | None:
    text = read_text("/proc/uptime")
    try:
        return float(text.split()[0]) if text else None
    except (ValueError, IndexError):
        return None


def disks_info() -> list[dict[str, Any]]:
    result = run_command(["df", "-B1", "-P", "-T"], timeout=4.0)
    if result is None or result.returncode != 0:
        return []
    disks = []
    pattern = re.compile(r"^(\S+)\s+(\S+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)%\s+(.+)$")
    for line in result.stdout.splitlines()[1:]:
        match = pattern.match(line.strip())
        if not match:
            continue
        device, filesystem, size, used, available, percent, mount = match.groups()
        if mount.startswith("/snap/") or filesystem in {"squashfs", "tmpfs", "devtmpfs", "efivarfs"}:
            continue
        disks.append({
            "device": device.replace("\\040", " "),
            "filesystem": filesystem,
            "size": int(size),
            "used": int(used),
            "available": int(available),
            "percent": int(percent),
            "mount": mount.replace("\\040", " "),
        })
    return disks


def gpu_info() -> list[str]:
    result = run_command(["lspci", "-nn"], timeout=3.0)
    if result is None or result.returncode != 0:
        return []
    return [
        line.split(": ", 1)[1].strip() if ": " in line else line.strip()
        for line in result.stdout.splitlines()
        if re.search(r"(VGA compatible controller|3D controller|Display controller)", line, re.I)
    ]


def temperature_info() -> list[dict[str, Any]]:
    temperatures: list[dict[str, Any]] = []
    for hwmon in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        chip = (read_text(hwmon / "name") or hwmon.name).strip()
        for input_path in sorted(hwmon.glob("temp*_input")):
            raw = read_text(input_path)
            try:
                celsius = int(raw.strip()) / 1000 if raw else None
            except ValueError:
                celsius = None
            if celsius is None or celsius < -20 or celsius > 150:
                continue
            label_path = input_path.with_name(input_path.name.replace("_input", "_label"))
            label = (read_text(label_path) or input_path.stem.replace("_input", "")).strip()
            temperatures.append({"name": f"{chip} · {label}", "celsius": round(celsius, 1)})
    if temperatures:
        return temperatures

    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp")):
        raw = read_text(zone)
        type_name = (read_text(zone.parent / "type") or zone.parent.parent.name).strip()
        try:
            celsius = int(raw.strip()) / 1000 if raw else None
        except ValueError:
            celsius = None
        if celsius is not None and -20 <= celsius <= 150:
            temperatures.append({"name": type_name, "celsius": round(celsius, 1)})
    if temperatures:
        return temperatures

    result = run_command(["sensors", "-j"], timeout=3.0)
    if result is not None and result.returncode == 0:
        try:
            payload = json.loads(result.stdout)
            def visit(node: Any, prefix: str = "") -> None:
                if isinstance(node, dict):
                    for key, value in node.items():
                        if isinstance(value, (int, float)) and "input" in key.lower() and "temp" in prefix.lower():
                            temperatures.append({"name": prefix or "Sensor", "celsius": round(float(value), 1)})
                        else:
                            visit(value, f"{prefix} · {key}".strip(" ·"))
                elif isinstance(node, list):
                    for value in node:
                        visit(value, prefix)
            visit(payload)
        except (json.JSONDecodeError, TypeError):
            pass
    return temperatures


def default_gateway() -> str | None:
    text = read_text("/proc/net/route") or ""
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[1] != "00000000":
            continue
        try:
            flags = int(fields[3], 16)
            raw = bytes.fromhex(fields[2])
            if flags & 0x2 and len(raw) == 4:
                return socket.inet_ntoa(raw[::-1])
        except (ValueError, OSError):
            continue
    return None


def network_info() -> dict[str, Any]:
    interfaces: list[dict[str, Any]] = []
    ip_result = run_command(["ip", "-j", "-4", "addr", "show"], timeout=3.0)
    ip_addresses: dict[str, list[str]] = {}
    if ip_result is not None and ip_result.returncode == 0:
        try:
            for item in json.loads(ip_result.stdout):
                ip_addresses[item.get("ifname", "")] = [
                    address.get("local", "") for address in item.get("addr_info", [])
                    if address.get("family") == "inet"
                ]
        except (json.JSONDecodeError, TypeError):
            pass
    try:
        interface_names = socket.if_nameindex()
    except OSError:
        interface_names = []
        for interface_path in sorted(Path("/sys/class/net").iterdir()):
            raw_index = read_text(interface_path / "ifindex")
            try:
                index = int(raw_index.strip()) if raw_index else len(interface_names) + 1
            except ValueError:
                index = len(interface_names) + 1
            interface_names.append((index, interface_path.name))
    for index, name in interface_names:
        operstate = (read_text(f"/sys/class/net/{name}/operstate") or "").strip()
        interfaces.append({
            "name": name,
            "index": index,
            "addresses": ip_addresses.get(name, []),
            "state": "Ativa" if name == "lo" or operstate == "up" else "Inativa",
        })
    return {"interfaces": interfaces, "gateway": default_gateway()}


def parse_proc_stat(text: str) -> dict[str, Any] | None:
    opening = text.find("(")
    closing = text.rfind(")")
    if opening < 0 or closing <= opening:
        return None
    try:
        pid = int(text[:opening].strip())
        name = text[opening + 1:closing]
        fields = text[closing + 2:].split()
        if len(fields) < 22:
            return None
        return {
            "pid": pid,
            "comm": name,
            "state_code": fields[0],
            "utime": int(fields[11]),
            "stime": int(fields[12]),
            "start_ticks": int(fields[19]),
            "rss_pages": max(0, int(fields[21])),
        }
    except (ValueError, IndexError):
        return None


def read_process_record(pid: int) -> dict[str, Any] | None:
    process_dir = Path("/proc") / str(pid)
    raw_stat = read_text(process_dir / "stat")
    stat = parse_proc_stat(raw_stat) if raw_stat else None
    if stat is None:
        return None
    status = read_text(process_dir / "status") or ""
    uid_match = re.search(r"^Uid:\s+(\d+)", status, re.M)
    uid = int(uid_match.group(1)) if uid_match else None
    try:
        command = (process_dir / "cmdline").read_bytes().split(b"\0", 1)[0].decode("utf-8", "replace")
    except OSError:
        command = ""
    name = Path(command).name if command else stat["comm"]
    state_names = {
        "R": "Executando", "S": "Dormindo", "D": "Espera", "T": "Parado",
        "t": "Rastreado", "Z": "Zumbi", "I": "Ocioso", "X": "Finalizando",
    }
    protected_pids = {1, os.getpid(), os.getppid()}
    stat.update({
        "name": name or stat["comm"],
        "uid": uid,
        "memory_bytes": stat["rss_pages"] * os.sysconf("SC_PAGE_SIZE"),
        "state": state_names.get(stat["state_code"], stat["state_code"]),
        "can_terminate": uid == os.getuid() and uid != 0 and pid not in protected_pids,
    })
    return stat


def _system_cpu_ticks() -> int | None:
    text = read_text("/proc/stat")
    if not text:
        return None
    fields = text.splitlines()[0].split()
    if not fields or fields[0] != "cpu":
        return None
    try:
        return sum(int(value) for value in fields[1:])
    except ValueError:
        return None


def process_list(sample_seconds: float = 0.12) -> list[dict[str, Any]]:
    before = {}
    for path in Path("/proc").glob("[0-9]*"):
        if path.name.isdigit():
            record = read_process_record(int(path.name))
            if record is not None:
                before[record["pid"]] = record
    ticks_before = _system_cpu_ticks()
    started = time.monotonic()
    time.sleep(max(0.0, sample_seconds))
    elapsed = max(0.001, time.monotonic() - started)
    ticks_after = _system_cpu_ticks()
    clock_ticks = os.sysconf("SC_CLK_TCK")
    processes = []
    for pid, record in before.items():
        raw_after = read_text(Path("/proc") / str(pid) / "stat")
        after = parse_proc_stat(raw_after) if raw_after else None
        if after and after["start_ticks"] == record["start_ticks"]:
            process_delta = max(0, after["utime"] + after["stime"] - record["utime"] - record["stime"])
            record["cpu_percent"] = round(process_delta * 100 / (clock_ticks * elapsed), 1)
        else:
            record["cpu_percent"] = None
        if ticks_before is not None and ticks_after is not None:
            record["sampled_system_ticks"] = max(0, ticks_after - ticks_before)
        processes.append(record)
    return sorted(processes, key=lambda item: (item["cpu_percent"] is None, -(item["cpu_percent"] or 0), item["name"].casefold()))


def terminate_process(pid: int, expected_start_ticks: int) -> None:
    if pid <= 1 or pid in {os.getpid(), os.getppid()}:
        raise PermissionError("Processo protegido pelo BooM.")
    process = read_process_record(pid)
    if process is None or process["start_ticks"] != expected_start_ticks:
        raise ProcessLookupError("O processo já encerrou ou o PID foi reutilizado.")
    if not process["can_terminate"]:
        raise PermissionError("Só é possível encerrar processos do usuário atual; processos do sistema são protegidos.")

    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
    if callable(pidfd_open) and callable(pidfd_send_signal):
        descriptor = pidfd_open(pid, 0)
        try:
            latest_text = read_text(Path("/proc") / str(pid) / "stat")
            latest = parse_proc_stat(latest_text) if latest_text else None
            if latest is None or latest["start_ticks"] != expected_start_ticks:
                raise ProcessLookupError("O processo já encerrou ou o PID foi reutilizado.")
            pidfd_send_signal(descriptor, signal.SIGTERM)
        finally:
            os.close(descriptor)
        return

    latest_text = read_text(Path("/proc") / str(pid) / "stat")
    latest = parse_proc_stat(latest_text) if latest_text else None
    if latest is None or latest["start_ticks"] != expected_start_ticks:
        raise ProcessLookupError("O processo já encerrou ou o PID foi reutilizado.")
    os.kill(pid, signal.SIGTERM)


def parse_dmidecode_memory(text: str) -> list[dict[str, str]]:
    modules = []
    for block in re.split(r"\n\s*\n", text):
        if not re.search(r"^\s*Memory Device\s*$", block, re.M):
            continue
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, value = line.strip().split(":", 1)
            fields[key.strip().casefold()] = value.strip()
        size = fields.get("size", "")
        if not size or size.casefold() in {"no module installed", "unknown", "not installed"}:
            continue
        def meaningful(key: str) -> str:
            value = fields.get(key, "")
            return "" if value.casefold() in {"unknown", "not specified", "none", "n/a"} else value
        modules.append({
            "size": size,
            "type": meaningful("type"),
            "form_factor": meaningful("form factor"),
            "locator": meaningful("locator") or meaningful("bank locator"),
            "manufacturer": meaningful("manufacturer"),
            "part_number": meaningful("part number"),
            "speed": meaningful("speed"),
            "configured_speed": meaningful("configured memory speed") or meaningful("configured clock speed"),
            "voltage": meaningful("configured voltage"),
            "data_width": meaningful("data width"),
            "total_width": meaningful("total width"),
        })
    return modules


def memory_module_info(request_authorization: bool = False) -> dict[str, Any]:
    dmidecode = shutil.which("dmidecode")
    if not dmidecode:
        return {"modules": [], "needs_authorization": False, "message": "Instale dmidecode para consultar módulos SMBIOS."}
    command = [dmidecode, "--type", "17"]
    timeout = 5.0
    if request_authorization:
        pkexec = shutil.which("pkexec")
        if not pkexec:
            return {"modules": [], "needs_authorization": False, "message": "pkexec não está disponível para solicitar leitura autorizada."}
        command = [pkexec, dmidecode, "--type", "17"]
        timeout = 90.0
    result = run_command(command, timeout=timeout)
    if result is None:
        return {"modules": [], "needs_authorization": not request_authorization, "message": "Não foi possível ler o inventário SMBIOS."}
    if result.returncode != 0:
        output = (result.stdout + result.stderr).casefold()
        permission_denied = "permission denied" in output or "can't read memory" in output or "operation not permitted" in output
        return {
            "modules": [],
            "needs_authorization": permission_denied and not request_authorization,
            "message": "A leitura SMBIOS precisa de autorização." if permission_denied else "O firmware não forneceu dados de memória legíveis.",
        }
    modules = parse_dmidecode_memory(result.stdout)
    return {
        "modules": modules,
        "needs_authorization": False,
        "message": "" if modules else "O firmware não informou módulos de memória instalados.",
    }


def platform_detail_info() -> dict[str, str]:
    dmi_root = Path("/sys/devices/virtual/dmi/id")
    keys = {
        "board_vendor": "board_vendor",
        "board_name": "board_name",
        "board_version": "board_version",
        "bios_vendor": "bios_vendor",
        "bios_version": "bios_version",
        "bios_date": "bios_date",
        "product_name": "product_name",
        "product_family": "product_family",
    }
    placeholders = {
        "unknown", "not specified", "none", "default string", "system product name",
        "system manufacturer", "to be filled by o.e.m.", "to be filled by oem",
    }
    result = {}
    for output_key, filename in keys.items():
        value = (read_text(dmi_root / filename) or "").strip()
        if value and value.casefold() not in placeholders:
            result[output_key] = value
    return result


def format_memory_speed(value: str, memory_type: str = "") -> str:
    if not value:
        return UNAVAILABLE
    match = re.search(r"([\d,]+)\s*(MT/s|MHz)", value, re.I)
    if not match:
        return value
    amount = int(match.group(1).replace(",", ""))
    unit = match.group(2)
    if unit.casefold() == "mt/s" and "ddr" in memory_type.casefold():
        return f"{amount} MT/s (≈ {amount // 2} MHz)"
    return f"{amount} {unit}"


def memory_module_terminal_rows(module: dict[str, str], index: int) -> list[str]:
    module_name = module.get("locator") or f"Módulo {index}"
    memory_type = module.get("type", "")
    configured = format_memory_speed(module.get("configured_speed", ""), memory_type)
    nominal = format_memory_speed(module.get("speed", ""), memory_type)
    rows = [
        f"RAM {module_name} · {module.get('size') or UNAVAILABLE} · {memory_type or 'tipo indisponível'} · Configurada: {configured}",
        f"  Nominal: {nominal} · Fabricante: {module.get('manufacturer') or UNAVAILABLE}",
        f"  Modelo: {module.get('part_number') or UNAVAILABLE} · Formato: {module.get('form_factor') or UNAVAILABLE}",
    ]
    extra = " · ".join(item for item in (
        f"Tensão: {module['voltage']}" if module.get("voltage") else "",
        f"Largura dados/total: {module['data_width']}/{module['total_width']}" if module.get("data_width") and module.get("total_width") else "",
    ) if item)
    if extra:
        rows.append("  " + extra)
    return rows


def desktop_application_list() -> list[dict[str, Any]]:
    home = Path.home()
    xdg_dirs = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share").split(":")
    directories = [home / ".local/share/applications", home / ".local/share/flatpak/exports/share/applications"]
    directories.extend(Path(base) / "applications" for base in xdg_dirs if base)
    directories.extend([Path("/var/lib/flatpak/exports/share/applications")])
    desktop_names = os.environ.get("XDG_CURRENT_DESKTOP", "").replace(":", ";").split(";")
    current_desktops = {name.strip() for name in desktop_names if name.strip()}
    by_id: dict[str, dict[str, Any]] = {}
    for directory in directories:
        try:
            files = sorted(directory.glob("*.desktop"))
        except OSError:
            continue
        for path in files:
            app_id = path.name
            if app_id in by_id:
                continue
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                parser.read(path, encoding="utf-8")
                entry = parser["Desktop Entry"]
            except (OSError, KeyError, configparser.Error):
                continue
            if entry.get("Type", "").casefold() != "application":
                continue
            if entry.get("Hidden", "false").casefold() == "true" or entry.get("NoDisplay", "false").casefold() == "true":
                continue
            only_show = {item for item in entry.get("OnlyShowIn", "").split(";") if item}
            not_show = {item for item in entry.get("NotShowIn", "").split(";") if item}
            if only_show and current_desktops and not only_show.intersection(current_desktops):
                continue
            if not_show.intersection(current_desktops):
                continue
            name = entry.get("Name", "").strip()
            exec_line = entry.get("Exec", "").strip()
            if not name or not exec_line:
                continue
            try_exec = entry.get("TryExec", "").strip()
            available = not try_exec or shutil.which(try_exec) is not None
            by_id[app_id] = {
                "id": app_id.removesuffix(".desktop"),
                "name": name,
                "path": str(path),
                "available": available,
            }
    return sorted(by_id.values(), key=lambda item: item["name"].casefold())


def launch_desktop_application(path: str) -> str:
    try:
        import gi  # type: ignore
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio  # type: ignore
        app = Gio.DesktopAppInfo.new_from_filename(path)
        if app is None:
            return "O lançador deste aplicativo não pôde ser lido."
        if not app.launch([], None):
            return "O Linux não conseguiu abrir este aplicativo."
        return "Aplicativo iniciado."
    except Exception as exc:
        return f"Falha ao abrir o aplicativo: {exc}"


def xdg_config_home() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME", "")
    path = Path(configured).expanduser() if configured else Path.home() / ".config"
    return path if path.is_absolute() else Path.home() / ".config"


def boom_autostart_path() -> Path:
    return xdg_config_home() / "autostart" / "boom-monitor.desktop"


def autostart_enabled() -> bool:
    path = boom_autostart_path()
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read(path, encoding="utf-8")
        entry = parser["Desktop Entry"]
    except (OSError, KeyError, configparser.Error):
        return False
    return entry.get("Hidden", "false").casefold() != "true" and entry.get("X-GNOME-Autostart-enabled", "true").casefold() != "false"


def desktop_exec_argument(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$")
    return f'"{escaped}"'


def set_autostart(enabled: bool) -> Path:
    path = boom_autostart_path()
    if not enabled:
        path.unlink(missing_ok=True)
        return path

    path.parent.mkdir(parents=True, exist_ok=True)
    python = desktop_exec_argument(sys.executable)
    script = desktop_exec_argument(str(Path(__file__).resolve()))
    contents = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=BooM\n"
        "Comment=Monitoramento local de CPU, memória e temperatura\n"
        f"Exec={python} {script} --tray\n"
        "Icon=utilities-system-monitor\n"
        "Terminal=false\n"
        "StartupNotify=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".boom-", suffix=".desktop.tmp", delete=False
        ) as temporary:
            temporary.write(contents)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return path


def collect_tray_metrics() -> dict[str, Any]:
    return {
        "cpu_percent": cpu_usage(),
        "memory": memory_info(),
        "temperatures": temperature_info(),
        "updated_at": datetime.now().astimezone().strftime("%H:%M:%S"),
    }


def format_tray_metrics(metrics: dict[str, Any]) -> dict[str, str]:
    cpu = metrics.get("cpu_percent")
    cpu_label = f"CPU: {cpu:.1f}%" if isinstance(cpu, (int, float)) else f"CPU: {UNAVAILABLE}"
    memory = metrics.get("memory") or {}
    total = memory.get("total")
    used = memory.get("used")
    percent = None
    if total:
        percent = used * 100 / total if used is not None else None
        memory_label = f"RAM: {format_bytes(used)} / {format_bytes(total)}"
        if percent is not None:
            memory_label += f" ({percent:.0f}%)"
    else:
        memory_label = f"RAM: {UNAVAILABLE}"
    temperatures = [item.get("celsius") for item in metrics.get("temperatures", []) if isinstance(item.get("celsius"), (int, float))]
    temperature_label = f"Temperatura máxima: {max(temperatures):.1f} °C" if temperatures else f"Temperatura: {UNAVAILABLE}"
    updated_label = f"Atualizado às {metrics.get('updated_at', UNAVAILABLE)}"
    return {
        "cpu": cpu_label,
        "memory": memory_label,
        "temperature": temperature_label,
        "updated": updated_label,
        "title": f"BooM · {cpu_label} · {memory_label}",
        "panel": f"CPU {cpu:.0f}% · RAM {percent:.0f}%" if isinstance(cpu, (int, float)) and percent is not None else "BooM",
    }


def status_notifier_watcher_status(timeout_ms: int = 800) -> tuple[bool, str | None]:
    """Check that the session bus owns the SNI watcher without calling the watcher itself.

    COSMIC has had versions where the watcher service does not answer Properties.GetAll.
    Asking the bus daemon for NameHasOwner avoids treating that slow call as a missing tray.
    """
    try:
        import gi  # type: ignore
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib  # type: ignore
        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        reply = connection.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
            GLib.VariantType.new("(b)"),
            Gio.DBusCallFlags.NO_AUTO_START,
            timeout_ms,
            None,
        )
        if reply is not None and reply.unpack()[0]:
            return True, None
        return False, "O painel COSMIC não anunciou o serviço de bandeja StatusNotifier."
    except Exception as exc:
        return False, f"Não foi possível consultar a bandeja da sessão: {exc}"


def status_notifier_watcher_ready(timeout_ms: int = 800) -> bool:
    ready, _reason = status_notifier_watcher_status(timeout_ms)
    return ready


def autostart_list() -> list[dict[str, str]]:
    config_home = xdg_config_home()
    config_dirs = os.environ.get("XDG_CONFIG_DIRS", "/etc/xdg").split(":")
    directories = [(config_home / "autostart", "Usuário")]
    directories.extend((Path(base) / "autostart", "Sistema") for base in config_dirs if base)
    entries: dict[str, dict[str, str]] = {}
    for directory, source in directories:
        try:
            files = sorted(directory.glob("*.desktop"))
        except OSError:
            continue
        for path in files:
            if path.name in entries:
                continue
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                parser.read(path, encoding="utf-8")
                entry = parser["Desktop Entry"]
            except (OSError, KeyError, configparser.Error):
                continue
            if entry.get("Type", "Application").casefold() != "application":
                continue
            hidden = entry.get("Hidden", "false").casefold() == "true"
            enabled = entry.get("X-GNOME-Autostart-enabled", "true").casefold() != "false"
            entries[path.name] = {
                "id": path.name.removesuffix(".desktop"),
                "name": entry.get("Name", path.stem),
                "enabled": "Ativo" if enabled and not hidden else "Desativado",
                "source": source,
            }
    return sorted(entries.values(), key=lambda item: item["name"].casefold())


def collect_manager_state() -> dict[str, Any]:
    return {
        "processes": process_list(),
        "applications": desktop_application_list(),
        "startup": autostart_list(),
    }


def collect_snapshot() -> dict[str, Any]:
    memory_modules = memory_module_info()
    return {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "cpu": {"model": cpu_model(), "usage_percent": cpu_usage(), "cores": os.cpu_count()},
        "memory": memory_info(),
        "memory_modules": memory_modules["modules"],
        "memory_modules_status": memory_modules["message"],
        "memory_modules_needs_authorization": memory_modules["needs_authorization"],
        "platform_details": platform_detail_info(),
        "os": os_info(),
        "uptime_seconds": uptime_seconds(),
        "disks": disks_info(),
        "gpus": gpu_info(),
        "temperatures": temperature_info(),
        "network": network_info(),
    }


def ping_target(label: str, address: str) -> dict[str, Any]:
    result = run_command(["ping", "-n", "-c", "3", "-W", "1", address], timeout=5.0)
    if result is None:
        return {"target": label, "address": address, "status": "Comando ping indisponível"}
    output = result.stdout + result.stderr
    lowered = output.lower()
    transmitted = re.search(r"(\d+) packets transmitted", output)
    received = re.search(r"(\d+) received", output)
    average = re.search(r"=\s*[\d.]+/([\d.]+)/[\d.]+", output)
    if "operation not permitted" in lowered or "permission denied" in lowered:
        status = "Sem permissão para enviar ICMP"
    elif "network is unreachable" in lowered or "no route to host" in lowered:
        status = "Sem rota para a rede"
    else:
        status = "Conectado" if result.returncode == 0 else "Sem resposta"
    return {
        "target": label,
        "address": address,
        "sent": int(transmitted.group(1)) if transmitted else (0 if result.returncode == 2 else 3),
        "received": int(received.group(1)) if received else 0,
        "average_ms": float(average.group(1)) if average else None,
        "status": status,
    }


def run_network_test(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    targets = []
    gateway = snapshot.get("network", {}).get("gateway")
    if gateway:
        targets.append(("Gateway", gateway))
    targets.append(("Internet · Cloudflare DNS", "1.1.1.1"))
    threads: list[threading.Thread] = []
    results: list[dict[str, Any]] = []
    lock = threading.Lock()
    def worker(label: str, address: str) -> None:
        item = ping_target(label, address)
        with lock:
            results.append(item)
    for label, address in targets:
        thread = threading.Thread(target=worker, args=(label, address), daemon=True)
        threads.append(thread)
        thread.start()
    for thread in threads:
        thread.join(timeout=6.0)
    return results


def format_bytes(value: int | float | None) -> str:
    if value is None:
        return UNAVAILABLE
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or unit == "TB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{amount:.0f} B"
        amount /= 1024
    return UNAVAILABLE


def format_uptime(seconds: float | None) -> str:
    if seconds is None:
        return UNAVAILABLE
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    return f"{days}d {hours:02}h {minutes:02}min" if days else f"{hours}h {minutes:02}min"


def curses_text(window: Any, y: int, x: int, text: str, width: int | None = None, attr: int = 0) -> None:
    try:
        max_y, max_x = window.getmaxyx()
        if y < max_y and x < max_x:
            value = str(text)
            if width is not None:
                value = value[:width]
            window.addstr(y, x, value[:max(0, max_x - x - 1)], attr)
    except curses.error:
        pass


def run_tui() -> None:
    def main(stdscr: Any) -> None:
        curses.curs_set(0)
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_GREEN, -1)
            curses.init_pair(2, curses.COLOR_CYAN, -1)
            curses.init_pair(3, curses.COLOR_WHITE, -1)
            curses.init_pair(4, curses.COLOR_YELLOW, -1)
        except curses.error:
            pass
        pages = ["Visão geral", "Sistema", "Discos", "Rede", "Processos", "Programas", "Inicialização"]
        page = 0
        data: dict[str, Any] = {}
        manager: dict[str, Any] = {"processes": [], "applications": [], "startup": []}
        net_results: list[dict[str, Any]] = []
        selected = 0
        system_scroll = 0
        system_max_scroll = 0
        status = "Lendo dados do Linux…"
        def refresh() -> None:
            nonlocal data, manager, status, selected
            status = "Atualizando dados…"
            data = collect_snapshot()
            manager = collect_manager_state()
            selected = min(selected, max(0, len(manager["processes"]) - 1))
            status = "Atualizado às " + datetime.now().astimezone().strftime("%H:%M:%S")

        def refresh_manager() -> None:
            nonlocal manager, selected
            manager = collect_manager_state()
            selected = min(selected, max(0, len(manager["processes"]) - 1))

        def confirm_terminate() -> None:
            nonlocal status
            processes = manager.get("processes", [])
            if not processes or selected >= len(processes):
                status = "Nenhum processo selecionado."
                return
            process = processes[selected]
            if not process["can_terminate"]:
                status = "Este processo está protegido ou pertence a outro usuário."
                return
            prompt = f"Encerrar {process['name']} (PID {process['pid']}) normalmente? [s/N] "
            curses_text(stdscr, stdscr.getmaxyx()[0] - 2, 2, prompt, width=stdscr.getmaxyx()[1] - 4, attr=curses.color_pair(4))
            stdscr.refresh()
            answer = stdscr.getch()
            if answer not in (ord("s"), ord("S")):
                status = "Encerramento cancelado."
                return
            try:
                terminate_process(process["pid"], process["start_ticks"])
                status = f"Sinal SIGTERM enviado para {process['name']} (PID {process['pid']})."
                refresh_manager()
            except (OSError, ValueError) as exc:
                status = f"Não foi possível encerrar: {exc}"

        def open_selected_app() -> None:
            nonlocal status
            applications = manager.get("applications", [])
            if not applications or selected >= len(applications):
                status = "Nenhum aplicativo selecionado."
                return
            status = launch_desktop_application(applications[selected]["path"])

        def load_authorized_memory() -> None:
            nonlocal data, status
            status = "Aguardando autorização para consultar SMBIOS…"
            details = memory_module_info(request_authorization=True)
            data["memory_modules"] = details["modules"]
            data["memory_modules_status"] = details["message"]
            data["memory_modules_needs_authorization"] = details["needs_authorization"]
            status = details["message"] or "Detalhes de memória atualizados."

        refresh()
        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            curses_text(stdscr, 1, 2, "BooM", attr=curses.color_pair(1) | curses.A_BOLD)
            curses_text(stdscr, 1, 10, "PAINEL LINUX · MONITORAR E CONTROLAR", attr=curses.color_pair(2) | curses.A_BOLD)
            curses_text(stdscr, 3, 2, "1 Painel  2 Sistema  3 Discos  4 Rede", attr=curses.A_BOLD)
            curses_text(stdscr, 4, 2, "5 Processos  6 Programas  7 Inicialização", attr=curses.A_BOLD)
            curses_text(stdscr, 5, 2, f"{pages[page]}", attr=curses.color_pair(1) | curses.A_BOLD)
            y = 7
            cpu = data.get("cpu", {})
            mem = data.get("memory") or {}
            os_data = data.get("os", {})
            disks = data.get("disks", [])
            network = data.get("network", {})
            if page == 0:
                usage = cpu.get("usage_percent")
                curses_text(stdscr, y, 2, f"CPU       {usage:.1f}%" if isinstance(usage, (int, float)) else f"CPU       {UNAVAILABLE}", attr=curses.color_pair(3) | curses.A_BOLD); y += 2
                curses_text(stdscr, y, 2, f"Processador  {cpu.get('model', UNAVAILABLE)}", width=width - 4); y += 1
                curses_text(stdscr, y, 2, f"Núcleos      {cpu.get('cores') or UNAVAILABLE}"); y += 2
                curses_text(stdscr, y, 2, f"Memória   {format_bytes(mem.get('used'))} usados / {format_bytes(mem.get('total'))}", attr=curses.color_pair(3) | curses.A_BOLD); y += 2
                curses_text(stdscr, y, 2, f"Processos   {len(manager.get('processes', []))} · Aplicativos   {len(manager.get('applications', []))}"); y += 1
                if disks:
                    for disk in disks[:3]:
                        curses_text(stdscr, y, 2, f"Disco {disk['mount']}   {disk['percent']}% usado · {format_bytes(disk['available'])} livre", width=width - 4); y += 1
                else:
                    curses_text(stdscr, y, 2, "Discos   Sem volumes disponíveis"); y += 1
                y += 1
                temps = data.get("temperatures", [])
                curses_text(stdscr, y, 2, "Temperaturas  " + (" · ".join(f"{t['name']}: {t['celsius']:.1f} °C" for t in temps[:3]) if temps else UNAVAILABLE), width=width - 4)
            elif page == 1:
                platform = data.get("platform_details", {})
                model = platform.get("product_name") or platform.get("product_family") or UNAVAILABLE
                board = " ".join(filter(None, (platform.get("board_vendor"), platform.get("board_name")))) or UNAVAILABLE
                board_version = platform.get("board_version")
                if board_version:
                    board += f" · {board_version}"
                bios = " · ".join(filter(None, (platform.get("bios_vendor"), platform.get("bios_version"), platform.get("bios_date")))) or UNAVAILABLE
                system_rows = [
                    f"Distribuição: {os_data.get('name', UNAVAILABLE)} · Kernel: {os_data.get('kernel', UNAVAILABLE)}",
                    f"Arquitetura: {os_data.get('architecture', UNAVAILABLE)} · Computador: {os_data.get('hostname', UNAVAILABLE)}",
                    f"Modelo do computador: {model}",
                    f"Placa-mãe: {board}",
                    f"BIOS: {bios}",
                    f"Processador: {cpu.get('model', UNAVAILABLE)} · Núcleos lógicos: {cpu.get('cores') or UNAVAILABLE}",
                    f"Memória total: {format_bytes(mem.get('total'))} · Ligado há: {format_uptime(data.get('uptime_seconds'))}",
                    f"Gráficos: {'; '.join(data.get('gpus', [])) or UNAVAILABLE}",
                ]
                modules = data.get("memory_modules", [])
                for index, module in enumerate(modules, start=1):
                    system_rows.extend(memory_module_terminal_rows(module, index))
                if not modules:
                    message = data.get("memory_modules_status") or "Módulos SMBIOS não disponíveis"
                    system_rows.append("Memória por módulo: " + message)
                    if data.get("memory_modules_needs_authorization"):
                        system_rows.append("Pressione M para pedir autorização de leitura SMBIOS.")
                for item in data.get("temperatures", []):
                    system_rows.append(f"Temperatura · {item['name']}: {item['celsius']:.1f} °C")
                if not data.get("temperatures"):
                    system_rows.append("Temperaturas: sem sensores compatíveis disponíveis")
                visible = max(1, height - 11)
                system_max_scroll = max(0, len(system_rows) - visible)
                system_scroll = min(system_scroll, system_max_scroll)
                for line in system_rows[system_scroll:system_scroll + visible]:
                    curses_text(stdscr, y, 2, line, width=width - 4, attr=curses.color_pair(4) if line.startswith("Pressione M") else 0)
                    y += 1
            elif page == 2:
                if not disks:
                    curses_text(stdscr, y, 2, "Nenhum volume montado disponível."); y += 1
                for disk in disks:
                    curses_text(stdscr, y, 2, f"{disk['mount']}   {disk['percent']}%", attr=curses.color_pair(3) | curses.A_BOLD); y += 1
                    curses_text(stdscr, y, 4, f"{format_bytes(disk['used'])} usados · {format_bytes(disk['available'])} livres de {format_bytes(disk['size'])}", width=width - 6); y += 1
                    curses_text(stdscr, y, 4, f"{disk['device']} · {disk['filesystem']}", width=width - 6); y += 2
            elif page == 3:
                gateway = network.get("gateway") or UNAVAILABLE
                curses_text(stdscr, y, 2, f"Gateway padrão  {gateway}", attr=curses.color_pair(3) | curses.A_BOLD); y += 2
                for interface in network.get("interfaces", []):
                    addresses = ", ".join(interface.get("addresses", [])) or "sem IPv4"
                    curses_text(stdscr, y, 2, f"{interface['name']:<18} {interface['state']:<8} {addresses}", width=width - 4); y += 1
                y += 1
                for result in net_results:
                    latency = f" · média {result['average_ms']:.1f} ms" if result.get("average_ms") is not None else ""
                    curses_text(stdscr, y, 2, f"{result['target']}: {result['status']}{latency}", width=width - 4); y += 1
                if not net_results:
                    curses_text(stdscr, y, 2, "Pressione N para testar o gateway e 1.1.1.1.", attr=curses.color_pair(4)); y += 1
            elif page == 4:
                processes = manager.get("processes", [])
                curses_text(stdscr, y, 2, "PID       CPU     RAM       ESTADO      PROCESSO", attr=curses.color_pair(2) | curses.A_BOLD); y += 1
                visible = max(1, height - 11)
                first = max(0, min(selected - visible + 1, len(processes) - visible))
                for index in range(first, min(len(processes), first + visible)):
                    process = processes[index]
                    marker = ">" if index == selected else " "
                    cpu_value = f"{process['cpu_percent']:.1f}%" if process.get("cpu_percent") is not None else "—"
                    access = "encerrável" if process["can_terminate"] else "protegido"
                    line = f"{marker} {process['pid']:<8} {cpu_value:<7} {format_bytes(process['memory_bytes']):<9} {process['state']:<10} {process['name']} · {access}"
                    curses_text(stdscr, y, 2, line, width=width - 4, attr=curses.color_pair(3) if index == selected else 0); y += 1
                if not processes:
                    curses_text(stdscr, y, 2, "Nenhum processo visível."); y += 1
            elif page == 5:
                applications = manager.get("applications", [])
                visible = max(1, height - 10)
                first = max(0, min(selected - visible + 1, len(applications) - visible))
                for index in range(first, min(len(applications), first + visible)):
                    app = applications[index]
                    marker = ">" if index == selected else " "
                    line = f"{marker} {app['name']} · {app['id']}" + (" · executável ausente" if not app["available"] else "")
                    curses_text(stdscr, y, 2, line, width=width - 4, attr=curses.color_pair(3) if index == selected else 0); y += 1
                if not applications:
                    curses_text(stdscr, y, 2, "Nenhum lançador .desktop encontrado."); y += 1
            else:
                startup = manager.get("startup", [])
                curses_text(stdscr, y, 2, "ESTADO       ORIGEM     PROGRAMA", attr=curses.color_pair(2) | curses.A_BOLD); y += 1
                for item in startup[:max(0, height - 10)]:
                    curses_text(stdscr, y, 2, f"{item['enabled']:<12} {item['source']:<10} {item['name']}", width=width - 4); y += 1
                if not startup:
                    curses_text(stdscr, y, 2, "Nenhum aplicativo de inicialização encontrado."); y += 1
            if y < height - 3:
                y += 1
            curses_text(stdscr, height - 2, 2, status, attr=curses.color_pair(2))
            controls = "1-7 telas · ↑↓ mover · K encerrar · L abrir · M RAM · R atualizar · Q sair"
            curses_text(stdscr, height - 1, 2, controls, width=width - 4)
            stdscr.refresh()
            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                return
            if key in tuple(ord(str(number)) for number in range(1, 8)):
                page = key - ord("1")
                selected = 0
                if page == 1:
                    system_scroll = 0
            elif key in (ord("r"), ord("R")):
                refresh()
            elif key in (ord("n"), ord("N")) and page == 3:
                status = "Testando conectividade; aguarde…"
                net_results = run_network_test(data)
                status = "Diagnóstico concluído às " + datetime.now().astimezone().strftime("%H:%M:%S")
            elif key in (curses.KEY_UP, ord("k")) and page == 1:
                system_scroll = max(0, system_scroll - 1)
            elif key in (curses.KEY_DOWN, ord("j")) and page == 1:
                system_scroll = min(system_max_scroll, system_scroll + 1)
            elif key in (curses.KEY_UP, ord("k"), ord("K")) and page in (4, 5):
                if key == ord("K") and page == 4:
                    confirm_terminate()
                elif key == ord("k") and page == 4:
                    selected = max(0, selected - 1)
                else:
                    selected = max(0, selected - 1)
            elif key in (curses.KEY_DOWN, ord("j")) and page in (4, 5):
                limit = len(manager.get("processes", [])) if page == 4 else len(manager.get("applications", []))
                selected = min(max(0, limit - 1), selected + 1)
            elif key in (ord("l"), ord("L")) and page == 5:
                open_selected_app()
            elif key in (ord("m"), ord("M")) and page == 1 and data.get("memory_modules_needs_authorization"):
                load_authorized_memory()
    curses.wrapper(main)


def launch_gtk(command_line: list[str] | None = None) -> bool:
    try:
        import gi  # type: ignore
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        gi.require_version("Gio", "2.0")
        from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # type: ignore
    except (ImportError, ValueError):
        return False

    indicator_api = None
    try:
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as indicator_api  # type: ignore
    except (ImportError, ValueError):
        try:
            gi.require_version("AppIndicator3", "0.1")
            from gi.repository import AppIndicator3 as indicator_api  # type: ignore
        except (ImportError, ValueError):
            pass

    try:
        init_result = Gtk.init_check([])
        has_display = init_result[0] if isinstance(init_result, tuple) else bool(init_result)
    except Exception:
        has_display = False
    if not has_display:
        return False

    application = Gtk.Application(
        application_id="com.boom.SystemMonitor",
        flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
    )
    state: dict[str, Any] = {
        "initialized": False,
        "tray_available": False,
        "tray_error": None,
        "autostart_error": None,
        "tray_retry_count": 0,
        "tray_retry_hold": False,
        "tray_retry_source": None,
        "indicator": None,
        "menu": None,
        "window": None,
        "metrics_busy": False,
        "metrics_timer": None,
        "closing": False,
        "syncing_autostart": False,
    }

    class BooMWindow(Gtk.ApplicationWindow):
        titles = ["Visão geral", "Sistema", "Discos", "Rede", "Processos", "Programas", "Inicialização"]
        descriptions = [
            "Resumo ao vivo do seu computador.",
            "Detalhes de hardware e da distribuição Linux.",
            "Volumes montados e espaço disponível.",
            "Interfaces e diagnóstico de conectividade.",
            "Uso de recursos e encerramento normal de processos.",
            "Aplicativos gráficos instalados e seus lançadores Linux.",
            "Programas configurados para iniciar junto com sua sessão.",
        ]

        def __init__(self) -> None:
            super().__init__(application=application, title="BooM")
            self.set_default_size(1160, 760)
            self.set_border_width(0)
            self.connect("delete-event", self.on_close_request)
            provider = Gtk.CssProvider()
            provider.load_from_data(GTK_CSS)
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
            self.data: dict[str, Any] = {}
            self.manager_data: dict[str, Any] = {"processes": [], "applications": [], "startup": []}
            self.page = 0
            self.network_results: list[dict[str, Any]] = []
            self.navigation_in_progress = False
            self.render_scheduled = False
            self.restoring_autostart = False
            self.refresh_state = Gtk.Label(label="Lendo dados do sistema…")
            self.refresh_state.get_style_context().add_class("muted")
            self.nav_buttons: list[Any] = []

            root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            root.get_style_context().add_class("boom-root")
            self.add(root)
            sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            sidebar.set_size_request(220, -1)
            sidebar.set_border_width(20)
            sidebar.get_style_context().add_class("sidebar")
            root.pack_start(sidebar, False, False, 0)

            brand = Gtk.Label(label="BooM")
            brand.set_xalign(0)
            brand.get_style_context().add_class("brand")
            sidebar.pack_start(brand, False, False, 0)
            tagline = Gtk.Label(label="MONITOR DE SISTEMA")
            tagline.set_xalign(0)
            tagline.get_style_context().add_class("eyebrow")
            sidebar.pack_start(tagline, False, False, 0)
            spacer = Gtk.Box()
            spacer.set_size_request(-1, 26)
            sidebar.pack_start(spacer, False, False, 0)

            for index, title in enumerate(self.titles):
                button = Gtk.Button(label=title)
                button.set_relief(Gtk.ReliefStyle.NONE)
                button.get_style_context().add_class("nav-button")
                if index == 0:
                    button.get_style_context().add_class("selected")
                button.connect("clicked", self.on_navigation, index)
                sidebar.pack_start(button, False, False, 0)
                self.nav_buttons.append(button)

            bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            bottom.set_valign(Gtk.Align.END)
            bottom.set_vexpand(True)
            sidebar.pack_start(bottom, True, True, 0)
            local = Gtk.Label(label="●  DADOS LOCAIS")
            local.set_xalign(0)
            local.get_style_context().add_class("eyebrow")
            bottom.pack_start(local, False, False, 0)
            host = Gtk.Label(label=socket.gethostname())
            host.set_xalign(0)
            host.get_style_context().add_class("muted")
            bottom.pack_start(host, False, False, 0)

            main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            main.set_hexpand(True)
            root.pack_start(main, True, True, 0)
            header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            header.set_border_width(24)
            header.set_margin_bottom(4)
            main.pack_start(header, False, False, 0)
            heading_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            header.pack_start(heading_box, True, True, 0)
            self.title_label = Gtk.Label(label=self.titles[0])
            self.title_label.set_xalign(0)
            self.title_label.get_style_context().add_class("title")
            heading_box.pack_start(self.title_label, False, False, 0)
            self.description_label = Gtk.Label(label=self.descriptions[0])
            self.description_label.set_xalign(0)
            self.description_label.get_style_context().add_class("subtitle")
            heading_box.pack_start(self.description_label, False, False, 0)
            self.refresh_button = Gtk.Button(label="Atualizar")
            self.refresh_button.get_style_context().add_class("action")
            self.refresh_button.connect("clicked", self.refresh)
            header.pack_end(self.refresh_button, False, False, 0)

            separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            main.pack_start(separator, False, False, 0)
            self.scroller = Gtk.ScrolledWindow()
            self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            main.pack_start(self.scroller, True, True, 0)
            self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
            self.content.set_border_width(24)
            self.scroller.add(self.content)
            footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            footer.set_border_width(15)
            footer.pack_start(self.refresh_state, True, True, 0)
            tray_note = "Bandeja ativa · atualização a cada 5 s" if state["tray_available"] else "Sem bandeja · BooM fica na janela"
            note = Gtk.Label(label=f"Dados locais · {tray_note}")
            note.get_style_context().add_class("muted")
            footer.pack_end(note, False, False, 0)
            main.pack_end(footer, False, False, 0)
            self.render_page()
            self.refresh()

        def on_close_request(self, *_args: Any) -> bool:
            if state["tray_available"]:
                self.hide()
                self.refresh_state.set_text("BooM continua monitorando na bandeja do sistema.")
                return True
            application.quit()
            return False

        def on_navigation(self, button: Any, index: int) -> None:
            if self.navigation_in_progress or self.page == index:
                return
            self.navigation_in_progress = True
            try:
                for other_index, other in enumerate(self.nav_buttons):
                    style = other.get_style_context()
                    style.remove_class("selected")
                    if other_index == index:
                        style.add_class("selected")
                self.page = index
                self.title_label.set_text(self.titles[index])
                self.description_label.set_text(self.descriptions[index])
                if not self.render_scheduled:
                    self.render_scheduled = True
                    GLib.idle_add(self.render_scheduled_page)
            finally:
                self.navigation_in_progress = False

        def render_scheduled_page(self) -> bool:
            try:
                self.render_page()
            finally:
                self.render_scheduled = False
            return False

        def card(self, title: str) -> Any:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=11)
            box.get_style_context().add_class("card")
            label = Gtk.Label(label=title.upper())
            label.set_xalign(0)
            label.get_style_context().add_class("card-title")
            box.pack_start(label, False, False, 0)
            return box

        def metric(self, card: Any, value: str, detail: str = "") -> None:
            label = Gtk.Label(label=value)
            label.set_xalign(0)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            label.get_style_context().add_class("metric")
            card.pack_start(label, False, False, 0)
            if detail:
                hint = Gtk.Label(label=detail)
                hint.set_xalign(0)
                hint.set_line_wrap(True)
                hint.get_style_context().add_class("muted")
                card.pack_start(hint, False, False, 0)

        def add_row(self, card: Any, name: str, value: str) -> None:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            key = Gtk.Label(label=name)
            key.set_xalign(0)
            key.get_style_context().add_class("muted")
            val = Gtk.Label(label=value)
            val.set_xalign(1)
            val.set_ellipsize(Pango.EllipsizeMode.END)
            val.set_selectable(True)
            val.get_style_context().add_class("body-copy")
            row.pack_start(key, True, True, 0)
            row.pack_end(val, True, True, 0)
            card.pack_start(row, False, False, 0)

        def add_card(self, card: Any) -> None:
            self.content.pack_start(card, False, False, 0)

        def make_grid(self) -> Any:
            grid = Gtk.Grid()
            grid.set_column_spacing(14)
            grid.set_row_spacing(14)
            grid.set_column_homogeneous(True)
            return grid

        def render_page(self) -> None:
            for child in self.content.get_children():
                self.content.remove(child)
            if not self.data:
                placeholder = Gtk.Label(label="Carregando informações do computador…")
                placeholder.set_xalign(0)
                placeholder.get_style_context().add_class("muted")
                self.content.pack_start(placeholder, False, False, 0)
                self.content.show_all()
                return
            cpu = self.data.get("cpu", {})
            memory = self.data.get("memory") or {}
            os_data = self.data.get("os", {})
            if self.page == 0:
                grid = self.make_grid()
                cpu_card = self.card("Processador")
                usage = cpu.get("usage_percent")
                self.metric(cpu_card, f"{usage:.1f}%" if isinstance(usage, (int, float)) else UNAVAILABLE, cpu.get("model", UNAVAILABLE))
                mem_card = self.card("Memória")
                used = memory.get("used")
                total = memory.get("total")
                percent = used * 100 / total if total else 0
                self.metric(mem_card, f"{format_bytes(used)} usados", f"de {format_bytes(total)} · {percent:.1f}% em uso" if total else UNAVAILABLE)
                disk = next((item for item in self.data.get("disks", []) if item.get("mount") == "/"), None)
                if not disk:
                    disk = next(iter(self.data.get("disks", [])), None)
                disk_card = self.card("Armazenamento")
                self.metric(disk_card, f"{disk['percent']}% usado" if disk else UNAVAILABLE, f"{disk['mount']} · {format_bytes(disk['available'])} livre" if disk else "Nenhum volume disponível")
                os_card = self.card("Sistema operacional")
                self.metric(os_card, os_data.get("name", UNAVAILABLE), f"Kernel {os_data.get('kernel', UNAVAILABLE)} · ligado há {format_uptime(self.data.get('uptime_seconds'))}")
                for index, card in enumerate((cpu_card, mem_card, disk_card, os_card)):
                    grid.attach(card, index % 2, index // 2, 1, 1)
                self.content.pack_start(grid, False, False, 0)
                thermal = self.card("Sensores térmicos")
                if self.data.get("temperatures"):
                    for item in self.data["temperatures"]:
                        self.add_row(thermal, item["name"], f"{item['celsius']:.1f} °C")
                else:
                    self.add_row(thermal, "Leitura", "Sem sensores compatíveis disponíveis")
                self.add_card(thermal)
            elif self.page == 1:
                system = self.card("Inventário do sistema")
                platform_details = self.data.get("platform_details", {})
                for label, value in (
                    ("Distribuição", os_data.get("name", UNAVAILABLE)),
                    ("Kernel", os_data.get("kernel", UNAVAILABLE)),
                    ("Arquitetura", os_data.get("architecture", UNAVAILABLE)),
                    ("Modelo do computador", platform_details.get("product_name") or platform_details.get("product_family") or UNAVAILABLE),
                    ("Placa-mãe", " ".join(filter(None, (platform_details.get("board_vendor"), platform_details.get("board_name")))) or UNAVAILABLE),
                    ("Versão da placa", platform_details.get("board_version", UNAVAILABLE)),
                    ("BIOS", platform_details.get("bios_version", UNAVAILABLE)),
                    ("Fabricante do BIOS", platform_details.get("bios_vendor", UNAVAILABLE)),
                    ("Data do BIOS", platform_details.get("bios_date", UNAVAILABLE)),
                    ("Processador", cpu.get("model", UNAVAILABLE)),
                    ("Núcleos lógicos", str(cpu.get("cores") or UNAVAILABLE)),
                    ("Memória", format_bytes(memory.get("total"))),
                    ("Tempo ligado", format_uptime(self.data.get("uptime_seconds"))),
                    ("Computador", os_data.get("hostname", UNAVAILABLE)),
                ):
                    self.add_row(system, label, value)
                self.add_card(system)
                memory_card = self.card("Memória por módulo")
                modules = self.data.get("memory_modules", [])
                if modules:
                    for index, module in enumerate(modules, start=1):
                        module_name = module.get("locator") or f"Módulo {index}"
                        self.add_row(memory_card, module_name, f"{module['size']} · {module.get('type') or 'RAM'} · {module.get('form_factor') or 'formato indisponível'}")
                        configured = format_memory_speed(module.get("configured_speed", ""), module.get("type", ""))
                        nominal = format_memory_speed(module.get("speed", ""), module.get("type", ""))
                        self.add_row(memory_card, "Velocidade configurada", configured)
                        self.add_row(memory_card, "Velocidade nominal", nominal)
                        self.add_row(memory_card, "Fabricante", module.get("manufacturer") or UNAVAILABLE)
                        self.add_row(memory_card, "Modelo (part number)", module.get("part_number") or UNAVAILABLE)
                        widths = " · ".join(part for part in (module.get("data_width"), module.get("total_width")) if part)
                        if widths:
                            self.add_row(memory_card, "Largura de dados / total", widths)
                        if module.get("voltage"):
                            self.add_row(memory_card, "Tensão configurada", module["voltage"])
                else:
                    self.add_row(memory_card, "Capacidade total", format_bytes(memory.get("total")))
                    self.add_row(memory_card, "Detalhes por módulo", self.data.get("memory_modules_status") or "Não fornecidos pelo firmware")
                    if self.data.get("memory_modules_needs_authorization"):
                        authorize = Gtk.Button(label="Ler dados SMBIOS com autorização")
                        authorize.get_style_context().add_class("action")
                        authorize.connect("clicked", self.authorize_memory_details)
                        memory_card.pack_start(authorize, False, False, 4)
                self.add_card(memory_card)
                graphics = self.card("Adaptadores gráficos")
                for gpu in self.data.get("gpus", []) or [UNAVAILABLE + " · instale pciutils para consultar"]:
                    self.add_row(graphics, "GPU", gpu)
                self.add_card(graphics)
                thermal = self.card("Temperaturas")
                for item in self.data.get("temperatures", []) or [{"name": "Sensores", "celsius": None}]:
                    self.add_row(thermal, item["name"], f"{item['celsius']:.1f} °C" if item["celsius"] is not None else "Sem leitura disponível")
                self.add_card(thermal)
            elif self.page == 2:
                disks = self.data.get("disks", [])
                if not disks:
                    empty = self.card("Volumes")
                    self.add_row(empty, "Estado", "Nenhum volume montado disponível")
                    self.add_card(empty)
                for disk in disks:
                    item = self.card(disk["mount"])
                    self.metric(item, f"{disk['percent']}% usado", f"{disk['device']} · {disk['filesystem']}")
                    progress = Gtk.ProgressBar()
                    progress.set_fraction(max(0, min(1, disk["percent"] / 100)))
                    item.pack_start(progress, False, False, 2)
                    self.add_row(item, "Usado", format_bytes(disk["used"]))
                    self.add_row(item, "Disponível", format_bytes(disk["available"]))
                    self.add_row(item, "Capacidade", format_bytes(disk["size"]))
                    self.add_card(item)
            elif self.page == 3:
                network = self.data.get("network", {})
                route = self.card("Rota padrão")
                self.add_row(route, "Gateway", network.get("gateway") or UNAVAILABLE)
                self.add_card(route)
                interfaces = self.card("Interfaces de rede")
                for item in network.get("interfaces", []) or [{"name": "Interfaces", "state": UNAVAILABLE, "addresses": []}]:
                    self.add_row(interfaces, item["name"], f"{item['state']} · {', '.join(item['addresses']) or 'sem IPv4'}")
                self.add_card(interfaces)
                diagnostic = self.card("Diagnóstico de conexão")
                self.add_row(diagnostic, "Destinos", "Gateway padrão e 1.1.1.1")
                for result in self.network_results:
                    detail = result["status"]
                    if result.get("average_ms") is not None:
                        detail += f" · média {result['average_ms']:.1f} ms"
                    self.add_row(diagnostic, result["target"], detail)
                test_button = Gtk.Button(label="Testar conexão")
                test_button.get_style_context().add_class("action")
                test_button.connect("clicked", self.test_network)
                diagnostic.pack_start(test_button, False, False, 4)
                self.add_card(diagnostic)
            elif self.page == 4:
                self.render_process_page()
            elif self.page == 5:
                self.render_application_page()
            else:
                self.render_startup_page()
            self.content.show_all()

        def add_tree_column(self, tree: Any, title: str, model_column: int, expand: bool = False) -> None:
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=model_column)
            column.set_resizable(True)
            column.set_expand(expand)
            tree.append_column(column)

        def render_process_page(self) -> None:
            processes = self.manager_data.get("processes", [])
            summary = self.card("Gerenciador de processos")
            self.add_row(summary, "Processos", str(len(processes)))
            self.add_row(summary, "Encerramento", "SIGTERM normal · sem forçar · processos de sistema protegidos")
            search = Gtk.SearchEntry()
            search.set_placeholder_text("Filtrar por nome ou PID")
            summary.pack_start(search, False, False, 0)
            self.add_card(summary)

            store = Gtk.ListStore(int, str, str, str, str, bool, int, str)
            for item in processes:
                cpu = f"{item['cpu_percent']:.1f}%" if item.get("cpu_percent") is not None else "—"
                store.append([
                    item["pid"], item["name"], cpu, format_bytes(item["memory_bytes"]),
                    item["state"], item["can_terminate"], item["start_ticks"],
                    "Encerrar" if item["can_terminate"] else "Protegido",
                ])
            filtered = store.filter_new()
            filtered.set_visible_func(self.process_matches, search)
            search.connect("search-changed", lambda entry: filtered.refilter())
            tree = Gtk.TreeView(model=filtered)
            tree.set_headers_visible(True)
            tree.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
            for title, column, expand in (
                ("PID", 0, False), ("Processo", 1, True), ("CPU", 2, False),
                ("RAM", 3, False), ("Estado", 4, False), ("Ação", 7, False),
            ):
                self.add_tree_column(tree, title, column, expand)
            list_scroll = Gtk.ScrolledWindow()
            list_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            list_scroll.set_min_content_height(330)
            list_scroll.add(tree)
            self.content.pack_start(list_scroll, True, True, 0)
            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            terminate = Gtk.Button(label="Encerrar processo")
            terminate.get_style_context().add_class("action")
            terminate.set_sensitive(False)
            tree.get_selection().connect("changed", self.on_process_selection, terminate)
            terminate.connect("clicked", self.request_process_termination, tree.get_selection())
            actions.pack_start(terminate, False, False, 0)
            note = Gtk.Label(label="Encerrar envia SIGTERM e pode fechar um aplicativo com trabalho não salvo.")
            note.set_xalign(0)
            note.set_line_wrap(True)
            note.get_style_context().add_class("muted")
            actions.pack_start(note, True, True, 0)
            self.content.pack_start(actions, False, False, 0)

        def process_matches(self, model: Any, iterator: Any, search: Any) -> bool:
            query = search.get_text().strip().casefold()
            if not query:
                return True
            return query in model.get_value(iterator, 1).casefold() or query in str(model.get_value(iterator, 0))

        def on_process_selection(self, selection: Any, button: Any) -> None:
            model, iterator = selection.get_selected()
            button.set_sensitive(iterator is not None and bool(model.get_value(iterator, 5)))

        def request_process_termination(self, _button: Any, selection: Any) -> None:
            model, iterator = selection.get_selected()
            if iterator is None:
                return
            pid = int(model.get_value(iterator, 0))
            name = model.get_value(iterator, 1)
            start_ticks = int(model.get_value(iterator, 6))
            if not model.get_value(iterator, 5):
                self.refresh_state.set_text("Esse processo está protegido.")
                return
            dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=Gtk.DialogFlags.MODAL,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.CANCEL,
                text=f"Encerrar {name} (PID {pid})?",
            )
            dialog.format_secondary_text("O BooM enviará SIGTERM para encerrar normalmente. O programa pode fechar trabalho não salvo; processos do sistema não podem ser encerrados aqui.")
            dialog.add_button("Encerrar normalmente", Gtk.ResponseType.ACCEPT)
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.ACCEPT:
                self.refresh_state.set_text("Encerramento cancelado.")
                return
            try:
                terminate_process(pid, start_ticks)
                self.refresh_state.set_text(f"SIGTERM enviado para {name} (PID {pid}).")
                self.refresh_manager_data()
            except (OSError, ValueError) as exc:
                self.refresh_state.set_text(f"Não foi possível encerrar: {exc}")

        def render_application_page(self) -> None:
            applications = self.manager_data.get("applications", [])
            card = self.card("Aplicativos gráficos instalados")
            self.add_row(card, "Encontrados", str(len(applications)))
            self.add_row(card, "Origem", "Lançadores .desktop visíveis para esta sessão")
            search = Gtk.SearchEntry()
            search.set_placeholder_text("Buscar aplicativo")
            card.pack_start(search, False, False, 0)
            self.add_card(card)
            store = Gtk.ListStore(str, str, str, bool, str)
            for app in applications:
                store.append([app["name"], app["id"], app["path"], app["available"], "Sim" if app["available"] else "Executável ausente"])
            filtered = store.filter_new()
            filtered.set_visible_func(
                lambda model, iterator, entry: not entry.get_text().strip()
                or entry.get_text().strip().casefold() in model.get_value(iterator, 0).casefold()
                or entry.get_text().strip().casefold() in model.get_value(iterator, 1).casefold(),
                search,
            )
            search.connect("search-changed", lambda entry: filtered.refilter())
            tree = Gtk.TreeView(model=filtered)
            for title, column, expand in (("Aplicativo", 0, True), ("ID", 1, False), ("Disponível", 4, False)):
                self.add_tree_column(tree, title, column, expand)
            tree.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
            list_scroll = Gtk.ScrolledWindow()
            list_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            list_scroll.set_min_content_height(330)
            list_scroll.add(tree)
            self.content.pack_start(list_scroll, True, True, 0)
            button = Gtk.Button(label="Abrir aplicativo selecionado")
            button.get_style_context().add_class("action")
            button.set_sensitive(False)
            tree.get_selection().connect("changed", self.on_application_selection, button)
            button.connect("clicked", self.launch_selected_application, tree.get_selection())
            self.content.pack_start(button, False, False, 0)

        def on_application_selection(self, selection: Any, button: Any) -> None:
            model, iterator = selection.get_selected()
            button.set_sensitive(iterator is not None and bool(model.get_value(iterator, 3)))

        def launch_selected_application(self, _button: Any, selection: Any) -> None:
            model, iterator = selection.get_selected()
            if iterator is None:
                return
            message = launch_desktop_application(model.get_value(iterator, 2))
            self.refresh_state.set_text(message)

        def render_startup_page(self) -> None:
            entries = self.manager_data.get("startup", [])
            card = self.card("Inicialização da sessão")
            self.add_row(card, "Entradas encontradas", str(len(entries)))
            tray_ready = state["tray_available"]
            monitor_status = "CPU, RAM e temperatura atualizam na bandeja a cada 5 s" if tray_ready else "Bandeja indisponível; BooM continuará acessível na janela"
            window_status = "Fechar oculta; use Sair no menu da bandeja" if tray_ready else "Fechar encerra o BooM"
            self.add_row(card, "Monitoramento", monitor_status)
            self.add_row(card, "Janela", window_status)
            autostart_label = "Iniciar BooM oculto ao entrar na sessão" if tray_ready else "Iniciar BooM ao entrar na sessão"
            autostart = Gtk.CheckButton(label=autostart_label)
            autostart.set_active(autostart_enabled())
            autostart.connect("toggled", self.toggle_autostart)
            card.pack_start(autostart, False, False, 4)
            self.add_card(card)
            store = Gtk.ListStore(str, str, str)
            for item in entries:
                store.append([item["name"], item["enabled"], item["source"]])
            tree = Gtk.TreeView(model=store)
            for title, column, expand in (("Programa", 0, True), ("Estado", 1, False), ("Origem", 2, False)):
                self.add_tree_column(tree, title, column, expand)
            list_scroll = Gtk.ScrolledWindow()
            list_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            list_scroll.set_min_content_height(380)
            list_scroll.add(tree)
            self.content.pack_start(list_scroll, True, True, 0)

        def toggle_autostart(self, checkbox: Any) -> None:
            if self.restoring_autostart:
                return
            try:
                path = set_autostart(checkbox.get_active())
                state = "ativado" if checkbox.get_active() else "desativado"
                self.refresh_state.set_text(f"Início automático {state}: {path}")
            except OSError as exc:
                self.restoring_autostart = True
                checkbox.set_active(not checkbox.get_active())
                self.restoring_autostart = False
                self.refresh_state.set_text(f"Não foi possível alterar a inicialização: {exc}")
                return
            self.refresh_state.set_text(
                "Início automático ativado." if checkbox.get_active() else "Início automático desativado."
            )
            self.refresh_manager_data()

        def refresh(self, *_args: Any) -> None:
            self.refresh_button.set_sensitive(False)
            self.refresh_state.set_text("Atualizando dados do Linux…")
            def worker() -> None:
                result = collect_snapshot()
                manager_data = collect_manager_state()
                GLib.idle_add(self.finish_refresh, result, manager_data)
            threading.Thread(target=worker, daemon=True).start()

        def finish_refresh(self, result: dict[str, Any], manager_data: dict[str, Any]) -> bool:
            self.data = result
            self.manager_data = manager_data
            self.refresh_button.set_sensitive(True)
            self.refresh_state.set_text("Atualizado às " + datetime.now().astimezone().strftime("%H:%M:%S"))
            self.render_page()
            return False

        def refresh_manager_data(self) -> None:
            self.refresh_state.set_text("Atualizando processos e programas…")
            def worker() -> None:
                result = collect_manager_state()
                GLib.idle_add(self.finish_manager_refresh, result)
            threading.Thread(target=worker, daemon=True).start()

        def finish_manager_refresh(self, result: dict[str, Any]) -> bool:
            self.manager_data = result
            self.refresh_state.set_text("Lista de processos atualizada.")
            self.render_page()
            return False

        def authorize_memory_details(self, button: Any) -> None:
            button.set_sensitive(False)
            self.refresh_state.set_text("Aguardando autorização para ler os dados SMBIOS…")
            def worker() -> None:
                result = memory_module_info(request_authorization=True)
                GLib.idle_add(self.finish_memory_details, result, button)
            threading.Thread(target=worker, daemon=True).start()

        def finish_memory_details(self, result: dict[str, Any], button: Any) -> bool:
            self.data["memory_modules"] = result["modules"]
            self.data["memory_modules_status"] = result["message"]
            self.data["memory_modules_needs_authorization"] = result["needs_authorization"]
            button.set_sensitive(True)
            self.refresh_state.set_text(result["message"] or "Detalhes de memória atualizados.")
            self.render_page()
            return False

        def test_network(self, button: Any) -> None:
            button.set_sensitive(False)
            button.set_label("Testando…")
            def worker() -> None:
                results = run_network_test(self.data)
                GLib.idle_add(self.finish_network_test, results)
            threading.Thread(target=worker, daemon=True).start()

        def finish_network_test(self, results: list[dict[str, Any]]) -> bool:
            self.network_results = results
            self.refresh_state.set_text("Diagnóstico concluído às " + datetime.now().astimezone().strftime("%H:%M:%S"))
            self.render_page()
            return False

    def ensure_window() -> Any:
        window = state.get("window")
        if window is None:
            window = BooMWindow()
            state["window"] = window
        return window

    def sync_autostart_menu(*_args: Any) -> None:
        checkbox = state.get("autostart_check")
        if checkbox is None:
            return
        enabled = autostart_enabled()
        if checkbox.get_active() != enabled:
            state["syncing_autostart"] = True
            checkbox.set_active(enabled)
            state["syncing_autostart"] = False

    def toggle_indicator_autostart(checkbox: Any) -> None:
        if state["syncing_autostart"]:
            return
        try:
            set_autostart(checkbox.get_active())
        except OSError as exc:
            state["syncing_autostart"] = True
            checkbox.set_active(not checkbox.get_active())
            state["syncing_autostart"] = False
            state["indicator"].set_title(f"BooM · falha ao alterar início automático: {exc}")
            return
        sync_autostart_menu()
        window = state.get("window")
        if window is not None:
            window.refresh_manager_data()
            window.refresh_state.set_text("Inicialização do BooM atualizada.")

    def finish_tray_refresh(metrics: dict[str, Any], error: str | None = None) -> bool:
        state["metrics_busy"] = False
        indicator = state.get("indicator")
        if indicator is None or state["closing"]:
            return False
        if error:
            indicator.set_title(f"BooM · falha na leitura: {error}")
            return False
        labels = format_tray_metrics(metrics)
        for key, label in (
            ("cpu_item", labels["cpu"]),
            ("memory_item", labels["memory"]),
            ("temperature_item", labels["temperature"]),
            ("updated_item", labels["updated"]),
        ):
            item = state.get(key)
            if item is not None:
                item.set_label(label)
        indicator.set_title(labels["title"])
        try:
            indicator.set_label(labels["panel"], "BooM")
        except Exception:
            pass
        return False

    def request_tray_refresh(*_args: Any) -> bool:
        if state["closing"] or not state["tray_available"]:
            return False
        if state["metrics_busy"]:
            return True
        state["metrics_busy"] = True

        def worker() -> None:
            try:
                metrics = collect_tray_metrics()
                GLib.idle_add(finish_tray_refresh, metrics)
            except Exception as exc:
                GLib.idle_add(finish_tray_refresh, {}, str(exc))

        threading.Thread(target=worker, name="boom-tray-monitor", daemon=True).start()
        return True

    def present_window(*_args: Any) -> None:
        window = ensure_window()
        window.show_all()
        window.present()

    def show_tray_fallback_window() -> None:
        window = ensure_window()
        window.show_all()
        window.present()
        message = state["autostart_error"] or state["tray_error"] or "Bandeja indisponível."
        window.refresh_state.set_text(f"{message} BooM permanece acessível nesta janela.")

    def enable_autostart_after_tray() -> None:
        if not state["tray_available"] or autostart_enabled():
            return
        try:
            set_autostart(True)
        except OSError as exc:
            state["autostart_error"] = f"Falha ao ativar início automático: {exc}"
        sync_autostart_menu()

    def schedule_tray_retry(hold_application: bool) -> None:
        state["tray_retry_count"] = 0
        state["tray_retry_hold"] = hold_application
        if hold_application:
            application.hold()
        state["tray_retry_source"] = GLib.timeout_add(
            TRAY_STARTUP_RETRY_INTERVAL_MS, retry_tray_startup
        )

    def retry_tray_startup() -> bool:
        if state["closing"]:
            return False
        state["tray_retry_count"] += 1
        state["tray_available"] = setup_indicator()
        if state["tray_available"]:
            enable_autostart_after_tray()
            window = state.get("window")
            if window is not None:
                window.render_page()
                window.refresh_state.set_text("Bandeja conectada · monitoramento ativo.")
            if state["tray_retry_hold"]:
                state["tray_retry_hold"] = False
                application.release()
            state["tray_retry_source"] = None
            return False
        if state["tray_retry_count"] >= TRAY_STARTUP_MAX_ATTEMPTS:
            show_tray_fallback_window()
            if state["tray_retry_hold"]:
                state["tray_retry_hold"] = False
                application.release()
            state["tray_retry_source"] = None
            return False
        return True

    def setup_indicator() -> bool:
        if indicator_api is None:
            state["tray_error"] = "Suporte Ayatana AppIndicator não está instalado."
            return False
        watcher_ready, watcher_error = status_notifier_watcher_status()
        if not watcher_ready:
            state["tray_error"] = watcher_error
            return False
        try:
            indicator = indicator_api.Indicator.new(
                "boom-monitor", "utilities-system-monitor", indicator_api.IndicatorCategory.APPLICATION_STATUS
            )
            indicator.set_icon_full("utilities-system-monitor", "BooM · monitor do sistema")
            menu = Gtk.Menu()
            cpu_item = Gtk.MenuItem(label="CPU: lendo…")
            memory_item = Gtk.MenuItem(label="RAM: lendo…")
            temperature_item = Gtk.MenuItem(label="Temperatura: lendo…")
            updated_item = Gtk.MenuItem(label="Aguardando primeira leitura…")
            for item in (cpu_item, memory_item, temperature_item, updated_item):
                item.set_sensitive(False)
                menu.append(item)
            menu.append(Gtk.SeparatorMenuItem())
            open_item = Gtk.MenuItem(label="Abrir painel BooM")
            open_item.connect("activate", present_window)
            menu.append(open_item)
            refresh_item = Gtk.MenuItem(label="Atualizar monitoramento")
            refresh_item.connect("activate", request_tray_refresh)
            menu.append(refresh_item)
            autostart_check = Gtk.CheckMenuItem(label="Iniciar com o Pop!_OS")
            autostart_check.set_active(autostart_enabled())
            autostart_check.connect("toggled", toggle_indicator_autostart)
            menu.append(autostart_check)
            menu.connect("show", sync_autostart_menu)
            menu.append(Gtk.SeparatorMenuItem())
            quit_item = Gtk.MenuItem(label="Sair do BooM")
            quit_item.connect("activate", lambda *_: application.quit())
            menu.append(quit_item)
            menu.show_all()
            indicator.set_title("BooM · iniciando monitoramento")
            indicator.set_menu(menu)
            indicator.set_status(indicator_api.IndicatorStatus.ACTIVE)
        except Exception as exc:
            state["tray_error"] = f"Falha ao registrar o ícone BooM: {exc}"
            return False

        state.update({
            "indicator": indicator,
            "menu": menu,
            "cpu_item": cpu_item,
            "memory_item": memory_item,
            "temperature_item": temperature_item,
            "updated_item": updated_item,
            "autostart_check": autostart_check,
            "tray_available": True,
        })
        application.hold()
        state["metrics_timer"] = GLib.timeout_add_seconds(5, request_tray_refresh)
        request_tray_refresh()
        return True

    def on_command_line(app: Any, invocation: Any) -> int:
        arguments = invocation.get_arguments()
        start_hidden = "--tray" in arguments
        if not state["initialized"]:
            state["initialized"] = True
            state["tray_available"] = setup_indicator()
            if state["tray_available"]:
                enable_autostart_after_tray()
            elif indicator_api is not None:
                schedule_tray_retry(hold_application=start_hidden)
                if start_hidden:
                    return 0
            if start_hidden and not state["tray_available"]:
                start_hidden = False
        if not start_hidden:
            show_tray_fallback_window() if not state["tray_available"] else present_window()
            if state["autostart_error"]:
                ensure_window().refresh_state.set_text(state["autostart_error"])
        return 0

    def on_shutdown(*_args: Any) -> None:
        state["closing"] = True
        timer = state.get("metrics_timer")
        if timer is not None:
            try:
                GLib.source_remove(timer)
            except Exception:
                pass
        retry_source = state.get("tray_retry_source")
        if retry_source is not None:
            try:
                GLib.source_remove(retry_source)
            except Exception:
                pass

    application.connect("command-line", on_command_line)
    application.connect("shutdown", on_shutdown)
    try:
        application.run(command_line or [sys.argv[0]])
    except Exception as exc:
        print(f"Não foi possível iniciar a aplicação GTK: {exc}", file=sys.stderr)
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Painel de sistema BooM para Linux")
    parser.add_argument("--version", action="version", version=f"BooM {__version__}")
    parser.add_argument("--snapshot", action="store_true", help="imprime os dados atuais como JSON")
    parser.add_argument("--network-test", action="store_true", help="executa ping para o gateway e 1.1.1.1")
    parser.add_argument("--tui", action="store_true", help="abre o painel interativo no terminal")
    parser.add_argument("--tray", action="store_true", help="inicia oculto na bandeja e monitora CPU, RAM e temperatura")
    autostart_group = parser.add_mutually_exclusive_group()
    autostart_group.add_argument("--enable-autostart", action="store_true", help="inicia o BooM oculto ao entrar na sessão")
    autostart_group.add_argument("--disable-autostart", action="store_true", help="remove o início automático do BooM")
    args = parser.parse_args()
    if args.enable_autostart or args.disable_autostart:
        try:
            path = set_autostart(args.enable_autostart)
        except OSError as exc:
            print(f"Não foi possível alterar a inicialização do BooM: {exc}", file=sys.stderr)
            return 1
        action = "ativado" if args.enable_autostart else "desativado"
        print(f"Início automático {action}: {path}")
        return 0
    if args.snapshot:
        print(json.dumps(collect_snapshot(), ensure_ascii=False, indent=2))
        return 0
    if args.network_test:
        snapshot = collect_snapshot()
        print(json.dumps(run_network_test(snapshot), ensure_ascii=False, indent=2))
        return 0
    if args.tui:
        run_tui()
        return 0
    if launch_gtk(sys.argv):
        return 0
    if args.tray:
        print(
            "O modo de bandeja precisa de uma sessão gráfica GTK com suporte Ayatana AppIndicator.",
            file=sys.stderr,
        )
        return 1
    if sys.stdin.isatty() and sys.stdout.isatty():
        run_tui()
        return 0
    print(
        "Não foi possível abrir a janela GTK neste ambiente. Execute `python3 boom.py --tui` "
        "em um terminal ou instale o suporte GTK 3 (PyGObject) em uma sessão gráfica.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
