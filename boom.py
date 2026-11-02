#!/usr/bin/env python3
"""BooM: painel local de sistema para Linux, com GTK e modo de terminal."""

from __future__ import annotations

import argparse
import configparser
import curses
import csv
import html
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
(
    PAGE_OVERVIEW,
    PAGE_SYSTEM,
    PAGE_MEMORY,
    PAGE_GRAPHICS,
    PAGE_DISKS,
    PAGE_NETWORK,
    PAGE_PROCESSES,
    PAGE_APPLICATIONS,
    PAGE_STARTUP,
) = range(9)
NAVIGATION_PAGES = (
    "Visão geral",
    "Sistema",
    "Memória",
    "Gráficos",
    "Discos",
    "Rede",
    "Processos",
    "Programas",
    "Inicialização",
)
NAVIGATION_DESCRIPTIONS = (
    "Resumo ao vivo do seu computador.",
    "Detalhes do hardware e da distribuição Linux.",
    "Uso de RAM, swap, VRAM e especificações dos módulos.",
    "Telemetria de GPUs, clocks, temperatura, potência e link PCIe.",
    "Volumes montados e espaço disponível.",
    "Interfaces e diagnóstico de conectividade.",
    "Uso de recursos e encerramento normal de processos.",
    "Aplicativos gráficos instalados e seus lançadores Linux.",
    "Programas configurados para iniciar junto com sua sessão.",
)
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


def parse_cpu_frequency_readings(readings: dict[int, str]) -> list[dict[str, int | float]]:
    frequencies: list[dict[str, int | float]] = []
    for core, raw in sorted(readings.items()):
        try:
            khz = int(raw.strip())
        except (AttributeError, ValueError):
            continue
        if khz <= 0:
            continue
        frequencies.append({"core": core, "mhz": round(khz / 1000, 1)})
    return frequencies


def cpu_frequency_info() -> dict[str, Any]:
    cpu_root = Path("/sys/devices/system/cpu")
    frequency_paths = sorted(cpu_root.glob("cpu[0-9]*/cpufreq/cpuinfo_cur_freq"))
    source = "sysfs · cpuinfo_cur_freq (leitura do hardware)"
    if not frequency_paths:
        frequency_paths = sorted(cpu_root.glob("cpu[0-9]*/cpufreq/scaling_cur_freq"))
        source = "sysfs · scaling_cur_freq (frequência solicitada/reportada pelo driver)"

    def read_frequencies(paths: list[Path]) -> list[dict[str, int | float]]:
        readings: dict[int, str] = {}
        for path in paths:
            match = re.search(r"/cpu(\d+)/", str(path))
            raw = read_text(path)
            if match and raw:
                readings[int(match.group(1))] = raw
        return parse_cpu_frequency_readings(readings)

    cores = read_frequencies(frequency_paths)
    if not cores and "cpuinfo_cur_freq" in source:
        frequency_paths = sorted(cpu_root.glob("cpu[0-9]*/cpufreq/scaling_cur_freq"))
        source = "sysfs · scaling_cur_freq (frequência solicitada/reportada pelo driver)"
        cores = read_frequencies(frequency_paths)
    mhz_values = [float(item["mhz"]) for item in cores]
    return {
        "cores": cores,
        "average_mhz": round(sum(mhz_values) / len(mhz_values), 1) if mhz_values else None,
        "minimum_mhz": min(mhz_values) if mhz_values else None,
        "maximum_mhz": max(mhz_values) if mhz_values else None,
        "source": source if cores else None,
    }


def format_frequency(mhz: int | float | None) -> str:
    if not isinstance(mhz, (int, float)):
        return UNAVAILABLE
    return f"{mhz / 1000:.2f} GHz" if mhz >= 1000 else f"{mhz:.0f} MHz"


def format_vram_usage(gpu: dict[str, Any]) -> str:
    percent = vram_usage_percent(gpu)
    used = gpu.get("vram_used_mib")
    total = gpu.get("vram_total_mib")
    if percent is None or not isinstance(used, (int, float)) or not isinstance(total, (int, float)):
        return UNAVAILABLE
    return f"{used:.0f} / {total:.0f} MiB · {percent:.1f}%"


def vram_usage_percent(gpu: dict[str, Any]) -> float | None:
    used = gpu.get("vram_used_mib")
    total = gpu.get("vram_total_mib")
    if not isinstance(used, (int, float)) or not isinstance(total, (int, float)) or total <= 0:
        return None
    return used * 100 / total


def parse_nvidia_smi_csv(output: str) -> list[dict[str, Any]]:
    fields = (
        "name", "driver_version", "gpu_utilization_percent", "memory_utilization_percent",
        "temperature_celsius", "vram_used_mib", "vram_total_mib", "graphics_clock_mhz",
        "memory_clock_mhz", "power_draw_watts", "power_limit_watts", "performance_state",
    )
    numeric_fields = set(fields[2:11])
    parsed: list[dict[str, Any]] = []
    for row in csv.reader(output.splitlines(), skipinitialspace=True):
        if not row:
            continue
        values = [value.strip() for value in row]
        if len(values) < len(fields):
            continue
        gpu: dict[str, Any] = {"source": "nvidia-smi/NVML"}
        for field, value in zip(fields, values):
            if field in numeric_fields:
                if value.casefold() in {"", "n/a", "na", "[not supported]"}:
                    gpu[field] = None
                    continue
                try:
                    number = float(value)
                    gpu[field] = int(number) if number.is_integer() else number
                except ValueError:
                    gpu[field] = None
            else:
                gpu[field] = value or None
        parsed.append(gpu)
    return parsed


def gpu_telemetry_info() -> tuple[list[dict[str, Any]], str]:
    if not shutil.which("nvidia-smi"):
        return [], "Métricas NVIDIA indisponíveis: nvidia-smi não está instalado."
    query = (
        "name,driver_version,utilization.gpu,utilization.memory,temperature.gpu,"
        "memory.used,memory.total,clocks.current.graphics,clocks.current.memory,"
        "power.draw,power.limit,pstate"
    )
    result = run_command(
        ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
        timeout=2.5,
    )
    if result is None:
        return [], "Métricas NVIDIA indisponíveis: nvidia-smi não respondeu."
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[0] if result.stderr.strip() else "erro do driver"
        return [], f"Métricas NVIDIA indisponíveis: {detail}"
    metrics = parse_nvidia_smi_csv(result.stdout)
    if not metrics:
        return [], "Métricas NVIDIA indisponíveis: o driver não retornou dados reconhecíveis."
    optional = run_command(
        [
            "nvidia-smi",
            "--query-gpu=fan.speed,pcie.link.gen.current,pcie.link.width.current",
            "--format=csv,noheader,nounits",
        ],
        timeout=2.5,
    )
    if optional is not None and optional.returncode == 0:
        for gpu, row in zip(metrics, csv.reader(optional.stdout.splitlines(), skipinitialspace=True)):
            if len(row) >= 3:
                gpu["fan_percent"] = _parse_smi_number(row[0])
                gpu["pcie_generation"] = _parse_smi_number(row[1])
                gpu["pcie_link_width"] = _parse_smi_number(row[2])
    return metrics, "Métricas obtidas de nvidia-smi/NVML."


def _parse_smi_number(value: str) -> int | float | None:
    normalized = value.strip()
    if normalized.casefold() in {"", "n/a", "na", "[not supported]"}:
        return None
    try:
        number = float(normalized)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def format_gpu_graphics_terminal_rows(gpu: dict[str, Any]) -> list[str]:
    rows = [f"GPU: {gpu.get('name') or UNAVAILABLE} · Driver: {gpu.get('driver_version') or UNAVAILABLE}"]
    for label, key, suffix in (
        ("Uso do núcleo gráfico", "gpu_utilization_percent", "%"),
        ("Temperatura da GPU", "temperature_celsius", " °C"),
        ("Clock gráfico", "graphics_clock_mhz", " MHz"),
        ("Potência", "power_draw_watts", " W"),
        ("Limite de potência", "power_limit_watts", " W"),
        ("Ventoinha", "fan_percent", "%"),
    ):
        value = gpu.get(key)
        formatted = f"{value:.0f}{suffix}" if isinstance(value, (int, float)) else UNAVAILABLE
        rows.append(f"{label}: {formatted}")
    generation = gpu.get("pcie_generation")
    width = gpu.get("pcie_link_width")
    rows.append(
        f"Link PCIe: geração {generation} · x{width}"
        if isinstance(generation, (int, float)) or isinstance(width, (int, float))
        else f"Link PCIe: {UNAVAILABLE}"
    )
    if gpu.get("performance_state"):
        rows.append(f"Estado de desempenho: {gpu['performance_state']}")
    return rows


def format_gpu_memory_terminal_rows(gpu: dict[str, Any]) -> list[str]:
    return [
        f"GPU: {gpu.get('name') or UNAVAILABLE}",
        f"VRAM usada / total: {format_vram_usage(gpu)}",
        "Atividade do controlador de memória: " + (
            f"{gpu['memory_utilization_percent']:.0f}%"
            if isinstance(gpu.get("memory_utilization_percent"), (int, float))
            else UNAVAILABLE
        ),
        "Clock da memória gráfica: " + format_frequency(gpu.get("memory_clock_mhz")),
    ]


def format_gpu_terminal_rows(gpu: dict[str, Any]) -> list[str]:
    """Compatibilidade: devolve as métricas gráficas e de memória na sequência."""
    return format_gpu_graphics_terminal_rows(gpu) + format_gpu_memory_terminal_rows(gpu)[1:]


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


def parse_meminfo(text: str) -> dict[str, int] | None:
    values: dict[str, int] = {}
    for line in text.splitlines():
        match = re.match(r"^(\w+):\s+(\d+)", line)
        if match:
            values[match.group(1)] = int(match.group(2)) * 1024
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None or available is None:
        return None
    swap_total = values.get("SwapTotal", 0)
    swap_available = values.get("SwapFree", 0)
    return {
        "total": total,
        "available": available,
        "used": total - available,
        "swap_total": swap_total,
        "swap_available": swap_available,
        "swap_used": max(0, swap_total - swap_available),
    }


def memory_info() -> dict[str, int] | None:
    text = read_text("/proc/meminfo")
    return parse_meminfo(text) if text else None


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


def block_devices_info() -> list[dict[str, Any]]:
    result = run_command(
        ["lsblk", "--json", "--bytes", "--output", "NAME,TYPE,SIZE,MODEL,TRAN,PATH"],
        timeout=4.0,
    )
    if result is None or result.returncode != 0:
        return []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    devices = []
    for item in payload.get("blockdevices", []):
        path = item.get("path")
        if item.get("type") != "disk" or not isinstance(path, str) or not path.startswith("/dev/"):
            continue
        if item.get("name", "").startswith("zram"):
            continue
        devices.append({
            "device": path,
            "name": item.get("name"),
            "model": item.get("model"),
            "size": item.get("size"),
            "transport": item.get("tran"),
        })
    return devices


def smart_device_info(block_devices: list[dict[str, Any]] | None = None) -> tuple[list[dict[str, Any]], str]:
    block_devices = block_devices if block_devices is not None else block_devices_info()
    smartctl_path = shutil.which("smartctl")
    if not smartctl_path:
        return [dict(device, device_type=None, smart_supported=False) for device in block_devices], "smartctl não está instalado."
    result = run_command([smartctl_path, "--scan"], timeout=5.0)
    if result is None or result.returncode != 0:
        return [dict(device, device_type=None, smart_supported=False) for device in block_devices], "Não foi possível consultar smartctl --scan."

    scanned: list[dict[str, str | None]] = []
    for line in result.stdout.splitlines():
        content = line.split("#", 1)[0].strip()
        match = re.match(r"^(\S+)(?:\s+-d\s+(\S+))?", content)
        if not match:
            continue
        device, device_type = match.groups()
        if re.fullmatch(r"/dev/[A-Za-z0-9._-]+", device):
            scanned.append({"device": device, "device_type": device_type})

    output: list[dict[str, Any]] = []
    for disk in block_devices:
        path = disk["device"]
        scan = next((
            item for item in scanned
            if item["device"] == path
            or (item["device_type"] == "nvme" and path.startswith(str(item["device"]) + "n"))
        ), None)
        output.append({
            **disk,
            "smart_device": scan["device"] if scan else path,
            "device_type": scan["device_type"] if scan else None,
            "smart_supported": scan is not None,
        })
    if any(device.get("smart_supported") for device in output):
        status = "Discos físicos detectados; a leitura SMART ocorre após ação explícita e pode pedir autorização."
    elif output:
        status = "Discos físicos listados, mas smartctl não anunciou suporte SMART para eles."
    else:
        status = "Nenhum disco físico compatível com smartctl foi detectado."
    return output, status


def parse_smartctl_json(device: str, raw_json: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return {"device": device, "available": False, "message": "smartctl não retornou JSON válido."}
    smartctl = payload.get("smartctl", {})
    messages = smartctl.get("messages", []) if isinstance(smartctl, dict) else []
    errors = [item.get("string") for item in messages if item.get("severity") == "error" and item.get("string")]
    if errors:
        return {"device": device, "available": False, "message": str(errors[0])}

    smart_status = payload.get("smart_status", {})
    if not isinstance(smart_status, dict):
        smart_status = {}
    nvme = payload.get("nvme_smart_health_information_log", {})
    if not isinstance(nvme, dict):
        nvme = {}
    temperature = payload.get("temperature", {})
    temperature_celsius = temperature.get("current") if isinstance(temperature, dict) else None
    if not isinstance(temperature_celsius, (int, float)):
        temperature_celsius = nvme.get("temperature")
    power_on_time = payload.get("power_on_time", {})
    power_on_hours = power_on_time.get("hours") if isinstance(power_on_time, dict) else None
    if not isinstance(power_on_hours, (int, float)):
        power_on_hours = nvme.get("power_on_hours")
    passed = smart_status.get("passed") if isinstance(smart_status, dict) else None
    critical_warning = nvme.get("critical_warning")
    if passed is True:
        message = "O dispositivo informa SMART aprovado; isso não garante ausência de falhas futuras."
    elif passed is False:
        message = "O dispositivo informa falha SMART; faça backup e investigue o disco."
    else:
        message = "O dispositivo não forneceu um resultado SMART global."
    if isinstance(critical_warning, (int, float)) and critical_warning > 0:
        message += f" Aviso crítico NVMe: {int(critical_warning)}."
    return {
        "device": device,
        "available": True,
        "model": payload.get("model_name") or payload.get("model_family") or (payload.get("device", {}).get("name") if isinstance(payload.get("device"), dict) else None),
        "health_passed": passed if isinstance(passed, bool) else None,
        "temperature_celsius": temperature_celsius if isinstance(temperature_celsius, (int, float)) else None,
        "power_on_hours": power_on_hours if isinstance(power_on_hours, (int, float)) else None,
        "percentage_used": nvme.get("percentage_used") if isinstance(nvme.get("percentage_used"), (int, float)) else None,
        "available_spare_percent": nvme.get("available_spare") if isinstance(nvme.get("available_spare"), (int, float)) else None,
        "critical_warning": critical_warning if isinstance(critical_warning, (int, float)) else None,
        "message": message,
        "source": "smartctl JSON",
    }


def read_smart_device(device_info: dict[str, Any]) -> dict[str, Any]:
    device = device_info.get("smart_device") or device_info.get("device")
    device_type = device_info.get("device_type")
    if not isinstance(device, str) or not re.fullmatch(r"/dev/[A-Za-z0-9._-]+", device):
        return {"device": str(device), "available": False, "message": "Dispositivo SMART inválido."}
    if device_type is not None and (not isinstance(device_type, str) or not re.fullmatch(r"[A-Za-z0-9,+_-]+", device_type)):
        return {"device": device, "available": False, "message": "Tipo de dispositivo SMART inválido."}
    pkexec_path = shutil.which("pkexec")
    smartctl_path = shutil.which("smartctl")
    if not pkexec_path or not smartctl_path:
        return {"device": device, "available": False, "message": "Instale smartmontools e pkexec para ler SMART com autorização."}
    command = [pkexec_path, smartctl_path, "--json", "--all"]
    if device_type:
        command.append(f"--device={device_type}")
    command.append(device)
    result = run_command(command, timeout=15.0)
    if result is None:
        return {"device": device, "available": False, "message": "Leitura SMART cancelada, sem autorização ou excedeu o tempo limite."}
    parsed = parse_smartctl_json(device, result.stdout)
    if not parsed.get("available") and result.stderr.strip() and "JSON válido" in parsed.get("message", ""):
        parsed["message"] = result.stderr.strip().splitlines()[0]
    return parsed


def power_supply_info(root: Path = Path("/sys/class/power_supply")) -> dict[str, Any]:
    batteries: list[dict[str, Any]] = []
    ac_sources: list[bool] = []
    try:
        supplies = sorted(root.glob("*"))
    except OSError:
        supplies = []
    for supply in supplies:
        supply_type = (read_text(supply / "type") or "").strip()
        present_text = (read_text(supply / "present") or "1").strip()
        if present_text == "0":
            continue
        if supply_type.casefold() in {"mains", "usb", "wireless"}:
            online_text = (read_text(supply / "online") or "").strip()
            if online_text in {"0", "1"}:
                ac_sources.append(online_text == "1")
        if supply_type.casefold() not in {"battery", "ups"}:
            continue

        def read_number(name: str) -> float | None:
            raw = read_text(supply / name)
            try:
                return float(raw.strip()) if raw else None
            except ValueError:
                return None

        capacity = read_number("capacity")
        energy_now = read_number("energy_now")
        energy_full = read_number("energy_full")
        power_now = read_number("power_now")
        voltage_now = read_number("voltage_now")
        batteries.append({
            "name": supply.name,
            "status": (read_text(supply / "status") or UNAVAILABLE).strip(),
            "capacity_percent": capacity if capacity is not None and 0 <= capacity <= 100 else None,
            "energy_now_wh": energy_now / 1_000_000 if energy_now is not None else None,
            "energy_full_wh": energy_full / 1_000_000 if energy_full is not None else None,
            "power_watts": power_now / 1_000_000 if power_now is not None else None,
            "voltage_volts": voltage_now / 1_000_000 if voltage_now is not None else None,
            "source": "sysfs · power_supply",
        })
    return {
        "batteries": batteries,
        "ac_online": any(ac_sources) if ac_sources else None,
        "status": "Bateria detectada" if batteries else "Nenhuma bateria detectada.",
        "source": "sysfs · /sys/class/power_supply",
    }


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
    gpu_metrics, gpu_metrics_status = gpu_telemetry_info()
    return {
        "cpu_percent": cpu_usage(),
        "cpu_frequency": cpu_frequency_info(),
        "memory": memory_info(),
        "gpu_metrics": gpu_metrics,
        "gpu_metrics_status": gpu_metrics_status,
        "temperatures": temperature_info(),
        "power": power_supply_info(),
        "updated_at": datetime.now().astimezone().strftime("%H:%M:%S"),
    }


def format_tray_metrics(metrics: dict[str, Any]) -> dict[str, str]:
    cpu = metrics.get("cpu_percent")
    cpu_label = f"CPU: {cpu:.1f}%" if isinstance(cpu, (int, float)) else f"CPU: {UNAVAILABLE}"
    frequency = (metrics.get("cpu_frequency") or {}).get("average_mhz")
    cpu_clock_label = f"Frequência reportada média da CPU: {format_frequency(frequency)}"
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
    gpu = next(iter(metrics.get("gpu_metrics", [])), {})
    gpu_utilization = gpu.get("gpu_utilization_percent")
    gpu_temperature = gpu.get("temperature_celsius")
    gpu_summary = " · ".join(
        value for value in (
            gpu.get("name"),
            f"núcleo {gpu_utilization:.0f}%" if isinstance(gpu_utilization, (int, float)) else None,
            f"{gpu_temperature:.0f} °C" if isinstance(gpu_temperature, (int, float)) else None,
        ) if value
    )
    gpu_label = f"GPU: {gpu_summary or UNAVAILABLE}"
    gpu_memory_activity = gpu.get("memory_utilization_percent")
    gpu_memory_label = (
        f"Atividade do controlador de memória: {gpu_memory_activity:.0f}%"
        if isinstance(gpu_memory_activity, (int, float)) else f"Atividade do controlador de memória: {UNAVAILABLE}"
    )
    gpu_clock_label = (
        f"Clocks da GPU: núcleo {format_frequency(gpu.get('graphics_clock_mhz'))} · memória {format_frequency(gpu.get('memory_clock_mhz'))}"
        if gpu else f"Clocks da GPU: {UNAVAILABLE}"
    )
    power_draw = gpu.get("power_draw_watts")
    power_limit = gpu.get("power_limit_watts")
    gpu_power_label = (
        f"Potência da GPU: {power_draw:.1f} W"
        + (f" / {power_limit:.1f} W" if isinstance(power_limit, (int, float)) else "")
        if isinstance(power_draw, (int, float)) else f"Potência da GPU: {UNAVAILABLE}"
    )
    fan_speed = gpu.get("fan_percent")
    gpu_fan_label = f"Ventoinha da GPU: {fan_speed:.0f}%" if isinstance(fan_speed, (int, float)) else f"Ventoinha da GPU: {UNAVAILABLE}"
    pcie_generation = gpu.get("pcie_generation")
    pcie_width = gpu.get("pcie_link_width")
    gpu_link_label = (
        f"Link PCIe da GPU: geração {pcie_generation} · x{pcie_width}"
        if isinstance(pcie_generation, (int, float)) or isinstance(pcie_width, (int, float))
        else f"Link PCIe da GPU: {UNAVAILABLE}"
    )
    vram_label = f"VRAM usada / total: {format_vram_usage(gpu)}"
    vram_percent = vram_usage_percent(gpu)
    power = metrics.get("power") or {}
    batteries = power.get("batteries", [])
    battery = next(iter(batteries), {})
    if battery:
        battery_percent = battery.get("capacity_percent")
        battery_label = f"Bateria: {battery_percent:.0f}%" if isinstance(battery_percent, (int, float)) else "Bateria: nível indisponível"
        battery_status = battery.get("status")
        if battery_status and battery_status != UNAVAILABLE:
            battery_label += f" · {battery_status}"
    else:
        battery_label = f"Energia: {power.get('status', 'Nenhuma bateria detectada.')}"
    updated_label = f"Atualizado às {metrics.get('updated_at', UNAVAILABLE)}"
    panel_parts = []
    if isinstance(cpu, (int, float)):
        panel_parts.append(f"CPU {cpu:.0f}%")
    if isinstance(gpu_utilization, (int, float)):
        panel_parts.append(f"GPU {gpu_utilization:.0f}%")
    if percent is not None:
        panel_parts.append(f"RAM {percent:.0f}%")
    vram_used = gpu.get("vram_used_mib")
    vram_total = gpu.get("vram_total_mib")
    if (
        vram_percent is not None
        and isinstance(vram_used, (int, float))
        and isinstance(vram_total, (int, float))
    ):
        panel_parts.append(
            f"VRAM {vram_used / 1024:.1f}/{vram_total / 1024:.1f} GB · {vram_percent:.0f}%"
        )
    elif vram_percent is not None:
        panel_parts.append(f"VRAM {vram_percent:.0f}%")
    return {
        "cpu": cpu_label,
        "cpu_clock": cpu_clock_label,
        "memory": memory_label,
        "gpu": gpu_label,
        "gpu_memory_activity": gpu_memory_label,
        "gpu_clock": gpu_clock_label,
        "gpu_power": gpu_power_label,
        "gpu_fan": gpu_fan_label,
        "gpu_link": gpu_link_label,
        "vram": vram_label,
        "battery": battery_label,
        "temperature": temperature_label,
        "updated": updated_label,
        "title": f"BooM · {cpu_label} · {gpu_label} · {vram_label} · {memory_label}",
        "panel": " · ".join(panel_parts) if panel_parts else "BooM",
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
    gpu_metrics, gpu_metrics_status = gpu_telemetry_info()
    block_devices = block_devices_info()
    smart_devices, smart_devices_status = smart_device_info(block_devices)
    return {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "cpu": {
            "model": cpu_model(),
            "usage_percent": cpu_usage(),
            "cores": os.cpu_count(),
            "frequency": cpu_frequency_info(),
        },
        "memory": memory_info(),
        "memory_modules": memory_modules["modules"],
        "memory_modules_status": memory_modules["message"],
        "memory_modules_needs_authorization": memory_modules["needs_authorization"],
        "platform_details": platform_detail_info(),
        "os": os_info(),
        "uptime_seconds": uptime_seconds(),
        "disks": disks_info(),
        "block_devices": block_devices,
        "smart_devices": smart_devices,
        "smart_devices_status": smart_devices_status,
        "gpus": gpu_info(),
        "gpu_metrics": gpu_metrics,
        "gpu_metrics_status": gpu_metrics_status,
        "temperatures": temperature_info(),
        "power": power_supply_info(),
        "network": network_info(),
    }


REPORT_SECTION_LABELS = {
    "cpu": "Processador",
    "memory": "Memória RAM",
    "swap_total": "Swap total",
    "swap_used": "Swap usada",
    "swap_available": "Swap disponível",
    "memory_modules": "Módulos de memória",
    "memory_modules_status": "Estado dos dados de memória",
    "memory_modules_needs_authorization": "Autorização SMBIOS necessária",
    "platform_details": "Placa-mãe e firmware",
    "os": "Sistema operacional",
    "uptime_seconds": "Tempo ligado",
    "disks": "Volumes montados",
    "block_devices": "Discos físicos",
    "smart_devices": "Disponibilidade SMART",
    "smart_devices_status": "Estado SMART",
    "gpus": "Adaptadores gráficos detectados",
    "gpu_metrics": "Telemetria gráfica",
    "gpu_metrics_status": "Estado da telemetria gráfica",
    "temperatures": "Sensores térmicos",
    "power": "Energia e bateria",
    "network": "Rede local",
    "smart_readings": "Leituras SMART solicitadas",
}
REPORT_FIELD_LABELS = {
    "ac_online": "Alimentação externa conectada",
    "address": "Endereço local",
    "addresses": "Endereços locais",
    "average_mhz": "Frequência média",
    "available": "Disponível",
    "available_spare_percent": "Reserva disponível",
    "bios_date": "Data do BIOS",
    "bios_vendor": "Fabricante do BIOS",
    "bios_version": "Versão do BIOS",
    "board_name": "Modelo da placa-mãe",
    "board_vendor": "Fabricante da placa-mãe",
    "board_version": "Versão da placa-mãe",
    "capacity_percent": "Percentual da bateria",
    "cores": "Núcleos",
    "core": "Núcleo",
    "configured_speed": "Velocidade configurada",
    "celsius": "Temperatura",
    "data_width": "Largura de dados",
    "device": "Dispositivo",
    "driver_version": "Versão do driver",
    "energy_full_wh": "Energia total",
    "energy_now_wh": "Energia atual",
    "fan_percent": "Velocidade da ventoinha",
    "filesystem": "Sistema de arquivos",
    "form_factor": "Formato do módulo",
    "gateway": "Gateway",
    "gpu_utilization_percent": "Uso do núcleo gráfico",
    "graphics_clock_mhz": "Clock gráfico",
    "health_passed": "Saúde SMART aprovada",
    "locator": "Slot",
    "manufacturer": "Fabricante",
    "maximum_mhz": "Frequência máxima",
    "memory_clock_mhz": "Clock da memória gráfica",
    "memory_utilization_percent": "Atividade do controlador de memória",
    "minimum_mhz": "Frequência mínima",
    "model": "Modelo",
    "name": "Nome",
    "network": "Rede",
    "nominal_speed": "Velocidade nominal",
    "online": "Conectada",
    "part_number": "Código do módulo",
    "path": "Caminho",
    "pcie_generation": "Geração PCIe",
    "pcie_link_width": "Largura do link PCIe",
    "percentage_used": "Vida útil consumida",
    "percent": "Uso do volume",
    "power_draw_watts": "Potência da GPU",
    "power_limit_watts": "Limite de potência da GPU",
    "power_on_hours": "Horas ligada",
    "power_watts": "Potência",
    "product_family": "Família do produto",
    "product_name": "Nome do produto",
    "size": "Tamanho",
    "smart_supported": "SMART disponível",
    "source": "Fonte",
    "speed": "Velocidade nominal",
    "status": "Estado",
    "temperature_celsius": "Temperatura",
    "total": "Total",
    "total_width": "Largura total",
    "transport": "Transporte",
    "type": "Tipo",
    "used": "Em uso",
    "used_percent": "Percentual em uso",
    "usage_percent": "Percentual de uso",
    "vram_total_mib": "VRAM total",
    "vram_used_mib": "VRAM usada",
    "voltage": "Tensão configurada",
    "voltage_now": "Tensão atual",
    "voltage_volts": "Tensão",
}
REPORT_PRIVATE_KEYS = {
    "serial", "serialnumber", "uuid", "systemuuid", "productuuid", "machineuuid",
    "machineid", "mac", "macaddress", "hardwareaddress", "assettag",
}


def report_label(value: str) -> str:
    return REPORT_SECTION_LABELS.get(value, REPORT_FIELD_LABELS.get(value, value.replace("_", " ").capitalize()))


def format_report_number(value: int | float, decimals: int) -> str:
    formatted = f"{value:,.{decimals}f}"
    return formatted.replace(",", "\0").replace(".", ",").replace("\0", ".")


def format_report_scalar(value: Any, key: str) -> str:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    if value is None:
        return UNAVAILABLE
    if isinstance(value, bool):
        return "Sim" if value else "Não"
    if isinstance(value, (int, float)):
        if normalized in {"size", "used", "available", "total", "swapused", "swapavailable", "swaptotal"}:
            return format_bytes(value).replace(".", ",")
        if normalized.endswith("mib"):
            return f"{format_report_number(value, 0)} MiB"
        if normalized.endswith("percent") or normalized in {"percent", "capacity"}:
            return f"{format_report_number(value, 1)}%"
        if normalized.endswith("celsius"):
            return f"{format_report_number(value, 1)} °C"
        if normalized.endswith("watts") or normalized.endswith("watt"):
            return f"{format_report_number(value, 1)} W"
        if normalized.endswith("mhz"):
            return format_frequency(value).replace(".", ",")
        if normalized.endswith("seconds"):
            return format_uptime(value)
        if normalized.endswith("wh"):
            return f"{format_report_number(value, 1)} Wh"
        if isinstance(value, int) or value.is_integer():
            return format_report_number(value, 0)
        return format_report_number(value, 2)
    return str(value)


def render_report_value(value: Any, key: str = "") -> str:
    normalized_key = re.sub(r"[^a-z0-9]", "", key.casefold())
    if normalized_key in REPORT_PRIVATE_KEYS:
        return '<span class="redacted">Ocultado por privacidade</span>'
    if isinstance(value, dict):
        rows = "".join(
            "<tr><th scope=\"row\">"
            + html.escape(report_label(str(child_key)))
            + "</th><td>"
            + render_report_value(child_value, str(child_key))
            + "</td></tr>"
            for child_key, child_value in value.items()
        )
        return f'<table><tbody>{rows}</tbody></table>' if rows else "<span>Sem dados</span>"
    if isinstance(value, (list, tuple)):
        if not value:
            return "<span>Sem itens</span>"
        items = "".join(f"<li>{render_report_value(item, key)}</li>" for item in value)
        return f"<ul>{items}</ul>"
    return html.escape(format_report_scalar(value, key), quote=True)


def generate_html_report(snapshot: dict[str, Any]) -> str:
    generated_at = snapshot.get("updated_at")
    sections = []
    for key, value in snapshot.items():
        if key == "updated_at":
            continue
        sections.append(
            "<section><h2>"
            + html.escape(report_label(str(key)))
            + "</h2>"
            + render_report_value(value, str(key))
            + "</section>"
        )
    return """<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Relatório do BooM</title>
  <style>
    :root { color-scheme: light; font-family: system-ui, sans-serif; color: #17202a; background: #f2f4f7; }
    body { max-width: 1100px; margin: 0 auto; padding: 32px 20px; }
    header, section { background: #fff; border: 1px solid #dce1e8; border-radius: 12px; padding: 20px; margin-bottom: 16px; }
    h1 { margin: 0 0 8px; font-size: 1.8rem; } h2 { margin: 0 0 14px; font-size: 1.1rem; }
    p, th { color: #586273; } table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid #edf0f4; padding: 8px 10px; text-align: left; vertical-align: top; }
    th { width: 32%; font-weight: 600; } td table { margin: 4px 0; }
    ul { margin: 4px 0; padding-left: 22px; } li { margin: 4px 0; }
    .redacted { color: #6c7582; font-style: italic; }
    .privacy { font-size: .9rem; }
    @media print { :root { background: #fff; } body { padding: 0; } header, section { break-inside: avoid; } }
  </style>
</head>
<body>
  <header>
    <h1>Relatório do BooM</h1>
    <p>Coleta local · Atualizado em: """ + html.escape(str(generated_at or UNAVAILABLE), quote=True) + """</p>
    <p class="privacy">Número de série, UUID e endereço MAC são ocultados. Endereços IP e rotas locais podem aparecer. O arquivo não carrega recursos externos nem envia dados.</p>
  </header>
  """ + "\n  ".join(sections) + """
</body>
</html>
"""


def write_html_report(path: str | Path, snapshot: dict[str, Any]) -> Path:
    destination = Path(path).expanduser()
    if not destination.name or destination.exists() and destination.is_dir():
        raise ValueError("Escolha o caminho de um arquivo HTML.")
    if destination.suffix.casefold() != ".html":
        destination = destination.with_name(f"{destination.name}.html")
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as report_file:
            temporary_path = Path(report_file.name)
            report_file.write(generate_html_report(snapshot))
            report_file.flush()
            os.fsync(report_file.fileno())
        os.replace(temporary_path, destination)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return destination.resolve()


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
        pages = NAVIGATION_PAGES
        page = PAGE_OVERVIEW
        data: dict[str, Any] = {}
        manager: dict[str, Any] = {"processes": [], "applications": [], "startup": []}
        net_results: list[dict[str, Any]] = []
        selected = 0
        page_scroll = {PAGE_SYSTEM: 0, PAGE_MEMORY: 0, PAGE_GRAPHICS: 0}
        page_max_scroll = {PAGE_SYSTEM: 0, PAGE_MEMORY: 0, PAGE_GRAPHICS: 0}
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
            curses_text(stdscr, 3, 2, "1 Painel  2 Sistema  3 Memória  4 Gráficos  5 Discos", attr=curses.A_BOLD)
            curses_text(stdscr, 4, 2, "6 Rede  7 Processos  8 Programas  9 Inicialização", attr=curses.A_BOLD)
            curses_text(stdscr, 5, 2, f"{pages[page]}", attr=curses.color_pair(1) | curses.A_BOLD)
            y = 7
            cpu = data.get("cpu", {})
            mem = data.get("memory") or {}
            os_data = data.get("os", {})
            disks = data.get("disks", [])
            network = data.get("network", {})
            if page == PAGE_OVERVIEW:
                usage = cpu.get("usage_percent")
                curses_text(stdscr, y, 2, f"CPU       {usage:.1f}%" if isinstance(usage, (int, float)) else f"CPU       {UNAVAILABLE}", attr=curses.color_pair(3) | curses.A_BOLD); y += 1
                curses_text(stdscr, y, 2, f"Processador  {cpu.get('model', UNAVAILABLE)}", width=width - 4); y += 1
                frequency = cpu.get("frequency", {})
                curses_text(stdscr, y, 2, f"Núcleos      {cpu.get('cores') or UNAVAILABLE} · clock médio {format_frequency(frequency.get('average_mhz'))}", width=width - 4); y += 1
                curses_text(stdscr, y, 2, f"Memória   {format_bytes(mem.get('used'))} usados / {format_bytes(mem.get('total'))}", attr=curses.color_pair(3) | curses.A_BOLD); y += 1
                gpu = next(iter(data.get("gpu_metrics", [])), {})
                gpu_name = gpu.get("name") or next(iter(data.get("gpus", [])), UNAVAILABLE)
                gpu_usage = gpu.get("gpu_utilization_percent")
                gpu_temp = gpu.get("temperature_celsius")
                gpu_details = " · ".join(filter(None, (
                    f"{gpu_usage:.0f}%" if isinstance(gpu_usage, (int, float)) else None,
                    f"{gpu_temp:.0f} °C" if isinstance(gpu_temp, (int, float)) else None,
                    format_frequency(gpu.get("graphics_clock_mhz")) if gpu else None,
                )))
                curses_text(stdscr, y, 2, f"GPU          {gpu_name}" + (f" · {gpu_details}" if gpu_details else ""), width=width - 4); y += 1
                gpu_memory_activity = gpu.get("memory_utilization_percent")
                curses_text(stdscr, y, 2, f"VRAM         {format_vram_usage(gpu)}", width=width - 4); y += 1
                curses_text(stdscr, y, 2, f"Controlador de memória GPU   {gpu_memory_activity:.0f}%" if isinstance(gpu_memory_activity, (int, float)) else f"Controlador de memória GPU   {UNAVAILABLE}", width=width - 4); y += 1
                curses_text(stdscr, y, 2, f"Processos   {len(manager.get('processes', []))} · Aplicativos   {len(manager.get('applications', []))}"); y += 1
                if disks:
                    for disk in disks[:3]:
                        curses_text(stdscr, y, 2, f"Disco {disk['mount']}   {disk['percent']}% usado · {format_bytes(disk['available'])} livre", width=width - 4); y += 1
                else:
                    curses_text(stdscr, y, 2, "Discos   Sem volumes disponíveis"); y += 1
                y += 1
                temps = data.get("temperatures", [])
                curses_text(stdscr, y, 2, "Temperaturas  " + (" · ".join(f"{t['name']}: {t['celsius']:.1f} °C" for t in temps[:3]) if temps else UNAVAILABLE), width=width - 4)
            elif page == PAGE_SYSTEM:
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
                    f"Frequência reportada média da CPU: {format_frequency(cpu.get('frequency', {}).get('average_mhz'))} · Fonte: {cpu.get('frequency', {}).get('source') or UNAVAILABLE}",
                    f"Ligado há: {format_uptime(data.get('uptime_seconds'))}",
                ]
                for core in cpu.get("frequency", {}).get("cores", []):
                    system_rows.append(f"Clock CPU{core['core']}: {format_frequency(core['mhz'])}")
                for item in data.get("temperatures", []):
                    system_rows.append(f"Temperatura · {item['name']}: {item['celsius']:.1f} °C")
                if not data.get("temperatures"):
                    system_rows.append("Temperaturas: sem sensores compatíveis disponíveis")
                power = data.get("power", {})
                batteries = power.get("batteries", [])
                if batteries:
                    for battery in batteries:
                        capacity = battery.get("capacity_percent")
                        detail = f"{capacity:.0f}% · {battery.get('status', UNAVAILABLE)}" if isinstance(capacity, (int, float)) else battery.get("status", UNAVAILABLE)
                        system_rows.append(f"Energia · {battery.get('name', 'Bateria')}: {detail}")
                else:
                    system_rows.append("Energia: " + power.get("status", "Nenhuma bateria detectada."))
                visible = max(1, height - 11)
                page_max_scroll[page] = max(0, len(system_rows) - visible)
                page_scroll[page] = min(page_scroll[page], page_max_scroll[page])
                for line in system_rows[page_scroll[page]:page_scroll[page] + visible]:
                    curses_text(stdscr, y, 2, line, width=width - 4, attr=curses.color_pair(4) if line.startswith("Pressione M") else 0)
                    y += 1
            elif page == PAGE_MEMORY:
                used = mem.get("used")
                total = mem.get("total")
                available = mem.get("available")
                percent = used * 100 / total if isinstance(used, (int, float)) and total else None
                memory_rows = [
                    "RAM em uso: " + (f"{percent:.1f}%" if percent is not None else UNAVAILABLE),
                    f"RAM usada / total: {format_bytes(used)} / {format_bytes(total)}",
                    f"RAM disponível: {format_bytes(available)}",
                ]
                swap_total = mem.get("swap_total", 0)
                swap_used = mem.get("swap_used", 0)
                swap_available = mem.get("swap_available", 0)
                swap_percent = swap_used * 100 / swap_total if swap_total else None
                memory_rows.extend([
                    "Swap: " + (f"{swap_percent:.1f}% em uso" if swap_percent is not None else "Não configurada"),
                    f"Swap usada / total: {format_bytes(swap_used)} / {format_bytes(swap_total)}",
                    f"Swap disponível: {format_bytes(swap_available)}",
                    "",
                    "Especificações físicas da RAM · SMBIOS",
                ])
                modules = data.get("memory_modules", [])
                for index, module in enumerate(modules, start=1):
                    memory_rows.extend(memory_module_terminal_rows(module, index))
                if not modules:
                    message = data.get("memory_modules_status") or "Módulos SMBIOS não disponíveis"
                    memory_rows.append("Memória por módulo: " + message)
                    if data.get("memory_modules_needs_authorization"):
                        memory_rows.append("Pressione M para pedir autorização de leitura SMBIOS.")
                memory_rows.extend(["", "Memória de vídeo dedicada · VRAM"])
                gpu_metrics = data.get("gpu_metrics", [])
                if gpu_metrics:
                    for gpu in gpu_metrics:
                        memory_rows.extend(format_gpu_memory_terminal_rows(gpu))
                else:
                    memory_rows.append("VRAM/telemetria: " + data.get("gpu_metrics_status", UNAVAILABLE))
                visible = max(1, height - 11)
                page_max_scroll[page] = max(0, len(memory_rows) - visible)
                page_scroll[page] = min(page_scroll[page], page_max_scroll[page])
                for line in memory_rows[page_scroll[page]:page_scroll[page] + visible]:
                    curses_text(stdscr, y, 2, line, width=width - 4, attr=curses.color_pair(4) if line.startswith("Pressione M") else 0)
                    y += 1
            elif page == PAGE_GRAPHICS:
                graphics_rows = ["Adaptadores detectados: " + ("; ".join(data.get("gpus", [])) or UNAVAILABLE)]
                gpu_metrics = data.get("gpu_metrics", [])
                if gpu_metrics:
                    for gpu in gpu_metrics:
                        graphics_rows.extend(format_gpu_graphics_terminal_rows(gpu))
                        graphics_rows.append("")
                else:
                    graphics_rows.append("Telemetria: " + data.get("gpu_metrics_status", UNAVAILABLE))
                graphics_rows.append("Uso da VRAM, controlador de memória e clock da memória: área Memória.")
                visible = max(1, height - 11)
                page_max_scroll[page] = max(0, len(graphics_rows) - visible)
                page_scroll[page] = min(page_scroll[page], page_max_scroll[page])
                for line in graphics_rows[page_scroll[page]:page_scroll[page] + visible]:
                    curses_text(stdscr, y, 2, line, width=width - 4)
                    y += 1
            elif page == PAGE_DISKS:
                if not disks:
                    curses_text(stdscr, y, 2, "Nenhum volume montado disponível."); y += 1
                for disk in disks:
                    curses_text(stdscr, y, 2, f"{disk['mount']}   {disk['percent']}%", attr=curses.color_pair(3) | curses.A_BOLD); y += 1
                    curses_text(stdscr, y, 4, f"{format_bytes(disk['used'])} usados · {format_bytes(disk['available'])} livres de {format_bytes(disk['size'])}", width=width - 6); y += 1
                    curses_text(stdscr, y, 4, f"{disk['device']} · {disk['filesystem']}", width=width - 6); y += 2
                curses_text(stdscr, y, 2, "Discos físicos · saúde SMART", attr=curses.color_pair(2) | curses.A_BOLD); y += 1
                for device in data.get("smart_devices", [])[:5]:
                    model = device.get("model") or device.get("name") or UNAVAILABLE
                    state_text = "compatível · leitura exige ação/autorização" if device.get("smart_supported") else "leitura SMART indisponível"
                    curses_text(stdscr, y, 2, f"{device.get('device')} · {model} · {state_text}", width=width - 4); y += 1
                if not data.get("smart_devices"):
                    curses_text(stdscr, y, 2, data.get("smart_devices_status", "Nenhum disco físico compatível"), width=width - 4); y += 1
            elif page == PAGE_NETWORK:
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
            elif page == PAGE_PROCESSES:
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
            elif page == PAGE_APPLICATIONS:
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
            controls = "1-9 telas · ↑↓ rolar/lista · K encerrar · L abrir · M SMBIOS · R atualizar · Q sair"
            curses_text(stdscr, height - 1, 2, controls, width=width - 4)
            stdscr.refresh()
            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                return
            if key in tuple(ord(str(number)) for number in range(1, 10)):
                page = key - ord("1")
                selected = 0
                if page in page_scroll:
                    page_scroll[page] = 0
            elif key in (ord("r"), ord("R")):
                refresh()
            elif key in (ord("n"), ord("N")) and page == PAGE_NETWORK:
                status = "Testando conectividade; aguarde…"
                net_results = run_network_test(data)
                status = "Diagnóstico concluído às " + datetime.now().astimezone().strftime("%H:%M:%S")
            elif key in (curses.KEY_UP, ord("k")) and page in page_scroll:
                page_scroll[page] = max(0, page_scroll[page] - 1)
            elif key in (curses.KEY_DOWN, ord("j")) and page in page_scroll:
                page_scroll[page] = min(page_max_scroll[page], page_scroll[page] + 1)
            elif key in (curses.KEY_UP, ord("k"), ord("K")) and page in (PAGE_PROCESSES, PAGE_APPLICATIONS):
                if key == ord("K") and page == PAGE_PROCESSES:
                    confirm_terminate()
                elif key == ord("k") and page == PAGE_PROCESSES:
                    selected = max(0, selected - 1)
                else:
                    selected = max(0, selected - 1)
            elif key in (curses.KEY_DOWN, ord("j")) and page in (PAGE_PROCESSES, PAGE_APPLICATIONS):
                limit = len(manager.get("processes", [])) if page == PAGE_PROCESSES else len(manager.get("applications", []))
                selected = min(max(0, limit - 1), selected + 1)
            elif key in (ord("l"), ord("L")) and page == PAGE_APPLICATIONS:
                open_selected_app()
            elif key in (ord("m"), ord("M")) and page == PAGE_MEMORY and data.get("memory_modules_needs_authorization"):
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
        "window_metrics_timer": None,
        "closing": False,
        "syncing_autostart": False,
    }

    class BooMWindow(Gtk.ApplicationWindow):
        titles = list(NAVIGATION_PAGES)
        descriptions = list(NAVIGATION_DESCRIPTIONS)

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
            self.page = PAGE_OVERVIEW
            self.network_results: list[dict[str, Any]] = []
            self.smart_results: dict[str, dict[str, Any]] = {}
            self.smart_busy_devices: set[str] = set()
            self.live_metrics_busy = False
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
                if index == PAGE_OVERVIEW:
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
            self.report_button = Gtk.Button(label="Exportar HTML")
            self.report_button.connect("clicked", self.export_report)
            header.pack_end(self.report_button, False, False, 0)
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
            tray_note = "Bandeja e janela atualizam métricas a cada 5 s" if state["tray_available"] else "Sem bandeja · janela atualiza métricas a cada 5 s"
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
            if self.page == PAGE_OVERVIEW:
                grid = self.make_grid()
                cpu_card = self.card("Processador")
                usage = cpu.get("usage_percent")
                cpu_frequency = cpu.get("frequency", {})
                cpu_detail = f"{cpu.get('model', UNAVAILABLE)} · clock médio {format_frequency(cpu_frequency.get('average_mhz'))}"
                self.metric(cpu_card, f"{usage:.1f}%" if isinstance(usage, (int, float)) else UNAVAILABLE, cpu_detail)
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
                gpu = next(iter(self.data.get("gpu_metrics", [])), {})
                gpu_inventory = self.data.get("gpus", [])
                gpu_name = gpu.get("name") or next(iter(gpu_inventory), UNAVAILABLE)
                gpu_usage = gpu.get("gpu_utilization_percent")
                gpu_temp = gpu.get("temperature_celsius")
                gpu_detail = " · ".join(filter(None, (
                    gpu_name,
                    f"{gpu_temp:.1f} °C" if isinstance(gpu_temp, (int, float)) else None,
                    f"clock {format_frequency(gpu.get('graphics_clock_mhz'))}" if gpu else None,
                )))
                gpu_card = self.card("Placa de vídeo")
                self.metric(gpu_card, f"{gpu_usage:.1f}% de uso" if isinstance(gpu_usage, (int, float)) else "Telemetria indisponível", gpu_detail)
                for label, value in (
                    ("Clock da memória", format_frequency(gpu.get("memory_clock_mhz")) if gpu else UNAVAILABLE),
                    ("VRAM usada / total", format_vram_usage(gpu)),
                    ("Atividade do controlador de memória", f"{gpu['memory_utilization_percent']:.0f}%" if isinstance(gpu.get("memory_utilization_percent"), (int, float)) else UNAVAILABLE),
                    ("Potência", f"{gpu['power_draw_watts']:.1f} W" if isinstance(gpu.get("power_draw_watts"), (int, float)) else UNAVAILABLE),
                ):
                    self.add_row(gpu_card, label, value)
                power = self.data.get("power", {})
                battery = next(iter(power.get("batteries", [])), {})
                power_card = self.card("Energia")
                if battery:
                    capacity = battery.get("capacity_percent")
                    self.metric(power_card, f"Bateria {capacity:.0f}%" if isinstance(capacity, (int, float)) else "Bateria detectada", battery.get("status", UNAVAILABLE))
                    self.add_row(power_card, "Potência", f"{battery['power_watts']:.1f} W" if isinstance(battery.get("power_watts"), (int, float)) else UNAVAILABLE)
                else:
                    self.metric(power_card, power.get("status", "Nenhuma bateria detectada."), "Leitura local de /sys/class/power_supply")
                for index, card in enumerate((cpu_card, mem_card, gpu_card, disk_card, os_card)):
                    grid.attach(card, index % 2, index // 2, 1, 1)
                grid.attach(power_card, 1, 2, 1, 1)
                self.content.pack_start(grid, False, False, 0)
                thermal = self.card("Sensores térmicos")
                if self.data.get("temperatures"):
                    for item in self.data["temperatures"]:
                        self.add_row(thermal, item["name"], f"{item['celsius']:.1f} °C")
                else:
                    self.add_row(thermal, "Leitura", "Sem sensores compatíveis disponíveis")
                self.add_card(thermal)
            elif self.page == PAGE_SYSTEM:
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
                    ("Frequência reportada média da CPU", format_frequency(cpu.get("frequency", {}).get("average_mhz"))),
                    ("Tempo ligado", format_uptime(self.data.get("uptime_seconds"))),
                    ("Computador", os_data.get("hostname", UNAVAILABLE)),
                ):
                    self.add_row(system, label, value)
                self.add_card(system)
                cpu_clocks = self.card("Clock atual por núcleo")
                frequency_cores = cpu.get("frequency", {}).get("cores", [])
                if frequency_cores:
                    for core in frequency_cores:
                        self.add_row(cpu_clocks, f"CPU {core['core']}", format_frequency(core["mhz"]))
                    self.add_row(cpu_clocks, "Fonte", cpu.get("frequency", {}).get("source") or UNAVAILABLE)
                else:
                    self.add_row(cpu_clocks, "Leitura", "Frequência atual por núcleo indisponível neste kernel/driver")
                self.add_card(cpu_clocks)
                thermal = self.card("Temperaturas")
                for item in self.data.get("temperatures", []) or [{"name": "Sensores", "celsius": None}]:
                    self.add_row(thermal, item["name"], f"{item['celsius']:.1f} °C" if item["celsius"] is not None else "Sem leitura disponível")
                self.add_card(thermal)
                energy = self.card("Energia e bateria")
                power = self.data.get("power", {})
                batteries = power.get("batteries", [])
                if batteries:
                    for battery in batteries:
                        self.add_row(energy, battery.get("name", "Bateria"), f"{battery['capacity_percent']:.0f}% · {battery.get('status', UNAVAILABLE)}" if isinstance(battery.get("capacity_percent"), (int, float)) else battery.get("status", UNAVAILABLE))
                        self.add_row(energy, "Energia armazenada", f"{battery['energy_now_wh']:.1f} / {battery['energy_full_wh']:.1f} Wh" if isinstance(battery.get("energy_now_wh"), (int, float)) and isinstance(battery.get("energy_full_wh"), (int, float)) else UNAVAILABLE)
                        self.add_row(energy, "Potência atual", f"{battery['power_watts']:.1f} W" if isinstance(battery.get("power_watts"), (int, float)) else UNAVAILABLE)
                else:
                    self.add_row(energy, "Estado", power.get("status", "Nenhuma bateria detectada."))
                if power.get("ac_online") is not None:
                    self.add_row(energy, "Alimentação externa", "Conectada" if power["ac_online"] else "Desconectada")
                self.add_card(energy)
            elif self.page == PAGE_MEMORY:
                used = memory.get("used")
                total = memory.get("total")
                available = memory.get("available")
                percent = used * 100 / total if isinstance(used, (int, float)) and total else None
                ram = self.card("Memória RAM do sistema")
                self.metric(
                    ram,
                    f"{percent:.1f}% em uso" if percent is not None else UNAVAILABLE,
                    f"{format_bytes(used)} usados de {format_bytes(total)}",
                )
                progress = Gtk.ProgressBar()
                progress.set_fraction(max(0.0, min(1.0, (percent or 0) / 100)))
                ram.pack_start(progress, False, False, 2)
                self.add_row(ram, "Disponível", format_bytes(available))
                self.add_row(ram, "Capacidade total", format_bytes(total))
                self.add_card(ram)

                swap_total = memory.get("swap_total", 0)
                swap_used = memory.get("swap_used", 0)
                swap_available = memory.get("swap_available", 0)
                swap_percent = swap_used * 100 / swap_total if swap_total else None
                swap = self.card("Memória virtual · Swap")
                self.metric(
                    swap,
                    f"{swap_percent:.1f}% em uso" if swap_percent is not None else "Não configurada",
                    f"{format_bytes(swap_used)} usados de {format_bytes(swap_total)}" if swap_total else "Este sistema não tem espaço de swap configurado.",
                )
                swap_progress = Gtk.ProgressBar()
                swap_progress.set_fraction(max(0.0, min(1.0, (swap_percent or 0) / 100)))
                swap.pack_start(swap_progress, False, False, 2)
                self.add_row(swap, "Disponível", format_bytes(swap_available) if swap_total else UNAVAILABLE)
                self.add_card(swap)

                modules_card = self.card("Módulos físicos de RAM · SMBIOS")
                modules = self.data.get("memory_modules", [])
                if modules:
                    for index, module in enumerate(modules, start=1):
                        module_name = module.get("locator") or f"Módulo {index}"
                        self.add_row(modules_card, module_name, f"{module.get('size') or UNAVAILABLE} · {module.get('type') or 'RAM'} · {module.get('form_factor') or 'formato indisponível'}")
                        self.add_row(modules_card, "Velocidade configurada", format_memory_speed(module.get("configured_speed", ""), module.get("type", "")))
                        self.add_row(modules_card, "Velocidade nominal", format_memory_speed(module.get("speed", ""), module.get("type", "")))
                        self.add_row(modules_card, "Fabricante", module.get("manufacturer") or UNAVAILABLE)
                        self.add_row(modules_card, "Modelo (part number)", module.get("part_number") or UNAVAILABLE)
                        widths = " · ".join(part for part in (module.get("data_width"), module.get("total_width")) if part)
                        if widths:
                            self.add_row(modules_card, "Largura de dados / total", widths)
                        if module.get("voltage"):
                            self.add_row(modules_card, "Tensão configurada", module["voltage"])
                else:
                    self.add_row(modules_card, "Detalhes por módulo", self.data.get("memory_modules_status") or "Não fornecidos pelo firmware")
                    if self.data.get("memory_modules_needs_authorization"):
                        authorize = Gtk.Button(label="Ler dados SMBIOS com autorização")
                        authorize.get_style_context().add_class("action")
                        authorize.connect("clicked", self.authorize_memory_details)
                        modules_card.pack_start(authorize, False, False, 4)
                self.add_card(modules_card)

                gpu_metrics = self.data.get("gpu_metrics", [])
                if gpu_metrics:
                    for gpu in gpu_metrics:
                        vram_card = self.card(f"VRAM · {gpu.get('name') or 'GPU'}")
                        vram_percent = vram_usage_percent(gpu)
                        self.metric(vram_card, format_vram_usage(gpu), "Memória dedicada atualmente alocada pelo driver.")
                        vram_progress = Gtk.ProgressBar()
                        vram_progress.set_fraction(max(0.0, min(1.0, (vram_percent or 0) / 100)))
                        vram_card.pack_start(vram_progress, False, False, 2)
                        activity = gpu.get("memory_utilization_percent")
                        self.add_row(vram_card, "Atividade do controlador", f"{activity:.0f}%" if isinstance(activity, (int, float)) else UNAVAILABLE)
                        self.add_row(vram_card, "Clock da memória gráfica", format_frequency(gpu.get("memory_clock_mhz")))
                        self.add_card(vram_card)
                else:
                    vram_card = self.card("Memória da GPU · VRAM")
                    for gpu in self.data.get("gpus", []) or [UNAVAILABLE]:
                        self.add_row(vram_card, "Adaptador", gpu)
                    self.add_row(vram_card, "Telemetria", self.data.get("gpu_metrics_status") or "VRAM não exposta pelo adaptador/driver")
                    self.add_card(vram_card)
            elif self.page == PAGE_GRAPHICS:
                inventory = self.card("Adaptadores gráficos detectados")
                for gpu in self.data.get("gpus", []) or [UNAVAILABLE + " · instale pciutils para consultar"]:
                    self.add_row(inventory, "GPU", gpu)
                self.add_card(inventory)
                gpu_metrics = self.data.get("gpu_metrics", [])
                if gpu_metrics:
                    for gpu in gpu_metrics:
                        graphics = self.card(gpu.get("name") or "GPU")
                        self.add_row(graphics, "Driver", gpu.get("driver_version") or UNAVAILABLE)
                        for label, key, suffix in (
                            ("Uso do núcleo gráfico", "gpu_utilization_percent", "%"),
                            ("Temperatura", "temperature_celsius", " °C"),
                            ("Ventoinha", "fan_percent", "%"),
                            ("Consumo de potência", "power_draw_watts", " W"),
                            ("Limite de potência", "power_limit_watts", " W"),
                        ):
                            value = gpu.get(key)
                            self.add_row(graphics, label, f"{value:.1f}{suffix}" if isinstance(value, (int, float)) else UNAVAILABLE)
                        self.add_row(graphics, "Clock gráfico", format_frequency(gpu.get("graphics_clock_mhz")))
                        self.add_row(graphics, "Estado de desempenho", gpu.get("performance_state") or UNAVAILABLE)
                        generation = gpu.get("pcie_generation")
                        width = gpu.get("pcie_link_width")
                        link = f"Geração {generation} · x{width}" if isinstance(generation, (int, float)) or isinstance(width, (int, float)) else UNAVAILABLE
                        self.add_row(graphics, "Link PCIe atual", link)
                        self.add_card(graphics)
                else:
                    telemetry = self.card("Telemetria da GPU")
                    self.add_row(telemetry, "Estado", self.data.get("gpu_metrics_status") or "Métricas ao vivo não fornecidas pelo adaptador/driver")
                    self.add_card(telemetry)
                note = self.card("Memória gráfica")
                self.add_row(note, "VRAM, atividade do controlador e clock da memória", "Consulte a área Memória para ver uso e capacidade.")
                self.add_card(note)
            elif self.page == PAGE_DISKS:
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
                smart_card = self.card("Discos físicos e saúde SMART")
                smart_devices = self.data.get("smart_devices", [])
                self.add_row(smart_card, "Detecção", self.data.get("smart_devices_status", "Dispositivos não consultados"))
                if not smart_devices:
                    self.add_row(smart_card, "Estado", "Nenhum disco físico compatível detectado")
                for device in smart_devices:
                    model = device.get("model") or device.get("name") or UNAVAILABLE
                    self.add_row(smart_card, device.get("device", "Disco"), f"{model} · {format_bytes(device.get('size'))}")
                    result = self.smart_results.get(device.get("device", ""))
                    if result:
                        self.add_row(smart_card, "Saúde informada pelo dispositivo", result.get("message", UNAVAILABLE))
                        if result.get("temperature_celsius") is not None:
                            self.add_row(smart_card, "Temperatura SMART", f"{result['temperature_celsius']:.1f} °C")
                        if result.get("power_on_hours") is not None:
                            self.add_row(smart_card, "Horas ligado", f"{result['power_on_hours']:.0f} h")
                        if result.get("percentage_used") is not None:
                            self.add_row(smart_card, "Vida útil consumida (NVMe)", f"{result['percentage_used']}%")
                        if result.get("available_spare_percent") is not None:
                            self.add_row(smart_card, "Reserva disponível (NVMe)", f"{result['available_spare_percent']}%")
                    read_button = Gtk.Button(label="Ler SMART · solicitar autorização" if device.get("smart_supported") else "SMART indisponível para este disco")
                    read_button.set_sensitive(bool(device.get("smart_supported")))
                    read_button.set_tooltip_text("Ação somente leitura; o sistema poderá pedir autorização de administrador.")
                    read_button.connect("clicked", self.check_smart_device, device)
                    smart_card.pack_start(read_button, False, False, 4)
                self.add_card(smart_card)
            elif self.page == PAGE_NETWORK:
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
            elif self.page == PAGE_PROCESSES:
                self.render_process_page()
            elif self.page == PAGE_APPLICATIONS:
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
            monitor_status = "CPU, clocks, RAM, GPU e temperaturas atualizam a cada 5 s" if tray_ready else "Bandeja indisponível; métricas atualizam na janela a cada 5 s"
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

        def export_report(self, *_args: Any) -> None:
            if not self.data:
                self.refresh_state.set_text("Aguarde a leitura do sistema antes de exportar o relatório.")
                return
            dialog = Gtk.FileChooserDialog(
                title="Salvar relatório local do BooM",
                parent=self,
                action=Gtk.FileChooserAction.SAVE,
            )
            dialog.add_buttons(
                "Cancelar", Gtk.ResponseType.CANCEL,
                "Salvar relatório", Gtk.ResponseType.ACCEPT,
            )
            dialog.set_do_overwrite_confirmation(True)
            dialog.set_current_name(
                f"boom-relatorio-{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}.html"
            )
            html_filter = Gtk.FileFilter()
            html_filter.set_name("Relatório HTML")
            html_filter.add_mime_type("text/html")
            html_filter.add_pattern("*.html")
            dialog.add_filter(html_filter)
            try:
                if dialog.run() != Gtk.ResponseType.ACCEPT:
                    return
                selected_path = dialog.get_filename()
                if not selected_path:
                    self.refresh_state.set_text("Exportação cancelada.")
                    return
                report_data = dict(self.data)
                if self.smart_results:
                    report_data["smart_readings"] = dict(self.smart_results)
                try:
                    saved_path = write_html_report(selected_path, report_data)
                except (OSError, ValueError) as exc:
                    self.refresh_state.set_text(f"Não foi possível salvar o relatório: {exc}")
                    return
                self.refresh_state.set_text(f"Relatório local salvo: {saved_path}")
            finally:
                dialog.destroy()

        def finish_refresh(self, result: dict[str, Any], manager_data: dict[str, Any]) -> bool:
            self.data = result
            self.manager_data = manager_data
            self.refresh_button.set_sensitive(True)
            self.refresh_state.set_text("Atualizado às " + datetime.now().astimezone().strftime("%H:%M:%S"))
            self.render_page()
            return False

        def apply_live_metrics(self, metrics: dict[str, Any]) -> None:
            if not self.data:
                return
            self.data.setdefault("cpu", {})["usage_percent"] = metrics.get("cpu_percent")
            self.data["cpu"]["frequency"] = metrics.get("cpu_frequency") or {}
            self.data["memory"] = metrics.get("memory")
            self.data["gpu_metrics"] = metrics.get("gpu_metrics", [])
            self.data["gpu_metrics_status"] = metrics.get("gpu_metrics_status", UNAVAILABLE)
            self.data["temperatures"] = metrics.get("temperatures", [])
            self.data["power"] = metrics.get("power", {})
            updated_at = metrics.get("updated_at") or datetime.now().astimezone().strftime("%H:%M:%S")
            self.refresh_state.set_text(f"Métricas ao vivo · Atualizado às {updated_at}")
            if self.page in (PAGE_OVERVIEW, PAGE_SYSTEM, PAGE_MEMORY, PAGE_GRAPHICS):
                self.render_page()

        def refresh_live_metrics(self) -> None:
            if self.live_metrics_busy or not self.data or not self.get_visible():
                return
            self.live_metrics_busy = True

            def worker() -> None:
                try:
                    metrics = collect_tray_metrics()
                    GLib.idle_add(self.finish_live_metrics, metrics, None)
                except Exception as exc:
                    GLib.idle_add(self.finish_live_metrics, {}, str(exc))

            threading.Thread(target=worker, name="boom-window-monitor", daemon=True).start()

        def finish_live_metrics(self, metrics: dict[str, Any], error: str | None) -> bool:
            self.live_metrics_busy = False
            if error:
                self.refresh_state.set_text(f"Falha ao atualizar métricas ao vivo: {error}")
            else:
                self.apply_live_metrics(metrics)
            return False

        def check_smart_device(self, button: Any, device_info: dict[str, Any]) -> None:
            key = str(device_info.get("device", ""))
            if not key or key in self.smart_busy_devices:
                return
            self.smart_busy_devices.add(key)
            button.set_sensitive(False)
            button.set_label("Lendo dados SMART…")

            def worker() -> None:
                result = read_smart_device(device_info)
                GLib.idle_add(self.finish_smart_check, key, result, button, bool(device_info.get("smart_supported")))

            threading.Thread(target=worker, name="boom-smart-reader", daemon=True).start()

        def finish_smart_check(self, key: str, result: dict[str, Any], button: Any, supported: bool) -> bool:
            self.smart_busy_devices.discard(key)
            self.smart_results[key] = result
            button.set_sensitive(supported)
            button.set_label("Consultar SMART novamente" if result.get("available") else "Tentar leitura SMART novamente")
            self.refresh_state.set_text(result.get("message", "Leitura SMART concluída."))
            if self.page == PAGE_DISKS:
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
        if state.get("window_metrics_timer") is None:
            state["window_metrics_timer"] = GLib.timeout_add_seconds(5, refresh_visible_window)
        return window

    def refresh_visible_window() -> bool:
        if state["closing"]:
            state["window_metrics_timer"] = None
            return False
        window = state.get("window")
        if window is None or not window.get_visible() or state["tray_available"]:
            return True
        window.refresh_live_metrics()
        return True

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
            ("cpu_clock_item", labels["cpu_clock"]),
            ("memory_item", labels["memory"]),
            ("battery_item", labels["battery"]),
            ("gpu_item", labels["gpu"]),
            ("gpu_memory_activity_item", labels["gpu_memory_activity"]),
            ("gpu_clock_item", labels["gpu_clock"]),
            ("gpu_memory_item", labels["vram"]),
            ("gpu_power_item", labels["gpu_power"]),
            ("gpu_fan_item", labels["gpu_fan"]),
            ("gpu_link_item", labels["gpu_link"]),
            ("temperature_item", labels["temperature"]),
            ("updated_item", labels["updated"]),
        ):
            item = state.get(key)
            if item is not None:
                item.set_label(label)
        indicator.set_title(labels["title"])
        window = state.get("window")
        if window is not None and window.get_visible():
            window.apply_live_metrics(metrics)
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
            cpu_clock_item = Gtk.MenuItem(label="Clock médio da CPU: lendo…")
            memory_item = Gtk.MenuItem(label="RAM: lendo…")
            battery_item = Gtk.MenuItem(label="Energia: lendo…")
            gpu_item = Gtk.MenuItem(label="GPU: lendo…")
            gpu_memory_activity_item = Gtk.MenuItem(label="Atividade do controlador de memória: lendo…")
            gpu_clock_item = Gtk.MenuItem(label="Clocks da GPU: lendo…")
            gpu_memory_item = Gtk.MenuItem(label="VRAM: lendo…")
            gpu_power_item = Gtk.MenuItem(label="Potência da GPU: lendo…")
            gpu_fan_item = Gtk.MenuItem(label="Ventoinha da GPU: lendo…")
            gpu_link_item = Gtk.MenuItem(label="Link PCIe da GPU: lendo…")
            temperature_item = Gtk.MenuItem(label="Temperatura: lendo…")
            updated_item = Gtk.MenuItem(label="Aguardando primeira leitura…")
            for item in (
                cpu_item, cpu_clock_item, memory_item, battery_item, gpu_item, gpu_memory_activity_item, gpu_clock_item,
                gpu_memory_item, gpu_power_item, gpu_fan_item, gpu_link_item,
                temperature_item, updated_item,
            ):
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
            "cpu_clock_item": cpu_clock_item,
            "memory_item": memory_item,
            "battery_item": battery_item,
            "gpu_item": gpu_item,
            "gpu_memory_activity_item": gpu_memory_activity_item,
            "gpu_clock_item": gpu_clock_item,
            "gpu_memory_item": gpu_memory_item,
            "gpu_power_item": gpu_power_item,
            "gpu_fan_item": gpu_fan_item,
            "gpu_link_item": gpu_link_item,
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
        window_timer = state.get("window_metrics_timer")
        if window_timer is not None:
            try:
                GLib.source_remove(window_timer)
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
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--snapshot", action="store_true", help="imprime os dados atuais como JSON")
    mode_group.add_argument("--report-html", metavar="CAMINHO", help="salva um relatório local HTML no caminho indicado")
    mode_group.add_argument("--network-test", action="store_true", help="executa ping para o gateway e 1.1.1.1")
    mode_group.add_argument("--tui", action="store_true", help="abre o painel interativo no terminal")
    mode_group.add_argument("--tray", action="store_true", help="inicia oculto e monitora CPU, clocks, RAM, GPU e temperaturas")
    mode_group.add_argument("--enable-autostart", action="store_true", help="inicia o BooM oculto ao entrar na sessão")
    mode_group.add_argument("--disable-autostart", action="store_true", help="remove o início automático do BooM")
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
    if args.report_html is not None:
        try:
            output_path = write_html_report(args.report_html, collect_snapshot())
        except (OSError, ValueError) as exc:
            print(f"Não foi possível salvar o relatório: {exc}", file=sys.stderr)
            return 1
        print(f"Relatório HTML salvo localmente: {output_path}")
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
