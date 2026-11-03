import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import boom_games


class SteamVdfTests(unittest.TestCase):
    def test_parser_reads_nested_objects_comments_and_escaped_quotes(self):
        parsed = boom_games.parse_vdf(
            '"libraryfolders" { // comment\n'
            '  "0" { "path" "/home/player/Steam" "label" "My \\\"Games\\\"" }\n'
            '  "1" "/mnt/games/SteamLibrary"\n'
            '}\n'
        )
        self.assertEqual(parsed["libraryfolders"]["0"]["path"], "/home/player/Steam")
        self.assertEqual(parsed["libraryfolders"]["0"]["label"], 'My "Games"')
        self.assertEqual(parsed["libraryfolders"]["1"], "/mnt/games/SteamLibrary")

    def test_parser_rejects_unclosed_or_malformed_objects(self):
        deeply_nested = '"a" {' * 66 + '}' * 66
        for payload in ('"root" { "key" "value"', '"root" { "key" }', '}', deeply_nested):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                boom_games.parse_vdf(payload)

    def test_library_discovery_reads_only_complete_local_manifests(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            steam_root = home / ".steam/steam"
            extra_library = home / "Games/SteamLibrary"
            for root in (steam_root, extra_library):
                (root / "steamapps/common").mkdir(parents=True)
            (steam_root / "steamapps/libraryfolders.vdf").write_text(
                '"libraryfolders" { "0" { "path" "' + str(steam_root) + '" } '
                '"1" { "path" "' + str(extra_library) + '" } }',
                encoding="utf-8",
            )
            manifests = (
                (steam_root, "10", "A & B", "Game A", "4"),
                (extra_library, "20", "Game B", "Game B", "4"),
                (steam_root, "30", "Partial", "Partial", "1026"),
                (steam_root, "40", "Steam Linux Runtime - Soldier", "steam-runtime", "4"),
                (steam_root, "50", "Outside Library", "..", "4"),
            )
            for root, app_id, name, install_dir, flags in manifests:
                content = root / "steamapps/common" / install_dir
                if install_dir != "..":
                    content.mkdir()
                (root / f"steamapps/appmanifest_{app_id}.acf").write_text(
                    '"AppState" { '
                    f'"appid" "{app_id}" "name" "{name}" '
                    f'"installdir" "{install_dir}" "StateFlags" "{flags}" '
                    '}',
                    encoding="utf-8",
                )
            result = boom_games.steam_library_info(home=home, data_home=home / "xdg-data")

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["library_count"], 2)
        self.assertEqual([game["app_id"] for game in result["games"]], ["10", "20"])
        self.assertEqual(result["games"][0]["name"], "A & B")
        self.assertNotIn("library", result["games"][0])

    def test_library_without_steam_has_explicit_empty_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            result = boom_games.steam_library_info(home=home, data_home=home / "xdg-data")
        self.assertEqual(result["state"], "not-installed")
        self.assertEqual(result["games"], [])
        self.assertIn("foi encontrada", result["message"])
        self.assertEqual(result["steam_client"], "Não detectado")

    def test_steam_flatpak_installation_is_identified(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            root = home / ".var/app/com.valvesoftware.Steam/.local/share/Steam"
            (root / "steamapps").mkdir(parents=True)
            result = boom_games.steam_library_info(home=home, data_home=home / "xdg-data")
        self.assertEqual(result["steam_client"], "Flatpak")

    def test_uri_requires_a_positive_decimal_steam_app_id(self):
        self.assertEqual(boom_games.steam_game_uri(570), "steam://rungameid/570")
        for invalid in (0, -1, "abc", "1/2", "", "١٢٣"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                boom_games.steam_game_uri(invalid)

    def test_launch_hands_fixed_uri_to_xdg_open_without_a_shell(self):
        process = object()
        with patch("boom_games.shutil.which", return_value="/usr/bin/xdg-open"), patch(
            "boom_games.subprocess.Popen", return_value=process,
        ) as popen:
            result = boom_games.launch_steam_game("570")
        self.assertTrue(result["ok"])
        self.assertEqual(popen.call_args.args[0], ["/usr/bin/xdg-open", "steam://rungameid/570"])
        self.assertEqual(popen.call_args.kwargs["start_new_session"], True)
        self.assertNotIn("shell", popen.call_args.kwargs)

    def test_launch_reports_missing_desktop_uri_handler(self):
        with patch("boom_games.shutil.which", return_value=None):
            result = boom_games.launch_steam_game("570")
        self.assertFalse(result["ok"])
        self.assertIn("xdg-open", result["message"])

    def test_optional_game_tools_have_copyable_steam_prefixes(self):
        self.assertEqual(boom_games.STEAM_LAUNCH_OPTIONS["MangoHud (FPS e métricas no jogo)"], "mangohud %command%")
        self.assertEqual(boom_games.STEAM_LAUNCH_OPTIONS["GameMode (perfil de desempenho durante o jogo)"], "gamemoderun %command%")
        with patch("boom_games.shutil.which", side_effect=lambda name: f"/usr/bin/{name}"):
            status = boom_games.gaming_tool_status()
        self.assertTrue(all(item["installed"] for item in status))


class LutrisLibraryTests(unittest.TestCase):
    @staticmethod
    def create_database(path: Path, rows: list[tuple[object, ...]] | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE games (id INTEGER PRIMARY KEY, name TEXT, slug TEXT, runner TEXT, "
                "installed INTEGER, configpath TEXT, directory TEXT)"
            )
            connection.executemany(
                "INSERT INTO games(id, name, slug, runner, installed, configpath, directory) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows or [],
            )

    def test_discovery_reads_only_installed_games_from_native_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            database = home / ".local/share/lutris/pga.db"
            self.create_database(database, [
                (10, "Game A", "game-a", "wine", 1, "game-a-10", "/games/a"),
                (20, "Partial Download", "partial", "wine", 0, "partial-20", "/games/partial"),
                (30, "No Configuration", "no-config", "wine", 1, "", "/games/no-config"),
            ])
            result = boom_games.lutris_library_info(home=home, data_home=home / ".local/share")
            with sqlite3.connect(database) as connection:
                rows_after = connection.execute("SELECT id, installed FROM games ORDER BY id").fetchall()

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["database_count"], 1)
        self.assertEqual(result["games"], [{
            "game_id": "10", "name": "Game A", "slug": "game-a", "runner": "wine",
        }])
        self.assertEqual(rows_after, [(10, 1), (20, 0), (30, 1)])

    def test_discovery_supports_flatpak_path_and_reports_no_games(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            database = home / ".var/app/net.lutris.Lutris/data/lutris/pga.db"
            self.create_database(database)
            result = boom_games.lutris_library_info(home=home, data_home=home / ".local/share")
        self.assertEqual(result["state"], "empty")
        self.assertEqual(result["games"], [])
        self.assertIn("nenhum jogo instalado", result["message"])

    def test_discovery_sanitizes_display_names_and_handles_invalid_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            database = home / ".local/share/lutris/pga.db"
            self.create_database(database, [
                (1, "Game\nName", "game", "wine", 1, "game-1", "/games/game"),
            ])
            result = boom_games.lutris_library_info(home=home, data_home=home / ".local/share")
            malformed = home / ".var/app/net.lutris.Lutris/data/lutris/pga.db"
            malformed.parent.mkdir(parents=True)
            malformed.write_text("not sqlite", encoding="utf-8")
            partial = boom_games.lutris_library_info(home=home, data_home=home / ".local/share")
        self.assertEqual(result["games"][0]["name"], "GameName")
        self.assertEqual(partial["state"], "partial")
        self.assertIn("não puderam ser lidas", partial["message"])

    def test_lutris_uri_validates_numeric_game_id(self):
        self.assertEqual(boom_games.lutris_game_uri(123), "lutris:rungameid/123")
        for invalid in (0, -1, "abc", "1/2", "١٢٣", str(2**63)):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                boom_games.lutris_game_uri(invalid)

    def test_launch_forwards_only_the_validated_lutris_uri_without_shell(self):
        with patch("boom_games.shutil.which", return_value="/usr/bin/xdg-open"), patch(
            "boom_games.subprocess.Popen", return_value=object(),
        ) as popen:
            result = boom_games.launch_lutris_game("123")
        self.assertTrue(result["ok"])
        self.assertEqual(popen.call_args.args[0], ["/usr/bin/xdg-open", "lutris:rungameid/123"])
        self.assertNotIn("shell", popen.call_args.kwargs)
        self.assertFalse(boom_games.launch_lutris_game("1/2")["ok"])

    def test_combined_game_list_sorts_launchers_and_uses_fixed_dispatch(self):
        games = boom_games.installed_game_list(
            {"games": [{"name": "Steam Game", "app_id": "570"}, {"name": "Invalid", "app_id": "x"}]},
            {"games": [{"name": "Lutris Game", "game_id": "22"}]},
            {"games": [{
                "name": "Heroic Game", "game_id": "epic-id", "runner": "legendary", "store": "Epic",
            }]},
        )
        self.assertEqual(games, [
            {"name": "Heroic Game", "launcher": "Heroic", "store": "Epic", "game_id": "epic-id", "runner": "legendary"},
            {"name": "Lutris Game", "launcher": "Lutris", "store": "", "game_id": "22", "runner": ""},
            {"name": "Steam Game", "launcher": "Steam", "store": "", "game_id": "570", "runner": ""},
        ])
        with patch("boom_games.launch_lutris_game", return_value={"ok": True, "message": "ok"}) as launch:
            self.assertTrue(boom_games.launch_installed_game("Lutris", "22")["ok"])
        launch.assert_called_once_with("22")
        with patch("boom_games.launch_heroic_game", return_value={"ok": True, "message": "ok"}) as launch:
            self.assertTrue(boom_games.launch_installed_game("Heroic", "epic-id", "legendary")["ok"])
        launch.assert_called_once_with("epic-id", "legendary")
        self.assertFalse(boom_games.launch_installed_game("Untrusted", "1")["ok"])


class HeroicLibraryTests(unittest.TestCase):
    @staticmethod
    def write_json(path: Path, payload: object) -> bytes:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        path.write_bytes(content)
        return content

    def test_native_config_discovers_installed_games_from_all_heroic_stores_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            config = home / ".config/heroic"
            epic_path = home / "Games/Epic Game"
            gog_path = home / "Games/GOG Game"
            amazon_path = home / "Games/Amazon Game"
            manual_executable = home / "Games/Manual/game.exe"
            for path in (epic_path, gog_path, amazon_path):
                path.mkdir(parents=True)
            manual_executable.parent.mkdir(parents=True)
            manual_executable.write_bytes(b"fixture")

            self.write_json(config / "store_cache/legendary_library.json", {"library": [
                {"app_name": "epic-id", "title": "Epic Game", "runner": "legendary", "is_installed": True,
                 "install": {"install_path": str(epic_path), "is_dlc": False}},
                {"app_name": "download-id", "title": "Downloading", "runner": "legendary", "is_installed": False,
                 "install": {"install_path": str(home / "missing"), "is_dlc": False}},
                {"app_name": "dlc-id", "title": "DLC", "runner": "legendary", "is_installed": True,
                 "install": {"install_path": str(epic_path), "is_dlc": True}},
            ]})
            epic_installed = config / "legendaryConfig/legendary/installed.json"
            epic_bytes = self.write_json(epic_installed, {
                "epic-id": {"app_name": "epic-id", "install_path": str(epic_path)},
                "dlc-id": {"app_name": "dlc-id", "install_path": str(epic_path)},
            })
            self.write_json(config / "store_cache/gog_library.json", {"games": [
                {"app_name": "12345", "title": "GOG Game", "runner": "gog", "is_installed": True,
                 "install": {"install_path": str(gog_path), "is_dlc": False}},
                {"app_name": "stale-id", "title": "Stale GOG", "runner": "gog", "is_installed": True,
                 "install": {"install_path": str(gog_path), "is_dlc": False}},
            ]})
            self.write_json(config / "gog_store/installed.json", {"installed": [
                {"appName": "12345", "install_path": str(gog_path)},
            ]})
            self.write_json(config / "store_cache/nile_library.json", {"library": [
                {"app_name": "amzn1.adg.product.id", "title": "Amazon Game", "runner": "nile",
                 "is_installed": True, "install": {"install_path": str(amazon_path), "is_dlc": False}},
            ]})
            self.write_json(config / "nile_config/nile/installed.json", [
                {"id": "amzn1.adg.product.id", "path": str(amazon_path)},
            ])
            self.write_json(config / "sideload_apps/library.json", {"games": [
                {"app_name": "manual-id", "title": "Manual Game", "runner": "sideload", "is_installed": True,
                 "install": {"executable": str(manual_executable)}},
                {"app_name": "missing-id", "title": "Missing Manual", "runner": "sideload", "is_installed": True,
                 "install": {"executable": str(home / "missing.exe")}},
            ]})

            result = boom_games.heroic_library_info(home=home, config_home=home / ".config")
            epic_bytes_after = epic_installed.read_bytes()

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["config_count"], 1)
        self.assertEqual(result["library_count"], 4)
        self.assertEqual(
            {(game["name"], game["runner"], game["game_id"]) for game in result["games"]},
            {
                ("Epic Game", "legendary", "epic-id"),
                ("GOG Game", "gog", "12345"),
                ("Amazon Game", "nile", "amzn1.adg.product.id"),
                ("Manual Game", "sideload", "manual-id"),
            },
        )
        self.assertEqual(epic_bytes_after, epic_bytes)

    def test_flatpak_heroic_config_and_xdg_config_home_are_discovered(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            custom = home / "custom-config/heroic"
            flatpak = home / ".var/app/com.heroicgameslauncher.hgl/config/heroic"
            custom.mkdir(parents=True)
            flatpak.mkdir(parents=True)
            roots = boom_games.discover_heroic_config_dirs(home=home, config_home=home / "custom-config")
        self.assertEqual(roots, [custom.resolve(), flatpak.resolve()])

    def test_duplicate_game_across_native_and_flatpak_configs_is_listed_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            executable = home / "Games/Manual/game.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"fixture")
            configs = [
                home / ".config/heroic",
                home / ".var/app/com.heroicgameslauncher.hgl/config/heroic",
            ]
            for config in configs:
                self.write_json(config / "sideload_apps/library.json", {"games": [
                    {"app_name": "manual-id", "title": "Manual Game", "runner": "sideload",
                     "is_installed": True, "install": {"executable": str(executable)}},
                ]})

            result = boom_games.heroic_library_info(home=home, config_home=home / ".config")

        self.assertEqual(result["config_count"], 2)
        self.assertEqual(len(result["games"]), 1)
        self.assertEqual(result["games"][0]["game_id"], "manual-id")

    def test_heroic_reports_missing_config_and_partial_corrupt_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            missing = boom_games.heroic_library_info(home=home, config_home=home / ".config")
            cache = home / ".config/heroic/store_cache/legendary_library.json"
            cache.parent.mkdir(parents=True)
            cache.write_text("not json", encoding="utf-8")
            partial = boom_games.heroic_library_info(home=home, config_home=home / ".config")
        self.assertEqual(missing["state"], "not-installed")
        self.assertEqual(partial["state"], "error")
        self.assertIn("não puderam ser confirmados", partial["message"])

    def test_heroic_uri_validates_runner_and_encodes_query(self):
        self.assertEqual(
            boom_games.heroic_game_uri("amzn1.adg.product.id", "nile"),
            "heroic://launch?appName=amzn1.adg.product.id&runner=nile",
        )
        for app_name, runner in (("", "gog"), ("app&id", "legendary"), ("game", "unknown")):
            with self.subTest(app_name=app_name, runner=runner), self.assertRaises(ValueError):
                boom_games.heroic_game_uri(app_name, runner)

    def test_heroic_launch_uses_only_the_registered_uri_and_no_shell(self):
        with patch("boom_games.shutil.which", return_value="/usr/bin/xdg-open"), patch(
            "boom_games.subprocess.Popen", return_value=object(),
        ) as popen:
            result = boom_games.launch_heroic_game("amzn1.adg.product.id", "nile")
        self.assertTrue(result["ok"])
        self.assertEqual(
            popen.call_args.args[0],
            ["/usr/bin/xdg-open", "heroic://launch?appName=amzn1.adg.product.id&runner=nile"],
        )
        self.assertNotIn("shell", popen.call_args.kwargs)
        self.assertFalse(boom_games.launch_heroic_game("app&id", "nile")["ok"])


if __name__ == "__main__":
    unittest.main()
