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


if __name__ == "__main__":
    unittest.main()
