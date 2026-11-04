import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import boom_game_sessions


RAW_HEADER = (
    "fps,frametime,cpu_load,cpu_power,gpu_load,cpu_temp,gpu_temp,"
    "gpu_core_clock,gpu_mem_clock,gpu_vram_used,gpu_power,ram_used,"
    "swap_used,process_rss,cpu_mhz,elapsed\n"
)


def raw_log(rows):
    return RAW_HEADER + "".join(",".join(str(value) for value in row) + "\n" for row in rows)


class MangoHudCsvTests(unittest.TestCase):
    def test_parses_raw_log_metadata_and_aggregates_metrics_in_source_units(self):
        content = (
            '"System info","OS","Linux"\n'
            '"System info","GPU","Example GPU"\n'
            + raw_log([
                [100, 10, 40, 50, 60, 65, 70, 1800, 7000, 8.5, 220, 7.5, 1.0, 1.25, 4200, 1_000_000_000],
                [50, 20, 60, 70, 80, 75, 80, 1600, 6800, 9.5, 240, 8.5, 2.0, 1.75, 3800, 3_000_000_000],
            ])
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Sample_2026-11-03_21-30-00.csv"
            path.write_text(content, encoding="utf-8")

            result = boom_game_sessions.parse_mangohud_csv(path)

        self.assertEqual(result["format"], "raw")
        self.assertEqual(result["sample_count"], 2)
        self.assertEqual(result["duration_seconds"], 2.0)
        self.assertEqual(result["average_fps"], 66.67)
        self.assertEqual(result["low_1pct_fps"], 50.0)
        self.assertEqual(result["low_01pct_fps"], 50.0)
        self.assertEqual(result["avg_gpu_load_pct"], 70.0)
        self.assertEqual(result["peak_cpu_temp_c"], 75.0)
        self.assertEqual(result["avg_vram_used_gib"], 9.0)
        self.assertEqual(result["avg_ram_used_gib"], 8.0)
        self.assertEqual(result["avg_process_rss_gib"], 1.5)
        self.assertEqual(boom_game_sessions.game_label_from_filename(path), "Sample")

    def test_parses_summary_csv_and_derives_frametime_when_absent(self):
        content = (
            "0.1% Min FPS,1% Min FPS,97% Percentile FPS,Average FPS,GPU Load,CPU Load,"
            "Average GPU Temp,Average CPU Temp,Average VRAM Used,Average RAM Used,Average Swap Used\n"
            "32,45,115,90,75,40,68,62,6.5,9.0,0.5\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Game_summary.csv"
            path.write_text(content, encoding="utf-8")

            result = boom_game_sessions.parse_mangohud_csv(path)

        self.assertEqual(result["format"], "summary")
        self.assertEqual(result["low_01pct_fps"], 32)
        self.assertEqual(result["low_1pct_fps"], 45)
        self.assertEqual(result["fps_p97"], 115)
        self.assertEqual(result["average_fps"], 90)
        self.assertEqual(result["avg_frametime_ms"], 11.111)
        self.assertEqual(result["avg_vram_used_gib"], 6.5)
        self.assertEqual(boom_game_sessions.game_label_from_filename(path), "Game")

    def test_rejects_unrecognized_insufficient_and_oversized_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.csv"
            path.write_text("not a MangoHud log\n", encoding="utf-8")
            with self.assertRaises(boom_game_sessions.MangoHudImportError):
                boom_game_sessions.parse_mangohud_csv(path)

            path.write_text(RAW_HEADER + "100,10\n", encoding="utf-8")
            with self.assertRaisesRegex(boom_game_sessions.MangoHudImportError, "amostras suficientes"):
                boom_game_sessions.parse_mangohud_csv(path)

            with patch.object(boom_game_sessions, "MAX_LOG_BYTES", 4):
                with self.assertRaisesRegex(boom_game_sessions.MangoHudImportError, "excede o limite"):
                    boom_game_sessions.parse_mangohud_csv(path)

    def test_private_store_compares_sessions_and_never_copies_the_raw_log(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "session.csv"
            database = root / "state" / "boom" / "game-sessions.sqlite3"
            source.write_text(raw_log([
                [100, 10, 40, 50, 60, 65, 70, 1800, 7000, 8, 220, 7, 1, 1.25, 4200, 1_000_000_000],
                [50, 20, 60, 70, 80, 75, 80, 1600, 6800, 9, 240, 8, 2, 1.75, 3800, 3_000_000_000],
            ]), encoding="utf-8")
            store = boom_game_sessions.GameSessionStore(database)

            first = store.import_file(source, "Example Game")
            source.write_text(raw_log([
                [200, 5, 40, 50, 60, 65, 70, 1800, 7000, 8, 220, 7, 1, 1.25, 4200, 1_000_000_000],
                [100, 10, 60, 70, 80, 75, 80, 1600, 6800, 9, 240, 8, 2, 1.75, 3800, 3_000_000_000],
            ]), encoding="utf-8")
            imported_content = source.read_bytes()
            second = store.import_file(source, "example game")

            self.assertIsNone(first["comparison"])
            self.assertEqual(second["comparison"]["average_fps"]["delta_percent"], 99.99)
            self.assertEqual(len(store.sessions()), 2)
            self.assertEqual(store.status()["session_count"], 2)
            self.assertEqual(stat.S_IMODE(database.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)
            self.assertEqual(store.sessions()[1]["source_name"], "session.csv")
            self.assertNotIn(str(source), str(store.sessions()))
            self.assertEqual(source.read_bytes(), imported_content)
            self.assertEqual(store.clear(), 2)
            self.assertEqual(store.status()["session_count"], 0)


if __name__ == "__main__":
    unittest.main()
