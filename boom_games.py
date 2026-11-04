"""Steam library discovery and opt-in game launching for BooM."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlencode


MAX_VDF_BYTES = 4 * 1024 * 1024
_VDF_TOKEN = re.compile(r'//[^\r\n]*|"(?:\\.|[^"\\])*"|[{}]|[^\s{}"]+')
_APP_MANIFEST = re.compile(r"appmanifest_(\d+)\.acf\Z")
_HEROIC_APP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")
_STEAM_ROOT_SUFFIXES = (
    Path(".steam/steam"),
    Path(".steam/root"),
    Path(".local/share/Steam"),
    Path(".var/app/com.valvesoftware.Steam/.local/share/Steam"),
)
_LUTRIS_FLATPAK_APP_ID = "net.lutris.Lutris"
_HEROIC_FLATPAK_APP_ID = "com.heroicgameslauncher.hgl"
_BOTTLES_FLATPAK_APP_ID = "com.usebottles.bottles"
_HEROIC_JSON_MAX_BYTES = 32 * 1024 * 1024
_BOTTLES_CONFIG_MAX_BYTES = 4 * 1024 * 1024
_BOTTLES_CLI_TIMEOUT = 5
_HEROIC_STORES = {
    "legendary": {
        "label": "Epic",
        "cache": "store_cache/legendary_library.json",
        "cache_key": "library",
        "installed": "legendaryConfig/legendary/installed.json",
        "installed_format": "mapping",
    },
    "gog": {
        "label": "GOG",
        "cache": "store_cache/gog_library.json",
        "cache_key": "games",
        "installed": "gog_store/installed.json",
        "installed_format": "store-list",
    },
    "nile": {
        "label": "Amazon",
        "cache": "store_cache/nile_library.json",
        "cache_key": "library",
        "installed": "nile_config/nile/installed.json",
        "installed_format": "array",
    },
    "sideload": {
        "label": "Manual",
        "cache": "sideload_apps/library.json",
        "cache_key": "games",
        "installed": "",
        "installed_format": "library",
    },
}


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


def discover_lutris_databases(
    home: Path | None = None,
    data_home: Path | None = None,
) -> list[Path]:
    """Find native and Flatpak Lutris databases without creating or modifying files."""
    home = home or Path.home()
    if data_home is None:
        configured = os.environ.get("XDG_DATA_HOME")
        configured_path = Path(configured).expanduser() if configured else None
        data_home = configured_path if configured_path and configured_path.is_absolute() else home / ".local/share"
    candidates = [
        data_home / "lutris/pga.db",
        home / f".var/app/{_LUTRIS_FLATPAK_APP_ID}/data/lutris/pga.db",
    ]
    databases: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            if not resolved.is_file():
                continue
            identity = os.path.normcase(str(resolved))
            if identity not in seen:
                seen.add(identity)
                databases.append(resolved)
        except OSError:
            continue
    return databases


def _read_lutris_database(path: Path) -> list[dict[str, str]]:
    """Read installed Lutris games from SQLite in URI read-only/query-only mode."""
    uri = f"{path.as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=0.2) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        columns = {
            str(row["name"]).casefold()
            for row in connection.execute('PRAGMA table_info("games")')
        }
        required = {"id", "name", "slug", "installed", "configpath"}
        if not required.issubset(columns):
            raise ValueError("A base do Lutris não contém o esquema esperado.")
        runner = '"runner"' if "runner" in columns else "NULL AS runner"
        query = f'SELECT "id", "name", "slug", "installed", "configpath", {runner} FROM "games"'
        games: list[dict[str, str]] = []
        for row in connection.execute(query):
            try:
                game_id = str(row["id"])
                installed = int(row["installed"]) == 1
            except (TypeError, ValueError, OverflowError):
                continue
            name = row["name"]
            slug = row["slug"]
            config_id = row["configpath"]
            if (
                not game_id.isascii()
                or not game_id.isdecimal()
                or not 0 < int(game_id) <= 2**63 - 1
                or not installed
                or not isinstance(config_id, str)
                or not config_id.strip()
                or not isinstance(name, str)
                or not isinstance(slug, str)
            ):
                continue
            safe_name = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", name).strip()
            if not safe_name:
                continue
            games.append({
                "game_id": game_id,
                "name": safe_name,
                "slug": slug,
                "runner": str(row["runner"] or ""),
            })
    return games


def lutris_library_info(
    home: Path | None = None,
    data_home: Path | None = None,
) -> dict[str, Any]:
    """Return installed Lutris games from local database(s), never contacting Lutris."""
    databases = discover_lutris_databases(home=home, data_home=data_home)
    if not databases:
        return {
            "state": "not-installed",
            "message": "Nenhuma base de jogos Lutris local foi encontrada.",
            "games": [],
            "database_count": 0,
            "source": "base(s) de dados local(is) do Lutris",
        }
    games_by_id: dict[tuple[str, str], dict[str, str]] = {}
    readable = 0
    errors = 0
    for database in databases:
        try:
            games = _read_lutris_database(database)
        except (OSError, sqlite3.Error, ValueError):
            errors += 1
            continue
        readable += 1
        for game in games:
            games_by_id[(str(database), game["game_id"])] = game
    games = sorted(
        games_by_id.values(),
        key=lambda game: (game["name"].casefold(), game["game_id"]),
    )
    if games:
        state = "partial" if errors else "ready"
        message = f"{len(games)} jogo(s) instalado(s) em {readable} base(s) Lutris local(is)."
        if errors:
            message += f" {errors} base(s) não puderam ser lidas."
    elif errors and not readable:
        state = "error"
        message = "As bases locais do Lutris foram encontradas, mas não puderam ser lidas."
    else:
        state = "empty"
        message = "Lutris encontrado; nenhum jogo instalado com configuração local foi localizado."
    return {
        "state": state,
        "message": message,
        "games": games,
        "database_count": readable,
        "source": "base(s) de dados local(is) do Lutris",
    }


def lutris_game_uri(game_id: str | int) -> str:
    value = str(game_id)
    if not value.isascii() or not value.isdecimal() or not 0 < int(value) <= 2**63 - 1:
        raise ValueError("ID de jogo Lutris inválido.")
    return f"lutris:rungameid/{value}"


def launch_lutris_game(game_id: str | int) -> dict[str, Any]:
    """Forward an explicitly selected installed game to Lutris' registered URI handler."""
    try:
        uri = lutris_game_uri(game_id)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    opener = shutil.which("xdg-open")
    if not opener:
        return {"ok": False, "message": "O comando xdg-open não está disponível para abrir o jogo no Lutris."}
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
        return {"ok": False, "message": f"Não foi possível encaminhar a abertura ao Lutris: {exc}"}
    return {"ok": True, "message": "Solicitação de abertura enviada ao Lutris."}


def discover_heroic_config_dirs(
    home: Path | None = None,
    config_home: Path | None = None,
) -> list[Path]:
    """Find native and Flatpak Heroic data directories without creating files."""
    home = home or Path.home()
    if config_home is None:
        configured = os.environ.get("XDG_CONFIG_HOME")
        configured_path = Path(configured).expanduser() if configured else None
        config_home = (
            configured_path
            if configured_path and configured_path.is_absolute()
            else home / ".config"
        )
    candidates = [
        config_home / "heroic",
        home / f".var/app/{_HEROIC_FLATPAK_APP_ID}/config/heroic",
    ]
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            if not resolved.is_dir():
                continue
            identity = os.path.normcase(str(resolved))
            if identity not in seen:
                seen.add(identity)
                roots.append(resolved)
        except OSError:
            continue
    return roots


def _read_heroic_json(path: Path) -> Any:
    if path.stat().st_size > _HEROIC_JSON_MAX_BYTES:
        raise ValueError("Arquivo de biblioteca Heroic maior que o limite de leitura.")
    return json.loads(path.read_text(encoding="utf-8"))


def _heroic_installed_index(path: Path, data_format: str) -> dict[str, dict[str, Any]]:
    payload = _read_heroic_json(path)
    if data_format == "mapping":
        if not isinstance(payload, dict):
            raise ValueError("Índice Epic/Heroic inválido.")
        return {
            str(app_name): metadata
            for app_name, metadata in payload.items()
            if isinstance(metadata, dict)
        }
    if data_format == "store-list":
        entries = payload.get("installed") if isinstance(payload, dict) else None
    else:
        entries = payload
    if not isinstance(entries, list):
        raise ValueError("Índice de jogos instalados Heroic inválido.")
    installed: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        app_name = entry.get("appName") or entry.get("app_name") or entry.get("id")
        if isinstance(app_name, str) and app_name:
            installed[app_name] = entry
    return installed


def _heroic_install_exists(
    game: dict[str, Any],
    installed_info: dict[str, Any] | None,
    runner: str,
) -> bool:
    install = game.get("install")
    install = install if isinstance(install, dict) else {}
    installed_info = installed_info if isinstance(installed_info, dict) else {}
    possible_paths = [
        install.get("install_path"),
        installed_info.get("install_path"),
        installed_info.get("path"),
    ]
    if runner == "sideload":
        possible_paths.insert(0, install.get("executable"))
    for value in possible_paths:
        if not isinstance(value, str) or not value.strip():
            continue
        candidate = Path(value).expanduser()
        if candidate.is_absolute() and candidate.exists():
            return True
    return False


def _heroic_library_games(config_dir: Path, runner: str) -> tuple[list[dict[str, str]], bool, int]:
    store = _HEROIC_STORES[runner]
    cache_path = config_dir / str(store["cache"])
    if not cache_path.is_file():
        return [], False, 0
    try:
        payload = _read_heroic_json(cache_path)
        entries = payload.get(str(store["cache_key"])) if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise ValueError("Cache de biblioteca Heroic sem a lista esperada.")
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return [], True, 1

    installed_index: dict[str, dict[str, Any]] = {}
    if runner != "sideload":
        installed_path = config_dir / str(store["installed"])
        if not installed_path.is_file():
            has_installed_cache_entries = any(
                isinstance(entry, dict) and entry.get("is_installed") is True
                for entry in entries
            )
            return [], True, int(has_installed_cache_entries)
        try:
            installed_index = _heroic_installed_index(
                installed_path,
                str(store["installed_format"]),
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            return [], True, 1

    games: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("is_installed") is not True:
            continue
        if entry.get("runner") != runner:
            continue
        install = entry.get("install")
        if isinstance(install, dict) and install.get("is_dlc") is True:
            continue
        app_name = entry.get("app_name")
        title = entry.get("title")
        if not isinstance(app_name, str) or not _HEROIC_APP_NAME.fullmatch(app_name):
            continue
        installed_info = None if runner == "sideload" else installed_index.get(app_name)
        if runner != "sideload" and installed_info is None:
            continue
        if not _heroic_install_exists(entry, installed_info, runner):
            continue
        if not isinstance(title, str):
            continue
        safe_name = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", title).strip()
        if not safe_name:
            continue
        games.append({
            "game_id": app_name,
            "name": safe_name,
            "runner": runner,
            "store": str(store["label"]),
        })
    return games, True, 0


def heroic_library_info(
    home: Path | None = None,
    config_home: Path | None = None,
) -> dict[str, Any]:
    """Read installed Heroic games from its local store caches and install indexes only."""
    roots = discover_heroic_config_dirs(home=home, config_home=config_home)
    if not roots:
        return {
            "state": "not-installed",
            "message": "Nenhuma biblioteca local do Heroic foi encontrada.",
            "games": [],
            "config_count": 0,
            "library_count": 0,
            "source": "caches locais do Heroic",
        }

    # The Heroic URI cannot select a config root, so one row per runner/app ID
    # is actionable. Config discovery is ordered native/XDG before Flatpak.
    games_by_id: dict[tuple[str, str], dict[str, str]] = {}
    libraries = 0
    errors = 0
    for config_dir in roots:
        for runner in _HEROIC_STORES:
            games, found, store_errors = _heroic_library_games(config_dir, runner)
            libraries += int(found)
            errors += store_errors
            for game in games:
                games_by_id.setdefault((game["runner"], game["game_id"]), game)

    games = sorted(
        games_by_id.values(),
        key=lambda game: (game["name"].casefold(), game["store"], game["game_id"]),
    )
    if errors:
        state = "partial" if games else "error"
        message = (
            f"{len(games)} jogo(s) Heroic lido(s); dados de {errors} biblioteca(s) "
            "não puderam ser confirmados."
        )
    elif games:
        state = "ready"
        message = f"{len(games)} jogo(s) Heroic instalado(s) em {libraries} biblioteca(s) local(is)."
    else:
        state = "empty"
        message = "Heroic encontrado; nenhum jogo instalado com arquivos locais válidos foi localizado."
    return {
        "state": state,
        "message": message,
        "games": games,
        "config_count": len(roots),
        "library_count": libraries,
        "source": "caches e índices locais de instalação do Heroic",
    }


def heroic_game_uri(app_name: str, runner: str) -> str:
    if not isinstance(runner, str) or runner not in _HEROIC_STORES:
        raise ValueError("Loja Heroic inválida.")
    if not isinstance(app_name, str) or not _HEROIC_APP_NAME.fullmatch(app_name):
        raise ValueError("ID de jogo Heroic inválido.")
    query = urlencode({"appName": app_name, "runner": runner})
    return f"heroic://launch?{query}"


def launch_heroic_game(app_name: str, runner: str) -> dict[str, Any]:
    """Forward an explicitly selected game to Heroic's registered launch URI."""
    try:
        uri = heroic_game_uri(app_name, runner)
    except ValueError as exc:
        return {"ok": False, "message": str(exc)}
    opener = shutil.which("xdg-open")
    if not opener:
        return {"ok": False, "message": "O comando xdg-open não está disponível para abrir o jogo no Heroic."}
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
        return {"ok": False, "message": f"Não foi possível encaminhar a abertura ao Heroic: {exc}"}
    return {"ok": True, "message": "Solicitação de abertura enviada ao Heroic."}


def _bottles_cli_backends(
    home: Path | None = None,
    data_home: Path | None = None,
) -> tuple[list[dict[str, str]], int]:
    home = home or Path.home()
    if data_home is None:
        configured = os.environ.get("XDG_DATA_HOME")
        configured_path = Path(configured).expanduser() if configured else None
        data_home = (
            configured_path
            if configured_path and configured_path.is_absolute()
            else home / ".local/share"
        )
    backends: list[dict[str, str]] = []
    native_cli = shutil.which("bottles-cli")
    if native_cli:
        backends.append({
            "kind": "native",
            "label": "Nativo",
            "prefix": native_cli,
            "root": str(data_home / "bottles/bottles"),
        })

    flatpak = shutil.which("flatpak")
    errors = 0
    if flatpak:
        try:
            result = subprocess.run(
                [flatpak, "list", "--app", "--columns=application"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_BOTTLES_CLI_TIMEOUT,
                check=False,
                close_fds=True,
            )
        except (OSError, subprocess.TimeoutExpired):
            errors = 1
        else:
            if result.returncode != 0:
                errors = 1
            elif _BOTTLES_FLATPAK_APP_ID in result.stdout.splitlines():
                backends.append({
                    "kind": "flatpak",
                    "label": "Flatpak",
                    "prefix": flatpak,
                    "root": str(
                        home / ".var/app/com.usebottles.bottles/data/bottles/bottles"
                    ),
                })
    return backends, errors


def _read_bottles_yaml(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > _BOTTLES_CONFIG_MAX_BYTES:
            raise ValueError("Um arquivo de configuração do Bottles excedeu 4 MiB.")
        import yaml
    except ImportError as exc:
        raise ValueError(
            "A leitura segura do Bottles requer PyYAML (pacote python3-yaml)."
        ) from exc
    except OSError as exc:
        raise ValueError("Um arquivo local do Bottles não pôde ser lido.") from exc
    try:
        with path.open("r", encoding="utf-8") as stream:
            payload = yaml.safe_load(stream)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError("Um arquivo YAML do Bottles está inválido ou ilegível.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Um arquivo YAML do Bottles tem formato desconhecido.")
    return payload


def _bottles_text(value: Any) -> str | None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 512
        or any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F for char in value)
    ):
        return None
    return value


def _bottle_config_path(bottle_dir: Path) -> Path:
    placeholder_path = bottle_dir / "placeholder.yml"
    if placeholder_path.is_file():
        placeholder = _read_bottles_yaml(placeholder_path)
        custom_path = placeholder.get("Path")
        if not isinstance(custom_path, str) or not custom_path.strip():
            raise ValueError("O placeholder do Bottles não informa um caminho válido.")
        target = Path(custom_path).expanduser()
        if not target.is_absolute():
            raise ValueError("O caminho customizado do Bottles não é absoluto.")
        return target / "bottle.yml"
    return bottle_dir / "bottle.yml"


def bottles_library_info(
    home: Path | None = None,
    data_home: Path | None = None,
) -> dict[str, Any]:
    """Read Bottles bottle.yml files without invoking its mutating inventory CLI."""
    home = home or Path.home()
    backends, errors = _bottles_cli_backends(home=home, data_home=data_home)
    if not backends and not errors:
        return {
            "state": "not-installed",
            "message": "Bottles não está instalado (CLI nativa ou Flatpak).",
            "games": [],
            "bottle_count": 0,
            "backend_count": 0,
            "source": "configuração local do Bottles",
        }

    programs_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    bottle_count = 0
    ambiguous = 0
    for backend in backends:
        root = Path(backend["root"])
        try:
            bottle_dirs = sorted(
                (
                    path for path in root.iterdir()
                    if path.is_dir() and not path.is_symlink()
                ),
                key=lambda path: path.name.casefold(),
            )
        except FileNotFoundError:
            bottle_dirs = []
        except OSError:
            errors += 1
            continue
        configs: list[tuple[str, dict[str, Any]]] = []
        for bottle_dir in bottle_dirs:
            try:
                config = _read_bottles_yaml(_bottle_config_path(bottle_dir))
            except ValueError:
                errors += 1
                continue
            bottle_name = _bottles_text(config.get("Name"))
            external_programs = config.get("External_Programs", {})
            if bottle_name is None or not isinstance(external_programs, dict):
                errors += 1
                continue
            configs.append((bottle_name, external_programs))
        bottle_count += len(configs)

        bottle_name_counts: dict[str, int] = {}
        for bottle_name, _programs in configs:
            bottle_name_counts[bottle_name] = bottle_name_counts.get(bottle_name, 0) + 1

        backend_programs: list[tuple[str, dict[str, str]]] = []
        for bottle_name, external_programs in configs:
            if bottle_name_counts[bottle_name] > 1:
                ambiguous += 1
                continue
            for program in external_programs.values():
                if not isinstance(program, dict) or program.get("removed"):
                    continue
                name = _bottles_text(program.get("name"))
                if name is None:
                    continue
                backend_programs.append((bottle_name, {"name": name}))

        names_per_bottle: dict[tuple[str, str], list[dict[str, str]]] = {}
        for bottle_name, program in backend_programs:
            names_per_bottle.setdefault((bottle_name, program["name"]), []).append(program)
        for (bottle_name, program_name), entries in names_per_bottle.items():
            if len(entries) > 1:
                ambiguous += len(entries)
                continue
            for entry in entries:
                key = (backend["kind"], bottle_name, program_name)
                programs_by_key[key] = {
                    "name": program_name,
                    "game_id": program_name,
                    "bottle": bottle_name,
                    "runner": bottle_name,
                    "backend": backend["kind"],
                    "store": bottle_name,
                    "program_id": "",
                    "use_program_id": False,
                }

    games = sorted(
        programs_by_key.values(),
        key=lambda game: (
            game["name"].casefold(), game["backend"], game["bottle"].casefold(), game["program_id"],
        ),
    )
    if errors or ambiguous:
        state = "partial" if games else "error"
    elif games:
        state = "ready"
    else:
        state = "empty"
    if games:
        message = f"{len(games)} programa(s) registrado(s) em {bottle_count} ambiente(s) Bottles local(is)."
        if errors:
            message += f" {errors} erro(s) ao verificar instalações ou ler configurações locais."
        if ambiguous:
            message += f" {ambiguous} entrada(s) com nomes duplicados de bottle/programa foram ocultadas."
    elif errors:
        message = "Não foi possível verificar as instalações Bottles ou ler suas configurações locais."
    elif ambiguous:
        message = "Há nomes de bottles ou programas duplicados que não podem ser abertos com segurança."
    else:
        message = "Bottles encontrado; nenhum programa registrado foi localizado."
    return {
        "state": state,
        "message": message,
        "games": games,
        "bottle_count": bottle_count,
        "backend_count": len(backends),
        "source": "arquivos bottle.yml locais do Bottles (leitura somente)",
    }


def launch_bottles_program(
    program_name: str,
    bottle_name: str,
    backend: str,
    program_id: str = "",
    use_program_id: bool = False,
) -> dict[str, Any]:
    """Launch one selected Bottles program through the Bottles CLI without a shell."""
    safe_program = _bottles_text(program_name)
    safe_bottle = _bottles_text(bottle_name)
    safe_id = _bottles_text(program_id) if use_program_id else None
    if safe_program is None or safe_bottle is None or (use_program_id and safe_id is None):
        return {"ok": False, "message": "Programa ou bottle do Bottles inválido."}
    if backend == "native":
        cli = shutil.which("bottles-cli")
        if not cli:
            return {"ok": False, "message": "A CLI nativa bottles-cli não está disponível."}
        command = [cli]
    elif backend == "flatpak":
        flatpak = shutil.which("flatpak")
        if not flatpak:
            return {"ok": False, "message": "O comando flatpak não está disponível para abrir Bottles."}
        command = [flatpak, "run", "--command=bottles-cli", _BOTTLES_FLATPAK_APP_ID]
    else:
        return {"ok": False, "message": "Origem de instalação Bottles inválida."}

    program_argument = (
        f"--program-id={safe_id}" if use_program_id else f"--program={safe_program}"
    )
    try:
        subprocess.Popen(
            [*command, "run", f"--bottle={safe_bottle}", program_argument],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        return {"ok": False, "message": f"Não foi possível encaminhar a abertura ao Bottles: {exc}"}
    return {"ok": True, "message": "Solicitação de abertura enviada ao Bottles."}


def installed_game_list(
    steam: dict[str, Any],
    lutris: dict[str, Any],
    heroic: dict[str, Any] | None = None,
    bottles: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    games: list[dict[str, Any]] = []
    for game in steam.get("games", []):
        app_id = str(game.get("app_id", ""))
        name = game.get("name")
        try:
            steam_game_uri(app_id)
        except ValueError:
            continue
        if not isinstance(name, str) or not name.strip():
            continue
        games.append({"name": name, "launcher": "Steam", "store": "", "game_id": app_id, "runner": ""})
    for game in lutris.get("games", []):
        game_id = str(game.get("game_id", ""))
        name = game.get("name")
        try:
            lutris_game_uri(game_id)
        except ValueError:
            continue
        if not isinstance(name, str) or not name.strip():
            continue
        games.append({"name": name, "launcher": "Lutris", "store": "", "game_id": game_id, "runner": ""})
    for game in (heroic or {}).get("games", []):
        app_name = game.get("game_id")
        runner = game.get("runner")
        name = game.get("name")
        store = game.get("store")
        try:
            heroic_game_uri(app_name, runner)
        except (TypeError, ValueError):
            continue
        if not isinstance(name, str) or not name.strip() or not isinstance(store, str):
            continue
        games.append({
            "name": name,
            "launcher": "Heroic",
            "store": store,
            "game_id": app_name,
            "runner": runner,
        })
    for program in (bottles or {}).get("games", []):
        name = _bottles_text(program.get("name"))
        bottle_name = _bottles_text(program.get("bottle"))
        backend = program.get("backend")
        program_id = _bottles_text(program.get("program_id")) or ""
        use_program_id = program.get("use_program_id") is True
        if name is None or bottle_name is None or backend not in {"native", "flatpak"}:
            continue
        if use_program_id and not program_id:
            continue
        games.append({
            "name": name,
            "launcher": "Bottles",
            "store": f"{bottle_name} · {backend}",
            "game_id": name,
            "runner": bottle_name,
            "backend": backend,
            "program_id": program_id,
            "use_program_id": use_program_id,
        })
    return sorted(
        games,
        key=lambda game: (game["name"].casefold(), game["launcher"], game["store"], game["game_id"]),
    )


def launch_installed_game(
    launcher: str,
    game_id: str | int,
    runner: str | None = None,
    backend: str | None = None,
    program_id: str = "",
    use_program_id: bool = False,
) -> dict[str, Any]:
    if launcher == "Steam":
        return launch_steam_game(game_id)
    if launcher == "Lutris":
        return launch_lutris_game(game_id)
    if launcher == "Heroic" and isinstance(game_id, str) and isinstance(runner, str):
        return launch_heroic_game(game_id, runner)
    if launcher == "Bottles" and isinstance(game_id, str) and isinstance(runner, str) and isinstance(backend, str):
        return launch_bottles_program(game_id, runner, backend, program_id, use_program_id)
    return {"ok": False, "message": "Este lançador de jogos não é compatível."}


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
