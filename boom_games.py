"""Steam library discovery and opt-in game launching for BooM."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


MAX_VDF_BYTES = 4 * 1024 * 1024
_VDF_TOKEN = re.compile(r'//[^\r\n]*|"(?:\\.|[^"\\])*"|[{}]|[^\s{}"]+')
_APP_MANIFEST = re.compile(r"appmanifest_(\d+)\.acf\Z")
_STEAM_ROOT_SUFFIXES = (
    Path(".steam/steam"),
    Path(".steam/root"),
    Path(".local/share/Steam"),
    Path(".var/app/com.valvesoftware.Steam/.local/share/Steam"),
)


def _decode_vdf_token(token: str) -> str:
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        token = token[1:-1]
    return re.sub(r'\\(["\\])', r"\1", token)


def parse_vdf(text: str) -> dict[str, Any]:
    """Parse the quoted key/value and object subset used by Steam metadata."""
    tokens = [token for token in _VDF_TOKEN.findall(text) if not token.startswith("//")]

    def parse_object(index: int, expect_close: bool, depth: int = 0) -> tuple[dict[str, Any], int]:
        if depth > 64:
            raise ValueError("Estrutura VDF aninhada demais.")
        result: dict[str, Any] = {}
        while index < len(tokens):
            if tokens[index] == "}":
                if not expect_close:
                    raise ValueError("Chave VDF fechada sem objeto correspondente.")
                return result, index + 1
            key = _decode_vdf_token(tokens[index])
            if key in {"{", "}"}:
                raise ValueError("Estrutura VDF inválida.")
            index += 1
            if index >= len(tokens):
                raise ValueError("Valor VDF ausente.")
            if tokens[index] == "{":
                value, index = parse_object(index + 1, True, depth + 1)
            elif tokens[index] == "}":
                raise ValueError("Valor VDF ausente antes de fechar objeto.")
            else:
                value = _decode_vdf_token(tokens[index])
                index += 1
            result[key] = value
        if expect_close:
            raise ValueError("Objeto VDF não foi fechado.")
        return result, index

    parsed, _ = parse_object(0, False)
    return parsed


def _read_vdf(path: Path) -> dict[str, Any] | None:
    try:
        if path.stat().st_size > MAX_VDF_BYTES:
            return None
        return parse_vdf(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None


def discover_steam_roots(
    home: Path | None = None,
    data_home: Path | None = None,
) -> list[Path]:
    home = home or Path.home()
    if data_home is None:
        configured = os.environ.get("XDG_DATA_HOME")
        configured_path = Path(configured).expanduser() if configured else None
        data_home = configured_path if configured_path and configured_path.is_absolute() else home / ".local/share"
    candidates = [home / suffix for suffix in _STEAM_ROOT_SUFFIXES]
    candidates.insert(0, data_home / "Steam")

    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            if not (resolved / "steamapps").is_dir():
                continue
            identity = os.path.normcase(str(resolved))
            if identity in seen:
                continue
            seen.add(identity)
            roots.append(resolved)
        except OSError:
            continue
    return roots


def _steam_library_roots(steam_roots: list[Path]) -> list[Path]:
    libraries: list[Path] = []
    seen: set[str] = set()
    for steam_root in steam_roots:
        paths = [steam_root]
        parsed = _read_vdf(steam_root / "steamapps/libraryfolders.vdf")
        folder_map = parsed.get("libraryfolders") if parsed else None
        if not isinstance(folder_map, dict) and parsed:
            folder_map = parsed.get("LibraryFolders")
        if isinstance(folder_map, dict):
            for entry in folder_map.values():
                library_path = entry.get("path") if isinstance(entry, dict) else entry
                if isinstance(library_path, str) and library_path.startswith("/"):
                    paths.append(Path(library_path))
        for path in paths:
            try:
                resolved = path.resolve(strict=True)
                if not (resolved / "steamapps").is_dir():
                    continue
                identity = os.path.normcase(str(resolved))
                if identity in seen:
                    continue
                seen.add(identity)
                libraries.append(resolved)
            except OSError:
                continue
    return libraries


def _app_state(manifest: Path) -> dict[str, Any] | None:
    parsed = _read_vdf(manifest)
    state = parsed.get("AppState") if parsed else None
    return state if isinstance(state, dict) else None


def _valid_game_content_directory(steamapps: Path, install_dir: str) -> bool:
    if Path(install_dir).is_absolute():
        return False
    common = steamapps / "common"
    try:
        common_root = common.resolve(strict=True)
        target = (common_root / install_dir).resolve(strict=True)
        target.relative_to(common_root)
        return target.is_dir()
    except (OSError, ValueError):
        return False


def steam_library_info(
    home: Path | None = None,
    data_home: Path | None = None,
) -> dict[str, Any]:
    """Return only locally installed Steam apps; this never starts Steam or scans the network."""
    home = home or Path.home()
    steam_roots = discover_steam_roots(home=home, data_home=data_home)
    libraries = _steam_library_roots(steam_roots)
    flatpak_root = home / ".var/app/com.valvesoftware.Steam"
    flatpak_count = 0
    for root in steam_roots:
        try:
            root.relative_to(flatpak_root.resolve(strict=False))
            flatpak_count += 1
        except ValueError:
            pass
    if not steam_roots:
        steam_client = "Não detectado"
    elif flatpak_count == len(steam_roots):
        steam_client = "Flatpak"
    elif flatpak_count:
        steam_client = "Nativo e Flatpak"
    else:
        steam_client = "Nativo"
    games_by_id: dict[str, dict[str, str]] = {}
    for library in libraries:
        steamapps = library / "steamapps"
        try:
            manifests = sorted(steamapps.glob("appmanifest_*.acf"))
        except OSError:
            continue
        for manifest in manifests:
            filename_match = _APP_MANIFEST.fullmatch(manifest.name)
            if not filename_match:
                continue
            app_state = _app_state(manifest)
            if not app_state:
                continue
            app_id = filename_match.group(1)
            if str(app_state.get("appid", "")) != app_id:
                continue
            name = app_state.get("name")
            install_dir = app_state.get("installdir")
            try:
                fully_installed = bool(int(str(app_state.get("StateFlags", "0"))) & 4)
            except ValueError:
                fully_installed = False
            if not isinstance(name, str) or not name.strip() or not isinstance(install_dir, str):
                continue
            safe_name = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", name).strip()
            if not safe_name:
                continue
            normalized_name = safe_name.casefold()
            if "steam linux runtime" in normalized_name or "steamworks common redistributables" in normalized_name:
                continue
            if not fully_installed or not install_dir.strip():
                continue
            if not _valid_game_content_directory(steamapps, install_dir):
                continue
            games_by_id[app_id] = {
                "app_id": app_id,
                "name": safe_name,
            }

    games = sorted(games_by_id.values(), key=lambda game: (game["name"].casefold(), game["app_id"]))
    if not steam_roots:
        state = "not-installed"
        message = "Nenhuma instalação local do Steam foi encontrada."
    elif not games:
        state = "empty"
        message = "Steam encontrado; nenhum jogo completamente instalado foi localizado nas bibliotecas configuradas."
    else:
        state = "ready"
        message = f"{len(games)} jogo(s) instalado(s) em {len(libraries)} biblioteca(s) Steam local(is)."
    return {
        "state": state,
        "message": message,
        "games": games,
        "steam_client": steam_client,
        "library_count": len(libraries),
        "steam_installation_count": len(steam_roots),
        "source": "manifestos locais do Steam",
    }


def steam_game_uri(app_id: str | int) -> str:
    value = str(app_id)
    if not value.isascii() or not value.isdigit() or not 0 < len(value) <= 20 or int(value) <= 0:
        raise ValueError("ID de aplicativo Steam inválido.")
    return f"steam://rungameid/{value}"


def launch_steam_game(app_id: str | int) -> dict[str, Any]:
    """Ask the desktop URI handler to open a selected Steam app, without shell execution."""
    try:
        uri = steam_game_uri(app_id)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    opener = shutil.which("xdg-open")
    if not opener:
        return {"ok": False, "message": "O comando xdg-open não está disponível para abrir o jogo no Steam."}
    try:
        subprocess.Popen(
            [opener, uri],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        return {"ok": False, "message": f"Não foi possível encaminhar a abertura ao Steam: {exc}"}
    return {"ok": True, "message": "Solicitação de abertura enviada ao Steam."}


STEAM_LAUNCH_OPTIONS = {
    "MangoHud (FPS e métricas no jogo)": "mangohud %command%",
    "GameMode (perfil de desempenho durante o jogo)": "gamemoderun %command%",
    "MangoHud + GameMode": "mangohud gamemoderun %command%",
}


def gaming_tool_status() -> list[dict[str, str | bool]]:
    return [
        {
            "name": "MangoHud",
            "command": "mangohud",
            "package": "mangohud",
            "installed": shutil.which("mangohud") is not None,
        },
        {
            "name": "GameMode",
            "command": "gamemoderun",
            "package": "gamemode",
            "installed": shutil.which("gamemoderun") is not None,
        },
    ]
