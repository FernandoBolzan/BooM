#!/usr/bin/env python3
"""BooM: painel local de sistema para Linux, com GTK e modo de terminal."""

from __future__ import annotations

import argparse
import configparser
import curses
import csv
import html
import json
import math
import os
import platform
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from boom_local_ai import LocalAIError, explain_performance_diagnosis, local_ollama_models, OLLAMA_DOWNLOAD_URL
from boom_performance import (
    HISTORY_SAMPLE_INTERVAL_SECONDS,
    MIN_HISTORY_SAMPLES,
    DiskIoSampler,
    PerformanceHistoryStore,
    analyze_performance_history,
)
from boom_disk_benchmark import (
    DISK_BENCHMARK_RANDOM_JOB_SECONDS,
    DISK_BENCHMARK_SIZE_MIB,
    DISK_BENCHMARK_TIMEOUT_SECONDS,
    DiskBenchmarkSession,
    disk_benchmark_readiness,
)
from boom_sysbench import (
    SYSBENCH_ABORT_TEMP_C,
    SYSBENCH_CPU_DURATION_SECONDS,
    SYSBENCH_MEMORY_MAX_BUFFER_BYTES,
    SYSBENCH_MEMORY_MIN_AVAILABLE_BYTES,
    SYSBENCH_MEMORY_PASS_SECONDS,
    SYSBENCH_PRECHECK_TEMP_C,
    SysbenchBenchmarkSession,
    sysbench_benchmark_readiness,
)
from boom_test_workflow import TestSessions, test_action_for_shortcut, test_action_legend
from boom_games import (
    STEAM_LAUNCH_OPTIONS,
    heroic_library_info,
    gaming_tool_status,
    installed_game_list,
    launch_installed_game,
    lutris_library_info,
    steam_library_info,
)


ROOT = Path(__file__).resolve().parent
__version__ = "0.2.2"
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
    PAGE_TESTS,
) = range(10)
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
    "Testes",
)
NAVIGATION_DESCRIPTIONS = (
    "Resumo ao vivo do seu computador.",
    "Detalhes do hardware, sensores e perfis de energia suportados.",
    "Uso de RAM, swap, VRAM e especificações dos módulos.",
    "Telemetria de GPUs, clocks, temperatura, potência e link PCIe.",
    "Volumes montados e espaço disponível.",
    "Interfaces e diagnóstico de conectividade.",
    "Uso de recursos e encerramento normal de processos.",
    "Aplicativos gráficos instalados e seus lançadores Linux.",
    "Programas configurados para iniciar junto com sua sessão.",
    "Cargas e benchmarks controlados de CPU, RAM, GPU e armazenamento.",
)
NAVIGATION_GROUPS = (
    ("Monitorar", (PAGE_OVERVIEW, PAGE_SYSTEM, PAGE_MEMORY, PAGE_GRAPHICS, PAGE_DISKS, PAGE_NETWORK)),
    ("Gerenciar", (PAGE_PROCESSES, PAGE_APPLICATIONS, PAGE_STARTUP)),
    ("Diagnosticar", (PAGE_TESTS,)),
)
NAVIGATION_GROUP_BY_PAGE = {
    page: title for title, pages in NAVIGATION_GROUPS for page in pages
}
NAVIGATION_ICONS = (
    "view-grid-symbolic",
    "computer-symbolic",
    "drive-harddisk-symbolic",
    "video-display-symbolic",
    "drive-harddisk-symbolic",
    "network-wired-symbolic",
    "system-run-symbolic",
    "application-x-executable-symbolic",
    "system-run-symbolic",
    "utilities-system-monitor-symbolic",
)
STRESS_TEST_DURATIONS = (10, 30, 60, 120)
STRESS_TEST_PRECHECK_TEMP_C = 80.0
STRESS_TEST_ABORT_TEMP_C = 85.0
STRESS_TEST_MAX_CPU_WORKERS = 4
STRESS_TEST_MAX_MEMORY_BYTES = 1024**3
STRESS_TEST_MIN_MEMORY_BYTES = 128 * 1024**2
OPTIONAL_TOOL_PACKAGES = (
    ("lspci", "pciutils"),
    ("sensors", "lm-sensors"),
    ("smartctl", "smartmontools"),
    ("dmidecode", "dmidecode"),
    ("fwupdmgr", "fwupd"),
    ("pkexec", "pkexec"),
    ("ping", "iputils-ping"),
    ("stress-ng", "stress-ng"),
    ("sysbench", "sysbench"),
    ("fio", "fio"),
    ("mangohud", "mangohud"),
    ("gamemoderun", "gamemode"),
)
POWER_PROFILE_IDS = ("power-saver", "balanced", "performance")
POWER_PROFILE_LABELS = {
    "power-saver": "Economia de energia",
    "balanced": "Balanceado",
    "performance": "Desempenho",
}
_PERFORMANCE_HISTORY = PerformanceHistoryStore()
_DISK_IO_SAMPLER = DiskIoSampler()
GTK_CSS = b"""
.boom-root { font-family: 'Fira Sans', 'Ubuntu', sans-serif; background-color: #f1f5f1; color: #17271d; }
.boom-root .sidebar { background-image: linear-gradient(180deg, #193526 0%, #10251a 48%, #0d1b14 100%); border: 1px solid #294734; border-radius: 18px; box-shadow: 0 8px 24px rgba(12, 30, 18, .10); }
.boom-root .brand-mark { background-color: #c5f36e; color: #18351f; border-radius: 12px; font-size: 25px; font-weight: 800; min-width: 42px; min-height: 42px; }
.boom-root .brand { color: #f4fbf3; font-size: 24px; font-weight: 800; letter-spacing: -.4px; }
.boom-root .eyebrow { color: #9ebba7; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
.boom-root .sidebar .muted { color: #9db5a3; }
.boom-root .sidebar .local-status { color: #c5f36e; }
.boom-root .nav-section { color: #84a08c; font-size: 10px; font-weight: 700; letter-spacing: 1px; margin-top: 18px; margin-bottom: 5px; }
.boom-root .nav-button { background-color: transparent; color: #c8d8cc; border: 1px solid transparent; border-radius: 11px; min-height: 44px; padding: 9px 14px; }
.boom-root .nav-button:hover { background-color: #244333; color: #ffffff; }
.boom-root .nav-button image { color: #9db6a4; }
.boom-root .nav-button.selected { background-color: #c5f36e; color: #17351e; border-color: #d3ff8c; box-shadow: 0 3px 10px rgba(4, 12, 6, .18); font-weight: 700; }
.boom-root .nav-button.selected image { color: #17351e; }
.boom-root .nav-button:focus { border-color: #f3ffde; box-shadow: 0 0 0 2px #a4d852; }
.boom-root .sidebar-separator { background-color: #385744; min-height: 1px; margin-top: 17px; margin-bottom: 12px; }
.boom-root .page-title { color: #14281b; font-size: 32px; font-weight: 700; }
.boom-root .page-subtitle { color: #65796a; font-size: 13px; }
.boom-root .page-overline { background-color: #e3f0db; color: #397047; border-radius: 20px; font-size: 10px; font-weight: 700; letter-spacing: .7px; padding: 4px 10px; }
.boom-root .page-divider { background-color: #dfe8e0; color: #dfe8e0; min-height: 1px; }
.boom-root .card { background-color: #ffffff; border: 1px solid #dce7dd; border-radius: 16px; padding: 26px; box-shadow: 0 3px 14px rgba(24, 53, 33, .07); }
.boom-root .card:hover { border-color: #c6d9c9; box-shadow: 0 5px 18px rgba(24, 53, 33, .10); }
.boom-root .card-title { color: #4c6252; font-size: 14px; font-weight: 600; }
.boom-root .card-divider { background-color: transparent; min-height: 0; margin: 0; opacity: 0; }
.boom-root .card.performance-cpu { background-image: linear-gradient(135deg, #ffffff 0%, #f6fbea 100%); border-top: 3px solid #a8dc58; }
.boom-root .card.performance-gpu { background-image: linear-gradient(135deg, #ffffff 0%, #edf9f6 100%); border-top: 3px solid #55c5ad; }
.boom-root .metric { color: #14281b; font-size: 36px; font-weight: 700; }
.boom-root .body-copy { color: #23392b; font-size: 13px; }
.boom-root .muted { color: #6b806f; font-size: 12px; }
.boom-root .accent { color: #387442; }
.boom-root .section-heading { border-left: 3px solid #a9d95f; margin-top: 8px; padding-left: 14px; }
.boom-root .section-title { color: #1a3523; font-size: 19px; font-weight: 700; }
.boom-root .section-detail { color: #718575; font-size: 12px; }
.boom-root .detail-row { border-bottom: 1px solid #eef2ee; padding-top: 7px; padding-bottom: 7px; }
.boom-root .content-grid { background-color: transparent; }
.boom-root .content-grid > flowboxchild { background-color: transparent; border: 0; padding: 0; }
.boom-root .action-flow { background-color: transparent; }
.boom-root .action-flow > flowboxchild { background-color: transparent; border: 0; margin: 0; padding: 0; }
.boom-root .footer { background-color: #fbfdfb; border-top: 1px solid #dce7dd; }
.boom-root .local-status { color: #377146; font-size: 10px; font-weight: 700; letter-spacing: .8px; }
.boom-root .metric-progress trough, .boom-root progressbar trough { background-color: #e7eee7; border-radius: 6px; min-height: 9px; }
.boom-root .metric-progress progress, .boom-root progressbar progress { background-color: #8dcb43; border-radius: 6px; min-height: 9px; }
.boom-root button { background-image: none; background-color: #ffffff; color: #294331; border: 1px solid #d3dfd5; border-radius: 10px; min-height: 40px; padding: 8px 13px; box-shadow: none; }
.boom-root button:hover { background-color: #f0f6f0; border-color: #a8c0ac; color: #183522; }
.boom-root button:active { background-color: #e4eee4; }
.boom-root button:focus { border-color: #58a54d; box-shadow: 0 0 0 2px rgba(116, 181, 72, .25); }
.boom-root button:disabled { color: #94a296; background-color: #f4f6f4; border-color: #e4e9e4; }
.boom-root button.action { background-color: #1d5835; color: #ffffff; border-color: #1d5835; font-weight: 700; }
.boom-root button.action:hover { background-color: #267346; border-color: #267346; color: #ffffff; }
.boom-root button.action:focus { border-color: #ffffff; box-shadow: 0 0 0 2px #76b84b; }
.boom-root button.destructive { color: #a6383d; border-color: #e2bfc0; }
.boom-root button.destructive:hover { background-color: #fff1f0; border-color: #ce8b8d; }
.boom-root entry, .boom-root searchentry, .boom-root combobox button { background-color: #ffffff; color: #203729; border: 1px solid #d3dfd5; border-radius: 10px; min-height: 38px; padding: 5px 10px; }
.boom-root entry:focus, .boom-root searchentry:focus, .boom-root combobox button:focus { border-color: #58a54d; box-shadow: 0 0 0 2px rgba(116, 181, 72, .2); }
.boom-root treeview.view { background-color: #ffffff; color: #263b2d; }
.boom-root treeview.view:selected { background-color: #e1f4d3; color: #183522; }
.boom-root treeview header button { background-color: #f2f6f2; color: #526b57; border-color: #dce7dd; border-radius: 0; }
.boom-root treeview.view row { min-height: 38px; }
.boom-root scrollbar slider { background-color: #bdcbbf; border-radius: 8px; min-width: 8px; min-height: 8px; }
.boom-root scrollbar slider:hover { background-color: #8fa791; }
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


def cpu_energy_preference_info(root: Path = Path("/sys/devices/system/cpu/cpufreq")) -> dict[str, Any]:
    """Read kernel EPP hints without writing CPUFreq policy files."""
    policies: list[dict[str, Any]] = []
    for path in sorted(root.glob("policy*"), key=lambda item: item.name):
        if not re.fullmatch(r"policy\d+", path.name):
            continue
        current = read_text(path / "energy_performance_preference")
        available = read_text(path / "energy_performance_available_preferences")
        if current is None and available is None:
            continue
        policies.append({
            "policy": path.name,
            "current": current.strip() if current else None,
            "available": available.split() if available else [],
        })

    preference_sets = [set(item["available"]) for item in policies]
    common = set.intersection(*preference_sets) if preference_sets and all(preference_sets) else set()
    first_available = next((item["available"] for item in policies if item["available"]), [])
    available_preferences = [value for value in first_available if value in common]
    current_values = {item["current"] for item in policies if item["current"]}
    return {
        "available": bool(policies),
        "policies": policies,
        "available_preferences": available_preferences,
        "current": next(iter(current_values)) if len(current_values) == 1 else None,
        "consistent": len(current_values) <= 1,
        "source": "CPUFreq/sysfs · energy_performance_preference",
        "message": (
            "Preferência energética reportada pelo driver da CPU."
            if policies else "O kernel/driver não expõe preferência energética CPUFreq neste sistema."
        ),
    }


def power_profile_control_supported(
    cpufreq_root: Path = Path("/sys/devices/system/cpu/cpufreq"),
    platform_profile_path: Path = Path("/sys/firmware/acpi/platform_profile"),
) -> bool:
    cpu_preferences = cpu_energy_preference_info(cpufreq_root)
    return bool(cpu_preferences["available"] or platform_profile_path.exists())


def parse_power_profiles_list(output: str) -> list[str]:
    profiles: list[str] = []
    pattern = re.compile(r"^\s*\*?\s*(power-saver|balanced|performance)(?=\s*[:\s]|$)")
    for line in output.splitlines():
        match = pattern.match(line)
        if match and match.group(1) not in profiles:
            profiles.append(match.group(1))
    return profiles


def xdg_state_home() -> Path:
    configured = os.environ.get("XDG_STATE_HOME", "")
    path = Path(configured).expanduser() if configured else Path.home() / ".local" / "state"
    return path if path.is_absolute() else Path.home() / ".local" / "state"


def power_profile_restore_path() -> Path:
    return xdg_state_home() / "boom" / "power-profile-restore.json"


def load_power_profile_restore_state(path: Path | None = None) -> dict[str, str] | None:
    target = path or power_profile_restore_path()
    if target.is_symlink():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    restore_profile = data.get("restore_profile")
    applied_profile = data.get("applied_profile")
    if restore_profile not in POWER_PROFILE_IDS or applied_profile not in POWER_PROFILE_IDS:
        return None
    return {"restore_profile": restore_profile, "applied_profile": applied_profile}


def save_power_profile_restore_state(state: dict[str, str], path: Path | None = None) -> Path:
    target = path or power_profile_restore_path()
    if target.is_symlink():
        raise OSError("O arquivo de restauração não pode ser um link simbólico.")
    if state.get("restore_profile") not in POWER_PROFILE_IDS or state.get("applied_profile") not in POWER_PROFILE_IDS:
        raise ValueError("Perfil de restauração inválido.")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.parent.is_symlink():
        raise OSError("O diretório de estado não pode ser um link simbólico.")
    os.chmod(target.parent, 0o700)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=target.parent, prefix=".boom-power-profile-", delete=False,
        ) as temporary:
            json.dump(state, temporary, ensure_ascii=False)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, target)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return target


def power_profile_manager_conflict() -> bool:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return False
    result = run_command([systemctl, "is-active", "tlp.service"], timeout=1.0)
    return bool(result and result.returncode == 0 and result.stdout.strip() == "active")


def power_profile_info() -> dict[str, Any]:
    cpu_preference = cpu_energy_preference_info()
    result: dict[str, Any] = {
        "supported": power_profile_control_supported(),
        "available": False,
        "command_installed": False,
        "profiles": [],
        "current": None,
        "restore_state": load_power_profile_restore_state(),
        "cpu_energy_preference": cpu_preference,
        "message": "",
    }
    if not result["supported"]:
        result["message"] = "Este kernel não expõe uma API de perfil de energia compatível."
        return result
    if power_profile_manager_conflict():
        result["message"] = "O serviço TLP está ativo; o BooM não altera perfis para evitar conflito entre gerenciadores."
        return result
    executable = shutil.which("powerprofilesctl")
    if not executable:
        result["message"] = "powerprofilesctl está ausente; instale power-profiles-daemon pelas ferramentas opcionais."
        return result
    result["command_installed"] = True
    listed = run_command([executable, "list"], timeout=2.0)
    current = run_command([executable, "get"], timeout=2.0)
    if listed is None or listed.returncode != 0 or current is None or current.returncode != 0:
        result["message"] = "power-profiles-daemon não respondeu. Confira o serviço de perfis de energia do sistema."
        return result
    profiles = parse_power_profiles_list(listed.stdout)
    active = current.stdout.strip()
    if not profiles or active not in POWER_PROFILE_IDS:
        result["message"] = "O serviço não informou perfis reconhecidos; o BooM não aplicará valores presumidos."
        return result
    result.update({
        "available": True,
        "profiles": profiles,
        "current": active,
        "message": "Perfis oferecidos pelo gerenciador de energia do sistema.",
    })
    restore_state = result["restore_state"]
    if restore_state and active != restore_state["applied_profile"]:
        result["external_change_detected"] = True
    return result


def set_power_profile(profile: str, state_path: Path | None = None) -> dict[str, Any]:
    info = power_profile_info()
    if not info["available"]:
        return {"ok": False, "message": info["message"]}
    if profile not in info["profiles"]:
        return {"ok": False, "message": "Este perfil não é oferecido pelo sistema."}
    current = info["current"]
    if profile == current:
        return {"ok": True, "message": f"O perfil {POWER_PROFILE_LABELS[profile]} já está ativo."}

    executable = shutil.which("powerprofilesctl")
    if not executable:
        return {"ok": False, "message": "powerprofilesctl deixou de estar disponível."}
    path = state_path or power_profile_restore_path()
    previous_state = load_power_profile_restore_state(path)
    restore_profile = previous_state["restore_profile"] if previous_state else current
    new_state = {"restore_profile": restore_profile, "applied_profile": profile}
    try:
        save_power_profile_restore_state(new_state, path)
    except (OSError, ValueError) as exc:
        return {"ok": False, "message": f"Não foi possível preparar a restauração: {exc}"}

    process = run_command([executable, "set", profile], timeout=5.0)
    verified = run_command([executable, "get"], timeout=2.0)
    if process is None or process.returncode != 0 or verified is None or verified.returncode != 0 or verified.stdout.strip() != profile:
        observed_profile = verified.stdout.strip() if verified and verified.returncode == 0 else None
        rollback_ok = observed_profile == current
        if observed_profile in POWER_PROFILE_IDS and observed_profile != current:
            rollback = run_command([executable, "set", current], timeout=5.0)
            rollback_check = run_command([executable, "get"], timeout=2.0)
            rollback_ok = bool(
                rollback and rollback.returncode == 0
                and rollback_check and rollback_check.returncode == 0
                and rollback_check.stdout.strip() == current
            )
        if rollback_ok:
            if previous_state:
                try:
                    save_power_profile_restore_state(previous_state, path)
                except OSError:
                    pass
            else:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        else:
            recovery_state = {
                "restore_profile": restore_profile,
                "applied_profile": observed_profile if observed_profile in POWER_PROFILE_IDS else profile,
            }
            try:
                save_power_profile_restore_state(recovery_state, path)
            except OSError:
                pass
        detail = ""
        if process and process.stderr.strip():
            detail = " " + process.stderr.strip().splitlines()[-1]
        recovery = " O perfil foi revertido." if rollback_ok else " O BooM preservou um caminho de restauração para revisão manual."
        return {"ok": False, "message": "Não foi possível confirmar a aplicação do perfil." + recovery + detail}
    return {
        "ok": True,
        "message": f"Perfil {POWER_PROFILE_LABELS[profile]} aplicado. O perfil anterior ({POWER_PROFILE_LABELS[restore_profile]}) pode ser restaurado pelo BooM.",
        "profile": profile,
    }


def restore_power_profile(state_path: Path | None = None) -> dict[str, Any]:
    path = state_path or power_profile_restore_path()
    state = load_power_profile_restore_state(path)
    if not state:
        return {"ok": False, "message": "Não há perfil anterior do BooM para restaurar."}
    info = power_profile_info()
    profile = state["restore_profile"]
    if not info["available"]:
        return {"ok": False, "message": info["message"]}
    if profile not in info["profiles"]:
        return {"ok": False, "message": "O perfil anterior não é mais oferecido pelo sistema; o registro foi mantido."}
    if info["current"] != profile:
        executable = shutil.which("powerprofilesctl")
        if not executable:
            return {"ok": False, "message": "powerprofilesctl deixou de estar disponível; o registro foi mantido."}
        process = run_command([executable, "set", profile], timeout=5.0)
        verified = run_command([executable, "get"], timeout=2.0)
        if process is None or process.returncode != 0 or verified is None or verified.returncode != 0 or verified.stdout.strip() != profile:
            return {"ok": False, "message": "Não foi possível confirmar a restauração; o registro foi mantido."}
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        return {"ok": False, "message": f"Perfil restaurado, mas não foi possível remover o registro: {exc}"}
    return {"ok": True, "message": f"Perfil anterior restaurado: {POWER_PROFILE_LABELS[profile]}.", "profile": profile}


def format_frequency(mhz: int | float | None) -> str:
    if not isinstance(mhz, (int, float)):
        return UNAVAILABLE
    return f"{mhz / 1000:.2f} GHz" if mhz >= 1000 else f"{mhz:.0f} MHz"


def format_local_ai_result(result: dict[str, Any]) -> str:
    """Render a local model's explanation alongside the deterministic evidence."""
    explanation = result.get("explanation") if isinstance(result.get("explanation"), dict) else {}
    diagnosis = result.get("diagnosis") if isinstance(result.get("diagnosis"), dict) else {}
    findings = diagnosis.get("findings") if isinstance(diagnosis.get("findings"), list) else []
    if not findings:
        findings = explanation.get("findings") if isinstance(explanation.get("findings"), list) else []
    findings_by_id = {
        item.get("id"): item for item in findings
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    lines = [
        str(explanation.get("summary", "A IA local não retornou um resumo.")),
        f"Modelo local: {explanation.get('model', UNAVAILABLE)}",
        (
            f"Dados: {explanation.get('sample_count', 0)} amostras · "
            f"{float(explanation.get('window_seconds', 0)):.0f} s · "
            f"cobertura {float(explanation.get('coverage_percent', 0)):.1f}% · "
            f"confiança {explanation.get('confidence', 'indisponível')}"
        ),
        "",
        "Os achados continuam sendo hipóteses. A IA não aplicou alterações e a resposta não foi salva.",
    ]
    for item in explanation.get("explanations", []):
        if not isinstance(item, dict):
            continue
        finding = findings_by_id.get(item.get("finding_id"), {})
        title = finding.get("title") or "Resumo da janela"
        lines.extend(["", str(title), str(item.get("explanation", ""))])
        evidence = finding.get("evidence") if isinstance(finding.get("evidence"), dict) else {}
        if evidence:
            measured = " · ".join(f"{key.replace('_', ' ')}: {value}" for key, value in evidence.items())
            lines.append("Evidência medida: " + measured)
        original_message = finding.get("message")
        if original_message:
            lines.append("Leitura do BooM: " + str(original_message))
        if item.get("recommendation"):
            lines.append("Próximo passo sugerido: " + str(item["recommendation"]))
        if item.get("risk"):
            lines.append("Risco ou limite: " + str(item["risk"]))
    limitations = explanation.get("limitations", [])
    if limitations:
        lines.extend(["", "Limitações"])
        lines.extend(f"· {item}" for item in limitations if isinstance(item, str))
    return "\n".join(lines)


def format_cpu_energy_preference(info: dict[str, Any]) -> str:
    if not info.get("available"):
        return "não exposta pelo kernel/driver"
    if info.get("current"):
        return str(info["current"])
    if info.get("consistent") is False:
        return "valores diferentes por política"
    return "leitura indisponível"


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
        "encoder_utilization_percent", "decoder_utilization_percent", "temperature_celsius",
        "vram_used_mib", "vram_total_mib", "graphics_clock_mhz", "memory_clock_mhz",
        "power_draw_watts", "power_limit_watts", "performance_state", "pci_address",
    )
    numeric_fields = set(fields[2:14])
    parsed: list[dict[str, Any]] = []
    for row in csv.reader(output.splitlines(), skipinitialspace=True):
        if not row:
            continue
        values = [value.strip() for value in row]
        if len(values) < len(fields) - 1:
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


def nvidia_gpu_telemetry_info() -> tuple[list[dict[str, Any]], str]:
    if not shutil.which("nvidia-smi"):
        return [], "Métricas NVIDIA indisponíveis: nvidia-smi não está instalado."
    query = (
        "name,driver_version,utilization.gpu,utilization.memory,utilization.encoder,utilization.decoder,temperature.gpu,"
        "memory.used,memory.total,clocks.current.graphics,clocks.current.memory,"
        "power.draw,power.limit,pstate,pci.bus_id"
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


def _sysfs_number(
    path: Path,
    divisor: float = 1.0,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    raw = read_text(path)
    try:
        value = float(raw.strip()) / divisor if raw else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if value is None or not math.isfinite(value):
        return None
    if minimum is not None and value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def _gpu_driver_name(device: Path) -> str | None:
    try:
        return (device / "driver").resolve(strict=True).name
    except OSError:
        return None


def _gpu_pci_address(device: Path) -> str | None:
    match = re.search(
        r"([0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7])$",
        str(device.resolve()),
    )
    return match.group(1).lower() if match else None


def _gpu_name_from_sysfs(device: Path, vendor_name: str, pci_address: str | None) -> str:
    product_name = (read_text(device / "product_name") or "").strip()
    if product_name:
        return product_name
    if pci_address and shutil.which("lspci"):
        result = run_command(["lspci", "-D", "-s", pci_address, "-nn"], timeout=1.5)
        if result is not None and result.returncode == 0:
            line = result.stdout.strip().splitlines()
            if line:
                name = re.sub(
                    r"^.*?\b(?:VGA compatible controller|3D controller|Display controller)(?:\s+\[[^]]+\])?:\s*",
                    "",
                    line[0],
                    flags=re.I,
                )
                name = re.sub(r"\s+\[[0-9a-fA-F]{4}:[0-9a-fA-F]{4}\](?:\s+\(rev [^)]+\))?$", "", name)
                if name:
                    return name
    suffix = f" · PCI {pci_address}" if pci_address else ""
    return f"GPU {vendor_name}{suffix}"


def _current_dpm_clock(path: Path) -> float | None:
    raw = read_text(path)
    if not raw:
        return None
    for line in raw.splitlines():
        if "*" not in line:
            continue
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([kmg]?)hz\b", line, re.I)
        if not match:
            continue
        value = float(match.group(1))
        unit = match.group(2).casefold()
        multiplier = {"": 1e-6, "k": 1e-3, "m": 1.0, "g": 1000.0}[unit]
        frequency = value * multiplier
        return frequency if math.isfinite(frequency) and frequency > 0 else None
    return None


def parse_intel_gpu_top_json(output: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    records: list[dict[str, Any]] = []
    try:
        parsed = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, dict):
        records = [parsed]
    elif isinstance(parsed, list):
        records = [item for item in parsed if isinstance(item, dict)]
    else:
        cursor = 0
        text = output or ""
        while cursor < len(text):
            start = text.find("{", cursor)
            if start < 0:
                break
            try:
                record, end = decoder.raw_decode(text, start)
            except json.JSONDecodeError:
                break
            if isinstance(record, dict):
                records.append(record)
            cursor = end
    if not records:
        return None

    sample = records[-1]
    metrics: dict[str, Any] = {
        "source": "intel_gpu_top/PMU",
        "gpu_utilization_label": "Motor mais ativo",
        "gpu_utilization_basis": "maior percentual entre motores gráficos",
    }
    engines = sample.get("engines")
    engine_utilization: dict[str, float] = {}
    if isinstance(engines, dict):
        for name, details in engines.items():
            if not isinstance(details, dict):
                continue
            busy = details.get("busy")
            if isinstance(busy, (int, float)) and math.isfinite(float(busy)) and 0 <= busy <= 100:
                engine_utilization[str(name)] = round(float(busy), 1)
    if engine_utilization:
        metrics["engine_utilization"] = engine_utilization
        metrics["gpu_utilization_percent"] = max(engine_utilization.values())

    frequency = sample.get("frequency")
    if isinstance(frequency, dict) and str(frequency.get("unit", "")).casefold() == "mhz":
        actual = frequency.get("actual")
        if isinstance(actual, (int, float)) and math.isfinite(float(actual)) and actual > 0:
            metrics["graphics_clock_mhz"] = round(float(actual), 1)

    power = sample.get("power")
    if isinstance(power, dict) and str(power.get("unit", "")).casefold() == "w":
        gpu_power = power.get("GPU")
        if isinstance(gpu_power, (int, float)) and math.isfinite(float(gpu_power)) and gpu_power >= 0:
            metrics["power_draw_watts"] = round(float(gpu_power), 2)
            metrics["power_scope"] = "GPU · intel_gpu_top/PMU"
    return metrics if len(metrics) > 3 else None


def intel_gpu_top_sample(device_node: str, timeout: float = 1.35) -> tuple[dict[str, Any] | None, str | None]:
    executable = shutil.which("intel_gpu_top")
    if not executable:
        return None, "intel-gpu-tools não está instalado."
    command = [executable, "-J", "-o", "-", "-s", "500", "-d", f"drm:{device_node}"]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except OSError as exc:
        return None, f"Não foi possível consultar intel_gpu_top: {exc}"

    stdout = ""
    stderr = ""
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGINT)
            except OSError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    stdout, stderr = process.communicate(timeout=0.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()

    metrics = parse_intel_gpu_top_json(stdout)
    if metrics:
        return metrics, None
    detail = next((line.strip() for line in stderr.splitlines() if line.strip()), None)
    return None, detail or "intel_gpu_top não retornou uma amostra válida; confira acesso a perf counters."


def drm_gpu_telemetry_info(root: Path = Path("/sys/class/drm")) -> tuple[list[dict[str, Any]], str]:
    vendor_names = {"0x1002": "AMD", "0x8086": "Intel", "0x10de": "NVIDIA"}
    telemetry: list[dict[str, Any]] = []
    notes: list[str] = []
    cards = sorted(path for path in root.glob("card[0-9]*") if re.fullmatch(r"card\d+", path.name))
    for card in cards:
        device = card / "device"
        vendor_id = (read_text(device / "vendor") or "").strip().casefold()
        vendor_name = vendor_names.get(vendor_id)
        if not vendor_name or not device.exists():
            continue
        driver_name = _gpu_driver_name(device)
        if vendor_id == "0x10de" and driver_name == "nvidia":
            continue

        pci_address = _gpu_pci_address(device)
        gpu: dict[str, Any] = {
            "name": _gpu_name_from_sysfs(device, vendor_name, pci_address),
            "driver_name": driver_name,
            "pci_address": pci_address,
            "source": f"DRM/sysfs · {driver_name or vendor_name}",
            "gpu_utilization_percent": None,
            "memory_utilization_percent": None,
            "temperature_celsius": None,
            "graphics_clock_mhz": None,
            "memory_clock_mhz": None,
            "vram_used_mib": None,
            "vram_total_mib": None,
            "power_draw_watts": None,
            "power_limit_watts": None,
            "fan_rpm": None,
            "fan_pwm_percent": None,
            "gpu_voltage_volts": None,
        }

        gpu_busy = _sysfs_number(device / "gpu_busy_percent", minimum=0, maximum=100)
        if gpu_busy is not None:
            gpu["gpu_utilization_percent"] = round(gpu_busy, 1)
            gpu["gpu_utilization_label"] = "Uso do núcleo gráfico"
            gpu["gpu_utilization_basis"] = "gpu_busy_percent · driver DRM"
        memory_busy = _sysfs_number(device / "mem_busy_percent", minimum=0, maximum=100)
        if memory_busy is not None:
            gpu["memory_utilization_percent"] = round(memory_busy, 1)

        for suffix, field, minimum in (
            ("vram_used", "vram_used_mib", 0),
            ("vram_total", "vram_total_mib", 1),
        ):
            value = _sysfs_number(device / f"mem_info_{suffix}", minimum=minimum)
            if value is not None:
                gpu[field] = round(value / 1024**2, 1)
        if not gpu["vram_total_mib"]:
            gpu["vram_total_mib"] = None
            gpu["vram_used_mib"] = None

        gtt_used = _sysfs_number(device / "mem_info_gtt_used", minimum=0)
        gtt_total = _sysfs_number(device / "mem_info_gtt_total", minimum=1)
        if gtt_used is not None:
            gpu["shared_memory_used_mib"] = round(gtt_used / 1024**2, 1)
        if gtt_total is not None:
            gpu["shared_memory_total_mib"] = round(gtt_total / 1024**2, 1)

        for hwmon in sorted((device / "hwmon").glob("hwmon*")):
            if gpu["temperature_celsius"] is None:
                for path in sorted(hwmon.glob("temp[1-9]_input")):
                    temperature = _sysfs_number(path, divisor=1000, minimum=-20, maximum=150)
                    if temperature is not None:
                        gpu["temperature_celsius"] = round(temperature, 1)
                        break
            if gpu["fan_rpm"] is None:
                fan = _sysfs_number(hwmon / "fan1_input", minimum=0, maximum=100000)
                if fan is not None:
                    gpu["fan_rpm"] = round(fan, 1)
            if gpu["fan_pwm_percent"] is None:
                pwm = _sysfs_number(hwmon / "pwm1", minimum=0)
                pwm_max = _sysfs_number(hwmon / "pwm1_max", minimum=1)
                if pwm is not None and pwm_max is not None and pwm <= pwm_max:
                    gpu["fan_pwm_percent"] = round(pwm * 100 / pwm_max, 1)
            if gpu["power_draw_watts"] is None:
                power = _sysfs_number(hwmon / "power1_average", divisor=1_000_000, minimum=0, maximum=10000)
                if power is None:
                    power = _sysfs_number(hwmon / "power1_input", divisor=1_000_000, minimum=0, maximum=10000)
                if power is not None:
                    gpu["power_draw_watts"] = round(power, 2)
                    gpu["power_scope"] = "SoC · AMDGPU (APUs podem incluir CPU)" if driver_name == "amdgpu" else "GPU · hwmon"
            if gpu["power_limit_watts"] is None:
                limit = _sysfs_number(hwmon / "power1_cap", divisor=1_000_000, minimum=0, maximum=10000)
                if limit is not None:
                    gpu["power_limit_watts"] = round(limit, 2)
            if gpu["gpu_voltage_volts"] is None:
                voltage = _sysfs_number(hwmon / "in0_input", divisor=1000, minimum=0, maximum=1000)
                if voltage is not None:
                    gpu["gpu_voltage_volts"] = round(voltage, 3)
            if gpu["graphics_clock_mhz"] is None:
                clock = _sysfs_number(hwmon / "freq1_input", divisor=1_000_000, minimum=1, maximum=10000)
                if clock is not None:
                    gpu["graphics_clock_mhz"] = round(clock, 1)
            if gpu["memory_clock_mhz"] is None:
                clock = _sysfs_number(hwmon / "freq2_input", divisor=1_000_000, minimum=1, maximum=10000)
                if clock is not None:
                    gpu["memory_clock_mhz"] = round(clock, 1)

        if driver_name == "amdgpu":
            if gpu["graphics_clock_mhz"] is None:
                gpu["graphics_clock_mhz"] = _current_dpm_clock(device / "pp_dpm_sclk")
            if gpu["memory_clock_mhz"] is None:
                gpu["memory_clock_mhz"] = _current_dpm_clock(device / "pp_dpm_mclk")
        elif driver_name == "i915":
            sample, error = intel_gpu_top_sample(f"/dev/dri/{card.name}")
            if sample:
                gpu.update({key: value for key, value in sample.items() if key != "source"})
                gpu["source"] += " + " + str(sample.get("source") or "intel_gpu_top/PMU")
            elif error:
                notes.append(f"{gpu['name']}: {error}")
        elif driver_name == "xe":
            notes.append(f"{gpu['name']}: o intel_gpu_top não dá suporte ao driver xe; telemetria disponível depende do sysfs exposto.")

        link_speed = (read_text(device / "current_link_speed") or "").strip()
        speed_match = re.search(r"[0-9]+(?:\.[0-9]+)?\s*GT/s", link_speed, re.I)
        if speed_match:
            gpu["pcie_link_speed"] = speed_match.group(0)
        link_width = _sysfs_number(device / "current_link_width", minimum=1, maximum=128)
        if link_width is not None:
            gpu["pcie_link_width"] = int(link_width)

        telemetry.append(gpu)

    if telemetry:
        status = "Métricas adicionais coletadas via DRM/sysfs."
        if notes:
            status += " " + " ".join(notes)
        return telemetry, status
    return [], " ".join(notes)


def gpu_telemetry_info() -> tuple[list[dict[str, Any]], str]:
    nvidia_metrics, nvidia_status = nvidia_gpu_telemetry_info()
    drm_metrics, drm_status = drm_gpu_telemetry_info()
    metrics = [*nvidia_metrics, *drm_metrics]
    statuses = []
    if nvidia_metrics or any(
        (read_text(card / "device" / "vendor") or "").strip().casefold() == "0x10de"
        for card in Path("/sys/class/drm").glob("card[0-9]*")
        if re.fullmatch(r"card\d+", card.name)
    ):
        statuses.append(nvidia_status)
    if drm_status:
        statuses.append(drm_status)
    if not statuses:
        statuses.append(nvidia_status)
    return metrics, " ".join(status for status in statuses if status)


def normalize_pci_address(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(?:[0-9a-fA-F]{4}|[0-9a-fA-F]{8})?:?([0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7])", value.strip())
    if not match:
        return None
    return "0000:" + match.group(1).lower()


def gpu_render_device(
    gpu: dict[str, Any],
    drm_root: Path = Path("/sys/class/drm"),
    device_root: Path = Path("/dev/dri"),
) -> tuple[str | None, str | None]:
    pci_address = normalize_pci_address(gpu.get("pci_address"))
    if not pci_address:
        return None, "O driver não identificou o endereço PCI desta GPU para selecionar o render node com segurança."
    for render_node in sorted(drm_root.glob("renderD*")):
        if not re.fullmatch(r"renderD\d+", render_node.name):
            continue
        if normalize_pci_address(_gpu_pci_address(render_node / "device")) != pci_address:
            continue
        device = device_root / render_node.name
        if not device.exists():
            return None, f"O render node {device} anunciado pelo DRM não está disponível."
        if not os.access(device, os.R_OK | os.W_OK):
            return None, f"Sem acesso de leitura e gravação a {device}; confira o grupo render da sessão."
        return str(device), None
    return None, f"Nenhum render node DRM corresponde à GPU {gpu.get('name') or pci_address}."


def stress_gpu_selection_key(index: int, gpu: dict[str, Any]) -> str:
    return normalize_pci_address(gpu.get("pci_address")) or f"gpu-{index}"


def select_stress_gpu(gpu_metrics: list[dict[str, Any]], selection_key: str | None) -> dict[str, Any] | None:
    for index, gpu in enumerate(gpu_metrics):
        if stress_gpu_selection_key(index, gpu) == selection_key:
            return gpu
    return None


def gpu_temperature_readings(
    gpu: dict[str, Any],
    pci_root: Path = Path("/sys/bus/pci/devices"),
) -> list[float]:
    pci_address = normalize_pci_address(gpu.get("pci_address"))
    if not pci_address:
        return []
    source = str(gpu.get("source") or "").casefold()
    if "nvidia-smi" in source or "nvml" in source or gpu.get("driver_name") == "nvidia":
        executable = shutil.which("nvidia-smi")
        if not executable:
            return []
        result = run_command(
            [executable, "--query-gpu=pci.bus_id,temperature.gpu", "--format=csv,noheader,nounits"],
            timeout=1.5,
        )
        if result is None or result.returncode != 0:
            return []
        for row in csv.reader(result.stdout.splitlines(), skipinitialspace=True):
            if len(row) < 2 or normalize_pci_address(row[0]) != pci_address:
                continue
            value = _parse_smi_number(row[1])
            if isinstance(value, (int, float)) and math.isfinite(float(value)) and -20 <= value <= 150:
                return [float(value)]
        return []

    device = pci_root / pci_address
    readings: list[float] = []
    for hwmon in sorted((device / "hwmon").glob("hwmon*")):
        for path in sorted(hwmon.glob("temp[1-9]_input")):
            value = _sysfs_number(path, divisor=1000, minimum=-20, maximum=150)
            if value is not None:
                readings.append(value)
    return readings


def gpu_utilization_label(gpu: dict[str, Any]) -> str:
    return str(gpu.get("gpu_utilization_label") or "Uso do núcleo gráfico")


def format_gpu_fan_reading(gpu: dict[str, Any]) -> str:
    percent = gpu.get("fan_percent")
    if isinstance(percent, (int, float)):
        return f"{percent:.0f}%"
    rpm = gpu.get("fan_rpm")
    if isinstance(rpm, (int, float)):
        return f"{rpm:.0f} RPM"
    pwm = gpu.get("fan_pwm_percent")
    if isinstance(pwm, (int, float)):
        return f"{pwm:.0f}% PWM"
    return UNAVAILABLE


def format_gpu_pcie_link(gpu: dict[str, Any]) -> str:
    speed = gpu.get("pcie_link_speed")
    width = gpu.get("pcie_link_width")
    if speed:
        return f"{speed} · x{width}" if isinstance(width, (int, float)) else str(speed)
    generation = gpu.get("pcie_generation")
    if isinstance(generation, (int, float)) or isinstance(width, (int, float)):
        generation_text = f"geração {generation}" if isinstance(generation, (int, float)) else "geração indisponível"
        width_text = f"x{width}" if isinstance(width, (int, float)) else "largura indisponível"
        return f"{generation_text} · {width_text}"
    return UNAVAILABLE


def format_gpu_graphics_terminal_rows(gpu: dict[str, Any]) -> list[str]:
    driver = gpu.get("driver_version") or gpu.get("driver_name") or UNAVAILABLE
    rows = [f"GPU: {gpu.get('name') or UNAVAILABLE} · Driver: {driver}"]
    for label, key, suffix in (
        (gpu_utilization_label(gpu), "gpu_utilization_percent", "%"),
        ("Temperatura da GPU", "temperature_celsius", " °C"),
        ("Uso de codificação de vídeo", "encoder_utilization_percent", "%"),
        ("Uso de decodificação de vídeo", "decoder_utilization_percent", "%"),
        ("Clock gráfico", "graphics_clock_mhz", " MHz"),
        ("Potência reportada", "power_draw_watts", " W"),
        ("Limite de potência", "power_limit_watts", " W"),
    ):
        if key in {"encoder_utilization_percent", "decoder_utilization_percent"} and key not in gpu:
            continue
        value = gpu.get(key)
        formatted = f"{value:.0f}{suffix}" if isinstance(value, (int, float)) else UNAVAILABLE
        rows.append(f"{label}: {formatted}")
    rows.append(f"Ventoinha: {format_gpu_fan_reading(gpu)}")
    if gpu.get("power_scope"):
        rows.append(f"Escopo da leitura de potência: {gpu['power_scope']}")
    voltage = gpu.get("gpu_voltage_volts")
    if isinstance(voltage, (int, float)):
        rows.append(f"Tensão reportada: {voltage:.3f} V")
    rows.append(f"Link PCIe: {format_gpu_pcie_link(gpu)}")
    engine_utilization = gpu.get("engine_utilization")
    if isinstance(engine_utilization, dict) and engine_utilization:
        engine_rows = [f"{name} {value:.0f}%" for name, value in engine_utilization.items()]
        rows.append("Motores gráficos: " + " · ".join(engine_rows[:8]))
    if gpu.get("performance_state"):
        rows.append(f"Estado de desempenho: {gpu['performance_state']}")
    return rows


def format_gpu_memory_terminal_rows(gpu: dict[str, Any]) -> list[str]:
    rows = [
        f"GPU: {gpu.get('name') or UNAVAILABLE}",
        f"VRAM usada / total: {format_vram_usage(gpu)}",
        "Atividade do controlador de memória: " + (
            f"{gpu['memory_utilization_percent']:.0f}%"
            if isinstance(gpu.get("memory_utilization_percent"), (int, float))
            else UNAVAILABLE
        ),
        "Clock da memória gráfica: " + format_frequency(gpu.get("memory_clock_mhz")),
    ]
    shared_used = gpu.get("shared_memory_used_mib")
    shared_total = gpu.get("shared_memory_total_mib")
    if isinstance(shared_used, (int, float)) or isinstance(shared_total, (int, float)):
        shared_value = (
            f"{shared_used:.0f} / {shared_total:.0f} MiB"
            if isinstance(shared_used, (int, float)) and isinstance(shared_total, (int, float))
            else UNAVAILABLE
        )
        rows.append(f"GTT compartilhada reportada pelo driver: {shared_value}")
    return rows


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


def parse_lm_sensors_json(payload: Any) -> list[dict[str, Any]]:
    readings: list[dict[str, Any]] = []
    metric_types = {
        "temp": ("temperature", "°C", -20.0, 150.0),
        "fan": ("fan", "RPM", 0.0, 100000.0),
        "in": ("voltage", "V", 0.0, 1000.0),
        "curr": ("current", "A", 0.0, 1000.0),
        "power": ("power", "W", 0.0, 10000.0),
    }
    if not isinstance(payload, dict):
        return readings

    for chip, features in payload.items():
        if not isinstance(features, dict):
            continue
        for feature, values in features.items():
            if not isinstance(values, dict):
                continue
            for field, raw_value in values.items():
                match = re.fullmatch(r"(temp|fan|in|curr|power)\d+_input", field.casefold())
                if not match or not isinstance(raw_value, (int, float)) or not math.isfinite(float(raw_value)):
                    continue
                prefix = match.group(1)
                kind, unit, minimum, maximum = metric_types[prefix]
                value = float(raw_value)
                if not minimum <= value <= maximum:
                    continue
                reading = {
                    "name": f"{chip} · {feature}",
                    "kind": kind,
                    "value": round(value, 1),
                    "unit": unit,
                    "source": "lm-sensors",
                }
                feature_label = str(feature).casefold()
                duplicate = next((
                    item for item in readings
                    if item["kind"] == kind
                    and item["name"].split(" · ", 1)[0] == str(chip)
                    and abs(item["value"] - reading["value"]) < 0.2
                    and (
                        item["name"].rsplit(" · ", 1)[-1].casefold() == "composite"
                        and re.fullmatch(r"sensor \d+", feature_label)
                        or re.fullmatch(r"sensor \d+", item["name"].rsplit(" · ", 1)[-1].casefold())
                        and feature_label == "composite"
                    )
                ), None)
                if duplicate:
                    if feature_label == "composite":
                        readings.remove(duplicate)
                    else:
                        continue
                readings.append(reading)
    return readings


def sysfs_sensor_readings() -> list[dict[str, Any]]:
    readings: list[dict[str, Any]] = []
    metric_types = {
        "temp": ("temperature", "°C", 1000.0, -20.0, 150.0),
        "fan": ("fan", "RPM", 1.0, 0.0, 100000.0),
        "in": ("voltage", "V", 1000.0, 0.0, 1000.0),
        "curr": ("current", "A", 1000.0, 0.0, 1000.0),
        "power": ("power", "W", 1000000.0, 0.0, 10000.0),
    }
    for hwmon in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        chip = (read_text(hwmon / "name") or hwmon.name).strip()
        for input_path in sorted(hwmon.glob("*_input")):
            match = re.fullmatch(r"(temp|fan|in|curr|power)(\d+)_input", input_path.name)
            if not match:
                continue
            raw = read_text(input_path)
            try:
                raw_value = float(raw.strip()) if raw else None
            except ValueError:
                raw_value = None
            if raw_value is None or not math.isfinite(raw_value):
                continue
            prefix, index = match.groups()
            kind, unit, divisor, minimum, maximum = metric_types[prefix]
            value = raw_value / divisor
            if not minimum <= value <= maximum:
                continue
            label_path = input_path.with_name(f"{prefix}{index}_label")
            label = (read_text(label_path) or f"{prefix}{index}").strip()
            readings.append({
                "name": f"{chip} · {label}",
                "kind": kind,
                "value": round(value, 1),
                "unit": unit,
                "source": "hwmon · sysfs",
            })

    if not any(item["kind"] == "temperature" for item in readings):
        for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp")):
            raw = read_text(zone)
            try:
                value = int(raw.strip()) / 1000 if raw else None
            except ValueError:
                value = None
            if value is not None and math.isfinite(value) and -20 <= value <= 150:
                type_name = (read_text(zone.parent / "type") or zone.parent.parent.name).strip()
                readings.append({
                    "name": type_name,
                    "kind": "temperature",
                    "value": round(value, 1),
                    "unit": "°C",
                    "source": "thermal zone · sysfs",
                })
    return readings


def sensor_readings_info() -> list[dict[str, Any]]:
    sensors = shutil.which("sensors")
    if sensors:
        result = run_command([sensors, "-j"], timeout=3.0)
        if result is not None and result.returncode == 0:
            try:
                readings = parse_lm_sensors_json(json.loads(result.stdout))
                if readings:
                    return readings
            except (json.JSONDecodeError, TypeError):
                pass
    return sysfs_sensor_readings()


def temperature_info(readings: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    source = readings if readings is not None else sensor_readings_info()
    return [
        {
            "name": item["name"],
            "celsius": item["value"],
            "source": item["source"],
        }
        for item in source
        if item.get("kind") == "temperature"
    ]


def cpu_temperature_readings(temperatures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cpu_sensor = re.compile(r"(?:k10temp|coretemp|zenpower|\bcpu\b|tctl|tdie|package id)", re.I)
    return [item for item in temperatures if cpu_sensor.search(str(item.get("name", "")))]


def sysbench_temperature_snapshot() -> dict[str, list[dict[str, Any]]]:
    temperatures = temperature_info()
    return {
        "cpu_temperatures": cpu_temperature_readings(temperatures),
        "temperatures": temperatures,
    }


def stress_test_readiness(
    temperatures: list[dict[str, Any]],
    kind: str = "cpu",
    gpu: dict[str, Any] | None = None,
    gpu_temperatures: list[float] | None = None,
) -> dict[str, Any]:
    if not shutil.which("stress-ng"):
        return {"ready": False, "message": "stress-ng não está instalado.", "current_celsius": None, "cpu_celsius": None}
    cpu_temperatures = cpu_temperature_readings(temperatures)
    if not cpu_temperatures:
        return {
            "ready": False,
            "message": "Teste bloqueado: o BooM precisa ler a temperatura da CPU para vigiar a carga.",
            "current_celsius": None,
            "cpu_celsius": None,
        }
    valid_temperatures = [
        float(item["celsius"]) for item in temperatures
        if isinstance(item.get("celsius"), (int, float))
        and math.isfinite(float(item["celsius"]))
        and -20 <= float(item["celsius"]) <= 150
    ]
    cpu_values = [
        float(item["celsius"]) for item in cpu_temperatures
        if isinstance(item.get("celsius"), (int, float))
        and math.isfinite(float(item["celsius"]))
        and -20 <= float(item["celsius"]) <= 150
    ]
    if not cpu_values:
        return {
            "ready": False,
            "message": "Teste bloqueado: a leitura identificável da temperatura da CPU não contém um valor válido.",
            "current_celsius": None,
            "cpu_celsius": None,
            "gpu_celsius": None,
        }
    cpu_hottest = max(cpu_values, default=None)
    gpu_values = [
        float(value) for value in (gpu_temperatures or [])
        if isinstance(value, (int, float))
        and math.isfinite(float(value))
        and -20 <= float(value) <= 150
    ]
    if kind == "gpu" and gpu_temperatures is None and not gpu_values and gpu is not None:
        cached_gpu_temperature = gpu.get("temperature_celsius")
        if (
            isinstance(cached_gpu_temperature, (int, float))
            and math.isfinite(float(cached_gpu_temperature))
            and -20 <= float(cached_gpu_temperature) <= 150
        ):
            gpu_values = [float(cached_gpu_temperature)]
    gpu_hottest = max(gpu_values, default=None)
    if kind == "gpu" and gpu is None:
        return {"ready": False, "message": "Selecione uma GPU detectada antes do teste.", "current_celsius": None, "cpu_celsius": cpu_hottest, "gpu_celsius": None}
    if kind == "gpu" and gpu_hottest is None:
        return {
            "ready": False,
            "message": "Teste de GPU bloqueado: o BooM precisa de uma leitura térmica ao vivo da placa selecionada.",
            "current_celsius": max(valid_temperatures, default=None),
            "cpu_celsius": cpu_hottest,
            "gpu_celsius": None,
        }
    gpu_device = None
    if kind == "gpu":
        gpu_device, device_error = gpu_render_device(gpu or {})
        if device_error:
            return {
                "ready": False,
                "message": "Teste de GPU bloqueado: " + device_error,
                "current_celsius": max([*valid_temperatures, gpu_hottest], default=None),
                "cpu_celsius": cpu_hottest,
                "gpu_celsius": gpu_hottest,
            }
    hottest_values = [*valid_temperatures, *gpu_values]
    hottest = max(hottest_values, default=None)
    if hottest is None:
        return {"ready": False, "message": "Teste bloqueado: nenhum sensor térmico legível.", "current_celsius": None, "cpu_celsius": cpu_hottest, "gpu_celsius": gpu_hottest}
    if hottest >= STRESS_TEST_PRECHECK_TEMP_C:
        return {
            "ready": False,
            "message": f"Aguarde a temperatura cair abaixo de {STRESS_TEST_PRECHECK_TEMP_C:.0f} °C antes de iniciar.",
            "current_celsius": hottest,
            "cpu_celsius": cpu_hottest,
            "gpu_celsius": gpu_hottest,
        }
    return {
        "ready": True,
        "message": "Sensores disponíveis; limite de parada em 85 °C." if kind != "gpu" else f"Sensores de CPU e GPU disponíveis em {gpu_device}; limite de parada em 85 °C.",
        "current_celsius": hottest,
        "cpu_celsius": cpu_hottest,
        "gpu_celsius": gpu_hottest,
        "gpu_device": gpu_device,
    }


def stress_test_command(
    kind: str,
    duration_seconds: int,
    workers: int = 1,
    available_memory_bytes: int | None = None,
    gpu_device: str | None = None,
) -> list[str]:
    executable = shutil.which("stress-ng")
    if not executable:
        raise RuntimeError("Instale o pacote stress-ng para executar testes de carga.")
    if duration_seconds not in STRESS_TEST_DURATIONS:
        raise ValueError("A duração precisa ser 10, 30, 60 ou 120 segundos.")
    common = [executable, "--timeout", f"{duration_seconds}s", "--metrics-brief", "--oom-avoid", "--abort"]
    if kind == "cpu":
        maximum_workers = min(STRESS_TEST_MAX_CPU_WORKERS, max(1, os.cpu_count() or 1))
        if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= maximum_workers:
            raise ValueError(f"Escolha entre 1 e {maximum_workers} workers de CPU.")
        return common + ["--cpu", str(workers), "--cpu-load", "80"]
    if kind == "memory":
        if not isinstance(available_memory_bytes, int) or available_memory_bytes <= 0:
            raise ValueError("O BooM não conseguiu medir a RAM disponível.")
        allocation = min(available_memory_bytes // 4, STRESS_TEST_MAX_MEMORY_BYTES)
        allocation = allocation // (1024**2) * (1024**2)
        if allocation < STRESS_TEST_MIN_MEMORY_BYTES:
            raise ValueError("São necessários pelo menos 512 MiB de RAM disponível para testar com segurança.")
        return common + ["--vm", "1", "--vm-bytes", str(allocation)]
    if kind == "gpu":
        if not isinstance(gpu_device, str) or not re.fullmatch(r"/dev/dri/renderD\d+", gpu_device):
            raise ValueError("Selecione um render node DRM válido para a GPU.")
        return common + ["--gpu", "1", "--gpu-devnode", gpu_device]
    raise ValueError("Tipo de teste desconhecido.")


def signal_stress_process(process: subprocess.Popen[str], force: bool = False) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.kill() if force else process.terminate()
        except OSError:
            pass


def intel_i915_adapter_present(root: Path = Path("/sys/class/drm")) -> bool:
    for card in root.glob("card[0-9]*"):
        if not re.fullmatch(r"card\d+", card.name):
            continue
        device = card / "device"
        vendor_id = (read_text(device / "vendor") or "").strip().casefold()
        if vendor_id == "0x8086" and _gpu_driver_name(device) == "i915":
            return True
    return False


def gtk3_runtime_available() -> bool:
    try:
        import gi  # type: ignore
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore
    except (ImportError, ValueError):
        return False
    return Gtk is not None


def appindicator_runtime_available() -> bool:
    try:
        import gi  # type: ignore
    except ImportError:
        return False
    for namespace in ("AyatanaAppIndicator3", "AppIndicator3"):
        try:
            gi.require_version(namespace, "0.1")
            if namespace == "AyatanaAppIndicator3":
                from gi.repository import AyatanaAppIndicator3  # type: ignore
                return AyatanaAppIndicator3 is not None
            from gi.repository import AppIndicator3  # type: ignore
            return AppIndicator3 is not None
        except (ImportError, ValueError):
            continue
    return False


def parse_fwupd_inventory(devices_payload: Any, updates_payload: Any) -> dict[str, Any]:
    """Keep only display-safe fwupd fields; device serials are never retained."""
    raw_devices = devices_payload.get("Devices", []) if isinstance(devices_payload, dict) else []
    raw_updates = updates_payload.get("Devices", []) if isinstance(updates_payload, dict) else []
    if not isinstance(raw_devices, list):
        raw_devices = []
    if not isinstance(raw_updates, list):
        raw_updates = []

    devices: list[dict[str, Any]] = []
    devices_by_id: dict[str, dict[str, Any]] = {}
    for item in raw_devices:
        if not isinstance(item, dict):
            continue
        name = item.get("Name")
        if not isinstance(name, str) or not name.strip():
            continue
        flags = item.get("Flags", [])
        flags = [flag for flag in flags if isinstance(flag, str)] if isinstance(flags, list) else []
        device = {
            "device_id": item.get("DeviceId") if isinstance(item.get("DeviceId"), str) else None,
            "name": name.strip(),
            "vendor": item.get("Vendor") if isinstance(item.get("Vendor"), str) else None,
            "version": item.get("Version") if isinstance(item.get("Version"), str) else None,
            "updatable": "updatable" in flags,
            "needs_reboot": "needs-reboot" in flags,
            "requires_ac": "require-ac" in flags,
        }
        devices.append(device)
        if device["device_id"]:
            devices_by_id[device["device_id"]] = device

    updates: list[dict[str, Any]] = []
    for item in raw_updates:
        if not isinstance(item, dict):
            continue
        device_id = item.get("DeviceId") if isinstance(item.get("DeviceId"), str) else None
        matching_device = devices_by_id.get(device_id or "")
        releases = item.get("Releases", item.get("Release", []))
        if isinstance(releases, dict):
            releases = [releases]
        if not isinstance(releases, list):
            releases = []
        release = next((entry for entry in releases if isinstance(entry, dict)), {})
        name = item.get("Name") or (matching_device or {}).get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        update_version = (
            item.get("UpdateVersion")
            or item.get("LatestVersion")
            or item.get("Version")
            or release.get("Version")
        )
        flags = item.get("Flags", [])
        flags = [flag for flag in flags if isinstance(flag, str)] if isinstance(flags, list) else []
        updates.append({
            "device_id": device_id,
            "name": name.strip(),
            "current_version": (matching_device or {}).get("version"),
            "update_version": update_version if isinstance(update_version, str) else None,
            "needs_reboot": "needs-reboot" in flags or bool((matching_device or {}).get("needs_reboot")),
            "requires_ac": "require-ac" in flags or bool((matching_device or {}).get("requires_ac")),
        })

    devices.sort(key=lambda item: item["name"].casefold())
    updates.sort(key=lambda item: item["name"].casefold())
    return {"devices": devices, "updates": updates}


def firmware_inventory_info() -> dict[str, Any]:
    """Read fwupd's local device list and cached update metadata without refreshing or writing."""
    fwupdmgr = shutil.which("fwupdmgr")
    if not fwupdmgr:
        return {
            "state": "missing",
            "message": "fwupd não está instalado; o pacote opcional é fwupd.",
            "devices": [],
            "updates": [],
        }

    payloads: list[dict[str, Any]] = []
    for command in ("get-devices", "get-updates"):
        result = run_command([fwupdmgr, command, "--json"], timeout=12.0)
        if result is None:
            return {
                "state": "error",
                "message": f"fwupdmgr {command} não respondeu dentro do limite de 12 s.",
                "devices": [],
                "updates": [],
            }
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip().splitlines()[-1:]
            suffix = f" {detail[0]}" if detail else ""
            return {
                "state": "error",
                "message": f"Falha ao consultar fwupd ({command}).{suffix}",
                "devices": [],
                "updates": [],
            }
        try:
            payload = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError):
            return {
                "state": "error",
                "message": f"fwupdmgr {command} retornou JSON inválido.",
                "devices": [],
                "updates": [],
            }
        if not isinstance(payload, dict):
            return {
                "state": "error",
                "message": f"fwupdmgr {command} retornou um formato inesperado.",
                "devices": [],
                "updates": [],
            }
        payloads.append(payload)

    inventory = parse_fwupd_inventory(payloads[0], payloads[1])
    count = len(inventory["devices"])
    update_count = len(inventory["updates"])
    if not count:
        message = "fwupd não identificou dispositivos de firmware neste sistema."
    else:
        message = f"{count} dispositivos identificados · {update_count} atualizações disponíveis nos metadados locais."
    return {
        "state": "ready",
        "message": message,
        "devices": inventory["devices"],
        "updates": inventory["updates"],
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": "fwupdmgr · metadados locais",
        "writes_firmware": False,
    }


def optional_tool_status() -> list[dict[str, Any]]:
    packages = list(OPTIONAL_TOOL_PACKAGES)
    if intel_i915_adapter_present():
        packages.append(("intel_gpu_top", "intel-gpu-tools"))
    if power_profile_control_supported() and not power_profile_manager_conflict():
        packages.append(("powerprofilesctl", "power-profiles-daemon"))
    status = [
        {"command": command, "package": package, "installed": shutil.which(command) is not None}
        for command, package in packages
    ]
    if not gtk3_runtime_available():
        status.extend([
            {"command": "GTK 3 / PyGObject", "package": "python3-gi", "installed": False},
            {"command": "GTK 3", "package": "gir1.2-gtk-3.0", "installed": False},
        ])
    if not appindicator_runtime_available():
        status.append({
            "command": "Ayatana AppIndicator",
            "package": "gir1.2-ayatanaappindicator3-0.1",
            "installed": False,
        })
    return status


def missing_optional_packages() -> list[str]:
    return [item["package"] for item in optional_tool_status() if not item["installed"]]


def run_privileged_apt_step(pkexec: str, apt_get: str, arguments: list[str], label: str) -> dict[str, Any]:
    command = [pkexec, apt_get, *arguments]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except OSError as exc:
        return {"ok": False, "message": f"{label}: não foi possível abrir o comando: {exc}"}
    try:
        output, _ = process.communicate(timeout=600.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5.0)
        except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        return {"ok": False, "message": f"{label}: excedeu 10 minutos e foi interrompido."}
    if process.returncode != 0:
        detail = (output or "").strip().splitlines()[-4:]
        suffix = " · ".join(detail)
        return {
            "ok": False,
            "message": f"{label}: foi cancelada ou falhou{': ' + suffix if suffix else '.'}",
        }
    return {"ok": True, "output": output or ""}


def install_missing_optional_dependencies() -> dict[str, Any]:
    packages = missing_optional_packages()
    if not packages:
        return {"ok": True, "message": "Todas as ferramentas opcionais já estão instaladas.", "packages": []}
    apt_get = shutil.which("apt-get")
    pkexec = shutil.which("pkexec")
    if not apt_get:
        return {"ok": False, "message": "Este BooM instala dependências pelo apt em Pop!_OS/Ubuntu/Debian.", "packages": packages}
    if not pkexec:
        manual = "sudo apt install " + " ".join(packages)
        return {"ok": False, "message": f"pkexec não está disponível. Execute manualmente: {manual}", "packages": packages}
    update = run_privileged_apt_step(pkexec, apt_get, ["update"], "Atualização dos índices do APT")
    if not update["ok"]:
        return {"ok": False, "message": update["message"], "packages": packages}
    install = run_privileged_apt_step(
        pkexec, apt_get, ["install", "--yes", "--no-install-recommends", *packages], "Instalação dos pacotes"
    )
    if not install["ok"]:
        return {"ok": False, "message": install["message"], "packages": packages}
    still_missing = missing_optional_packages()
    if still_missing:
        return {
            "ok": False,
            "message": "O apt terminou, mas ainda faltam: " + ", ".join(still_missing),
            "packages": packages,
        }
    return {"ok": True, "message": "Ferramentas instaladas e detectadas pelo BooM.", "packages": packages}


class StressTestSession:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancel_requested = threading.Event()
        self._process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {"status": "idle", "message": "Nenhum teste executado."}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
        if state.get("status") == "running":
            elapsed = max(0.0, time.monotonic() - state["started_at"])
            state["elapsed_seconds"] = min(elapsed, float(state["duration_seconds"]))
            state["remaining_seconds"] = max(0, int(state["duration_seconds"] - elapsed + 0.999))
            temperature = state.get("current_celsius")
            detail = f" · sensor mais quente {temperature:.1f} °C / {STRESS_TEST_ABORT_TEMP_C:.0f} °C" if isinstance(temperature, (int, float)) else ""
            gpu_temperature = state.get("gpu_celsius")
            if state.get("kind") == "gpu" and isinstance(gpu_temperature, (int, float)):
                detail += f" · GPU {gpu_temperature:.1f} °C"
            state["message"] = f"Teste de {state['kind_label']} em execução · {state['remaining_seconds']} s restantes{detail}"
        return state

    def is_active(self) -> bool:
        return self.snapshot().get("status") in {"starting", "running", "stopping"}

    def start(
        self,
        kind: str,
        duration_seconds: int,
        workers: int,
        available_memory_bytes: int | None,
        gpu: dict[str, Any] | None = None,
    ) -> str:
        if kind == "memory":
            available_memory_bytes = memory_info().get("available")
        kind_label = {"cpu": "CPU", "memory": "RAM", "gpu": "GPU"}.get(kind)
        if kind_label is None:
            raise ValueError("Tipo de teste desconhecido.")
        with self._lock:
            if self._state.get("status") in {"starting", "running", "stopping"}:
                raise RuntimeError("Já existe um teste em andamento.")
            self._cancel_requested.clear()
            self._state = {
                "status": "starting",
                "kind": kind,
                "kind_label": kind_label,
                "duration_seconds": duration_seconds,
                "message": "Conferindo sensores térmicos antes do teste…",
            }
        try:
            temperatures = temperature_info()
            live_gpu_temperatures = gpu_temperature_readings(gpu or {}) if kind == "gpu" else None
            readiness = stress_test_readiness(temperatures, kind, gpu, live_gpu_temperatures)
            if not readiness["ready"]:
                raise RuntimeError(readiness["message"])
            command = stress_test_command(
                kind,
                duration_seconds,
                workers,
                available_memory_bytes,
                gpu_device=readiness.get("gpu_device"),
            )
            with self._lock:
                if self._cancel_requested.is_set():
                    self._state.update({"status": "cancelled", "message": "Teste cancelado antes de iniciar."})
                    raise RuntimeError("Teste cancelado antes de iniciar.")
                self._state["message"] = "Iniciando stress-ng…"
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    start_new_session=True,
                )
                now = time.monotonic()
                self._process = process
                self._state.update({
                    "status": "running",
                    "started_at": now,
                    "current_celsius": readiness.get("current_celsius"),
                    "cpu_celsius": readiness.get("cpu_celsius"),
                    "gpu_celsius": readiness.get("gpu_celsius"),
                    "gpu_name": gpu.get("name") if kind == "gpu" and gpu else None,
                    "message": f"Teste de {kind_label} iniciado por até {duration_seconds} s.",
                })
                self._thread = threading.Thread(
                    target=self._supervise,
                    args=(process, kind, duration_seconds, now, gpu),
                    name="boom-stress-guard",
                    daemon=True,
                )
                self._thread.start()
        except (OSError, RuntimeError, ValueError) as exc:
            with self._lock:
                if self._state.get("status") in {"starting", "stopping"}:
                    status = "cancelled" if self._cancel_requested.is_set() else "failed"
                    self._state.update({"status": status, "message": str(exc)})
            if isinstance(exc, (RuntimeError, ValueError)):
                raise
            raise RuntimeError(f"Não foi possível iniciar stress-ng: {exc}") from exc
        return f"Teste de {kind_label} iniciado por até {duration_seconds} s."

    def _supervise(
        self,
        process: subprocess.Popen[str],
        kind: str,
        duration_seconds: int,
        started_at: float,
        gpu: dict[str, Any] | None = None,
    ) -> None:
        stop_reason: str | None = None
        deadline = started_at + duration_seconds
        while process.poll() is None:
            if self._cancel_requested.wait(1.0):
                stop_reason = "Teste cancelado pelo usuário."
                break
            temperatures = temperature_info()
            cpu_temperatures = cpu_temperature_readings(temperatures)
            cpu_values = [
                float(item["celsius"]) for item in cpu_temperatures
                if isinstance(item.get("celsius"), (int, float)) and math.isfinite(float(item["celsius"]))
            ]
            if not cpu_values:
                stop_reason = "Teste interrompido: leitura da temperatura da CPU ficou indisponível."
                break
            valid_temperatures = [
                float(item["celsius"]) for item in temperatures
                if isinstance(item.get("celsius"), (int, float)) and math.isfinite(float(item["celsius"]))
            ]
            gpu_temperatures = gpu_temperature_readings(gpu or {}) if kind == "gpu" else []
            if kind == "gpu" and not gpu_temperatures:
                stop_reason = "Teste interrompido: leitura térmica ao vivo da GPU ficou indisponível."
                break
            gpu_hottest = max(gpu_temperatures, default=None)
            hottest = max([*valid_temperatures, *gpu_temperatures], default=None)
            cpu_hottest = max(cpu_values, default=None)
            with self._lock:
                self._state["current_celsius"] = hottest
                self._state["cpu_celsius"] = cpu_hottest
                self._state["gpu_celsius"] = gpu_hottest
            if hottest is None:
                stop_reason = "Teste interrompido: nenhum sensor térmico responde."
                break
            if hottest >= STRESS_TEST_ABORT_TEMP_C:
                stop_reason = f"Teste interrompido ao atingir {hottest:.1f} °C (limite {STRESS_TEST_ABORT_TEMP_C:.0f} °C)."
                break
            if time.monotonic() >= deadline + 3:
                stop_reason = "Teste interrompido ao exceder o limite de duração."
                break
        if self._cancel_requested.is_set() and stop_reason is None:
            stop_reason = "Teste cancelado pelo usuário."
        if stop_reason and process.poll() is None:
            signal_stress_process(process)
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                signal_stress_process(process, force=True)
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    stop_reason += " O processo não encerrou dentro do prazo de segurança."
        try:
            output, _ = process.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            signal_stress_process(process, force=True)
            output, _ = process.communicate()
        return_code = process.returncode
        with self._lock:
            if stop_reason:
                status, message = "cancelled", stop_reason
            elif return_code == 0:
                kind_label = {"cpu": "CPU", "memory": "RAM", "gpu": "GPU"}.get(kind, kind)
                status, message = "completed", f"Teste de {kind_label} concluído."
            else:
                status, message = "failed", f"stress-ng encerrou com código {return_code}."
            self._state.update({
                "status": status,
                "message": message,
                "finished_at": time.monotonic(),
                "return_code": return_code,
                "summary": (output or "").strip()[-1500:],
            })

    def cancel(self) -> None:
        with self._lock:
            if self._state.get("status") not in {"starting", "running", "stopping"}:
                return
            self._cancel_requested.set()
            self._state["status"] = "stopping"
            self._state["message"] = "Cancelando teste de carga…"
            process = self._process
        if process is not None:
            signal_stress_process(process)

    def stop_and_wait(self, timeout: float = 4.0) -> None:
        self.cancel()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        process = self._process
        if process is not None and process.poll() is None:
            signal_stress_process(process, force=True)


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
            "ppid": int(fields[1]),
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


def process_records_snapshot() -> list[dict[str, Any]]:
    records = []
    try:
        proc_paths = Path("/proc").iterdir()
    except OSError:
        return records
    for path in proc_paths:
        if path.name.isdigit():
            record = read_process_record(int(path.name))
            if record is not None:
                records.append(record)
    return records


def process_tree_snapshot(
    root_pid: int,
    expected_start_ticks: int,
    process_records: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    records = process_records if process_records is not None else process_records_snapshot()
    by_pid = {record["pid"]: record for record in records}
    root = by_pid.get(root_pid)
    if root is None or root["start_ticks"] != expected_start_ticks:
        raise ProcessLookupError("O processo selecionado já encerrou ou o PID foi reutilizado.")

    children: dict[int, list[dict[str, Any]]] = {}
    for record in records:
        parent_pid = record.get("ppid")
        if isinstance(parent_pid, int) and parent_pid in by_pid:
            children.setdefault(parent_pid, []).append(record)
    for siblings in children.values():
        siblings.sort(key=lambda item: item["pid"])

    snapshot = []
    seen = {root_pid}
    pending = [(root, 0)]
    while pending:
        record, depth = pending.pop()
        captured = dict(record)
        captured["tree_depth"] = depth
        snapshot.append(captured)
        for child in reversed(children.get(record["pid"], [])):
            child_pid = child["pid"]
            if child_pid in seen:
                continue
            seen.add(child_pid)
            pending.append((child, depth + 1))
    return snapshot


def terminate_process_tree(
    root_pid: int,
    expected_start_ticks: int,
    process_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    snapshot = process_tree_snapshot(root_pid, expected_start_ticks, process_records)
    root = next(record for record in snapshot if record["pid"] == root_pid)
    if not root["can_terminate"]:
        raise PermissionError("O processo selecionado está protegido ou pertence a outro usuário.")

    protected = [record for record in snapshot if not record["can_terminate"]]
    if protected:
        examples = ", ".join(
            f"{record['name']} (PID {record['pid']})" for record in protected[:3]
        )
        remainder = len(protected) - min(3, len(protected))
        suffix = f" e mais {remainder}" if remainder else ""
        raise PermissionError(
            f"A árvore inclui processos protegidos: {examples}{suffix}. Nenhum sinal foi enviado."
        )

    active = []
    changed = []
    newly_protected = []
    for record in snapshot:
        current = read_process_record(record["pid"])
        if current is None or current["start_ticks"] != record["start_ticks"]:
            changed.append(record)
        elif not current["can_terminate"]:
            newly_protected.append(current)
        else:
            active.append(record)
    if newly_protected:
        examples = ", ".join(
            f"{record['name']} (PID {record['pid']})" for record in newly_protected[:3]
        )
        raise PermissionError(
            f"A árvore mudou e inclui processos protegidos: {examples}. Nenhum sinal foi enviado."
        )
    if not any(record["pid"] == root_pid for record in active):
        raise ProcessLookupError("O processo selecionado encerrou antes da ação.")

    sent = []
    failed = []
    for record in sorted(
        active,
        key=lambda item: (-item["tree_depth"], -item["pid"]),
    ):
        try:
            terminate_process(record["pid"], record["start_ticks"])
            sent.append(record)
        except ProcessLookupError:
            changed.append(record)
        except OSError as exc:
            failed.append({"process": record, "error": str(exc)})
    return {
        "total": len(snapshot),
        "sent": sent,
        "changed": changed,
        "failed": failed,
    }


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
    for record in process_records_snapshot():
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


def flatpak_application_installations() -> dict[str, set[str]]:
    flatpak = shutil.which("flatpak")
    if not flatpak:
        return {}
    result = run_command([flatpak, "list", "--app", "--columns=application,installation"], timeout=10.0)
    if result is None or result.returncode != 0:
        return {}
    installations: dict[str, set[str]] = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t") if "\t" in line else line.split()
        if len(fields) < 2 or fields[-1] not in {"user", "system"}:
            continue
        app_id = fields[0].strip()
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", app_id):
            installations.setdefault(app_id, set()).add(fields[-1])
    return installations


def dpkg_desktop_owners(paths: list[str]) -> dict[str, str]:
    dpkg_query = shutil.which("dpkg-query")
    if not dpkg_query or not paths:
        return {}
    result = run_command([dpkg_query, "--search", *paths], timeout=15.0)
    if result is None:
        return {}
    owners: dict[str, str] = {}
    for line in result.stdout.splitlines():
        owner_text, separator, owned_path = line.partition(": ")
        if not separator:
            continue
        packages = [part.strip() for part in owner_text.split(",")]
        if len(packages) != 1 or owned_path not in paths:
            continue
        package = packages[0]
        if re.fullmatch(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?", package):
            owners[owned_path] = package
    return owners


def flatpak_installation_for_launcher(path: Path, installations: dict[str, set[str]]) -> tuple[str, str] | None:
    app_id = path.name.removesuffix(".desktop")
    scopes = installations.get(app_id, set())
    if not scopes or path.suffix != ".desktop" or "flatpak/exports/share/applications" not in path.as_posix():
        return None
    home_exports = (Path.home() / ".local/share/flatpak/exports/share/applications").as_posix()
    system_exports = "/var/lib/flatpak/exports/share/applications"
    parent = path.parent.as_posix()
    if parent == home_exports and "user" in scopes:
        return app_id, "user"
    if parent == system_exports and "system" in scopes:
        return app_id, "system"
    return None


def desktop_application_uninstall_target(
    path: str,
    flatpak_installations: dict[str, set[str]],
    dpkg_owners: dict[str, str],
) -> dict[str, str] | None:
    launcher = Path(path)
    if launcher.suffix != ".desktop" or not launcher.is_file():
        return None
    flatpak = flatpak_installation_for_launcher(launcher, flatpak_installations)
    if flatpak:
        app_id, installation = flatpak
        return {"manager": "flatpak", "app_id": app_id, "installation": installation}
    package = dpkg_owners.get(path)
    if package:
        return {"manager": "apt", "package": package}
    return None


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
    application_paths = [item["path"] for item in by_id.values()]
    flatpak_installations = flatpak_application_installations()
    dpkg_owners = dpkg_desktop_owners(application_paths)
    for item in by_id.values():
        target = desktop_application_uninstall_target(item["path"], flatpak_installations, dpkg_owners)
        item["uninstall_target"] = target
        item["uninstallable"] = target is not None
        item["source"] = (
            "Flatpak (" + target["installation"] + ")"
            if target and target["manager"] == "flatpak"
            else "APT"
            if target
            else "Origem não identificada"
        )
    return sorted(by_id.values(), key=lambda item: item["name"].casefold())


def preview_desktop_application_uninstall(target: dict[str, str]) -> dict[str, Any]:
    if target.get("manager") == "flatpak":
        app_id = target.get("app_id", "")
        installation = target.get("installation", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", app_id) or installation not in {"user", "system"}:
            return {"ok": False, "message": "Identificação Flatpak inválida."}
        if not shutil.which("flatpak"):
            return {"ok": False, "message": "O comando flatpak não está disponível."}
        return {
            "ok": True,
            "message": f"Flatpak {app_id} ({installation}); removerá o aplicativo e os dados associados dele neste usuário.",
        }
    if target.get("manager") != "apt":
        return {"ok": False, "message": "Origem do aplicativo não compatível com desinstalação."}
    package = target.get("package", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*(?::[a-z0-9-]+)?", package):
        return {"ok": False, "message": "Identificação do pacote APT inválida."}
    apt_get = shutil.which("apt-get")
    if not apt_get:
        return {"ok": False, "message": "apt-get não está disponível nesta distribuição."}
    result = run_command([apt_get, "--simulate", "purge", "--no-auto-remove", package], timeout=60.0)
    if result is None or result.returncode != 0:
        return {"ok": False, "message": "Não foi possível simular a remoção pelo APT."}
    removal_lines = [
        line for line in result.stdout.splitlines()
        if re.match(r"^(?:Remv|Purg)\s+", line)
    ]
    removals = [line.split()[1] for line in removal_lines]
    if len(removals) != 1 or removals[0].split(":", 1)[0] != package.split(":", 1)[0]:
        extras = ", ".join(removals) or "nenhum pacote identificado"
        return {
            "ok": False,
            "message": f"Remoção bloqueada: o APT também removeria ou não identificou pacotes com segurança ({extras}).",
        }
    return {
        "ok": True,
        "message": f"APT removerá e purgará somente {package}. Dependências compartilhadas não serão removidas.",
    }


def uninstall_desktop_application(target: dict[str, str]) -> dict[str, Any]:
    preview = preview_desktop_application_uninstall(target)
    if not preview["ok"]:
        return preview
    if target["manager"] == "apt":
        apt_get = shutil.which("apt-get")
        pkexec = shutil.which("pkexec")
        if not apt_get or not pkexec:
            return {"ok": False, "message": "APT e pkexec são necessários para remover o pacote com autorização do sistema."}
        result = run_privileged_apt_step(
            pkexec,
            apt_get,
            ["purge", "--yes", "--no-auto-remove", target["package"]],
            "Desinstalação APT",
        )
        return {
            "ok": result["ok"],
            "message": "Pacote removido e configurações do sistema purgadas." if result["ok"] else result["message"],
        }
    flatpak = shutil.which("flatpak")
    if not flatpak:
        return {"ok": False, "message": "O comando flatpak não está disponível."}
    option = "--user" if target["installation"] == "user" else "--system"
    result = run_command(
        [flatpak, "uninstall", option, "--delete-data", "--assumeyes", target["app_id"]],
        timeout=600.0,
    )
    if result is None:
        return {"ok": False, "message": "A desinstalação Flatpak não pôde ser iniciada ou excedeu o tempo limite."}
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-4:]
        suffix = " · ".join(detail)
        return {"ok": False, "message": f"Desinstalação Flatpak cancelada ou falhou{': ' + suffix if suffix else '.'}"}
    return {"ok": True, "message": "Aplicativo Flatpak e dados associados removidos."}


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
    sensor_readings = sensor_readings_info()
    metrics = {
        "cpu_percent": cpu_usage(),
        "cpu_frequency": cpu_frequency_info(),
        "memory": memory_info(),
        "gpu_metrics": gpu_metrics,
        "gpu_metrics_status": gpu_metrics_status,
        "temperatures": temperature_info(sensor_readings),
        "sensor_readings": sensor_readings,
        "sensors_status": "Leituras locais via lm-sensors ou sysfs.",
        "disk_io": _DISK_IO_SAMPLER.sample(),
        "power": power_supply_info(),
        "updated_at": datetime.now().astimezone().strftime("%H:%M:%S"),
    }
    try:
        _PERFORMANCE_HISTORY.record(metrics)
    except (OSError, ValueError, sqlite3.Error) as exc:
        metrics["history_status"] = f"Falha no histórico local: {exc}"
    else:
        metrics["history_status"] = (
            "Amostra local registrada."
            if _PERFORMANCE_HISTORY.enabled()
            else "Histórico local desativado."
        )
    return metrics


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
    utilization_summary = None
    if isinstance(gpu_utilization, (int, float)):
        utilization_label = gpu_utilization_label(gpu)
        utilization_summary = (
            f"núcleo {gpu_utilization:.0f}%"
            if utilization_label == "Uso do núcleo gráfico"
            else f"{utilization_label.casefold()} {gpu_utilization:.0f}%"
        )
    gpu_summary = " · ".join(
        value for value in (
            gpu.get("name"),
            utilization_summary,
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
        + (f" · {gpu['power_scope']}" if gpu.get("power_scope") else "")
        if isinstance(power_draw, (int, float)) else f"Potência da GPU: {UNAVAILABLE}"
    )
    gpu_fan_label = f"Ventoinha da GPU: {format_gpu_fan_reading(gpu)}"
    gpu_link_label = f"Link PCIe da GPU: {format_gpu_pcie_link(gpu)}"
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
        short_gpu_metric = "GPU" if gpu_utilization_label(gpu) == "Uso do núcleo gráfico" else "Motor GPU"
        panel_parts.append(f"{short_gpu_metric} {gpu_utilization:.0f}%")
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
    steam = steam_library_info()
    lutris = lutris_library_info()
    heroic = heroic_library_info()
    return {
        "processes": process_list(),
        "applications": desktop_application_list(),
        "startup": autostart_list(),
        "steam": steam,
        "lutris": lutris,
        "heroic": heroic,
        "games": installed_game_list(steam, lutris, heroic),
    }


def collect_snapshot() -> dict[str, Any]:
    memory_modules = memory_module_info()
    gpu_metrics, gpu_metrics_status = gpu_telemetry_info()
    sensor_readings = sensor_readings_info()
    block_devices = block_devices_info()
    smart_devices, smart_devices_status = smart_device_info(block_devices)
    cpu_energy_preference = cpu_energy_preference_info()
    power_profiles = power_profile_info()
    return {
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "cpu": {
            "model": cpu_model(),
            "usage_percent": cpu_usage(),
            "cores": os.cpu_count(),
            "frequency": cpu_frequency_info(),
            "energy_preference": cpu_energy_preference,
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
        "temperatures": temperature_info(sensor_readings),
        "sensor_readings": sensor_readings,
        "sensors_status": "Leituras locais via lm-sensors ou sysfs.",
        "power": power_supply_info(),
        "power_profiles": power_profiles,
        "network": network_info(),
    }


REPORT_SECTION_LABELS = {
    "cpu": "Processador",
    "power_profiles": "Perfis de energia do sistema",
    "memory": "Memória RAM",
    "swap_total": "Swap total",
    "swap_used": "Swap usada",
    "swap_available": "Swap disponível",
    "memory_modules": "Módulos de memória",
    "memory_modules_status": "Estado dos dados de memória",
    "energy_preference": "Preferência energética da CPU",
    "memory_modules_needs_authorization": "Autorização SMBIOS necessária",
    "platform_details": "Placa-mãe e firmware",
    "os": "Sistema operacional",
    "uptime_seconds": "Tempo ligado",
    "disks": "Volumes montados",
    "block_devices": "Discos físicos",
    "smart_devices": "Disponibilidade SMART",
    "smart_devices_status": "Estado SMART",
    "sensor_readings": "Leituras dos sensores",
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
    "driver_name": "Driver gráfico",
    "encoder_utilization_percent": "Uso do codificador de vídeo",
    "decoder_utilization_percent": "Uso do decodificador de vídeo",
    "energy_full_wh": "Energia total",
    "energy_now_wh": "Energia atual",
    "fan_percent": "Velocidade da ventoinha",
    "fan_rpm": "Rotação da ventoinha",
    "fan_pwm_percent": "Sinal PWM da ventoinha",
    "filesystem": "Sistema de arquivos",
    "form_factor": "Formato do módulo",
    "gateway": "Gateway",
    "gpu_utilization_percent": "Uso do núcleo gráfico",
    "gpu_utilization_label": "Métrica de utilização da GPU",
    "gpu_utilization_basis": "Base da métrica de utilização da GPU",
    "engine_utilization": "Utilização por motor gráfico",
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
    "power_scope": "Escopo da potência reportada",
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
    "shared_memory_used_mib": "GTT compartilhada usada",
    "shared_memory_total_mib": "GTT compartilhada total",
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
    stress_session = StressTestSession()
    disk_session = DiskBenchmarkSession()
    benchmark_session = SysbenchBenchmarkSession(sysbench_temperature_snapshot)
    test_sessions = TestSessions(stress_session, disk_session, benchmark_session)

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
        manager: dict[str, Any] = {
            "processes": [], "applications": [], "startup": [],
            "steam": {"state": "unchecked", "message": "Steam ainda não foi consultado.", "games": []},
            "lutris": {"state": "unchecked", "message": "Lutris ainda não foi consultado.", "games": []},
            "heroic": {"state": "unchecked", "message": "Heroic ainda não foi consultado.", "games": []},
            "games": [],
        }
        firmware_info: dict[str, Any] = {"state": "unchecked", "devices": [], "updates": []}
        net_results: list[dict[str, Any]] = []
        ai_lock = threading.Lock()
        ai_state: dict[str, Any] = {
            "state": "unchecked",
            "models": [],
            "selected_model_index": 0,
            "message": "IA local não consultada.",
            "busy": False,
            "result": None,
        }
        selected = 0
        programs_mode = "applications"
        selected_gpu_index = 0
        page_scroll = {PAGE_SYSTEM: 0, PAGE_MEMORY: 0, PAGE_GRAPHICS: 0}
        page_max_scroll = {PAGE_SYSTEM: 0, PAGE_MEMORY: 0, PAGE_GRAPHICS: 0}
        status = "Lendo dados do Linux…"
        last_live_capture_at = 0.0
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

        def confirm_terminate_tree() -> None:
            nonlocal status
            processes = manager.get("processes", [])
            if not processes or selected >= len(processes):
                status = "Nenhum processo selecionado."
                return
            process = processes[selected]
            if not process["can_terminate"]:
                status = "Este processo está protegido ou pertence a outro usuário."
                return
            try:
                tree = process_tree_snapshot(
                    process["pid"], process["start_ticks"], process_records=processes,
                )
            except (OSError, ValueError) as exc:
                status = f"Não foi possível ler a árvore: {exc}"
                return
            if len(tree) <= 1:
                status = "O processo selecionado não tem descendentes nesta captura."
                return
            protected = [item for item in tree if not item["can_terminate"]]
            if protected:
                examples = ", ".join(
                    f"{item['name']} (PID {item['pid']})" for item in protected[:3]
                )
                status = f"A árvore contém processo(s) protegido(s): {examples}; nenhum sinal foi enviado."
                return

            descendants = len(tree) - 1
            prompt = (
                f"Encerrar árvore de {process['name']} (PID {process['pid']})? "
                f"{len(tree)} processos no total, incluindo {descendants} descendentes. [s/N] "
            )
            curses_text(
                stdscr, stdscr.getmaxyx()[0] - 2, 2, prompt,
                width=stdscr.getmaxyx()[1] - 4, attr=curses.color_pair(4),
            )
            stdscr.refresh()
            answer = stdscr.getch()
            if answer not in (ord("s"), ord("S")):
                status = "Encerramento da árvore cancelado."
                return
            try:
                result = terminate_process_tree(
                    process["pid"], process["start_ticks"], process_records=tree,
                )
                sent = len(result["sent"])
                changed = len(result["changed"])
                failures = result["failed"]
                failed = len(failures)
                failure_detail = ""
                if failures:
                    first = failures[0]
                    target = first["process"]
                    failure_detail = f" · falha em {target['name']} (PID {target['pid']})"
                status = (
                    f"SIGTERM enviado para {sent}/{result['total']} processos da árvore"
                    f"; {changed} já encerrados ou alterados; {failed} falhas{failure_detail}."
                )
                refresh_manager()
            except (OSError, ValueError) as exc:
                status = f"Não foi possível encerrar a árvore: {exc}"

        def open_selected_app() -> None:
            nonlocal status
            if programs_mode == "games":
                games = manager.get("games", [])
                if not games or selected >= len(games):
                    status = "Nenhum jogo selecionado."
                    return
                game = games[selected]
                result = launch_installed_game(game["launcher"], game["game_id"], game.get("runner"))
                status = f"{game['name']}: {result['message']}"
                return
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

        def confirm_stress_test(kind: str) -> None:
            nonlocal status
            if test_sessions.is_active():
                status = "Já existe um teste de carga em andamento."
                return
            memory = data.get("memory") or {}
            available = memory.get("available")
            duration = 30
            gpu_metrics = data.get("gpu_metrics", [])
            selected_gpu = gpu_metrics[selected_gpu_index] if selected_gpu_index < len(gpu_metrics) else None
            readiness = stress_test_readiness(data.get("temperatures", []), kind, selected_gpu)
            if not readiness["ready"]:
                status = readiness["message"]
                return
            try:
                stress_test_command(kind, duration, 1, available, gpu_device=readiness.get("gpu_device"))
            except (RuntimeError, ValueError) as exc:
                status = str(exc)

        def confirm_sysbench_benchmark(kind: str) -> None:
            nonlocal status
            if test_sessions.is_active():
                status = "Encerre o teste atual antes de iniciar outro benchmark."
                return
            if not shutil.which("sysbench"):
                confirm_optional_tool_install()
                return
            available = memory_info().get("available")
            readiness = sysbench_benchmark_readiness(
                kind,
                available,
                {
                    "cpu_temperatures": cpu_temperature_readings(data.get("temperatures", [])),
                    "temperatures": data.get("temperatures", []),
                },
                executable=shutil.which("sysbench"),
            )
            if not readiness["ready"]:
                status = readiness["message"]
                return
            if kind == "cpu-benchmark":
                details = (
                    f"sysbench CPU · {SYSBENCH_CPU_DURATION_SECONDS} s · até {readiness['threads']} threads · "
                    f"parada automática a {SYSBENCH_ABORT_TEMP_C:.0f} °C ou se a temperatura da CPU ficar indisponível"
                )
            else:
                buffer_bytes = int(readiness["buffer_bytes"])
                details = (
                    f"Leitura e escrita sequenciais · {SYSBENCH_MEMORY_PASS_SECONDS} s cada · "
                    f"buffer privado de {format_bytes(buffer_bytes)} (máximo {format_bytes(SYSBENCH_MEMORY_MAX_BUFFER_BYTES)}; "
                    f"no máximo 1/16 da RAM disponível) · sem arquivos ou acesso bruto"
                )
            prompt = f"Iniciar benchmark de {'CPU' if kind == 'cpu-benchmark' else 'RAM'}? {details}. [s/N] "
            curses_text(stdscr, stdscr.getmaxyx()[0] - 2, 2, prompt, width=stdscr.getmaxyx()[1] - 4, attr=curses.color_pair(4))
            stdscr.refresh()
            if stdscr.getch() not in (ord("s"), ord("S")):
                status = "Benchmark cancelado antes de iniciar."
                return
            try:
                status = test_sessions.start_benchmark(kind, available)
            except (RuntimeError, ValueError) as exc:
                status = str(exc)
                return
            label = {
                "cpu": "CPU · 1 worker",
                "memory": "RAM · até 25% disponível, limitado a 1 GiB",
                "gpu": f"GPU · {selected_gpu.get('name', 'adaptador selecionado')} · CPU/GPU monitoradas",
            }.get(kind, kind)
            prompt = f"Iniciar teste de {label} por {duration} s? [s/N] "
            curses_text(stdscr, stdscr.getmaxyx()[0] - 2, 2, prompt, width=stdscr.getmaxyx()[1] - 4, attr=curses.color_pair(4))
            stdscr.refresh()
            if stdscr.getch() not in (ord("s"), ord("S")):
                status = "Teste de carga cancelado antes de iniciar."
                return
            try:
                status = test_sessions.start_stress(kind, duration, 1, available, selected_gpu if kind == "gpu" else None)
            except (RuntimeError, ValueError) as exc:
                status = str(exc)

        def confirm_disk_benchmark() -> None:
            nonlocal status
            if test_sessions.is_active():
                status = "Encerre o teste atual antes de iniciar o benchmark de disco."
                return
            if not shutil.which("fio"):
                confirm_optional_tool_install()
                return
            height, width = stdscr.getmaxyx()
            prompt = "Pasta no volume para avaliar (Enter = diretório pessoal): "
            curses_text(stdscr, height - 2, 2, prompt, width=width - 4, attr=curses.color_pair(4))
            input_x = min(width - 2, 2 + len(prompt))
            stdscr.refresh()
            raw_path = b""
            try:
                curses.curs_set(1)
            except curses.error:
                pass
            curses.echo()
            try:
                raw_path = stdscr.getstr(height - 2, input_x, max(1, width - input_x - 2))
            except curses.error:
                pass
            finally:
                curses.noecho()
                try:
                    curses.curs_set(0)
                except curses.error:
                    pass
            selected_path = os.fsdecode(raw_path).strip() or str(Path.home())
            readiness = disk_benchmark_readiness(selected_path)
            if not readiness.get("ready"):
                status = str(readiness.get("message") or "Destino do benchmark indisponível.")
                return
            target = str(readiness["target"])
            source = readiness.get("source") or readiness.get("mountpoint") or "volume selecionado"
            curses_text(stdscr, height - 4, 2, f"Volume: {source} · pasta: {target}", width=width - 4)
            curses_text(
                stdscr,
                height - 3,
                2,
                f"fio: {DISK_BENCHMARK_SIZE_MIB} MiB temporários · sequencial + 4K aleatório QD1/QD32 · 4 perfis × {DISK_BENCHMARK_RANDOM_JOB_SECONDS} s · sem watchdog térmico",
                width=width - 4,
            )
            confirmation = f"Máximo {DISK_BENCHMARK_TIMEOUT_SECONDS} s; iniciar? [s/N] "
            curses_text(stdscr, height - 2, 2, confirmation, width=width - 4, attr=curses.color_pair(4))
            stdscr.refresh()
            if stdscr.getch() not in (ord("s"), ord("S")):
                status = "Benchmark de disco cancelado antes de iniciar."
                return
            try:
                status = test_sessions.start_disk(target)
            except (RuntimeError, ValueError) as exc:
                status = str(exc)

        def confirm_optional_tool_install() -> None:
            nonlocal status
            if test_sessions.is_active():
                status = "Cancele o teste de carga antes de instalar ferramentas."
                return
            packages = missing_optional_packages()
            if not packages:
                status = "Todas as ferramentas opcionais já estão instaladas."
                return
            prompt = f"Atualizar índices do APT e instalar {', '.join(packages)}? [s/N] "
            curses_text(stdscr, stdscr.getmaxyx()[0] - 2, 2, prompt, width=stdscr.getmaxyx()[1] - 4, attr=curses.color_pair(4))
            stdscr.refresh()
            if stdscr.getch() not in (ord("s"), ord("S")):
                status = "Instalação cancelada antes de iniciar."
                return
            result = install_missing_optional_dependencies()
            status = result["message"]
            if result["ok"]:
                refresh()

        def confirm_power_profile() -> None:
            nonlocal status, data
            if test_sessions.is_active():
                status = "Encerre o teste de carga antes de mudar o perfil de energia."
                return
            info = power_profile_info()
            data["power_profiles"] = info
            if not info["available"]:
                status = info["message"]
                return
            profiles = info["profiles"]
            choices = " · ".join(
                f"{index + 1} {POWER_PROFILE_LABELS[profile]}"
                for index, profile in enumerate(profiles)
            )
            prompt = f"Escolha perfil: {choices} · Esc cancela"
            curses_text(stdscr, stdscr.getmaxyx()[0] - 2, 2, prompt, width=stdscr.getmaxyx()[1] - 4, attr=curses.color_pair(4))
            stdscr.refresh()
            key = stdscr.getch()
            if key < ord("1") or key >= ord("1") + len(profiles):
                status = "Alteração de perfil cancelada."
                return
            profile = profiles[key - ord("1")]
            if profile == info["current"]:
                status = f"O perfil {POWER_PROFILE_LABELS[profile]} já está ativo."
                return
            prompt = f"Aplicar {POWER_PROFILE_LABELS[profile]}? Pode alterar resposta, ruído e consumo."
            curses_text(stdscr, stdscr.getmaxyx()[0] - 3, 2, prompt, width=stdscr.getmaxyx()[1] - 4, attr=curses.color_pair(4))
            curses_text(stdscr, stdscr.getmaxyx()[0] - 2, 2, "Confirmar? [s/N]", width=stdscr.getmaxyx()[1] - 4, attr=curses.color_pair(4))
            stdscr.refresh()
            if stdscr.getch() not in (ord("s"), ord("S")):
                status = "Alteração de perfil cancelada."
                return
            result = set_power_profile(profile)
            message = result["message"]
            refresh()
            status = message

        def confirm_power_profile_restore() -> None:
            nonlocal status
            if test_sessions.is_active():
                status = "Encerre o teste de carga antes de restaurar o perfil."
                return
            info = power_profile_info()
            restore_state = info.get("restore_state")
            if not restore_state:
                status = "Não há perfil anterior do BooM para restaurar."
                return
            profile = restore_state["restore_profile"]
            prompt = f"Restaurar {POWER_PROFILE_LABELS[profile]}? Isso substitui o perfil atual."
            curses_text(stdscr, stdscr.getmaxyx()[0] - 3, 2, prompt, width=stdscr.getmaxyx()[1] - 4, attr=curses.color_pair(4))
            curses_text(stdscr, stdscr.getmaxyx()[0] - 2, 2, "Confirmar? [s/N]", width=stdscr.getmaxyx()[1] - 4, attr=curses.color_pair(4))
            stdscr.refresh()
            if stdscr.getch() not in (ord("s"), ord("S")):
                status = "Restauração cancelada."
                return
            result = restore_power_profile()
            message = result["message"]
            refresh()
            status = message

        def toggle_performance_history() -> None:
            nonlocal status
            enabled = _PERFORMANCE_HISTORY.enabled()
            if not enabled:
                prompt = "Gravar métricas agregadas localmente por até 7 dias enquanto o BooM estiver aberto? [s/N] "
                curses_text(stdscr, stdscr.getmaxyx()[0] - 2, 2, prompt, width=stdscr.getmaxyx()[1] - 4, attr=curses.color_pair(4))
                stdscr.refresh()
                if stdscr.getch() not in (ord("s"), ord("S")):
                    status = "Histórico continua desativado."
                    return
            try:
                _PERFORMANCE_HISTORY.set_enabled(not enabled)
            except OSError as exc:
                status = f"Não foi possível alterar o histórico: {exc}"
                return
            status = "Gravação do histórico ativada." if not enabled else "Gravação do histórico pausada; dados existentes mantidos."

        def show_performance_diagnosis() -> None:
            nonlocal status, data
            try:
                diagnosis = analyze_performance_history(_PERFORMANCE_HISTORY.samples(window_minutes=15))
            except (OSError, sqlite3.Error, ValueError) as exc:
                status = f"Falha na análise local: {exc}"
                return
            data["performance_diagnosis"] = diagnosis
            status = diagnosis["summary"]

        def get_ai_state() -> dict[str, Any]:
            with ai_lock:
                return dict(ai_state)

        def update_ai_state(**values: Any) -> None:
            with ai_lock:
                ai_state.update(values)

        def check_local_ai_models() -> None:
            nonlocal status
            current = get_ai_state()
            if current["busy"]:
                status = "Aguarde a tarefa de IA local atual."
                return
            update_ai_state(state="checking", busy=True, message="Consultando modelos locais no Ollama…")
            status = "Consultando IA local em 127.0.0.1…"

            def worker() -> None:
                result = local_ollama_models()
                state = get_ai_state()
                names = [item["name"] for item in result.get("models", [])]
                selected = state.get("selected_model")
                index = names.index(selected) if selected in names else 0
                update_ai_state(
                    state=result["state"],
                    models=result.get("models", []),
                    selected_model_index=index,
                    selected_model=names[index] if names else None,
                    message=result["message"],
                    busy=False,
                )

            threading.Thread(target=worker, name="boom-local-ai-models", daemon=True).start()

        def cycle_local_ai_model() -> None:
            nonlocal status
            state = get_ai_state()
            models = state.get("models", [])
            if not models:
                status = "Verifique o Ollama local com O antes de selecionar um modelo."
                return
            index = (int(state.get("selected_model_index", 0)) + 1) % len(models)
            update_ai_state(
                selected_model_index=index,
                selected_model=models[index]["name"],
                result=None,
            )
            status = f"Modelo local selecionado: {models[index]['name']}"

        def start_local_ai_explanation() -> None:
            nonlocal status
            state = get_ai_state()
            if state["busy"]:
                status = "Aguarde a tarefa de IA local atual."
                return
            models = state.get("models", [])
            if not models:
                status = "Verifique o Ollama local com O antes de analisar."
                return
            index = min(max(0, int(state.get("selected_model_index", 0))), len(models) - 1)
            model = models[index]["name"]
            update_ai_state(state="analyzing", busy=True, message=f"IA local analisando com {model}…", result=None)
            status = f"IA local analisando com {model}; isso pode levar alguns minutos…"

            def worker() -> None:
                try:
                    diagnosis = analyze_performance_history(_PERFORMANCE_HISTORY.samples(window_minutes=15))
                    if diagnosis["status"] != "analyzed":
                        raise LocalAIError(diagnosis["summary"])
                    explanation = explain_performance_diagnosis(model, diagnosis)
                    result = {"state": "ready", "diagnosis": diagnosis, "explanation": explanation}
                    message = f"Explicação local pronta com {model}; nada foi alterado ou salvo."
                except (LocalAIError, OSError, sqlite3.Error, ValueError) as exc:
                    result = {"state": "error", "message": str(exc)}
                    message = str(exc)
                update_ai_state(state=result["state"], result=result, busy=False, message=message)

            threading.Thread(target=worker, name="boom-local-ai-analysis", daemon=True).start()

        refresh()
        while True:
            history_enabled = _PERFORMANCE_HISTORY.enabled()
            now = time.monotonic()
            if now - last_live_capture_at >= HISTORY_SAMPLE_INTERVAL_SECONDS:
                try:
                    live_metrics = collect_tray_metrics()
                    data.setdefault("cpu", {})["usage_percent"] = live_metrics.get("cpu_percent")
                    data["cpu"]["frequency"] = live_metrics.get("cpu_frequency") or {}
                    data["memory"] = live_metrics.get("memory")
                    data["gpu_metrics"] = live_metrics.get("gpu_metrics", [])
                    data["gpu_metrics_status"] = live_metrics.get("gpu_metrics_status", UNAVAILABLE)
                    data["temperatures"] = live_metrics.get("temperatures", [])
                    data["sensor_readings"] = live_metrics.get("sensor_readings", [])
                    data["disk_io"] = live_metrics.get("disk_io")
                    data["power"] = live_metrics.get("power", {})
                    if history_enabled:
                        status = live_metrics.get("history_status", "Amostra local coletada.")
                except Exception as exc:
                    status = f"Falha ao atualizar métricas locais: {exc}"
                last_live_capture_at = now
            stdscr.timeout(1000 if test_sessions.is_active() else int(HISTORY_SAMPLE_INTERVAL_SECONDS * 1000))
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            curses_text(stdscr, 1, 2, "BooM", attr=curses.color_pair(1) | curses.A_BOLD)
            curses_text(stdscr, 1, 10, "PAINEL LINUX · MONITORAR E CONTROLAR", attr=curses.color_pair(2) | curses.A_BOLD)
            curses_text(stdscr, 3, 2, "1 Painel  2 Sistema  3 Memória  4 Gráficos  5 Discos", attr=curses.A_BOLD)
            curses_text(stdscr, 4, 2, "6 Rede  7 Processos  8 Programas  9 Inicialização  0 Testes", attr=curses.A_BOLD)
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
                    f"{gpu_utilization_label(gpu)} {gpu_usage:.0f}%" if isinstance(gpu_usage, (int, float)) else None,
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
                epp = cpu.get("energy_preference", {})
                if epp.get("available"):
                    current_epp = format_cpu_energy_preference(epp)
                    system_rows.append(f"Preferência energética da CPU: {current_epp} · {len(epp.get('policies', []))} políticas")
                else:
                    system_rows.append("Preferência energética da CPU: não exposta pelo kernel/driver")
                if firmware_info.get("state") == "unchecked":
                    system_rows.append("Firmware: pressione F para consultar dispositivos e atualizações locais via fwupd")
                else:
                    system_rows.append("Firmware: " + firmware_info.get("message", "indisponível"))
                    for item in firmware_info.get("updates", [])[:5]:
                        current = item.get("current_version") or UNAVAILABLE
                        target = item.get("update_version") or "versão disponível"
                        reboot = " · exige reinicialização" if item.get("needs_reboot") else ""
                        system_rows.append(f"Atualização de firmware · {item['name']}: {current} → {target}{reboot}")
                    if firmware_info.get("state") == "ready" and not firmware_info.get("updates"):
                        updatable = [item for item in firmware_info.get("devices", []) if item.get("updatable")]
                        system_rows.append(f"Dispositivos atualizáveis pelo fwupd: {len(updatable)} · sem atualização nos metadados locais")
                    system_rows.append("BooM consulta metadados locais e não aplica firmware nem atualiza o catálogo.")
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
                profiles = data.get("power_profiles", {})
                if profiles.get("available"):
                    current_profile = profiles.get("current")
                    current_label = POWER_PROFILE_LABELS.get(current_profile, current_profile or UNAVAILABLE)
                    profile_names = ", ".join(
                        POWER_PROFILE_LABELS.get(profile, profile)
                        for profile in profiles.get("profiles", [])
                    )
                    system_rows.extend([
                        f"Perfil de energia do sistema: {current_label} · disponíveis: {profile_names}",
                        "Pressione P para escolher um perfil. A alteração é confirmada e pode ser restaurada.",
                    ])
                    restore_state = profiles.get("restore_state")
                    if restore_state:
                        system_rows.append(
                            f"Perfil anterior guardado: {POWER_PROFILE_LABELS[restore_state['restore_profile']]} · pressione V para restaurar"
                        )
                    if profiles.get("external_change_detected"):
                        system_rows.append("Outro componente mudou o perfil desde a última alteração do BooM.")
                else:
                    system_rows.extend([
                        "Perfis de energia: " + profiles.get("message", "indisponível"),
                        "Use I para instalar dependências opcionais ausentes, se houver pacote compatível.",
                    ])
                history_status = _PERFORMANCE_HISTORY.status()
                history_state = "gravando" if history_status["enabled"] else "desativado"
                system_rows.extend([
                    "",
                    f"Histórico local: {history_state} · {history_status['sample_count']} amostras · retenção de 7 dias durante a gravação",
                    "Ao pausar, amostras existentes ficam até retomar a gravação ou você apagá-las.",
                    "Pressione H para ativar/pausar o histórico e D para analisar os últimos 15 minutos.",
                ])
                ai = get_ai_state()
                system_rows.extend(["", "IA local: " + ai.get("message", "não consultada")])
                if ai.get("models"):
                    selected_index = min(max(0, int(ai.get("selected_model_index", 0))), len(ai["models"]) - 1)
                    system_rows.append(f"Modelo: {ai['models'][selected_index]['name']} · O consultar · M alternar")
                system_rows.append("Ollama usa apenas 127.0.0.1; métricas não saem deste computador.")
                if ai.get("state") in {"offline", "no_models"}:
                    system_rows.append("Instalação manual do runtime: " + OLLAMA_DOWNLOAD_URL)
                ai_result = ai.get("result") or {}
                if ai_result.get("state") == "ready":
                    answer = ai_result["explanation"]
                    system_rows.append(f"IA · {answer['summary']} · confiança dos dados: {answer['confidence']}")
                    explanation_findings = answer.get("findings", [])
                    diagnosis_findings = ai_result["diagnosis"].get("findings", [])
                    source_findings = diagnosis_findings or explanation_findings
                    diagnosis_by_id = {item["id"]: item for item in source_findings if isinstance(item, dict) and item.get("id")}
                    for item in answer["explanations"]:
                        original = diagnosis_by_id.get(item["finding_id"])
                        title = original.get("title") if original else "Resumo da janela"
                        system_rows.extend([
                            f"{title}: {item['explanation']}",
                            f"Recomendação: {item['recommendation']}",
                            f"Risco/limite: {item['risk']}",
                        ])
                    for limitation in answer["limitations"]:
                        system_rows.append("Limite: " + limitation)
                elif ai_result.get("state") == "error":
                    system_rows.append("IA local: " + ai_result.get("message", "falha sem detalhes"))
                diagnosis = data.get("performance_diagnosis") or {}
                if diagnosis:
                    system_rows.append("Diagnóstico: " + diagnosis.get("summary", "sem resumo"))
                    for finding in diagnosis.get("findings", []):
                        system_rows.append(f"· {finding['title']}: {finding['message']}")
                visible = max(1, height - 11)
                page_max_scroll[page] = max(0, len(system_rows) - visible)
                page_scroll[page] = min(page_scroll[page], page_max_scroll[page])
                for line in system_rows[page_scroll[page]:page_scroll[page] + visible]:
                    curses_text(stdscr, y, 2, line, width=width - 4, attr=curses.color_pair(4) if line.startswith("Pressione M") else 0)
                    y += 1
            elif page == PAGE_TESTS:
                readings = data.get("temperatures", [])
                readiness = stress_test_readiness(readings)
                session = test_sessions.snapshot()
                cpu_temps = cpu_temperature_readings(readings)
                gpu_metrics = data.get("gpu_metrics", [])
                if gpu_metrics:
                    selected_gpu_index = min(selected_gpu_index, len(gpu_metrics) - 1)
                selected_gpu = gpu_metrics[selected_gpu_index] if gpu_metrics else None
                gpu_readiness = stress_test_readiness(readings, "gpu", selected_gpu)
                active_test = session.get("status") in {"starting", "running", "stopping"}
                active_stress = active_test and session.get("kind") in {
                    "cpu", "memory", "gpu", "cpu-benchmark", "memory-benchmark",
                }
                cpu_temp = session.get("cpu_celsius") if active_stress else max((item["celsius"] for item in cpu_temps), default=None)
                gpu_temp = session.get("gpu_celsius") if active_stress and session.get("kind") == "gpu" else (selected_gpu or {}).get("temperature_celsius")
                all_temp = session.get("current_celsius") if active_stress else gpu_readiness.get("current_celsius") if selected_gpu else readiness.get("current_celsius")
                memory_available = (data.get("memory") or {}).get("available")
                sysbench_executable = shutil.which("sysbench")
                benchmark_cpu = sysbench_benchmark_readiness(
                    "cpu-benchmark", memory_available,
                    {"cpu_temperatures": cpu_temps, "temperatures": readings},
                    executable=sysbench_executable,
                )
                benchmark_memory = sysbench_benchmark_readiness(
                    "memory-benchmark", memory_available,
                    {"cpu_temperatures": cpu_temps, "temperatures": readings},
                    executable=sysbench_executable,
                )
                allocation = min((memory_available or 0) // 4, STRESS_TEST_MAX_MEMORY_BYTES)
                allocation = allocation // (1024**2) * (1024**2)
                test_rows = [
                    "Ferramentas ausentes: " + (", ".join(missing_optional_packages()) or "nenhuma"),
                    "Sensores: " + ("lm-sensors / sysfs" if readings else "sem leitura térmica"),
                    f"Temperatura CPU atual: {cpu_temp:.1f} °C" if isinstance(cpu_temp, (int, float)) else "Temperatura CPU atual: indisponível",
                    f"GPU selecionada: {selected_gpu.get('name', UNAVAILABLE)} · {selected_gpu.get('pci_address', 'PCI indisponível')}" if selected_gpu else "GPU selecionada: nenhuma detectada",
                    f"Temperatura GPU: {gpu_temp:.1f} °C" if isinstance(gpu_temp, (int, float)) else "Temperatura GPU: indisponível",
                    f"Sensor mais quente: {all_temp:.1f} °C · início abaixo de {STRESS_TEST_PRECHECK_TEMP_C:.0f} °C · parada em {STRESS_TEST_ABORT_TEMP_C:.0f} °C" if isinstance(all_temp, (int, float)) else "Sensor mais quente: indisponível",
                    f"RAM para teste: {format_bytes(allocation)} · um worker · no máximo 25% da RAM disponível",
                    f"Benchmark de disco: fio · arquivo temporário de {DISK_BENCHMARK_SIZE_MIB} MiB · sequencial + aleatório 4 KiB QD1/QD32 · máximo de {DISK_BENCHMARK_TIMEOUT_SECONDS} s",
                    "O benchmark só grava em arquivo novo na pasta escolhida; o arquivo é removido ao terminar.",
                    "O benchmark de disco ainda não tem parada automática por temperatura.",
                    "Benchmark CPU/RAM: " + ("sysbench instalado" if sysbench_executable else "sysbench ausente · use I para instalar ferramentas opcionais"),
                    "CPU: até 4 threads por 10 s. RAM: leitura e escrita, 5 s cada; buffer privado de até 256 MiB e 1/16 da RAM disponível.",
                    "O benchmark de RAM só começa com pelo menos 512 MiB disponíveis; CPU e RAM param a 85 °C ou se o sensor da CPU falhar.",
                    "Duração de carga no terminal: 30 s · GTK permite 10, 30, 60 ou 120 s",
                    "Pressione I para instalar ferramentas ausentes (apt + autorização do sistema).",
                    "Estado: " + session.get("message", "Nenhum teste executado."),
                    readiness["message"],
                    "GPU: " + gpu_readiness["message"],
                    "Benchmark CPU: " + benchmark_cpu["message"],
                    "Benchmark RAM: " + benchmark_memory["message"],
                    "Atalhos: " + test_action_legend() + " · [/] escolhe GPU · X cancela.",
                ]
                for line in test_rows:
                    curses_text(stdscr, y, 2, line, width=width - 4); y += 1
                if session.get("summary"):
                    curses_text(stdscr, y + 1, 2, "Resumo do teste:", attr=curses.color_pair(2) | curses.A_BOLD); y += 2
                    for line in session["summary"].splitlines()[-max(1, height - y - 4):]:
                        curses_text(stdscr, y, 2, line, width=width - 4); y += 1
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
                disk_io = data.get("disk_io") or {}
                busy = disk_io.get("busy_percent")
                curses_text(stdscr, y, 2, "Atividade de armazenamento", attr=curses.color_pair(2) | curses.A_BOLD); y += 1
                if isinstance(busy, (int, float)):
                    read_rate = format_bytes(disk_io.get("read_bytes_per_second")) + "/s"
                    write_rate = format_bytes(disk_io.get("write_bytes_per_second")) + "/s"
                    wait = disk_io.get("average_io_wait_ms")
                    wait_text = f"{wait:.1f} ms" if isinstance(wait, (int, float)) else UNAVAILABLE
                    curses_text(stdscr, y, 2, f"Mais ocupado: {busy:.1f}% · leitura {read_rate} · gravação {write_rate}", width=width - 4); y += 1
                    curses_text(stdscr, y, 2, f"Latência média aprox. de I/O: {wait_text}", width=width - 4); y += 1
                else:
                    curses_text(stdscr, y, 2, "Aguardando uma segunda amostra do kernel.", width=width - 4); y += 1
                curses_text(stdscr, y, 2, "Fonte: /proc/diskstats · estimativa por dispositivo inteiro; não identifica processos.", width=width - 4); y += 2
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
                curses_text(stdscr, y, 2, "PID       PPID      CPU     RAM       ESTADO      PROCESSO", attr=curses.color_pair(2) | curses.A_BOLD); y += 1
                visible = max(1, height - 11)
                first = max(0, min(selected - visible + 1, len(processes) - visible))
                for index in range(first, min(len(processes), first + visible)):
                    process = processes[index]
                    marker = ">" if index == selected else " "
                    cpu_value = f"{process['cpu_percent']:.1f}%" if process.get("cpu_percent") is not None else "—"
                    access = "encerrável" if process["can_terminate"] else "protegido"
                    line = f"{marker} {process['pid']:<8} {process['ppid']:<8} {cpu_value:<7} {format_bytes(process['memory_bytes']):<9} {process['state']:<10} {process['name']} · {access}"
                    curses_text(stdscr, y, 2, line, width=width - 4, attr=curses.color_pair(3) if index == selected else 0); y += 1
                if not processes:
                    curses_text(stdscr, y, 2, "Nenhum processo visível."); y += 1
            elif page == PAGE_APPLICATIONS:
                if programs_mode == "games":
                    steam = manager.get("steam", {})
                    lutris = manager.get("lutris", {})
                    heroic = manager.get("heroic", {})
                    games = manager.get("games", [])
                    curses_text(stdscr, y, 2, "BIBLIOTECAS LOCAIS DE JOGOS", attr=curses.color_pair(2) | curses.A_BOLD); y += 1
                    curses_text(stdscr, y, 2, "Steam: " + steam.get("message", "indisponível"), width=width - 4); y += 1
                    curses_text(stdscr, y, 2, "Lutris: " + lutris.get("message", "indisponível"), width=width - 4); y += 1
                    curses_text(stdscr, y, 2, "Heroic: " + heroic.get("message", "indisponível"), width=width - 4); y += 1
                    tool_states = {item["name"]: item["installed"] for item in gaming_tool_status()}
                    curses_text(
                        stdscr, y, 2,
                        f"MangoHud: {'instalado' if tool_states.get('MangoHud') else 'ausente'} · "
                        f"GameMode: {'instalado' if tool_states.get('GameMode') else 'ausente'}",
                        width=width - 4,
                    ); y += 1
                    curses_text(stdscr, y, 2, "Opções Steam: mangohud %command% · gamemoderun %command%", width=width - 4); y += 1
                    visible = max(1, height - 16)
                    first = max(0, min(selected - visible + 1, len(games) - visible))
                    for index in range(first, min(len(games), first + visible)):
                        game = games[index]
                        marker = ">" if index == selected else " "
                        store_label = f"/{game['store']}" if game.get("store") else ""
                        line = f"{marker} {game['name']} · {game['launcher']}{store_label} · ID {game['game_id']}"
                        curses_text(stdscr, y, 2, line, width=width - 4, attr=curses.color_pair(3) if index == selected else 0); y += 1
                    if not games:
                        curses_text(stdscr, y, 2, "Nenhum jogo instalado foi encontrado em Steam, Lutris ou Heroic."); y += 1
                else:
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
            if page == PAGE_SYSTEM:
                controls = "R atualizar · I instalar · F firmware · O IA local · M modelo · A explicar · P perfil · V restaurar · H histórico · D regras · Q sair"
            elif page == PAGE_TESTS:
                controls = f"1-9/0 telas · R atualizar · I instalar · {test_action_legend()} · [/] GPU · X cancelar · Q sair"
            elif page == PAGE_PROCESSES:
                controls = "↑↓ navegar · K processo · T/t árvore · R atualizar · Q sair"
            elif page == PAGE_APPLICATIONS:
                controls = (
                    "G alternar apps/jogos · ↑↓ navegar · L abrir no lançador · I instalar ferramentas · R atualizar · Q sair"
                    if programs_mode == "games" else
                    "G jogos Steam/Lutris/Heroic · ↑↓ navegar · L abrir aplicativo · R atualizar · Q sair"
                )
            else:
                controls = "1-9/0 telas · R atualizar · ↑↓ navegar/rolar · Q sair"
            curses_text(stdscr, height - 1, 2, controls, width=width - 4)
            stdscr.refresh()
            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                test_sessions.stop_and_wait()
                return
            if key == -1:
                continue
            if key in tuple(ord(str(number)) for number in range(1, 10)) or key == ord("0"):
                page = PAGE_TESTS if key == ord("0") else key - ord("1")
                selected = 0
                if page in page_scroll:
                    page_scroll[page] = 0
            elif key in (ord("r"), ord("R")):
                refresh()
            elif key in (ord("g"), ord("G")) and page == PAGE_APPLICATIONS:
                programs_mode = "applications" if programs_mode == "games" else "games"
                selected = 0
                status = "Jogos locais de Steam, Lutris e Heroic." if programs_mode == "games" else "Lista de aplicativos gráficos."
            elif key in (ord("n"), ord("N")) and page == PAGE_NETWORK:
                status = "Testando conectividade; aguarde…"
                net_results = run_network_test(data)
                status = "Diagnóstico concluído às " + datetime.now().astimezone().strftime("%H:%M:%S")
            elif key in (curses.KEY_UP, ord("k")) and page in page_scroll:
                page_scroll[page] = max(0, page_scroll[page] - 1)
            elif key in (curses.KEY_DOWN, ord("j")) and page in page_scroll:
                page_scroll[page] = min(page_max_scroll[page], page_scroll[page] + 1)
            elif key in (ord("T"), ord("t")) and page == PAGE_PROCESSES:
                confirm_terminate_tree()
            elif key in (curses.KEY_UP, ord("k"), ord("K")) and page in (PAGE_PROCESSES, PAGE_APPLICATIONS):
                if key == ord("K") and page == PAGE_PROCESSES:
                    confirm_terminate()
                elif key == ord("k") and page == PAGE_PROCESSES:
                    selected = max(0, selected - 1)
                else:
                    selected = max(0, selected - 1)
            elif key in (curses.KEY_DOWN, ord("j")) and page in (PAGE_PROCESSES, PAGE_APPLICATIONS):
                if page == PAGE_PROCESSES:
                    limit = len(manager.get("processes", []))
                elif programs_mode == "games":
                    limit = len(manager.get("games", []))
                else:
                    limit = len(manager.get("applications", []))
                selected = min(max(0, limit - 1), selected + 1)
            elif key in (ord("l"), ord("L")) and page == PAGE_APPLICATIONS:
                open_selected_app()
            elif key in (ord("m"), ord("M")) and page == PAGE_MEMORY and data.get("memory_modules_needs_authorization"):
                load_authorized_memory()
            elif key in (ord("["), ord("]")) and page == PAGE_TESTS and data.get("gpu_metrics"):
                step = -1 if key == ord("[") else 1
                selected_gpu_index = (selected_gpu_index + step) % len(data["gpu_metrics"])
                status = "GPU selecionada: " + str(data["gpu_metrics"][selected_gpu_index].get("name") or "adaptador")
            elif key in (ord("x"), ord("X")) and page == PAGE_TESTS:
                test_sessions.cancel()
                status = "Cancelamento do teste solicitado."
            elif page == PAGE_TESTS and key >= 0 and test_action_for_shortcut(chr(key)):
                action = test_action_for_shortcut(chr(key))
                if action and action.kind == "disk":
                    confirm_disk_benchmark()
                elif action and action.kind in {"cpu-benchmark", "memory-benchmark"}:
                    confirm_sysbench_benchmark(action.kind)
                elif action:
                    confirm_stress_test(action.kind)
            elif key in (ord("i"), ord("I")) and (
                page in (PAGE_SYSTEM, PAGE_TESTS)
                or (page == PAGE_APPLICATIONS and programs_mode == "games")
            ):
                confirm_optional_tool_install()
            elif key in (ord("f"), ord("F")) and page == PAGE_SYSTEM:
                firmware_info = firmware_inventory_info()
                status = firmware_info.get("message", "Consulta de firmware concluída.")
            elif key in (ord("o"), ord("O")) and page == PAGE_SYSTEM:
                check_local_ai_models()
            elif key in (ord("m"), ord("M")) and page == PAGE_SYSTEM:
                cycle_local_ai_model()
            elif key in (ord("a"), ord("A")) and page == PAGE_SYSTEM:
                start_local_ai_explanation()
            elif key in (ord("p"), ord("P")) and page == PAGE_SYSTEM:
                confirm_power_profile()
            elif key in (ord("v"), ord("V")) and page == PAGE_SYSTEM:
                confirm_power_profile_restore()
            elif key in (ord("h"), ord("H")) and page == PAGE_SYSTEM:
                toggle_performance_history()
            elif key in (ord("d"), ord("D")) and page == PAGE_SYSTEM:
                show_performance_diagnosis()
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
        "stress_session": StressTestSession(),
        "disk_session": DiskBenchmarkSession(),
        "benchmark_session": SysbenchBenchmarkSession(sysbench_temperature_snapshot),
    }
    state["test_sessions"] = TestSessions(
        state["stress_session"], state["disk_session"], state["benchmark_session"],
    )

    class BooMWindow(Gtk.ApplicationWindow):
        titles = list(NAVIGATION_PAGES)
        descriptions = list(NAVIGATION_DESCRIPTIONS)

        def __init__(self) -> None:
            super().__init__(application=application, title="BooM")
            self.set_default_size(1440, 900)
            self.set_size_request(1024, 680)
            self.set_border_width(0)
            self.connect("delete-event", self.on_close_request)
            provider = Gtk.CssProvider()
            provider.load_from_data(GTK_CSS)
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
            self.data: dict[str, Any] = {}
            self.manager_data: dict[str, Any] = {
                "processes": [], "applications": [], "startup": [], "games": [],
                "steam": {"state": "unchecked", "message": "Steam ainda não foi consultado.", "games": []},
                "lutris": {"state": "unchecked", "message": "Lutris ainda não foi consultado.", "games": []},
                "heroic": {"state": "unchecked", "message": "Heroic ainda não foi consultado.", "games": []},
            }
            self.page = PAGE_OVERVIEW
            self.network_results: list[dict[str, Any]] = []
            self.smart_results: dict[str, dict[str, Any]] = {}
            self.smart_busy_devices: set[str] = set()
            self.live_metrics_busy = False
            self.stress_starting = False
            self.optional_installing = False
            self.application_uninstalling = False
            self.ai_models: list[dict[str, Any]] = []
            self.ai_model: str | None = None
            self.ai_busy = False
            self.ai_status = "IA local não consultada."
            self.ai_result: dict[str, Any] | None = None
            self.ai_model_combo: Any | None = None
            self.firmware_info: dict[str, Any] = {"state": "unchecked", "devices": [], "updates": []}
            self.firmware_checking = False
            self.power_profile_busy = False
            self.stress_poll_source: int | None = None
            self.stress_duration_seconds = 30
            self.stress_cpu_workers = min(2, max(1, os.cpu_count() or 1), STRESS_TEST_MAX_CPU_WORKERS)
            self.stress_gpu_key: str | None = None
            self.stress_widgets: dict[str, Any] = {}
            self.navigation_in_progress = False
            self.render_scheduled = False
            self.restoring_autostart = False
            self.refresh_state = Gtk.Label(label="Lendo dados do sistema…")
            self.refresh_state.get_style_context().add_class("muted")
            self.nav_buttons: list[Any] = []

            root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            root.get_style_context().add_class("boom-root")
            self.add(root)
            sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            sidebar.set_size_request(226, -1)
            sidebar.set_margin_start(24)
            sidebar.set_margin_top(22)
            sidebar.set_margin_bottom(22)
            sidebar.set_margin_end(22)
            sidebar.get_style_context().add_class("sidebar")
            root.pack_start(sidebar, False, False, 0)

            sidebar_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            sidebar_content.set_margin_start(12)
            sidebar_content.set_margin_end(12)
            sidebar.pack_start(sidebar_content, True, True, 0)

            brand_lockup = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=11)
            brand_mark = Gtk.Label(label="💣")
            brand_mark.get_style_context().add_class("brand-mark")
            brand_lockup.pack_start(brand_mark, False, False, 0)
            brand_copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            brand = Gtk.Label(label="BooM")
            brand.set_xalign(0)
            brand.get_style_context().add_class("brand")
            brand_copy.pack_start(brand, False, False, 0)
            tagline = Gtk.Label(label="MONITORAMENTO LINUX")
            tagline.set_xalign(0)
            tagline.get_style_context().add_class("eyebrow")
            brand_copy.pack_start(tagline, False, False, 0)
            brand_lockup.pack_start(brand_copy, False, False, 0)
            sidebar_content.pack_start(brand_lockup, False, False, 12)
            sidebar_rule = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sidebar_rule.get_style_context().add_class("sidebar-separator")
            sidebar_content.pack_start(sidebar_rule, False, False, 0)
            self.nav_buttons = []
            for group_title, page_indices in NAVIGATION_GROUPS:
                group_label = Gtk.Label(label=group_title)
                group_label.set_xalign(0)
                group_label.get_style_context().add_class("nav-section")
                sidebar_content.pack_start(group_label, False, False, 0)
                for index in page_indices:
                    button = Gtk.Button()
                    button.set_relief(Gtk.ReliefStyle.NONE)
                    button.set_halign(Gtk.Align.FILL)
                    button.set_tooltip_text(self.descriptions[index])
                    button.get_accessible().set_name(self.titles[index])
                    button.get_style_context().add_class("nav-button")
                    if index == PAGE_OVERVIEW:
                        button.get_style_context().add_class("selected")
                    button_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=11)
                    icon = Gtk.Image.new_from_icon_name(NAVIGATION_ICONS[index], Gtk.IconSize.MENU)
                    icon.set_pixel_size(18)
                    button_content.pack_start(icon, False, False, 0)
                    label = Gtk.Label(label=self.titles[index])
                    label.set_xalign(0)
                    button_content.pack_start(label, True, True, 0)
                    button.add(button_content)
                    button.connect("clicked", self.on_navigation, index)
                    sidebar_content.pack_start(button, False, False, 0)
                    self.nav_buttons.append(button)

            bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            bottom.set_valign(Gtk.Align.END)
            bottom.set_vexpand(True)
            sidebar_content.pack_start(bottom, True, True, 0)
            local = Gtk.Label(label="DADOS LOCAIS")
            local.set_xalign(0)
            local.get_style_context().add_class("local-status")
            bottom.pack_start(local, False, False, 0)
            host = Gtk.Label(label=socket.gethostname())
            host.set_xalign(0)
            host.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            host.get_style_context().add_class("muted")
            bottom.pack_start(host, False, False, 0)

            main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            main.set_hexpand(True)
            root.pack_start(main, True, True, 0)
            header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
            header.set_margin_start(36)
            header.set_margin_end(36)
            header.set_margin_top(28)
            header.set_margin_bottom(24)
            header.get_style_context().add_class("page-header")
            main.pack_start(header, False, False, 0)
            heading_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
            header.pack_start(heading_box, True, True, 0)
            self.page_overline = Gtk.Label(label=NAVIGATION_GROUP_BY_PAGE[PAGE_OVERVIEW])
            self.page_overline.set_xalign(0)
            self.page_overline.set_halign(Gtk.Align.START)
            self.page_overline.get_style_context().add_class("page-overline")
            heading_box.pack_start(self.page_overline, False, False, 0)
            self.title_label = Gtk.Label(label=self.titles[0])
            self.title_label.set_xalign(0)
            self.title_label.set_line_wrap(True)
            self.title_label.get_style_context().add_class("page-title")
            heading_box.pack_start(self.title_label, False, False, 0)
            self.description_label = Gtk.Label(label=self.descriptions[0])
            self.description_label.set_xalign(0)
            self.description_label.set_line_wrap(True)
            self.description_label.set_max_width_chars(86)
            self.description_label.get_style_context().add_class("page-subtitle")
            heading_box.pack_start(self.description_label, False, False, 0)
            header_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            header_actions.set_valign(Gtk.Align.CENTER)
            self.report_button = Gtk.Button(label="Exportar HTML")
            self.report_button.connect("clicked", self.export_report)
            header_actions.pack_start(self.report_button, False, False, 0)
            self.refresh_button = Gtk.Button(label="Atualizar")
            self.refresh_button.get_style_context().add_class("action")
            self.refresh_button.connect("clicked", self.refresh)
            header_actions.pack_start(self.refresh_button, False, False, 0)
            header.pack_end(header_actions, False, False, 0)

            separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            separator.get_style_context().add_class("page-divider")
            main.pack_start(separator, False, False, 0)
            self.scroller = Gtk.ScrolledWindow()
            self.scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            main.pack_start(self.scroller, True, True, 0)
            self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=28)
            self.content.set_border_width(40)
            self.scroller.add(self.content)
            footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            footer.set_border_width(14)
            footer.get_style_context().add_class("footer")
            self.refresh_state.set_ellipsize(Pango.EllipsizeMode.END)
            self.refresh_state.set_max_width_chars(76)
            footer.pack_start(self.refresh_state, True, True, 0)
            note = Gtk.Label(label="Dados locais · atualização a cada 5 s")
            note.set_ellipsize(Pango.EllipsizeMode.END)
            note.set_max_width_chars(36)
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
            self.test_sessions().stop_and_wait()
            application.quit()
            return False

        def stress_session(self) -> StressTestSession:
            return state["stress_session"]

        def disk_session(self) -> DiskBenchmarkSession:
            return state["disk_session"]

        def test_sessions(self) -> TestSessions:
            return state["test_sessions"]

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
                self.page_overline.set_text(NAVIGATION_GROUP_BY_PAGE[index])
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
                adjustment = self.scroller.get_vadjustment()
                adjustment.set_value(adjustment.get_lower())
            finally:
                self.render_scheduled = False
            return False

        def card(self, title: str) -> Any:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
            box.get_style_context().add_class("card")
            box.set_hexpand(True)
            box.set_valign(Gtk.Align.START)
            label = Gtk.Label(label=title)
            label.set_xalign(0)
            label.set_line_wrap(True)
            label.get_style_context().add_class("card-title")
            box.pack_start(label, False, False, 0)
            return box

        def metric(
            self,
            card: Any,
            value: str,
            detail: str = "",
            progress: float | None = None,
        ) -> None:
            label = Gtk.Label(label=value)
            label.set_xalign(0)
            label.set_line_wrap(True)
            label.set_max_width_chars(54)
            label.get_style_context().add_class("metric")
            card.pack_start(label, False, False, 0)
            if detail:
                hint = Gtk.Label(label=detail)
                hint.set_xalign(0)
                hint.set_line_wrap(True)
                hint.set_max_width_chars(88)
                hint.get_style_context().add_class("muted")
                card.pack_start(hint, False, False, 0)
            if progress is not None:
                bar = Gtk.ProgressBar()
                bar.set_fraction(max(0.0, min(1.0, progress / 100)))
                bar.set_show_text(False)
                bar.get_style_context().add_class("metric-progress")
                card.pack_start(bar, False, False, 2)

        def add_row(self, card: Any, name: str, value: str) -> Any:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row.get_style_context().add_class("detail-row")
            key = Gtk.Label(label=name)
            key.set_xalign(0)
            key.set_line_wrap(True)
            key.set_max_width_chars(34)
            key.get_style_context().add_class("muted")
            val = Gtk.Label(label=value)
            val.set_xalign(1)
            val.set_line_wrap(True)
            val.set_justify(Gtk.Justification.RIGHT)
            val.set_max_width_chars(72)
            val.set_selectable(True)
            val.get_style_context().add_class("body-copy")
            row.pack_start(key, True, True, 0)
            row.pack_end(val, True, True, 0)
            card.pack_start(row, False, False, 0)
            return val

        def add_section_heading(self, title: str, detail: str = "") -> Any:
            heading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            heading.get_style_context().add_class("section-heading")
            label = Gtk.Label(label=title)
            label.set_xalign(0)
            label.get_style_context().add_class("section-title")
            heading.pack_start(label, False, False, 0)
            if detail:
                description = Gtk.Label(label=detail)
                description.set_xalign(0)
                description.set_line_wrap(True)
                description.get_style_context().add_class("section-detail")
                heading.pack_start(description, False, False, 0)
            self.content.pack_start(heading, False, False, 0)
            return heading

        def add_card(self, card: Any) -> None:
            self.content.pack_start(card, False, False, 0)

        def performance_history_card(self) -> Any:
            summary = _PERFORMANCE_HISTORY.status()
            enabled = summary.get("enabled", False)
            sample_count = int(summary.get("sample_count") or 0)
            card = self.card("Histórico local e diagnóstico")
            self.metric(
                card,
                "Gravando enquanto o BooM estiver aberto" if enabled else "Histórico desativado",
                "A análise local compara períodos e mostra hipóteses com suas evidências; não altera o sistema.",
            )
            self.add_row(card, "Amostras guardadas", str(sample_count))
            self.add_row(card, "Retenção durante a gravação", "7 dias · intervalo de 5 s")
            self.add_row(card, "Pausado", "Amostras existentes ficam até retomar ou apagar")
            self.add_row(card, "Privacidade", "Só métricas agregadas; não grava processos, rede ou identificadores")

            actions = self.make_action_flow()
            toggle = Gtk.Button(label="Pausar histórico" if enabled else "Ativar histórico local")
            toggle.connect("clicked", self.toggle_performance_history)
            analyze = Gtk.Button(label="Analisar últimos 15 min")
            analyze.connect("clicked", self.show_performance_diagnosis)
            clear = Gtk.Button(label="Apagar histórico")
            clear.set_sensitive(sample_count > 0)
            clear.connect("clicked", self.clear_performance_history)
            for button in (toggle, analyze, clear):
                button.set_halign(Gtk.Align.START)
                actions.add(button)
            card.pack_start(actions, False, False, 4)

            ai_card = self.card("Explicação de gargalos com IA local")
            self.add_row(ai_card, "Estado", self.ai_status)
            self.add_row(ai_card, "Privacidade", "Ollama local · loopback")
            privacy_note = Gtk.Label(
                label="Somente achados agregados vão para 127.0.0.1; a resposta fica em memória e nada é alterado automaticamente."
            )
            privacy_note.set_xalign(0)
            privacy_note.set_line_wrap(True)
            privacy_note.get_style_context().add_class("muted")
            ai_card.pack_start(privacy_note, False, False, 0)
            ai_controls = self.make_action_flow()
            model_combo = Gtk.ComboBoxText()
            model_combo.set_size_request(250, -1)
            for item in self.ai_models:
                label = item["name"]
                if item.get("parameter_size"):
                    label += f" · {item['parameter_size']}"
                model_combo.append(item["name"], label)
            if self.ai_model and any(item["name"] == self.ai_model for item in self.ai_models):
                model_combo.set_active_id(self.ai_model)
            model_combo.set_sensitive(bool(self.ai_models) and not self.ai_busy)
            model_combo.connect("changed", self.select_local_ai_model)
            self.ai_model_combo = model_combo
            ai_controls.add(model_combo)
            check_ai = Gtk.Button(label="Verificar Ollama local")
            check_ai.set_sensitive(not self.ai_busy)
            check_ai.connect("clicked", self.request_local_ai_check)
            check_ai.set_halign(Gtk.Align.START)
            ai_controls.add(check_ai)
            explain_ai = Gtk.Button(label="Explicar com IA local")
            explain_ai.get_style_context().add_class("action")
            explain_ai.set_sensitive(
                not self.ai_busy
                and self.ai_model is not None
                and sample_count >= MIN_HISTORY_SAMPLES
            )
            explain_ai.connect("clicked", self.request_local_ai_explanation)
            explain_ai.set_halign(Gtk.Align.START)
            ai_controls.add(explain_ai)
            ai_card.pack_start(ai_controls, False, False, 4)
            if sample_count < MIN_HISTORY_SAMPLES:
                history_hint = Gtk.Label(
                    label=f"Para analisar: ative o histórico local e aguarde {MIN_HISTORY_SAMPLES} amostras em pelo menos 55 segundos. Amostras atuais: {sample_count}."
                )
                history_hint.set_xalign(0)
                history_hint.set_line_wrap(True)
                history_hint.get_style_context().add_class("muted")
                ai_card.pack_start(history_hint, False, False, 0)
            if not self.ai_models:
                install_note = Gtk.Label(
                    label="Ollama é opcional e precisa estar instalado com um modelo local. O BooM não baixa nem executa instaladores automaticamente."
                )
                install_note.set_xalign(0)
                install_note.set_line_wrap(True)
                install_note.get_style_context().add_class("muted")
                ai_card.pack_start(install_note, False, False, 0)
                install_link = Gtk.LinkButton.new_with_label(OLLAMA_DOWNLOAD_URL, "Abrir instalação oficial do Ollama")
                install_link.set_halign(Gtk.Align.START)
                ai_card.pack_start(install_link, False, False, 0)
            if self.ai_result and self.ai_result.get("state") == "ready":
                answer = self.ai_result.get("explanation", {})
                self.add_row(ai_card, "Última explicação", str(answer.get("summary", "Pronta")))
                view_answer = Gtk.Button(label="Ver explicação, evidências e riscos")
                view_answer.connect("clicked", self.show_local_ai_explanation)
                ai_card.pack_start(view_answer, False, False, 4)
            elif self.ai_result and self.ai_result.get("state") == "error":
                self.add_row(ai_card, "Falha", str(self.ai_result.get("message", "Falha sem detalhes")))
            group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
            group.pack_start(card, False, False, 0)
            group.pack_start(ai_card, False, False, 0)
            return group

        def make_grid(self) -> Any:
            flow = Gtk.FlowBox()
            flow.get_style_context().add_class("content-grid")
            flow.set_selection_mode(Gtk.SelectionMode.NONE)
            flow.set_min_children_per_line(1)
            flow.set_max_children_per_line(2)
            flow.set_column_spacing(22)
            flow.set_row_spacing(22)
            flow.set_homogeneous(True)
            flow.set_valign(Gtk.Align.START)
            return flow

        def make_action_flow(self) -> Any:
            flow = Gtk.FlowBox()
            flow.get_style_context().add_class("action-flow")
            flow.set_selection_mode(Gtk.SelectionMode.NONE)
            flow.set_min_children_per_line(1)
            flow.set_max_children_per_line(3)
            flow.set_column_spacing(10)
            flow.set_row_spacing(10)
            flow.set_homogeneous(False)
            flow.set_valign(Gtk.Align.START)
            return flow

        def add_grid_cards(self, cards: list[Any]) -> Any:
            grid = self.make_grid()
            for card in cards:
                card.set_hexpand(True)
                card.set_valign(Gtk.Align.START)
                card.set_size_request(400, -1)
                grid.add(card)
            self.content.pack_start(grid, False, False, 0)
            return grid

        def render_page(self) -> None:
            for child in self.content.get_children():
                self.content.remove(child)
            if not self.data:
                loading = self.card("Preparando o BooM")
                self.metric(loading, "Lendo informações do computador", "A janela continua responsiva durante a coleta local.")
                spinner = Gtk.Spinner()
                spinner.start()
                spinner.set_halign(Gtk.Align.START)
                loading.pack_start(spinner, False, False, 0)
                self.add_card(loading)
                self.content.show_all()
                return
            cpu = self.data.get("cpu", {})
            memory = self.data.get("memory") or {}
            os_data = self.data.get("os", {})
            if self.page == PAGE_OVERVIEW:
                self.add_section_heading(
                    "Uso em tempo real",
                    "Atividade atual dos componentes, conforme os sensores e drivers disponíveis.",
                )
                cpu_card = self.card("Processador")
                cpu_card.get_style_context().add_class("performance-cpu")
                usage = cpu.get("usage_percent")
                cpu_frequency = cpu.get("frequency", {})
                cpu_detail = f"{cpu.get('model', UNAVAILABLE)} · clock médio {format_frequency(cpu_frequency.get('average_mhz'))}"
                self.metric(
                    cpu_card,
                    f"{usage:.1f}%" if isinstance(usage, (int, float)) else UNAVAILABLE,
                    cpu_detail,
                    progress=usage if isinstance(usage, (int, float)) else None,
                )
                self.add_row(cpu_card, "Núcleos lógicos", str(cpu.get("cores") or UNAVAILABLE))

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
                gpu_card.get_style_context().add_class("performance-gpu")
                gpu_usage_label = gpu_utilization_label(gpu)
                gpu_usage_text = (
                    f"{gpu_usage:.1f}% de uso"
                    if gpu_usage_label == "Uso do núcleo gráfico"
                    else f"{gpu_usage:.1f}% · {gpu_usage_label.casefold()}"
                ) if isinstance(gpu_usage, (int, float)) else "Telemetria indisponível"
                self.metric(
                    gpu_card,
                    gpu_usage_text,
                    gpu_detail,
                    progress=gpu_usage if isinstance(gpu_usage, (int, float)) else None,
                )
                for label, value in (
                    ("VRAM usada / total", format_vram_usage(gpu)),
                    (
                        "Controlador de memória",
                        f"{gpu['memory_utilization_percent']:.0f}%"
                        if isinstance(gpu.get("memory_utilization_percent"), (int, float)) else UNAVAILABLE,
                    ),
                    ("Clock da memória", format_frequency(gpu.get("memory_clock_mhz")) if gpu else UNAVAILABLE),
                ):
                    self.add_row(gpu_card, label, value)
                self.add_grid_cards([cpu_card, gpu_card])

                self.add_section_heading("Capacidade", "Memória do sistema e espaço do volume principal.")
                mem_card = self.card("Memória")
                used = memory.get("used")
                total = memory.get("total")
                percent = used * 100 / total if isinstance(used, (int, float)) and isinstance(total, (int, float)) and total else None
                self.metric(
                    mem_card,
                    f"{format_bytes(used)} usados",
                    f"de {format_bytes(total)} · {percent:.1f}% em uso" if percent is not None else "Capacidade indisponível",
                    progress=percent,
                )
                self.add_row(mem_card, "Disponível", format_bytes(memory.get("available")))
                disk = next((item for item in self.data.get("disks", []) if item.get("mount") == "/"), None)
                if not disk:
                    disk = next(iter(self.data.get("disks", [])), None)
                disk_card = self.card("Armazenamento")
                disk_percent = disk.get("percent") if disk else None
                disk_available = disk.get("available") if disk else None
                disk_mount = disk.get("mount") if disk else None
                self.metric(
                    disk_card,
                    f"{disk_percent}% usado" if isinstance(disk_percent, (int, float)) else UNAVAILABLE,
                    f"{disk_mount} · {format_bytes(disk_available)} livre" if disk else "Nenhum volume disponível",
                    progress=disk_percent if isinstance(disk_percent, (int, float)) else None,
                )
                if disk:
                    self.add_row(disk_card, "Dispositivo", disk.get("device", UNAVAILABLE))
                self.add_grid_cards([mem_card, disk_card])

                self.add_section_heading("Sistema e temperaturas")
                os_card = self.card("Sistema operacional")
                self.metric(os_card, os_data.get("name", UNAVAILABLE), f"Kernel {os_data.get('kernel', UNAVAILABLE)}")
                self.add_row(os_card, "Tempo ligado", format_uptime(self.data.get("uptime_seconds")))
                self.add_row(os_card, "Computador", os_data.get("hostname", UNAVAILABLE))
                power = self.data.get("power", {})
                battery = next(iter(power.get("batteries", [])), {})
                power_card = self.card("Energia")
                if battery:
                    capacity = battery.get("capacity_percent")
                    self.metric(power_card, f"Bateria {capacity:.0f}%" if isinstance(capacity, (int, float)) else "Bateria detectada", battery.get("status", UNAVAILABLE))
                    self.add_row(power_card, "Potência", f"{battery['power_watts']:.1f} W" if isinstance(battery.get("power_watts"), (int, float)) else UNAVAILABLE)
                else:
                    self.metric(power_card, power.get("status", "Nenhuma bateria detectada."), "Leitura local de /sys/class/power_supply")
                if power.get("ac_online") is not None:
                    self.add_row(power_card, "Alimentação externa", "Conectada" if power["ac_online"] else "Desconectada")
                thermal = self.card("Sensores térmicos")
                temperatures = self.data.get("temperatures", [])
                valid_temperatures = [
                    item for item in temperatures
                    if isinstance(item.get("celsius"), (int, float)) and math.isfinite(float(item["celsius"]))
                ]
                cpu_temperatures = [
                    float(item["celsius"]) for item in cpu_temperature_readings(temperatures)
                    if isinstance(item.get("celsius"), (int, float)) and math.isfinite(float(item["celsius"]))
                ]
                gpu_temperatures = [
                    float(item["temperature_celsius"]) for item in self.data.get("gpu_metrics", [])
                    if isinstance(item.get("temperature_celsius"), (int, float))
                    and math.isfinite(float(item["temperature_celsius"]))
                ]
                self.add_row(
                    thermal,
                    "CPU",
                    f"{max(cpu_temperatures):.1f} °C" if cpu_temperatures else UNAVAILABLE,
                )
                self.add_row(thermal, "GPU", f"{max(gpu_temperatures):.1f} °C" if gpu_temperatures else UNAVAILABLE)
                if valid_temperatures:
                    hottest = max(valid_temperatures, key=lambda item: float(item["celsius"]))
                    self.add_row(thermal, "Maior leitura", f"{hottest['name']} · {hottest['celsius']:.1f} °C")
                    self.add_row(thermal, "Sensores", str(len(valid_temperatures)))
                else:
                    self.add_row(thermal, "Leitura", "Sem sensores compatíveis disponíveis")
                self.add_grid_cards([os_card, power_card, thermal])
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
                    (
                        "Preferência energética da CPU",
                        format_cpu_energy_preference(cpu.get("energy_preference", {})),
                    ),
                    ("Tempo ligado", format_uptime(self.data.get("uptime_seconds"))),
                    ("Computador", os_data.get("hostname", UNAVAILABLE)),
                ):
                    self.add_row(system, label, value)
                cpu_clocks = self.card("Clock atual por núcleo")
                frequency_cores = cpu.get("frequency", {}).get("cores", [])
                if frequency_cores:
                    for core in frequency_cores:
                        self.add_row(cpu_clocks, f"CPU {core['core']}", format_frequency(core["mhz"]))
                    self.add_row(cpu_clocks, "Fonte", cpu.get("frequency", {}).get("source") or UNAVAILABLE)
                else:
                    self.add_row(cpu_clocks, "Leitura", "Frequência atual por núcleo indisponível neste kernel/driver")
                self.add_grid_cards([system, cpu_clocks])
                thermal = self.card("Temperaturas")
                for item in self.data.get("temperatures", []) or [{"name": "Sensores", "celsius": None}]:
                    value = f"{item['celsius']:.1f} °C" if item["celsius"] is not None else "Sem leitura disponível"
                    if item.get("source") and item["celsius"] is not None:
                        value += f" · {item['source']}"
                    self.add_row(thermal, item["name"], value)
                other_sensors = self.card("Outras leituras de hardware")
                labels = {"fan": "Ventoinha", "voltage": "Tensão", "current": "Corrente", "power": "Potência"}
                additional_readings = [item for item in self.data.get("sensor_readings", []) if item.get("kind") != "temperature"]
                for item in additional_readings:
                    label = labels.get(item.get("kind"), item.get("kind", "Sensor").capitalize())
                    self.add_row(other_sensors, f"{label} · {item['name']}", f"{item['value']:.1f} {item['unit']}")
                if not additional_readings:
                    self.add_row(other_sensors, "Leitura", "Este hardware não expôs ventoinhas, tensão, corrente ou potência pelo backend disponível.")
                self.add_row(other_sensors, "Fonte", self.data.get("sensors_status", "lm-sensors / hwmon / sysfs"))
                self.add_grid_cards([thermal, other_sensors])
                firmware = self.card("Firmware e dispositivos compatíveis")
                firmware_info = self.firmware_info
                if firmware_info.get("state") == "unchecked":
                    self.add_row(firmware, "Estado", "Ainda não consultado; usa fwupd e os metadados locais.")
                else:
                    self.add_row(firmware, "Estado", firmware_info.get("message", UNAVAILABLE))
                    for item in firmware_info.get("updates", [])[:6]:
                        current = item.get("current_version") or UNAVAILABLE
                        target = item.get("update_version") or "versão disponível"
                        reboot = " · exige reinicialização" if item.get("needs_reboot") else ""
                        self.add_row(firmware, item["name"], f"{current} → {target}{reboot}")
                    if firmware_info.get("state") == "ready" and not firmware_info.get("updates"):
                        updatable = sum(1 for item in firmware_info.get("devices", []) if item.get("updatable"))
                        self.add_row(firmware, "Dispositivos atualizáveis", str(updatable))
                self.add_row(firmware, "Ação do BooM", "Consulta local; não baixa catálogo nem aplica firmware.")
                check_firmware = Gtk.Button(label="Consultar dispositivos e atualizações")
                check_firmware.set_sensitive(not self.firmware_checking and shutil.which("fwupdmgr") is not None)
                check_firmware.set_tooltip_text("Consulta a lista de dispositivos e os metadados que o fwupd já possui.")
                check_firmware.connect("clicked", self.request_firmware_check)
                firmware.pack_start(check_firmware, False, False, 4)
                optional_tools = self.card("Ferramentas opcionais")
                missing_tools = missing_optional_packages()
                self.add_row(
                    optional_tools,
                    "Pacotes ausentes",
                    ", ".join(missing_tools) if missing_tools else "Nenhum",
                )
                self.add_row(
                    optional_tools,
                    "Instalação",
                    "Só após confirmação; o sistema solicitará autenticação.",
                )
                if missing_tools:
                    install_tools = Gtk.Button(label="Instalar ferramentas ausentes")
                    install_tools.set_sensitive(not self.optional_installing)
                    install_tools.connect("clicked", self.request_optional_install)
                    optional_tools.pack_start(install_tools, False, False, 4)
                self.add_grid_cards([firmware, optional_tools])
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
                profile_card = self.card("Perfis de energia do sistema")
                profiles = self.data.get("power_profiles", {})
                self.add_row(profile_card, "Estado", profiles.get("message", "Indisponível"))
                if profiles.get("available"):
                    current_profile = profiles.get("current")
                    self.add_row(
                        profile_card,
                        "Perfil ativo",
                        POWER_PROFILE_LABELS.get(current_profile, current_profile or UNAVAILABLE),
                    )
                    choices = Gtk.ComboBoxText()
                    for profile in profiles.get("profiles", []):
                        choices.append(profile, POWER_PROFILE_LABELS.get(profile, profile))
                    if current_profile in profiles.get("profiles", []):
                        choices.set_active_id(current_profile)
                    choices.set_sensitive(not self.power_profile_busy)
                    profile_controls = self.make_action_flow()
                    choices.set_size_request(230, -1)
                    profile_controls.add(choices)
                    apply_profile = Gtk.Button(label="Aplicar perfil")
                    apply_profile.get_style_context().add_class("action")
                    apply_profile.set_sensitive(not self.power_profile_busy)
                    apply_profile.connect("clicked", self.request_power_profile_change, choices)
                    apply_profile.set_halign(Gtk.Align.START)
                    profile_controls.add(apply_profile)
                    profile_card.pack_start(profile_controls, False, False, 4)
                    restore_state = profiles.get("restore_state")
                    if restore_state:
                        restore_label = POWER_PROFILE_LABELS[restore_state["restore_profile"]]
                        self.add_row(profile_card, "Perfil anterior guardado", restore_label)
                        if profiles.get("external_change_detected"):
                            self.add_row(profile_card, "Aviso", "Outro componente alterou o perfil depois do BooM.")
                        restore_button = Gtk.Button(label=f"Restaurar {restore_label}")
                        restore_button.set_sensitive(not self.power_profile_busy)
                        restore_button.connect("clicked", self.request_power_profile_restore)
                        profile_card.pack_start(restore_button, False, False, 4)
                    explanation = Gtk.Label(
                        label="O backend da distribuição decide quais componentes cada perfil afeta. Isso não fixa clocks nem garante mais FPS."
                    )
                    explanation.set_xalign(0)
                    explanation.set_line_wrap(True)
                    explanation.get_style_context().add_class("muted")
                    profile_card.pack_start(explanation, False, False, 4)
                elif profiles.get("restore_state"):
                    restore_state = profiles["restore_state"]
                    self.add_row(
                        profile_card,
                        "Perfil anterior salvo",
                        POWER_PROFILE_LABELS[restore_state["restore_profile"]] + " · aguarda o serviço ficar disponível",
                    )
                self.add_grid_cards([energy, profile_card])
                self.add_card(self.performance_history_card())
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
                self.add_grid_cards([ram, swap])

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
                    vram_cards = []
                    for gpu in gpu_metrics:
                        vram_card = self.card(f"VRAM · {gpu.get('name') or 'GPU'}")
                        vram_percent = vram_usage_percent(gpu)
                        vram_total = gpu.get("vram_total_mib")
                        if isinstance(vram_total, (int, float)) and vram_total > 0:
                            self.metric(vram_card, format_vram_usage(gpu), "Memória dedicada atualmente alocada pelo driver.")
                            vram_progress = Gtk.ProgressBar()
                            vram_progress.set_fraction(max(0.0, min(1.0, (vram_percent or 0) / 100)))
                            vram_card.pack_start(vram_progress, False, False, 2)
                        else:
                            self.add_row(vram_card, "VRAM dedicada", "Não exposta por este adaptador/driver.")
                        activity = gpu.get("memory_utilization_percent")
                        self.add_row(vram_card, "Atividade do controlador", f"{activity:.0f}%" if isinstance(activity, (int, float)) else UNAVAILABLE)
                        self.add_row(vram_card, "Clock da memória gráfica", format_frequency(gpu.get("memory_clock_mhz")))
                        shared_used = gpu.get("shared_memory_used_mib")
                        shared_total = gpu.get("shared_memory_total_mib")
                        if isinstance(shared_used, (int, float)) or isinstance(shared_total, (int, float)):
                            shared_value = (
                                f"{shared_used:.0f} / {shared_total:.0f} MiB"
                                if isinstance(shared_used, (int, float)) and isinstance(shared_total, (int, float))
                                else UNAVAILABLE
                            )
                            self.add_row(vram_card, "GTT compartilhada reportada pelo driver", shared_value)
                        vram_cards.append(vram_card)
                    self.add_grid_cards(vram_cards)
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
                    graphics_cards = []
                    for gpu in gpu_metrics:
                        graphics = self.card(gpu.get("name") or "GPU")
                        self.add_row(graphics, "Driver", gpu.get("driver_version") or gpu.get("driver_name") or UNAVAILABLE)
                        for label, key, suffix in (
                            ("Temperatura", "temperature_celsius", " °C"),
                            ("Codificação de vídeo", "encoder_utilization_percent", "%"),
                            ("Decodificação de vídeo", "decoder_utilization_percent", "%"),
                            ("Consumo de potência", "power_draw_watts", " W"),
                            ("Limite de potência", "power_limit_watts", " W"),
                        ):
                            if key in {"encoder_utilization_percent", "decoder_utilization_percent"} and key not in gpu:
                                continue
                            value = gpu.get(key)
                            self.add_row(graphics, label, f"{value:.1f}{suffix}" if isinstance(value, (int, float)) else UNAVAILABLE)
                        utilization = gpu.get("gpu_utilization_percent")
                        self.add_row(
                            graphics,
                            gpu_utilization_label(gpu),
                            f"{utilization:.1f}%" if isinstance(utilization, (int, float)) else UNAVAILABLE,
                        )
                        self.add_row(graphics, "Ventoinha", format_gpu_fan_reading(gpu))
                        if gpu.get("power_scope"):
                            self.add_row(graphics, "Escopo da potência", gpu["power_scope"])
                        if isinstance(gpu.get("gpu_voltage_volts"), (int, float)):
                            self.add_row(graphics, "Tensão reportada", f"{gpu['gpu_voltage_volts']:.3f} V")
                        self.add_row(graphics, "Clock gráfico", format_frequency(gpu.get("graphics_clock_mhz")))
                        self.add_row(graphics, "Estado de desempenho", gpu.get("performance_state") or UNAVAILABLE)
                        self.add_row(graphics, "Link PCIe atual", format_gpu_pcie_link(gpu))
                        engine_utilization = gpu.get("engine_utilization")
                        if isinstance(engine_utilization, dict):
                            for engine, busy in engine_utilization.items():
                                self.add_row(graphics, f"Motor · {engine}", f"{busy:.1f}%")
                        graphics_cards.append(graphics)
                    self.add_grid_cards(graphics_cards)
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
                volume_cards = []
                for disk in disks:
                    item = self.card(disk["mount"])
                    self.metric(item, f"{disk['percent']}% usado", f"{disk['device']} · {disk['filesystem']}")
                    progress = Gtk.ProgressBar()
                    progress.set_fraction(max(0, min(1, disk["percent"] / 100)))
                    item.pack_start(progress, False, False, 2)
                    self.add_row(item, "Usado", format_bytes(disk["used"]))
                    self.add_row(item, "Disponível", format_bytes(disk["available"]))
                    self.add_row(item, "Capacidade", format_bytes(disk["size"]))
                    volume_cards.append(item)
                if volume_cards:
                    self.add_grid_cards(volume_cards)
                disk_activity = self.card("Atividade de armazenamento")
                disk_io = self.data.get("disk_io") or {}
                busy = disk_io.get("busy_percent")
                if isinstance(busy, (int, float)):
                    self.add_row(disk_activity, "Dispositivo inteiro mais ocupado", f"{busy:.1f}%")
                    self.add_row(disk_activity, "Leitura", f"{format_bytes(disk_io.get('read_bytes_per_second'))}/s")
                    self.add_row(disk_activity, "Gravação", f"{format_bytes(disk_io.get('write_bytes_per_second'))}/s")
                    wait = disk_io.get("average_io_wait_ms")
                    self.add_row(disk_activity, "Latência média aprox. de I/O", f"{wait:.1f} ms" if isinstance(wait, (int, float)) else UNAVAILABLE)
                else:
                    self.add_row(disk_activity, "Leitura", "Aguardando uma segunda amostra do kernel")
                self.add_row(disk_activity, "Fonte e limite", disk_io.get("source") or "/proc/diskstats · valor de atividade aproximado")
                self.add_card(disk_activity)
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
                interfaces = self.card("Interfaces de rede")
                for item in network.get("interfaces", []) or [{"name": "Interfaces", "state": UNAVAILABLE, "addresses": []}]:
                    self.add_row(interfaces, item["name"], f"{item['state']} · {', '.join(item['addresses']) or 'sem IPv4'}")
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
                self.add_grid_cards([route, diagnostic])
                self.add_card(interfaces)
            elif self.page == PAGE_TESTS:
                self.render_stress_page()
            elif self.page == PAGE_PROCESSES:
                self.render_process_page()
            elif self.page == PAGE_APPLICATIONS:
                self.render_application_page()
            elif self.page == PAGE_STARTUP:
                self.render_startup_page()
            self.content.show_all()

        def toggle_performance_history(self, *_args: Any) -> None:
            enabled = _PERFORMANCE_HISTORY.enabled()
            if not enabled:
                dialog = Gtk.MessageDialog(
                    transient_for=self,
                    flags=Gtk.DialogFlags.MODAL,
                    message_type=Gtk.MessageType.QUESTION,
                    buttons=Gtk.ButtonsType.CANCEL,
                    text="Ativar histórico local de desempenho?",
                )
                dialog.format_secondary_text(
                    "Enquanto o BooM estiver monitorando, serão guardados percentuais agregados de CPU, RAM, GPU, temperaturas e atividade de disco em um arquivo privado local. A retenção de sete dias é aplicada durante a gravação; ao pausar, os dados existentes ficam guardados até você retomar ou apagar. Não são salvos processos, endereços ou identificadores."
                )
                dialog.add_button("Ativar histórico", Gtk.ResponseType.ACCEPT)
                response = dialog.run()
                dialog.destroy()
                if response != Gtk.ResponseType.ACCEPT:
                    self.refresh_state.set_text("Histórico continua desativado; nenhuma amostra foi salva.")
                    return
            try:
                _PERFORMANCE_HISTORY.set_enabled(not enabled)
            except OSError as exc:
                self.refresh_state.set_text(f"Não foi possível alterar o histórico local: {exc}")
                return
            self.refresh_state.set_text(
                "Histórico local ativado; a próxima amostra começa em até cinco segundos."
                if not enabled else "Gravação do histórico local pausada; os dados guardados foram mantidos."
            )
            self.render_page()

        def show_performance_diagnosis(self, *_args: Any) -> None:
            try:
                diagnosis = analyze_performance_history(_PERFORMANCE_HISTORY.samples(window_minutes=15))
            except (OSError, sqlite3.Error, ValueError) as exc:
                self.refresh_state.set_text(f"Não foi possível analisar o histórico local: {exc}")
                return
            lines = [
                diagnosis["summary"],
                f"Janela: {diagnosis['window_seconds']:.0f} s · amostras: {diagnosis['sample_count']} · confiança: {diagnosis['confidence']}",
            ]
            for finding in diagnosis["findings"]:
                evidence = " · ".join(
                    f"{key.replace('_', ' ')}: {value}"
                    for key, value in finding.get("evidence", {}).items()
                )
                lines.extend([
                    "",
                    finding["title"],
                    finding["message"],
                    f"Evidências: {evidence}" if evidence else "",
                    f"Próximo passo: {finding['suggestion']}",
                ])
            dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=Gtk.DialogFlags.MODAL,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.CLOSE,
                text="Análise local dos últimos 15 minutos",
            )
            dialog.format_secondary_text("\n".join(line for line in lines if line))
            dialog.run()
            dialog.destroy()

        def select_local_ai_model(self, combo: Any) -> None:
            selected = combo.get_active_id()
            if not selected or selected == self.ai_model:
                return
            self.ai_model = selected
            self.ai_result = None
            self.ai_status = f"Modelo local selecionado: {selected}."
            self.refresh_state.set_text(self.ai_status)
            if not self.render_scheduled:
                self.render_scheduled = True
                GLib.idle_add(self.render_scheduled_page)

        def request_local_ai_check(self, *_args: Any) -> None:
            if self.ai_busy:
                return
            self.ai_busy = True
            self.ai_status = "Consultando o Ollama em 127.0.0.1…"
            self.ai_result = None
            self.render_page()

            def worker() -> None:
                result = local_ollama_models()
                GLib.idle_add(self.finish_local_ai_check, result)

            threading.Thread(target=worker, name="boom-local-ai-models", daemon=True).start()

        def finish_local_ai_check(self, result: dict[str, Any]) -> bool:
            self.ai_models = result.get("models", [])
            names = [item["name"] for item in self.ai_models]
            if self.ai_model not in names:
                self.ai_model = names[0] if names else None
            self.ai_status = str(result.get("message", "Consulta local concluída."))
            self.ai_busy = False
            self.refresh_state.set_text(self.ai_status)
            self.render_page()
            return False

        def request_local_ai_explanation(self, *_args: Any) -> None:
            if self.ai_busy:
                return
            history_status = _PERFORMANCE_HISTORY.status()
            sample_count = int(history_status.get("sample_count") or 0)
            if sample_count < MIN_HISTORY_SAMPLES:
                self.ai_status = f"O diagnóstico exige ao menos {MIN_HISTORY_SAMPLES} amostras em 55 segundos."
                self.refresh_state.set_text(self.ai_status)
                self.render_page()
                return
            if not self.ai_model:
                self.ai_status = "Verifique o Ollama local e escolha um modelo instalado."
                self.refresh_state.set_text(self.ai_status)
                self.render_page()
                return
            model = self.ai_model
            self.ai_busy = True
            self.ai_result = None
            self.ai_status = f"Analisando dados locais com {model}; pode levar alguns minutos…"
            self.refresh_state.set_text(self.ai_status)
            self.render_page()

            def worker() -> None:
                try:
                    samples = _PERFORMANCE_HISTORY.samples(window_minutes=15)
                    diagnosis = analyze_performance_history(samples)
                    if diagnosis.get("status") != "analyzed":
                        raise LocalAIError(str(diagnosis.get("summary", "Histórico insuficiente para análise.")))
                    explanation = explain_performance_diagnosis(model, diagnosis)
                    result = {"state": "ready", "diagnosis": diagnosis, "explanation": explanation}
                    status = f"Explicação local pronta com {model}; nada foi alterado ou salvo."
                except (LocalAIError, OSError, sqlite3.Error, ValueError) as exc:
                    result = {"state": "error", "message": str(exc)}
                    status = str(exc)
                GLib.idle_add(self.finish_local_ai_explanation, result, status)

            threading.Thread(target=worker, name="boom-local-ai-analysis", daemon=True).start()

        def finish_local_ai_explanation(self, result: dict[str, Any], status: str) -> bool:
            self.ai_result = result
            self.ai_status = status
            self.ai_busy = False
            self.refresh_state.set_text(status)
            self.render_page()
            return False

        def show_local_ai_explanation(self, *_args: Any) -> None:
            if not self.ai_result or self.ai_result.get("state") != "ready":
                self.refresh_state.set_text("Ainda não há uma explicação local pronta para exibir.")
                return
            dialog = Gtk.Dialog(
                title="Explicação da IA local",
                transient_for=self,
                modal=True,
                destroy_with_parent=True,
            )
            dialog.add_button("Fechar", Gtk.ResponseType.CLOSE)
            dialog.set_default_size(860, 620)
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            scroller.set_border_width(16)
            text_view = Gtk.TextView()
            text_view.set_editable(False)
            text_view.set_cursor_visible(False)
            text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            text_view.get_buffer().set_text(format_local_ai_result(self.ai_result))
            scroller.add(text_view)
            dialog.get_content_area().pack_start(scroller, True, True, 0)
            dialog.show_all()
            dialog.run()
            dialog.destroy()

        def clear_performance_history(self, *_args: Any) -> None:
            summary = _PERFORMANCE_HISTORY.status()
            dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=Gtk.DialogFlags.MODAL,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.CANCEL,
                text="Apagar o histórico local do BooM?",
            )
            dialog.format_secondary_text(
                f"Serão removidas {summary.get('sample_count', 0)} amostras. A gravação também será pausada. Esta ação não pode ser desfeita."
            )
            dialog.add_button("Apagar histórico", Gtk.ResponseType.ACCEPT)
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.ACCEPT:
                self.refresh_state.set_text("Histórico mantido.")
                return
            try:
                _PERFORMANCE_HISTORY.set_enabled(False)
                _PERFORMANCE_HISTORY.clear()
            except (OSError, sqlite3.Error) as exc:
                self.refresh_state.set_text(f"Não foi possível apagar o histórico: {exc}")
                return
            self.refresh_state.set_text("Histórico local apagado e gravação pausada.")
            self.render_page()

        def render_stress_page(self) -> None:
            self.stress_widgets = {}
            temperatures = self.data.get("temperatures", [])
            cpu_temperatures = cpu_temperature_readings(temperatures)
            readiness = stress_test_readiness(temperatures)
            gpu_metrics = self.data.get("gpu_metrics", [])
            gpu_keys = [stress_gpu_selection_key(index, gpu) for index, gpu in enumerate(gpu_metrics)]
            if self.stress_gpu_key not in gpu_keys:
                self.stress_gpu_key = gpu_keys[0] if gpu_keys else None
            selected_gpu = select_stress_gpu(gpu_metrics, self.stress_gpu_key)
            gpu_readiness = stress_test_readiness(temperatures, "gpu", selected_gpu)
            session = self.test_sessions().snapshot()
            busy = self.stress_starting or self.optional_installing or self.test_sessions().is_active()

            sensors = self.card("Temperatura e proteção térmica")
            self.add_row(sensors, "Fonte", self.data.get("sensors_status", "lm-sensors / hwmon / sysfs"))
            active_stress = busy and session.get("kind") in {
                "cpu", "memory", "gpu", "cpu-benchmark", "memory-benchmark",
            }
            cpu_temperature = session.get("cpu_celsius") if active_stress else readiness.get("cpu_celsius")
            hottest = session.get("current_celsius") if active_stress else gpu_readiness.get("current_celsius") if selected_gpu else readiness.get("current_celsius")
            gpu_temperature = session.get("gpu_celsius") if active_stress and session.get("kind") == "gpu" else (selected_gpu or {}).get("temperature_celsius")
            cpu_temp_value = self.add_row(sensors, "CPU", f"{cpu_temperature:.1f} °C" if isinstance(cpu_temperature, (int, float)) else "Sensor da CPU indisponível")
            gpu_temp_value = self.add_row(sensors, "GPU", f"{gpu_temperature:.1f} °C" if isinstance(gpu_temperature, (int, float)) else "Sensor da GPU indisponível")
            hottest_temp_value = self.add_row(sensors, "Sensor mais quente", f"{hottest:.1f} °C" if isinstance(hottest, (int, float)) else "Sem leitura")
            self.add_row(sensors, "Limites do teste", f"Início abaixo de {STRESS_TEST_PRECHECK_TEMP_C:.0f} °C · parada em {STRESS_TEST_ABORT_TEMP_C:.0f} °C")
            readiness_value = self.add_row(sensors, "Prontidão", readiness["message"])
            gpu_readiness_value = self.add_row(sensors, "Prontidão da GPU", gpu_readiness["message"])
            self.add_card(sensors)
            self.stress_widgets.update({
                "cpu_temperature": cpu_temp_value,
                "gpu_temperature": gpu_temp_value,
                "hottest_temperature": hottest_temp_value,
                "readiness": readiness_value,
                "gpu_readiness": gpu_readiness_value,
            })

            options = self.card("Testes de carga")
            gpu_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            gpu_label = Gtk.Label(label="Adaptador para teste")
            gpu_label.set_xalign(0)
            gpu_label.get_style_context().add_class("muted")
            gpu_selector = Gtk.ComboBoxText()
            if gpu_metrics:
                for index, gpu in enumerate(gpu_metrics):
                    key = stress_gpu_selection_key(index, gpu)
                    name = str(gpu.get("name") or f"GPU {index + 1}")
                    address = gpu.get("pci_address")
                    gpu_selector.append(key, f"{name} · {address or 'PCI indisponível'}")
                gpu_selector.set_active_id(self.stress_gpu_key)
            else:
                gpu_selector.append("none", "Nenhuma GPU detectada")
                gpu_selector.set_active_id("none")
            gpu_selector.set_sensitive(bool(gpu_metrics) and not busy)
            gpu_selector.connect("changed", self.on_stress_gpu_changed)
            gpu_row.pack_start(gpu_label, True, True, 0)
            gpu_row.pack_end(gpu_selector, False, False, 0)
            options.pack_start(gpu_row, False, False, 0)
            duration_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            duration_label = Gtk.Label(label="Duração máxima")
            duration_label.set_xalign(0)
            duration_label.get_style_context().add_class("muted")
            duration = Gtk.ComboBoxText()
            for seconds in STRESS_TEST_DURATIONS:
                duration.append(str(seconds), f"{seconds} segundos")
            duration.set_active_id(str(self.stress_duration_seconds))
            duration.set_sensitive(not busy)
            duration.connect("changed", self.on_stress_duration_changed)
            duration_row.pack_start(duration_label, True, True, 0)
            duration_row.pack_end(duration, False, False, 0)
            options.pack_start(duration_row, False, False, 0)

            workers_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            workers_label = Gtk.Label(label="Workers de CPU")
            workers_label.set_xalign(0)
            workers_label.get_style_context().add_class("muted")
            max_workers = min(STRESS_TEST_MAX_CPU_WORKERS, max(1, os.cpu_count() or 1))
            workers = Gtk.SpinButton.new_with_range(1, max_workers, 1)
            workers.set_value(min(self.stress_cpu_workers, max_workers))
            workers.set_sensitive(not busy)
            workers.connect("value-changed", self.on_stress_workers_changed)
            workers_row.pack_start(workers_label, True, True, 0)
            workers_row.pack_end(workers, False, False, 0)
            options.pack_start(workers_row, False, False, 0)
            self.add_row(options, "Carga de CPU", "80% por worker · máximo de 4 workers")
            available_memory = (self.data.get("memory") or {}).get("available")
            memory_command: list[str] | None = None
            memory_error = ""
            try:
                memory_command = stress_test_command("memory", self.stress_duration_seconds, 1, available_memory)
            except (RuntimeError, ValueError) as exc:
                memory_error = str(exc)
            memory_bytes = int(memory_command[memory_command.index("--vm-bytes") + 1]) if memory_command else None
            self.add_row(
                options,
                "Carga de RAM",
                f"1 worker · {format_bytes(memory_bytes)} · até 25% da RAM disponível, limitado a 1 GiB" if memory_bytes else memory_error,
            )
            actions = Gtk.Grid()
            actions.set_column_spacing(12)
            actions.set_row_spacing(12)
            actions.set_column_homogeneous(True)
            cpu_button = Gtk.Button(label="Iniciar teste de CPU")
            cpu_button.get_style_context().add_class("action")
            cpu_button.set_sensitive(readiness["ready"] and not busy)
            cpu_button.connect("clicked", self.request_stress_test, "cpu")
            actions.attach(cpu_button, 0, 0, 1, 1)
            memory_button = Gtk.Button(label="Iniciar teste de RAM")
            memory_button.get_style_context().add_class("action")
            memory_button.set_sensitive(readiness["ready"] and not busy and memory_command is not None)
            memory_button.set_tooltip_text(memory_error or "Usa até 25% da RAM disponível, limitado a 1 GiB.")
            memory_button.connect("clicked", self.request_stress_test, "memory")
            actions.attach(memory_button, 1, 0, 1, 1)
            gpu_button = Gtk.Button(label="Iniciar teste de GPU")
            gpu_button.get_style_context().add_class("action")
            gpu_button.set_sensitive(gpu_readiness["ready"] and not busy)
            gpu_button.set_tooltip_text(gpu_readiness["message"])
            gpu_button.connect("clicked", self.request_stress_test, "gpu")
            actions.attach(gpu_button, 0, 1, 1, 1)
            options.pack_start(actions, False, False, 4)
            self.add_card(options)
            self.stress_widgets.update({
                "status": None,
                "progress": None,
                "cpu_button": cpu_button,
                "memory_button": memory_button,
                "gpu_button": gpu_button,
                "gpu_selector": gpu_selector,
            })

            sysbench_executable = shutil.which("sysbench")
            benchmark_thermal = {
                "cpu_temperatures": cpu_temperature_readings(self.data.get("temperatures", [])),
                "temperatures": self.data.get("temperatures", []),
            }
            benchmark_cpu = sysbench_benchmark_readiness(
                "cpu-benchmark", available_memory, benchmark_thermal, executable=sysbench_executable,
            )
            benchmark_memory = sysbench_benchmark_readiness(
                "memory-benchmark", available_memory, benchmark_thermal, executable=sysbench_executable,
            )
            benchmarks = self.card("Benchmarks CPU, RAM e disco")
            self.add_row(
                benchmarks,
                "CPU",
                f"sysbench · {SYSBENCH_CPU_DURATION_SECONDS} s · até 4 threads · eventos por segundo",
            )
            self.add_row(
                benchmarks,
                "RAM",
                f"Leitura e escrita · {SYSBENCH_MEMORY_PASS_SECONDS} s cada · buffer privado até 256 MiB e 1/16 da RAM disponível · mínimo de {format_bytes(SYSBENCH_MEMORY_MIN_AVAILABLE_BYTES)} livres",
            )
            self.add_row(
                benchmarks,
                "Disco",
                f"fio · sequencial + aleatório 4 KiB QD1/QD32 · {DISK_BENCHMARK_SIZE_MIB} MiB temporários · arquivo removido ao terminar",
            )
            benchmark_actions = Gtk.Grid()
            benchmark_actions.set_column_spacing(12)
            benchmark_actions.set_row_spacing(12)
            benchmark_actions.set_column_homogeneous(True)
            cpu_benchmark_button = Gtk.Button(label="Benchmark CPU")
            cpu_benchmark_button.get_style_context().add_class("action")
            cpu_benchmark_button.set_sensitive(not busy and bool(sysbench_executable) and benchmark_cpu["ready"])
            cpu_benchmark_button.set_tooltip_text(
                benchmark_cpu["message"] if sysbench_executable else "sysbench está ausente; instale as ferramentas opcionais para medir CPU e RAM."
            )
            cpu_benchmark_button.connect("clicked", self.request_sysbench_benchmark, "cpu-benchmark")
            benchmark_actions.attach(cpu_benchmark_button, 0, 0, 1, 1)
            memory_benchmark_button = Gtk.Button(label="Benchmark RAM")
            memory_benchmark_button.get_style_context().add_class("action")
            memory_benchmark_button.set_sensitive(not busy and bool(sysbench_executable) and benchmark_memory["ready"])
            memory_benchmark_button.set_tooltip_text(
                benchmark_memory["message"] if sysbench_executable else "sysbench está ausente; instale as ferramentas opcionais para medir CPU e RAM."
            )
            memory_benchmark_button.connect("clicked", self.request_sysbench_benchmark, "memory-benchmark")
            benchmark_actions.attach(memory_benchmark_button, 1, 0, 1, 1)
            fio_installed = bool(shutil.which("fio"))
            disk_button = Gtk.Button(label="Benchmark disco completo" if fio_installed else "Instalar fio")
            disk_button.get_style_context().add_class("action")
            disk_button.set_sensitive(not busy)
            disk_button.set_tooltip_text(
                f"Escolha uma pasta; sequencial e aleatório 4 KiB QD1/QD32 em um arquivo temporário de {DISK_BENCHMARK_SIZE_MIB} MiB. Quatro perfis aleatórios duram {DISK_BENCHMARK_RANDOM_JOB_SECONDS} s cada; sem watchdog térmico do disco."
                if fio_installed else "fio está ausente; a instalação assistida revisará os pacotes ausentes e pedirá confirmação."
            )
            disk_button.connect("clicked", self.request_disk_benchmark)
            benchmark_actions.attach(disk_button, 0, 1, 1, 1)
            benchmarks.pack_start(benchmark_actions, False, False, 4)
            self.add_card(benchmarks)

            run_card = self.card("Resultado do teste")
            status_label = Gtk.Label(label=session.get("message", "Nenhum teste executado."))
            status_label.set_xalign(0)
            status_label.set_line_wrap(True)
            status_label.set_selectable(True)
            status_label.get_style_context().add_class("body-copy")
            status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            status_row.pack_start(status_label, True, True, 0)
            cancel_button = Gtk.Button(label="Cancelar")
            cancel_button.set_sensitive(session.get("status") in {"starting", "running", "stopping"})
            cancel_button.connect("clicked", self.cancel_stress_test)
            status_row.pack_end(cancel_button, False, False, 0)
            run_card.pack_start(status_row, False, False, 0)
            progress = Gtk.ProgressBar()
            progress.set_fraction(float(session.get("elapsed_seconds", 0)) / max(1, session.get("duration_seconds", 1)) if session.get("status") == "running" else 1.0 if session.get("status") == "completed" else 0.0)
            run_card.pack_start(progress, False, False, 0)
            if session.get("summary"):
                summary = Gtk.Label(label=session["summary"])
                summary.set_xalign(0)
                summary.set_line_wrap(True)
                summary.set_selectable(True)
                summary.get_style_context().add_class("muted")
                run_card.pack_start(summary, False, False, 0)
            self.stress_widgets.update({
                "status": status_label,
                "progress": progress,
                "cancel_button": cancel_button,
            })
            self.add_card(run_card)

            note = self.card("Escopo e segurança")
            for item in optional_tool_status():
                state_label = "instalado" if item["installed"] else f"ausente · pacote {item['package']}"
                self.add_row(note, item["command"], state_label)
            self.add_row(note, "Execução", "Sem privilégios; um teste por vez. Benchmarks sysbench de CPU/RAM duram até 10 s; o de disco usa arquivo temporário de 128 MiB e termina em até 120 s.")
            self.add_row(note, "Parada automática", f"Cargas e benchmarks CPU/RAM param a {SYSBENCH_ABORT_TEMP_C:.0f} °C ou se o sensor necessário falhar. Disco: limite de {DISK_BENCHMARK_TIMEOUT_SECONDS} s; sem watchdog térmico nesta fase.")
            self.add_row(note, "Cancelamento", "O botão encerra o teste; arquivos temporários só são removidos após a confirmação de que o processo parou.")
            if missing_optional_packages():
                install_button = Gtk.Button(label="Instalar ferramentas ausentes")
                install_button.set_sensitive(not self.optional_installing and not self.test_sessions().is_active())
                install_button.set_tooltip_text("Pop!_OS/Ubuntu/Debian · solicita autenticação do sistema e instala somente os pacotes ausentes.")
                install_button.connect("clicked", self.request_optional_install)
                note.pack_start(install_button, False, False, 4)
            self.add_card(note)

        def on_stress_duration_changed(self, combo: Any) -> None:
            value = combo.get_active_id()
            if value and value.isdigit():
                self.stress_duration_seconds = int(value)

        def on_stress_workers_changed(self, spin_button: Any) -> None:
            self.stress_cpu_workers = int(spin_button.get_value_as_int())

        def on_stress_gpu_changed(self, combo: Any) -> None:
            selected_key = combo.get_active_id()
            if selected_key and selected_key != "none":
                self.stress_gpu_key = selected_key
                gpu = select_stress_gpu(self.data.get("gpu_metrics", []), selected_key)
                if gpu:
                    self.refresh_state.set_text("GPU selecionada: " + str(gpu.get("name") or "adaptador"))
                if self.page == PAGE_TESTS:
                    self.render_page()

        def request_stress_test(self, _button: Any, kind: str) -> None:
            if self.stress_starting or self.optional_installing or self.test_sessions().is_active():
                return
            temperatures = self.data.get("temperatures", [])
            selected_gpu = select_stress_gpu(self.data.get("gpu_metrics", []), self.stress_gpu_key) if kind == "gpu" else None
            readiness = stress_test_readiness(temperatures, kind, selected_gpu)
            if not readiness["ready"]:
                self.refresh_state.set_text(readiness["message"])
                self.render_page()
                return
            available_memory = (self.data.get("memory") or {}).get("available")
            try:
                command = stress_test_command(
                    kind,
                    self.stress_duration_seconds,
                    self.stress_cpu_workers,
                    available_memory,
                    gpu_device=readiness.get("gpu_device"),
                )
            except (RuntimeError, ValueError) as exc:
                self.refresh_state.set_text(str(exc))
                self.render_page()
                return
            kind_label = {"cpu": "CPU", "memory": "RAM", "gpu": "GPU"}.get(kind, kind)
            if kind == "cpu":
                details = (
                    f"Duração máxima: {self.stress_duration_seconds} s. Workers de CPU: "
                    f"{self.stress_cpu_workers}. Carga limitada a 80% por worker."
                )
            elif kind == "memory":
                try:
                    memory_bytes = int(command[command.index("--vm-bytes") + 1])
                except (ValueError, IndexError):
                    self.refresh_state.set_text("Não foi possível calcular a memória segura para este teste.")
                    self.render_page()
                    return
                details = (
                    f"Duração máxima: {self.stress_duration_seconds} s. Um worker usará "
                    f"{format_bytes(memory_bytes)}; máximo de 25% da RAM disponível e 1 GiB."
                )
            elif kind == "gpu":
                if not selected_gpu or not readiness.get("gpu_device"):
                    self.refresh_state.set_text("Selecione uma GPU compatível antes de iniciar o teste.")
                    self.render_page()
                    return
                details = (
                    f"Duração máxima: {self.stress_duration_seconds} s. Adaptador: "
                    f"{selected_gpu.get('name') or 'GPU selecionada'}. Render node: "
                    f"{readiness['gpu_device']}. O BooM monitora as temperaturas da CPU e da GPU."
                )
            else:
                self.refresh_state.set_text("Tipo de teste desconhecido.")
                return
            dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=Gtk.DialogFlags.MODAL,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.CANCEL,
                text=f"Iniciar teste de {kind_label}?",
            )
            dialog.format_secondary_text(
                f"{details} O teste aplica carga real ao computador. O BooM interrompe a 85 °C, se perder um sensor térmico necessário ou ao atingir o tempo."
            )
            dialog.add_button("Iniciar teste", Gtk.ResponseType.ACCEPT)
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.ACCEPT:
                self.refresh_state.set_text("Teste de carga cancelado antes de iniciar.")
                return
            self.stress_starting = True
            self.refresh_state.set_text("Conferindo sensores antes de iniciar o teste…")
            self.render_page()
            duration_seconds = self.stress_duration_seconds
            cpu_workers = self.stress_cpu_workers
            gpu_target = dict(selected_gpu) if selected_gpu else None

            def worker() -> None:
                try:
                    message = self.test_sessions().start_stress(
                        kind,
                        duration_seconds,
                        cpu_workers,
                        available_memory,
                        gpu_target,
                    )
                    GLib.idle_add(self.finish_stress_start, message, None)
                except (RuntimeError, ValueError) as exc:
                    GLib.idle_add(self.finish_stress_start, "", str(exc))

            threading.Thread(target=worker, name="boom-stress-start", daemon=True).start()

        def request_sysbench_benchmark(self, _button: Any, kind: str) -> None:
            if self.stress_starting or self.optional_installing or self.test_sessions().is_active():
                return
            if kind not in {"cpu-benchmark", "memory-benchmark"}:
                self.refresh_state.set_text("Tipo de benchmark desconhecido.")
                return
            if not shutil.which("sysbench"):
                self.request_optional_install(_button)
                return
            available_memory = memory_info().get("available")
            temperatures = self.data.get("temperatures", [])
            readiness = sysbench_benchmark_readiness(
                kind,
                available_memory,
                {
                    "cpu_temperatures": cpu_temperature_readings(temperatures),
                    "temperatures": temperatures,
                },
                executable=shutil.which("sysbench"),
            )
            if not readiness["ready"]:
                self.refresh_state.set_text(readiness["message"])
                self.render_page()
                return
            if kind == "cpu-benchmark":
                details = (
                    f"sysbench calcula eventos por segundo durante {SYSBENCH_CPU_DURATION_SECONDS} s, "
                    f"com {readiness['threads']} threads. Isso aplica carga real à CPU."
                )
                benchmark_label = "CPU"
            else:
                buffer_bytes = int(readiness["buffer_bytes"])
                details = (
                    f"sysbench mede leitura e escrita sequenciais por {SYSBENCH_MEMORY_PASS_SECONDS} s cada, "
                    f"com buffer privado de {format_bytes(buffer_bytes)}. O buffer não passa de 1/16 da RAM "
                    "disponível nem de 256 MiB; nenhum arquivo ou dispositivo é alterado."
                )
                benchmark_label = "RAM"
            dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=Gtk.DialogFlags.MODAL,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.CANCEL,
                text=f"Iniciar benchmark de {benchmark_label}?",
            )
            dialog.format_secondary_text(
                f"{details} Começa com os sensores abaixo de {SYSBENCH_PRECHECK_TEMP_C:.0f} °C e para a "
                f"{SYSBENCH_ABORT_TEMP_C:.0f} °C ou se a leitura térmica da CPU ficar indisponível."
            )
            dialog.add_button("Iniciar benchmark", Gtk.ResponseType.ACCEPT)
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.ACCEPT:
                self.refresh_state.set_text("Benchmark cancelado antes de iniciar.")
                return
            self.stress_starting = True
            self.refresh_state.set_text("Conferindo sensores térmicos antes do benchmark…")
            self.render_page()

            def worker() -> None:
                try:
                    message = self.test_sessions().start_benchmark(kind, available_memory)
                    GLib.idle_add(self.finish_stress_start, message, None)
                except (RuntimeError, ValueError) as exc:
                    GLib.idle_add(self.finish_stress_start, "", str(exc))

            threading.Thread(target=worker, name="boom-sysbench-start", daemon=True).start()

        def request_disk_benchmark(self, _button: Any) -> None:
            if self.stress_starting or self.optional_installing or self.test_sessions().is_active():
                return
            if not shutil.which("fio"):
                self.request_optional_install(_button)
                return
            chooser = Gtk.FileChooserDialog(
                title="Escolher pasta no volume para avaliar",
                parent=self,
                action=Gtk.FileChooserAction.SELECT_FOLDER,
            )
            chooser.add_buttons("Cancelar", Gtk.ResponseType.CANCEL, "Escolher pasta", Gtk.ResponseType.ACCEPT)
            chooser.set_select_multiple(False)
            target: str | None = None
            try:
                chooser.set_current_folder(str(Path.home()))
                response = chooser.run()
                target = chooser.get_filename() if response == Gtk.ResponseType.ACCEPT else None
            except Exception as exc:
                self.refresh_state.set_text(f"Não foi possível abrir o seletor de pasta: {exc}")
                return
            finally:
                chooser.destroy()
            if not target:
                self.refresh_state.set_text("Benchmark de disco cancelado antes de iniciar.")
                return
            readiness = disk_benchmark_readiness(target)
            if not readiness.get("ready"):
                self.refresh_state.set_text(str(readiness.get("message") or "Destino do benchmark indisponível."))
                return
            resolved_target = str(readiness["target"])
            mount_source = readiness.get("source") or readiness.get("mountpoint") or "volume selecionado"
            dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=Gtk.DialogFlags.MODAL,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.CANCEL,
                text="Executar benchmark sequencial e aleatório de disco?",
            )
            dialog.format_secondary_text(
                f"Volume: {mount_source}. Pasta: {resolved_target}. O fio usará um único arquivo temporário novo de {DISK_BENCHMARK_SIZE_MIB} MiB para leitura e escrita sequenciais de 1 MiB e quatro perfis aleatórios 4 KiB (leitura/escrita em QD1/QD32, {DISK_BENCHMARK_RANDOM_JOB_SECONDS} s cada). O limite total é {DISK_BENCHMARK_TIMEOUT_SECONDS} s; este fluxo não monitora temperatura do disco. O arquivo será removido ao terminar ou cancelar; nenhum dispositivo bruto nem arquivo existente será alterado."
            )
            dialog.add_button("Iniciar benchmark", Gtk.ResponseType.ACCEPT)
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.ACCEPT:
                self.refresh_state.set_text("Benchmark de disco cancelado antes de iniciar.")
                return
            self.stress_starting = True
            self.refresh_state.set_text("Preparando arquivo temporário privado para o benchmark…")
            self.render_page()

            def worker() -> None:
                try:
                    message = self.test_sessions().start_disk(resolved_target)
                    GLib.idle_add(self.finish_stress_start, message, None)
                except (RuntimeError, ValueError) as exc:
                    GLib.idle_add(self.finish_stress_start, "", str(exc))

            threading.Thread(target=worker, name="boom-disk-benchmark-start", daemon=True).start()

        def finish_stress_start(self, message: str, error: str | None) -> bool:
            self.stress_starting = False
            state_message = error or message
            self.refresh_state.set_text(state_message)
            session = self.test_sessions().snapshot()
            if session.get("status") in {"starting", "running", "stopping"} and self.stress_poll_source is None:
                self.stress_poll_source = GLib.timeout_add_seconds(1, self.poll_stress_test)
            if self.page == PAGE_TESTS:
                self.render_page()
            return False

        def poll_stress_test(self) -> bool:
            session = self.test_sessions().snapshot()
            if self.page == PAGE_TESTS:
                self.update_stress_sensor_widgets()
                widgets = self.stress_widgets
                status = widgets.get("status")
                progress = widgets.get("progress")
                if status is not None:
                    status.set_text(session.get("message", "Teste de carga."))
                if progress is not None:
                    duration = max(1, int(session.get("duration_seconds", 1)))
                    progress.set_fraction(min(1.0, float(session.get("elapsed_seconds", 0)) / duration) if session.get("status") == "running" else 1.0 if session.get("status") == "completed" else 0.0)
                if widgets.get("cancel_button") is not None:
                    widgets["cancel_button"].set_sensitive(session.get("status") in {"starting", "running", "stopping"})
            if session.get("status") in {"starting", "running", "stopping"}:
                return True
            self.stress_poll_source = None
            self.refresh_state.set_text(session.get("message", "Teste de carga finalizado."))
            if self.page == PAGE_TESTS:
                self.render_page()
            return False

        def update_stress_sensor_widgets(self) -> None:
            widgets = self.stress_widgets
            if not widgets:
                return
            session = self.test_sessions().snapshot()
            temperatures = self.data.get("temperatures", [])
            readiness = stress_test_readiness(temperatures)
            selected_gpu = select_stress_gpu(self.data.get("gpu_metrics", []), self.stress_gpu_key)
            gpu_readiness = stress_test_readiness(temperatures, "gpu", selected_gpu)
            busy = self.stress_starting or self.test_sessions().is_active()
            cpu_temperature = session.get("cpu_celsius") if busy else readiness.get("cpu_celsius")
            gpu_temperature = session.get("gpu_celsius") if busy and session.get("kind") == "gpu" else (selected_gpu or {}).get("temperature_celsius")
            hottest = session.get("current_celsius") if busy else gpu_readiness.get("current_celsius") if selected_gpu else readiness.get("current_celsius")
            if widgets.get("cpu_temperature") is not None:
                widgets["cpu_temperature"].set_text(f"{cpu_temperature:.1f} °C" if isinstance(cpu_temperature, (int, float)) else "Sensor da CPU indisponível")
            if widgets.get("gpu_temperature") is not None:
                widgets["gpu_temperature"].set_text(f"{gpu_temperature:.1f} °C" if isinstance(gpu_temperature, (int, float)) else "Sensor da GPU indisponível")
            if widgets.get("hottest_temperature") is not None:
                widgets["hottest_temperature"].set_text(f"{hottest:.1f} °C" if isinstance(hottest, (int, float)) else "Sem leitura")
            if widgets.get("readiness") is not None:
                widgets["readiness"].set_text(readiness["message"])
            if widgets.get("gpu_readiness") is not None:
                widgets["gpu_readiness"].set_text(gpu_readiness["message"])
            start_ready = readiness["ready"] and not busy
            if widgets.get("cpu_button") is not None:
                widgets["cpu_button"].set_sensitive(start_ready)
            memory_ready = False
            try:
                stress_test_command("memory", self.stress_duration_seconds, 1, (self.data.get("memory") or {}).get("available"))
                memory_ready = True
            except (RuntimeError, ValueError):
                pass
            if widgets.get("memory_button") is not None:
                widgets["memory_button"].set_sensitive(start_ready and memory_ready)
            if widgets.get("gpu_button") is not None:
                widgets["gpu_button"].set_sensitive(gpu_readiness["ready"] and not busy)
            if widgets.get("disk_button") is not None:
                widgets["disk_button"].set_sensitive(not busy)
            if widgets.get("gpu_selector") is not None:
                widgets["gpu_selector"].set_sensitive(not busy and bool(self.data.get("gpu_metrics")))

        def cancel_stress_test(self, _button: Any) -> None:
            self.test_sessions().cancel()
            self.refresh_state.set_text("Cancelamento do teste solicitado.")

        def request_optional_install(self, _button: Any) -> None:
            if self.optional_installing or self.test_sessions().is_active():
                return
            packages = missing_optional_packages()
            if not packages:
                self.refresh_state.set_text("Todas as ferramentas opcionais já estão instaladas.")
                self.render_page()
                return
            package_list = ", ".join(packages)
            dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=Gtk.DialogFlags.MODAL,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.CANCEL,
                text="Instalar ferramentas opcionais do BooM?",
            )
            dialog.format_secondary_text(
                f"Pacotes ausentes: {package_list}. O BooM atualizará os índices do APT e instalará somente esses pacotes após autenticação pelo sistema."
            )
            dialog.add_button("Instalar pacotes", Gtk.ResponseType.ACCEPT)
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.ACCEPT:
                self.refresh_state.set_text("Instalação cancelada antes de iniciar.")
                return
            self.optional_installing = True
            self.refresh_state.set_text(f"Atualizando o APT e preparando {package_list}; aguarde a autenticação do sistema…")
            self.render_page()

            def worker() -> None:
                try:
                    result = install_missing_optional_dependencies()
                except Exception as exc:
                    result = {"ok": False, "message": f"Falha ao instalar ferramentas: {exc}", "packages": []}
                GLib.idle_add(self.finish_optional_install, result)

            threading.Thread(target=worker, name="boom-optional-installer", daemon=True).start()

        def finish_optional_install(self, result: dict[str, Any]) -> bool:
            self.optional_installing = False
            self.refresh_state.set_text(result["message"])
            if result["ok"]:
                self.refresh()
            elif self.page in (PAGE_SYSTEM, PAGE_TESTS):
                self.render_page()
            return False

        def request_firmware_check(self, _button: Any) -> None:
            if self.firmware_checking:
                return
            if not shutil.which("fwupdmgr"):
                self.refresh_state.set_text("fwupd está ausente. Use ‘Instalar ferramentas ausentes’ para instalar o pacote fwupd.")
                return
            self.firmware_checking = True
            self.firmware_info = {"state": "checking", "message": "Consultando fwupd…", "devices": [], "updates": []}
            self.render_page()

            def worker() -> None:
                try:
                    result = firmware_inventory_info()
                except Exception as exc:
                    result = {"state": "error", "message": f"Falha ao consultar fwupd: {exc}", "devices": [], "updates": []}
                GLib.idle_add(self.finish_firmware_check, result)

            threading.Thread(target=worker, name="boom-firmware-query", daemon=True).start()

        def finish_firmware_check(self, result: dict[str, Any]) -> bool:
            self.firmware_checking = False
            self.firmware_info = result
            self.refresh_state.set_text(result.get("message", "Consulta de firmware concluída."))
            if self.page == PAGE_SYSTEM:
                self.render_page()
            return False

        def request_power_profile_change(self, _button: Any, choices: Any) -> None:
            if self.power_profile_busy:
                return
            if self.test_sessions().is_active():
                self.refresh_state.set_text("Encerre o teste de carga antes de mudar o perfil de energia.")
                return
            profile = choices.get_active_id()
            info = self.data.get("power_profiles", {})
            if not profile or profile not in info.get("profiles", []):
                self.refresh_state.set_text("Escolha um perfil oferecido pelo sistema.")
                return
            if profile == info.get("current"):
                self.refresh_state.set_text(f"O perfil {POWER_PROFILE_LABELS[profile]} já está ativo.")
                return
            dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=Gtk.DialogFlags.MODAL,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.CANCEL,
                text=f"Aplicar {POWER_PROFILE_LABELS[profile]}?",
            )
            dialog.format_secondary_text(
                "A mudança pode alterar resposta da CPU, consumo, temperatura e ruído. O BooM usa o perfil anunciado pelo gerenciador do sistema, guarda o perfil anterior e não força clocks nem overclock."
            )
            dialog.add_button("Aplicar perfil", Gtk.ResponseType.ACCEPT)
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.ACCEPT:
                self.refresh_state.set_text("Alteração de perfil cancelada.")
                return
            self.start_power_profile_action(profile=profile)

        def request_power_profile_restore(self, _button: Any) -> None:
            if self.power_profile_busy:
                return
            if self.test_sessions().is_active():
                self.refresh_state.set_text("Encerre o teste de carga antes de restaurar o perfil.")
                return
            state = (self.data.get("power_profiles") or {}).get("restore_state")
            if not state:
                self.refresh_state.set_text("Não há perfil anterior do BooM para restaurar.")
                return
            profile = state["restore_profile"]
            changed_elsewhere = (self.data.get("power_profiles") or {}).get("external_change_detected")
            message = f"Restaurar {POWER_PROFILE_LABELS[profile]}?"
            detail = "Isso substitui o perfil ativo do sistema."
            if changed_elsewhere:
                detail += " Outro componente alterou o perfil desde a última mudança do BooM."
            dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=Gtk.DialogFlags.MODAL,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.CANCEL,
                text=message,
            )
            dialog.format_secondary_text(detail)
            dialog.add_button("Restaurar perfil", Gtk.ResponseType.ACCEPT)
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.ACCEPT:
                self.refresh_state.set_text("Restauração cancelada.")
                return
            self.start_power_profile_action(restore=True)

        def start_power_profile_action(self, profile: str | None = None, restore: bool = False) -> None:
            if self.power_profile_busy:
                return
            self.power_profile_busy = True
            self.refresh_state.set_text("Aplicando perfil de energia pelo serviço do sistema…")
            self.render_page()

            def worker() -> None:
                try:
                    result = restore_power_profile() if restore else set_power_profile(profile or "")
                    info = power_profile_info()
                except Exception as exc:
                    result = {"ok": False, "message": f"Falha no controle de energia: {exc}"}
                    info = power_profile_info()
                GLib.idle_add(self.finish_power_profile_action, result, info)

            threading.Thread(target=worker, name="boom-power-profile", daemon=True).start()

        def finish_power_profile_action(self, result: dict[str, Any], info: dict[str, Any]) -> bool:
            self.power_profile_busy = False
            self.data["power_profiles"] = info
            self.refresh_state.set_text(result["message"])
            if self.page == PAGE_SYSTEM:
                self.render_page()
            return False

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
            search.get_accessible().set_name("Filtrar processos por nome ou PID")
            summary.pack_start(search, False, False, 0)
            self.add_card(summary)

            store = Gtk.ListStore(int, int, str, str, str, str, bool, int, str)
            for item in processes:
                cpu = f"{item['cpu_percent']:.1f}%" if item.get("cpu_percent") is not None else "—"
                store.append([
                    item["pid"], item["ppid"], item["name"], cpu, format_bytes(item["memory_bytes"]),
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
                ("PID", 0, False), ("PPID", 1, False), ("Processo", 2, True),
                ("CPU", 3, False), ("RAM", 4, False), ("Estado", 5, False),
                ("Ação", 8, False),
            ):
                self.add_tree_column(tree, title, column, expand)
            list_scroll = Gtk.ScrolledWindow()
            list_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            list_scroll.set_min_content_height(330)
            list_scroll.add(tree)
            self.content.pack_start(list_scroll, True, True, 0)
            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            terminate = Gtk.Button(label="Encerrar processo selecionado")
            terminate.get_style_context().add_class("action")
            terminate.set_sensitive(False)
            terminate_tree = Gtk.Button(label="Encerrar árvore")
            terminate_tree.set_sensitive(False)
            tree_hint = Gtk.Label(label="Selecione um processo para ver quantos descendentes seriam incluídos.")
            tree_hint.set_xalign(0)
            tree_hint.set_line_wrap(True)
            tree_hint.get_style_context().add_class("muted")
            tree.get_selection().connect(
                "changed", self.on_process_selection, terminate, terminate_tree, tree_hint,
            )
            terminate.connect("clicked", self.request_process_termination, tree.get_selection())
            terminate_tree.connect("clicked", self.request_process_tree_termination, tree.get_selection())
            actions.pack_start(terminate, False, False, 0)
            actions.pack_start(terminate_tree, False, False, 0)
            self.content.pack_start(actions, False, False, 0)
            note = Gtk.Label(
                label="Encerrar envia SIGTERM e pode fechar trabalho não salvo. A árvore inclui o processo selecionado e seus descendentes; filhos recebem o sinal antes dos pais. Processos protegidos bloqueiam a ação e filhos criados depois da confirmação podem continuar ativos."
            )
            note.set_xalign(0)
            note.set_line_wrap(True)
            note.get_style_context().add_class("muted")
            self.content.pack_start(note, False, False, 0)
            self.content.pack_start(tree_hint, False, False, 0)

        def process_matches(self, model: Any, iterator: Any, search: Any) -> bool:
            query = search.get_text().strip().casefold()
            if not query:
                return True
            return query in model.get_value(iterator, 2).casefold() or query in str(model.get_value(iterator, 0))

        def on_process_selection(
            self, selection: Any, process_button: Any, tree_button: Any, hint: Any,
        ) -> None:
            model, iterator = selection.get_selected()
            if iterator is None:
                process_button.set_sensitive(False)
                tree_button.set_sensitive(False)
                tree_button.set_label("Encerrar árvore")
                hint.set_text("Selecione um processo para ver quantos descendentes seriam incluídos.")
                return

            can_terminate = bool(model.get_value(iterator, 6))
            process_button.set_sensitive(can_terminate)
            pid = int(model.get_value(iterator, 0))
            name = model.get_value(iterator, 2)
            start_ticks = int(model.get_value(iterator, 7))
            try:
                snapshot = process_tree_snapshot(
                    pid, start_ticks, process_records=self.manager_data.get("processes", []),
                )
            except (OSError, ValueError):
                tree_button.set_label("Encerrar árvore")
                tree_button.set_sensitive(False)
                hint.set_text("A árvore mudou; atualize a lista antes de encerrar.")
                return

            total = len(snapshot)
            protected = sum(not item["can_terminate"] for item in snapshot)
            tree_button.set_label(f"Encerrar árvore ({total})")
            tree_button.set_sensitive(can_terminate and total > 1 and protected == 0)
            if not can_terminate:
                hint.set_text(f"{name} está protegido ou pertence a outro usuário.")
            elif total <= 1:
                hint.set_text(f"{name} não tem descendentes nesta captura.")
            elif protected:
                hint.set_text(
                    f"A árvore contém {protected} processo(s) protegido(s); nenhum receberá sinal."
                )
            else:
                hint.set_text(
                    f"{total} processos nesta árvore: o selecionado e {total - 1} descendentes."
                )

        def request_process_termination(self, _button: Any, selection: Any) -> None:
            model, iterator = selection.get_selected()
            if iterator is None:
                return
            pid = int(model.get_value(iterator, 0))
            name = model.get_value(iterator, 2)
            start_ticks = int(model.get_value(iterator, 7))
            if not model.get_value(iterator, 6):
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

        def request_process_tree_termination(self, _button: Any, selection: Any) -> None:
            model, iterator = selection.get_selected()
            if iterator is None:
                return
            pid = int(model.get_value(iterator, 0))
            name = model.get_value(iterator, 2)
            start_ticks = int(model.get_value(iterator, 7))
            if not model.get_value(iterator, 6):
                self.refresh_state.set_text("Esse processo está protegido.")
                return
            try:
                snapshot = process_tree_snapshot(pid, start_ticks)
            except (OSError, ValueError) as exc:
                self.refresh_state.set_text(f"Não foi possível ler a árvore: {exc}")
                return
            if len(snapshot) <= 1:
                self.refresh_state.set_text("O processo selecionado não tem descendentes nesta captura.")
                self.refresh_manager_data()
                return
            protected = [item for item in snapshot if not item["can_terminate"]]
            if protected:
                examples = ", ".join(
                    f"{item['name']} (PID {item['pid']})" for item in protected[:3]
                )
                self.refresh_state.set_text(
                    f"A árvore contém processo(s) protegido(s): {examples}; nenhum sinal foi enviado."
                )
                return

            total = len(snapshot)
            dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=Gtk.DialogFlags.MODAL,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.CANCEL,
                text=f"Encerrar árvore de {name} (PID {pid})?",
            )
            dialog.format_secondary_text(
                f"O BooM enviará SIGTERM para {total} processos: o selecionado e {total - 1} descendentes, começando pelos filhos. Isso pode fechar trabalho não salvo. Processos criados depois desta captura podem continuar ativos."
            )
            dialog.add_button("Encerrar árvore normalmente", Gtk.ResponseType.ACCEPT)
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.ACCEPT:
                self.refresh_state.set_text("Encerramento da árvore cancelado.")
                return
            try:
                result = terminate_process_tree(pid, start_ticks, process_records=snapshot)
                sent = len(result["sent"])
                changed = len(result["changed"])
                failed = result["failed"]
                details = []
                if changed:
                    details.append(f"{changed} já encerrados ou alterados")
                if failed:
                    first = failed[0]
                    target = first["process"]
                    details.append(
                        f"{len(failed)} falhas, por exemplo {target['name']} (PID {target['pid']})"
                    )
                suffix = " · " + "; ".join(details) if details else ""
                self.refresh_state.set_text(
                    f"SIGTERM enviado para {sent} de {result['total']} processos da árvore{suffix}."
                )
                self.refresh_manager_data()
            except (OSError, ValueError) as exc:
                self.refresh_state.set_text(f"Não foi possível encerrar a árvore: {exc}")

        def render_application_page(self) -> None:
            applications = self.manager_data.get("applications", [])
            card = self.card("Aplicativos gráficos instalados")
            self.add_row(card, "Encontrados", str(len(applications)))
            self.add_row(card, "Origem", "APT, Flatpak e outros lançadores visíveis para esta sessão")
            search = Gtk.SearchEntry()
            search.set_placeholder_text("Buscar aplicativo")
            search.get_accessible().set_name("Buscar aplicativos por nome ou ID")
            card.pack_start(search, False, False, 0)
            hint = Gtk.Label(
                label="Desinstalação APT purga arquivos e configurações do sistema, sem remover dependências compartilhadas ou dados pessoais. Flatpak também apaga os dados do aplicativo deste usuário. Origens não identificadas não podem ser removidas aqui."
            )
            hint.set_xalign(0)
            hint.set_line_wrap(True)
            hint.get_style_context().add_class("muted")
            card.pack_start(hint, False, False, 0)
            self.add_card(card)
            store = Gtk.ListStore(str, str, str, bool, str, str, bool, object)
            for app in applications:
                store.append([
                    app["name"], app["id"], app["path"], app["available"],
                    "Sim" if app["available"] else "Executável ausente",
                    app.get("source", "Origem não identificada"),
                    app.get("uninstallable", False),
                    app.get("uninstall_target"),
                ])
            filtered = store.filter_new()
            filtered.set_visible_func(
                lambda model, iterator, entry: not entry.get_text().strip()
                or entry.get_text().strip().casefold() in model.get_value(iterator, 0).casefold()
                or entry.get_text().strip().casefold() in model.get_value(iterator, 1).casefold(),
                search,
            )
            search.connect("search-changed", lambda entry: filtered.refilter())
            tree = Gtk.TreeView(model=filtered)
            for title, column, expand in (
                ("Aplicativo", 0, True), ("ID", 1, False), ("Origem", 5, False), ("Disponível", 4, False),
            ):
                self.add_tree_column(tree, title, column, expand)
            tree.get_selection().set_mode(Gtk.SelectionMode.SINGLE)
            list_scroll = Gtk.ScrolledWindow()
            list_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            list_scroll.set_min_content_height(300)
            list_scroll.add(tree)
            self.content.pack_start(list_scroll, False, False, 0)
            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            open_button = Gtk.Button(label="Abrir aplicativo selecionado")
            open_button.get_style_context().add_class("action")
            open_button.set_sensitive(False)
            remove_button = Gtk.Button(label="Desinstalar aplicativo selecionado")
            remove_button.set_sensitive(False)
            tree.get_selection().connect(
                "changed", self.on_application_selection, open_button, remove_button
            )
            open_button.connect("clicked", self.launch_selected_application, tree.get_selection())
            remove_button.connect("clicked", self.uninstall_selected_application, tree.get_selection())
            actions.pack_start(open_button, True, True, 0)
            actions.pack_start(remove_button, False, False, 0)
            self.content.pack_start(actions, False, False, 0)
            self.render_steam_library_section()

        def render_steam_library_section(self) -> None:
            steam = self.manager_data.get("steam", {})
            lutris = self.manager_data.get("lutris", {})
            heroic = self.manager_data.get("heroic", {})
            games = self.manager_data.get("games", [])
            library = self.card("Bibliotecas locais de jogos")
            self.add_row(library, "Steam", steam.get("message", "Biblioteca não consultada."))
            self.add_row(library, "Cliente", steam.get("steam_client", "Não detectado"))
            self.add_row(library, "Lutris", lutris.get("message", "Biblioteca não consultada."))
            self.add_row(library, "Bases Lutris", str(lutris.get("database_count", 0)))
            self.add_row(library, "Heroic", heroic.get("message", "Biblioteca não consultada."))
            self.add_row(library, "Caches Heroic", str(heroic.get("library_count", 0)))
            if games:
                store = Gtk.ListStore(str, str, str, str, str)
                for game in games:
                    store.append([
                        game["name"], game["launcher"], game.get("store", ""),
                        game["game_id"], game.get("runner", ""),
                    ])
                tree = Gtk.TreeView(model=store)
                tree.set_headers_visible(True)
                self.add_tree_column(tree, "Jogo instalado", 0, True)
                self.add_tree_column(tree, "Lançador", 1, False)
                self.add_tree_column(tree, "Loja", 2, False)
                self.add_tree_column(tree, "ID local", 3, False)
                selection = tree.get_selection()
                selection.set_mode(Gtk.SelectionMode.SINGLE)
                list_scroll = Gtk.ScrolledWindow()
                list_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
                list_scroll.set_size_request(-1, 230)
                list_scroll.add(tree)
                library.pack_start(list_scroll, False, False, 0)
                launch_button = Gtk.Button(label="Abrir jogo selecionado no lançador")
                launch_button.get_style_context().add_class("action")
                launch_button.set_sensitive(False)
                selection.connect("changed", self.on_steam_game_selection, launch_button)
                launch_button.connect("clicked", self.launch_selected_steam_game, selection)
                library.pack_start(launch_button, False, False, 0)
            else:
                hint = Gtk.Label(
                    label=(
                        "O BooM lê bibliotecas locais do Steam, Lutris e Heroic. Não acessa contas, "
                        "não baixa jogos e não executa comandos de instalação."
                    )
                )
                hint.set_xalign(0)
                hint.set_line_wrap(True)
                hint.get_style_context().add_class("muted")
                library.pack_start(hint, False, False, 0)
            self.add_card(library)

            tools = {item["name"]: item["installed"] for item in gaming_tool_status()}
            options = self.card("FPS no jogo e perfil de desempenho")
            self.add_row(
                options, "MangoHud",
                "Instalado" if tools.get("MangoHud") else "Ausente · disponível como ferramenta opcional",
            )
            self.add_row(
                options, "GameMode",
                "Instalado" if tools.get("GameMode") else "Ausente · disponível como ferramenta opcional",
            )
            hint = Gtk.Label(
                label=(
                    "Copie uma opção para Propriedades > Geral > Opções de inicialização do jogo no Steam. "
                    "MangoHud mede FPS compatíveis e desenha o overlay no jogo; GameMode pede o perfil ao daemon. "
                    "No Steam Flatpak, MangoHud precisa da camada/runtime dentro do sandbox; o pacote APT é para Steam nativo. "
                    "O BooM não altera opções salvas nem estima FPS fora do jogo."
                )
            )
            hint.set_xalign(0)
            hint.set_line_wrap(True)
            hint.get_style_context().add_class("muted")
            options.pack_start(hint, False, False, 0)
            for label, command in STEAM_LAUNCH_OPTIONS.items():
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                name = Gtk.Label(label=label)
                name.set_xalign(0)
                name.set_line_wrap(True)
                name.set_max_width_chars(30)
                name.get_style_context().add_class("body-copy")
                entry = Gtk.Entry()
                entry.set_text(command)
                entry.set_editable(False)
                entry.set_hexpand(True)
                entry.get_accessible().set_name(f"Opção de inicialização: {label}")
                copy_button = Gtk.Button(label="Copiar")
                required_tools = {
                    "MangoHud (FPS e métricas no jogo)": ("MangoHud",),
                    "GameMode (perfil de desempenho durante o jogo)": ("GameMode",),
                    "MangoHud + GameMode": ("MangoHud", "GameMode"),
                }[label]
                copy_button.set_sensitive(all(tools.get(tool, False) for tool in required_tools))
                copy_button.connect("clicked", self.copy_steam_launch_option, command)
                row.pack_start(name, False, False, 0)
                row.pack_start(entry, True, True, 0)
                row.pack_start(copy_button, False, False, 0)
                options.pack_start(row, False, False, 0)
            if not all(tools.values()):
                install_button = Gtk.Button(label="Instalar ferramentas opcionais ausentes")
                install_button.connect("clicked", self.request_optional_install)
                options.pack_start(install_button, False, False, 0)
            self.add_card(options)

        def on_steam_game_selection(self, selection: Any, launch_button: Any) -> None:
            _model, iterator = selection.get_selected()
            launch_button.set_sensitive(iterator is not None)

        def launch_selected_steam_game(self, _button: Any, selection: Any) -> None:
            model, iterator = selection.get_selected()
            if iterator is None:
                return
            name = model.get_value(iterator, 0)
            launcher = model.get_value(iterator, 1)
            game_id = model.get_value(iterator, 3)
            runner = model.get_value(iterator, 4)
            result = launch_installed_game(launcher, game_id, runner or None)
            self.refresh_state.set_text(f"{name}: {result['message']}")

        def copy_steam_launch_option(self, _button: Any, command: str) -> None:
            clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            clipboard.set_text(command, -1)
            self.refresh_state.set_text("Opção de inicialização copiada para a área de transferência.")

        def on_application_selection(self, selection: Any, open_button: Any, remove_button: Any) -> None:
            model, iterator = selection.get_selected()
            open_button.set_sensitive(
                iterator is not None
                and bool(model.get_value(iterator, 3))
                and not self.application_uninstalling
            )
            remove_button.set_sensitive(
                iterator is not None
                and bool(model.get_value(iterator, 6))
                and not self.application_uninstalling
            )

        def launch_selected_application(self, _button: Any, selection: Any) -> None:
            model, iterator = selection.get_selected()
            if iterator is None:
                return
            message = launch_desktop_application(model.get_value(iterator, 2))
            self.refresh_state.set_text(message)

        def uninstall_selected_application(self, _button: Any, selection: Any) -> None:
            model, iterator = selection.get_selected()
            if iterator is None or self.application_uninstalling:
                return
            name = model.get_value(iterator, 0)
            target = model.get_value(iterator, 7)
            if not target:
                return
            preview = preview_desktop_application_uninstall(target)
            if not preview["ok"]:
                self.refresh_state.set_text(preview["message"])
                return
            if target["manager"] == "apt":
                details = (
                    f"{preview['message']} APT purgará configurações do sistema; arquivos pessoais em sua pasta "
                    "pessoal não serão apagados."
                )
            else:
                details = preview["message"] + " Esta ação não pode ser desfeita."
            dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=Gtk.DialogFlags.MODAL,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.CANCEL,
                text=f"Desinstalar {name}?",
            )
            dialog.format_secondary_text(details)
            dialog.add_button("Desinstalar", Gtk.ResponseType.ACCEPT)
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.ACCEPT:
                self.refresh_state.set_text("Desinstalação cancelada.")
                return
            self.application_uninstalling = True
            self.refresh_state.set_text(f"Removendo {name}; aguarde a autenticação do sistema se solicitada…")
            self.render_page()

            def worker() -> None:
                try:
                    result = uninstall_desktop_application(target)
                except Exception as exc:
                    result = {"ok": False, "message": f"Falha ao desinstalar {name}: {exc}"}
                manager_data = None
                if result["ok"]:
                    try:
                        manager_data = collect_manager_state()
                    except Exception as exc:
                        result["message"] += f" A lista não pôde ser atualizada: {exc}"
                GLib.idle_add(self.finish_application_uninstall, result, name, manager_data)

            threading.Thread(target=worker, name="boom-application-uninstall", daemon=True).start()

        def finish_application_uninstall(
            self, result: dict[str, Any], name: str, manager_data: dict[str, Any] | None,
        ) -> bool:
            self.application_uninstalling = False
            if manager_data is not None:
                self.manager_data = manager_data
            self.refresh_state.set_text(
                f"{name}: {result['message']}" if result["ok"] else result["message"]
            )
            self.render_page()
            return False


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
            self.data["sensor_readings"] = metrics.get("sensor_readings", [])
            self.data["sensors_status"] = metrics.get("sensors_status", "lm-sensors / hwmon / sysfs")
            self.data["disk_io"] = metrics.get("disk_io")
            self.data["power"] = metrics.get("power", {})
            updated_at = metrics.get("updated_at") or datetime.now().astimezone().strftime("%H:%M:%S")
            self.refresh_state.set_text(f"Métricas ao vivo · Atualizado às {updated_at}")
            if self.page in (PAGE_OVERVIEW, PAGE_SYSTEM, PAGE_MEMORY, PAGE_GRAPHICS, PAGE_DISKS):
                self.render_page()
            elif self.page == PAGE_TESTS:
                self.update_stress_sensor_widgets()

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

    def quit_application(*_args: Any) -> None:
        state["test_sessions"].stop_and_wait()
        application.quit()

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
            quit_item.connect("activate", quit_application)
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
        state["test_sessions"].stop_and_wait()
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
    mode_group.add_argument("--history-enable", action="store_true", help="ativa gravação local e opcional de métricas enquanto o BooM estiver aberto")
    mode_group.add_argument("--history-disable", action="store_true", help="para novas gravações do histórico local")
    mode_group.add_argument("--history-status", action="store_true", help="mostra o estado e a quantidade de amostras locais")
    mode_group.add_argument("--history-clear", action="store_true", help="confirma e remove o histórico local")
    mode_group.add_argument("--diagnose", action="store_true", help="analisa a janela recente do histórico local, sem alterar o sistema")
    mode_group.add_argument("--network-test", action="store_true", help="executa ping para o gateway e 1.1.1.1")
    mode_group.add_argument("--tui", action="store_true", help="abre o painel interativo no terminal")
    mode_group.add_argument("--tray", action="store_true", help="inicia oculto e monitora CPU, clocks, RAM, GPU e temperaturas")
    mode_group.add_argument("--enable-autostart", action="store_true", help="inicia o BooM oculto ao entrar na sessão")
    mode_group.add_argument("--disable-autostart", action="store_true", help="remove o início automático do BooM")
    parser.add_argument(
        "--diagnose-window", type=float, default=15,
        help="janela em minutos para --diagnose (1 a 10080; padrão: 15)",
    )
    args = parser.parse_args()
    history_store = PerformanceHistoryStore()
    if args.history_enable:
        try:
            enabled_path = history_store.set_enabled(True)
        except OSError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps({
            "ok": True,
            "enabled": True,
            "settings_file": str(enabled_path),
            "database": str(history_store.database_path),
            "message": "O histórico é local e começa a receber amostras quando o BooM está monitorando.",
        }, ensure_ascii=False, indent=2))
        return 0
    if args.history_disable:
        try:
            enabled_path = history_store.set_enabled(False)
        except OSError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps({
            "ok": True,
            "enabled": False,
            "settings_file": str(enabled_path),
            "message": "Novas amostras foram desativadas; os dados existentes permanecem até a gravação retomar e aplicar a retenção ou até serem apagados.",
        }, ensure_ascii=False, indent=2))
        return 0
    if args.history_status:
        print(json.dumps(history_store.status(), ensure_ascii=False, indent=2))
        return 0
    if args.history_clear:
        confirmation = input("Digite APAGAR para desativar e remover todas as amostras locais do BooM: ")
        if confirmation.strip() != "APAGAR":
            print("Cancelado; o histórico não foi alterado.")
            return 0
        try:
            history_store.set_enabled(False)
            history_store.clear()
        except (OSError, sqlite3.Error) as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps({"ok": True, "enabled": False, "sample_count": 0}, ensure_ascii=False))
        return 0
    if args.diagnose:
        try:
            samples = history_store.samples(args.diagnose_window)
        except ValueError as exc:
            parser.error(str(exc))
        diagnosis = analyze_performance_history(samples)
        diagnosis["window_minutes"] = args.diagnose_window
        diagnosis["history_enabled"] = history_store.enabled()
        diagnosis["source"] = "histórico local opcional do BooM"
        print(json.dumps(diagnosis, ensure_ascii=False, indent=2))
        return 0
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
