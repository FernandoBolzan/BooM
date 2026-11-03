#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
action="${1:-install}"

if [[ "$action" != "install" && "$action" != "--uninstall" ]]; then
    printf 'Uso: %s [--uninstall]\n' "$0" >&2
    exit 2
fi

exec python3 - "$action" "$script_dir" <<'PY'
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


action, project_root_arg = sys.argv[1:]
project_root = Path(project_root_arg).resolve()
data_home_value = os.environ.get("XDG_DATA_HOME", "")
data_home = Path(data_home_value).expanduser() if data_home_value else Path.home() / ".local" / "share"
if not data_home.is_absolute():
    data_home = Path.home() / ".local" / "share"

desktop_path = data_home / "applications" / "com.boom.SystemMonitor.desktop"
icon_path = data_home / "icons" / "hicolor" / "scalable" / "apps" / "com.boom.SystemMonitor.svg"
icon_source = project_root / "data" / "icons" / "com.boom.SystemMonitor.svg"


def atomic_write(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as temporary:
            temporary.write(contents)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def desktop_exec_argument(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def refresh_application_cache() -> None:
    updater = shutil.which("update-desktop-database")
    if updater:
        subprocess.run([updater, str(desktop_path.parent)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    icon_updater = shutil.which("gtk-update-icon-cache")
    if icon_updater:
        subprocess.run(
            [icon_updater, "--force", "--ignore-theme-index", "--quiet", str(icon_path.parents[2])],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


if action == "--uninstall":
    if desktop_path.exists():
        desktop_text = desktop_path.read_text(encoding="utf-8")
        if "X-BooM-Managed=true" not in desktop_text:
            raise SystemExit(f"Recusando remover um lançador que não pertence ao BooM: {desktop_path}")
        desktop_path.unlink()
    if icon_path.is_file() and icon_source.is_file() and icon_path.read_bytes() == icon_source.read_bytes():
        icon_path.unlink()
    refresh_application_cache()
    print(f"Lançador do BooM removido: {desktop_path}")
    raise SystemExit(0)


if not (project_root / "start-linux.sh").is_file() or not icon_source.is_file():
    raise SystemExit("A instalação precisa ser executada a partir de uma cópia completa do BooM.")

launcher = desktop_exec_argument(str(project_root / "start-linux.sh"))
launcher_tray = desktop_exec_argument(str(project_root / "start-linux.sh"))
desktop_entry = (
    "[Desktop Entry]\n"
    "Version=1.0\n"
    "Type=Application\n"
    "Name=BooM\n"
    "GenericName=Monitor do sistema\n"
    "Comment=Monitore CPU, memória, GPU e sensores localmente\n"
    f"Exec={launcher}\n"
    "Icon=com.boom.SystemMonitor\n"
    "Terminal=false\n"
    "StartupNotify=true\n"
    "StartupWMClass=com.boom.SystemMonitor\n"
    "Categories=System;Monitor;\n"
    "Keywords=BooM;CPU;GPU;Memória;Desempenho;\n"
    "Actions=Tray;\n"
    "X-BooM-Managed=true\n"
    "\n"
    "[Desktop Action Tray]\n"
    "Name=Monitorar em segundo plano\n"
    f"Exec={launcher_tray} --tray\n"
)

atomic_write(icon_path, icon_source.read_bytes())
atomic_write(desktop_path, desktop_entry.encode("utf-8"))
refresh_application_cache()
print(f"BooM instalado no menu de aplicativos: {desktop_path}")
print("Pesquise por BooM no lançador do Pop!_OS.")
PY
