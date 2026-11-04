"""Import and compare local MangoHud CSV session summaries."""

from __future__ import annotations

import csv
import io
import math
import os
import re
import sqlite3
import time
from pathlib import Path
from statistics import fmean
from typing import Any, Iterable


MAX_LOG_BYTES = 32 * 1024 * 1024
MAX_LOG_ROWS = 500_000
MAX_STORED_SESSIONS = 500
MAX_GAME_LABEL_LENGTH = 80
MAX_SOURCE_NAME_LENGTH = 120
_MANGOHUD_TIMESTAMP = re.compile(r"_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")

_RAW_METRICS = {
    "fps": ("fps",),
    "frametime": ("frametime", "frame time"),
    "elapsed": ("elapsed",),
    "cpu_load": ("cpu_load", "cpu load"),
    "gpu_load": ("gpu_load", "gpu load"),
    "cpu_temp": ("cpu_temp", "cpu temp"),
    "gpu_temp": ("gpu_temp", "gpu temp"),
    "cpu_power": ("cpu_power", "cpu power"),
    "gpu_power": ("gpu_power", "gpu power"),
    "cpu_mhz": ("cpu_mhz", "cpu mhz"),
    "gpu_core_clock": ("gpu_core_clock", "gpu core clock"),
    "gpu_mem_clock": ("gpu_mem_clock", "gpu mem clock"),
    "gpu_vram_used": ("gpu_vram_used", "gpu vram used", "vram_used"),
    "ram_used": ("ram_used", "ram used"),
    "swap_used": ("swap_used", "swap used"),
    "process_rss": ("process_rss", "process rss"),
}

_SUMMARY_METRICS = {
    "low_01pct_fps": ("0.1% min fps", "0.1% low fps"),
    "low_1pct_fps": ("1% min fps", "1% low fps"),
    "fps_p97": ("97% percentile fps", "97th percentile fps"),
    "average_fps": ("average fps", "avg fps"),
    "avg_gpu_load_pct": ("gpu load", "average gpu load"),
    "avg_cpu_load_pct": ("cpu load", "average cpu load"),
    "avg_frametime_ms": ("average frame time", "average frametime"),
    "avg_gpu_temp_c": ("average gpu temp", "average gpu temperature"),
    "avg_cpu_temp_c": ("average cpu temp", "average cpu temperature"),
    "avg_vram_used_gib": ("average vram used", "average gpu vram used"),
    "avg_ram_used_gib": ("average ram used",),
    "avg_swap_used_gib": ("average swap used",),
    "peak_gpu_load_pct": ("peak gpu load",),
    "peak_cpu_load_pct": ("peak cpu load",),
    "peak_gpu_temp_c": ("peak gpu temp",),
    "peak_cpu_temp_c": ("peak cpu temp",),
    "peak_vram_used_gib": ("peak vram used", "peak gpu vram used"),
    "peak_ram_used_gib": ("peak ram used",),
    "peak_swap_used_gib": ("peak swap used",),
}


class MangoHudImportError(ValueError):
    """Raised when an input file is not a supported, useful MangoHud log."""


def game_sessions_database_path() -> Path:
    configured = os.environ.get("XDG_STATE_HOME", "")
    base = Path(configured).expanduser() if configured else Path.home() / ".local" / "state"
    if not base.is_absolute():
        base = Path.home() / ".local" / "state"
    return base / "boom" / "game-sessions.sqlite3"


def game_label_from_filename(filename: str | Path) -> str:
    stem = Path(filename).stem
    if stem.casefold().endswith("_summary"):
        stem = stem[:-8]
    stem = _MANGOHUD_TIMESTAMP.sub("", stem)
    return sanitize_game_label(stem) or "Jogo sem nome"


def sanitize_game_label(value: str) -> str:
    cleaned = " ".join(
        "".join(character for character in str(value) if character.isprintable()).split()
    )
    return cleaned[:MAX_GAME_LABEL_LENGTH].strip()


def _normalise_header(value: str) -> str:
    return " ".join(value.strip().lstrip("\ufeff").casefold().split())


def _find_metric_columns(header: list[str], aliases: dict[str, tuple[str, ...]]) -> dict[str, int]:
    positions = {_normalise_header(name): index for index, name in enumerate(header)}
    found: dict[str, int] = {}
    for metric, names in aliases.items():
        for name in names:
            if name in positions:
                found[metric] = positions[name]
                break
    return found


def _number(row: list[str], index: int | None) -> float | None:
    if index is None or index >= len(row):
        return None
    try:
        value = float(row[index].strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _mean_optional(values: list[float]) -> float | None:
    return round(fmean(values), 3) if values else None


def _summary_base(source_name: str, format_name: str) -> dict[str, Any]:
    return {
        "source_name": " ".join(
            "".join(character for character in Path(source_name).name if character.isprintable()).split()
        )[:MAX_SOURCE_NAME_LENGTH],
        "format": format_name,
        "duration_seconds": None,
        "sample_count": None,
        "average_fps": None,
        "low_1pct_fps": None,
        "low_01pct_fps": None,
        "fps_p97": None,
        "avg_frametime_ms": None,
        "p99_frametime_ms": None,
        "avg_gpu_load_pct": None,
        "avg_cpu_load_pct": None,
        "avg_gpu_temp_c": None,
        "avg_cpu_temp_c": None,
        "avg_vram_used_gib": None,
        "avg_ram_used_gib": None,
        "avg_swap_used_gib": None,
        "peak_gpu_load_pct": None,
        "peak_cpu_load_pct": None,
        "peak_gpu_temp_c": None,
        "peak_cpu_temp_c": None,
        "peak_vram_used_gib": None,
        "peak_ram_used_gib": None,
        "peak_swap_used_gib": None,
        "avg_cpu_power_w": None,
        "avg_gpu_power_w": None,
        "avg_cpu_clock_mhz": None,
        "avg_gpu_core_clock_mhz": None,
        "avg_gpu_mem_clock_mhz": None,
        "avg_process_rss_gib": None,
        "peak_process_rss_gib": None,
    }


def _parse_raw_rows(rows: Iterable[list[str]], columns: dict[str, int], source_name: str) -> dict[str, Any]:
    frame_times: list[float] = []
    fps_values: list[float] = []
    elapsed_ns: list[float] = []
    optional_values: dict[str, list[float]] = {
        name: [] for name in (
            "cpu_load", "gpu_load", "cpu_temp", "gpu_temp", "cpu_power", "gpu_power",
            "cpu_mhz", "gpu_core_clock", "gpu_mem_clock", "gpu_vram_used", "ram_used", "swap_used",
            "process_rss",
        )
    }
    scanned = 0
    malformed = 0
    for row in rows:
        if not any(cell.strip() for cell in row):
            continue
        scanned += 1
        if scanned > MAX_LOG_ROWS:
            raise MangoHudImportError(f"O arquivo excede o limite de {MAX_LOG_ROWS:,} linhas.")
        frame_time = _number(row, columns.get("frametime"))
        fps = _number(row, columns.get("fps"))
        if frame_time is None or not (0 < frame_time <= 60_000) or (fps is not None and fps < 0):
            malformed += 1
            continue
        if fps is None or fps == 0:
            fps = 1000 / frame_time
        if fps > 10_000:
            malformed += 1
            continue
        frame_times.append(frame_time)
        fps_values.append(fps)
        elapsed = _number(row, columns.get("elapsed"))
        if elapsed is not None and elapsed >= 0:
            elapsed_ns.append(elapsed)
        for field, target in optional_values.items():
            value = _number(row, columns.get(field))
            if value is None:
                continue
            if field.endswith("_load") and 0 <= value <= 100:
                target.append(value)
            elif field.endswith("_temp") and 0 <= value <= 250:
                target.append(value)
            elif field.endswith("_power") and 0 <= value <= 5_000:
                target.append(value)
            elif field.endswith("_clock") or field.endswith("_mhz"):
                if 0 <= value <= 100_000:
                    target.append(value)
            elif (field.endswith("_used") or field == "process_rss") and 0 <= value <= 1_000_000_000:
                target.append(value)

    if len(frame_times) < 2:
        detail = "Nenhuma linha utilizável." if not frame_times else "O log contém só uma amostra utilizável."
        if malformed:
            detail += f" {malformed} linha(s) sem FPS/frametime válidos foram ignoradas."
        raise MangoHudImportError(f"O CSV bruto do MangoHud não tem amostras suficientes. {detail}")

    mean_frame_time = fmean(frame_times)
    slow_samples = sorted(frame_times, reverse=True)
    low_1_count = max(1, math.ceil(len(slow_samples) * 0.01))
    low_01_count = max(1, math.ceil(len(slow_samples) * 0.001))
    duration = None
    if len(elapsed_ns) >= 2:
        span = max(elapsed_ns) - min(elapsed_ns)
        if 0 < span <= 7 * 24 * 60 * 60 * 1_000_000_000:
            duration = round(span / 1_000_000_000, 3)

    result = _summary_base(source_name, "raw")
    result.update({
        "duration_seconds": duration,
        "sample_count": len(frame_times),
        "average_fps": round(1000 / mean_frame_time, 2),
        "low_1pct_fps": round(1000 / fmean(slow_samples[:low_1_count]), 2),
        "low_01pct_fps": round(1000 / fmean(slow_samples[:low_01_count]), 2),
        "fps_p97": round(_percentile(fps_values, 0.97), 2),
        "avg_frametime_ms": round(mean_frame_time, 3),
        "p99_frametime_ms": round(_percentile(frame_times, 0.99), 3),
        "avg_gpu_load_pct": _mean_optional(optional_values["gpu_load"]),
        "avg_cpu_load_pct": _mean_optional(optional_values["cpu_load"]),
        "avg_gpu_temp_c": _mean_optional(optional_values["gpu_temp"]),
        "avg_cpu_temp_c": _mean_optional(optional_values["cpu_temp"]),
        "avg_vram_used_gib": _mean_optional(optional_values["gpu_vram_used"]),
        "avg_ram_used_gib": _mean_optional(optional_values["ram_used"]),
        "avg_swap_used_gib": _mean_optional(optional_values["swap_used"]),
        "peak_gpu_load_pct": round(max(optional_values["gpu_load"]), 3) if optional_values["gpu_load"] else None,
        "peak_cpu_load_pct": round(max(optional_values["cpu_load"]), 3) if optional_values["cpu_load"] else None,
        "peak_gpu_temp_c": round(max(optional_values["gpu_temp"]), 3) if optional_values["gpu_temp"] else None,
        "peak_cpu_temp_c": round(max(optional_values["cpu_temp"]), 3) if optional_values["cpu_temp"] else None,
        "peak_vram_used_gib": round(max(optional_values["gpu_vram_used"]), 3) if optional_values["gpu_vram_used"] else None,
        "peak_ram_used_gib": round(max(optional_values["ram_used"]), 3) if optional_values["ram_used"] else None,
        "peak_swap_used_gib": round(max(optional_values["swap_used"]), 3) if optional_values["swap_used"] else None,
        "avg_cpu_power_w": _mean_optional(optional_values["cpu_power"]),
        "avg_gpu_power_w": _mean_optional(optional_values["gpu_power"]),
        "avg_cpu_clock_mhz": _mean_optional(optional_values["cpu_mhz"]),
        "avg_gpu_core_clock_mhz": _mean_optional(optional_values["gpu_core_clock"]),
        "avg_gpu_mem_clock_mhz": _mean_optional(optional_values["gpu_mem_clock"]),
        "avg_process_rss_gib": _mean_optional(optional_values["process_rss"]),
        "peak_process_rss_gib": round(max(optional_values["process_rss"]), 3) if optional_values["process_rss"] else None,
    })
    return result


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _parse_summary_rows(header: list[str], rows: Iterable[list[str]], source_name: str) -> dict[str, Any]:
    result = _summary_base(source_name, "summary")
    values = _find_metric_columns(header, _SUMMARY_METRICS)
    data = next((row for row in rows if any(cell.strip() for cell in row)), None)
    if data is None:
        raise MangoHudImportError("O CSV de resumo do MangoHud não contém uma linha de métricas.")
    mapped = {
        "low_01pct_fps": "low_01pct_fps",
        "low_1pct_fps": "low_1pct_fps",
        "fps_p97": "fps_p97",
        "average_fps": "average_fps",
        "avg_gpu_load_pct": "avg_gpu_load_pct",
        "avg_cpu_load_pct": "avg_cpu_load_pct",
        "avg_frametime_ms": "avg_frametime_ms",
        "avg_gpu_temp_c": "avg_gpu_temp_c",
        "avg_cpu_temp_c": "avg_cpu_temp_c",
        "avg_vram_used_gib": "avg_vram_used_gib",
        "avg_ram_used_gib": "avg_ram_used_gib",
        "avg_swap_used_gib": "avg_swap_used_gib",
        "peak_gpu_load_pct": "peak_gpu_load_pct",
        "peak_cpu_load_pct": "peak_cpu_load_pct",
        "peak_gpu_temp_c": "peak_gpu_temp_c",
        "peak_cpu_temp_c": "peak_cpu_temp_c",
        "peak_vram_used_gib": "peak_vram_used_gib",
        "peak_ram_used_gib": "peak_ram_used_gib",
        "peak_swap_used_gib": "peak_swap_used_gib",
    }
    for source_key, destination in mapped.items():
        value = _number(data, values.get(source_key))
        if value is None:
            continue
        if destination in {"average_fps", "low_1pct_fps", "low_01pct_fps", "fps_p97"}:
            valid_value = 0 < value <= 10_000
        elif destination in {"avg_frametime_ms", "p99_frametime_ms"}:
            valid_value = 0 < value <= 60_000
        elif destination.endswith("_load_pct"):
            valid_value = 0 <= value <= 100
        elif destination.endswith("_temp_c"):
            valid_value = 0 <= value <= 250
        else:
            valid_value = 0 <= value <= 1_000_000_000
        if valid_value:
            result[destination] = round(value, 3)
    if result["average_fps"] is None:
        raise MangoHudImportError("O CSV de resumo não contém uma média de FPS válida.")
    if result["avg_frametime_ms"] is None and result["average_fps"] > 0:
        result["avg_frametime_ms"] = round(1000 / result["average_fps"], 3)
    return result


def parse_mangohud_csv(path: str | Path) -> dict[str, Any]:
    source = Path(path).expanduser()
    try:
        if not source.is_file():
            raise MangoHudImportError("Selecione um arquivo CSV do MangoHud existente.")
        with source.open("rb") as stream:
            content_bytes = stream.read(MAX_LOG_BYTES + 1)
        if not content_bytes:
            raise MangoHudImportError("O arquivo CSV está vazio.")
        if len(content_bytes) > MAX_LOG_BYTES:
            raise MangoHudImportError(f"O CSV excede o limite de {MAX_LOG_BYTES // (1024 * 1024)} MiB.")
        content = content_bytes.decode("utf-8-sig")
    except MangoHudImportError:
        raise
    except (OSError, UnicodeError) as exc:
        raise MangoHudImportError(f"Não foi possível ler o CSV: {exc}") from exc
    if "\x00" in content:
        raise MangoHudImportError("O arquivo contém bytes nulos e não parece ser um CSV válido.")
    reader = csv.reader(io.StringIO(content, newline=""), skipinitialspace=True)
    try:
        for row_index, row in enumerate(reader, start=1):
            if row_index > MAX_LOG_ROWS:
                raise MangoHudImportError(f"O arquivo excede o limite de {MAX_LOG_ROWS:,} linhas.")
            columns = _find_metric_columns(row, _RAW_METRICS)
            if "fps" in columns and "frametime" in columns:
                return _parse_raw_rows(reader, columns, source.name)
            summary_columns = _find_metric_columns(row, _SUMMARY_METRICS)
            if "average_fps" in summary_columns and "low_1pct_fps" in summary_columns:
                return _parse_summary_rows(row, reader, source.name)
    except csv.Error as exc:
        raise MangoHudImportError(f"CSV inválido: {exc}") from exc
    raise MangoHudImportError(
        "Cabeçalho não reconhecido. Selecione o CSV bruto de FPS ou o arquivo _summary.csv do MangoHud."
    )


class GameSessionStore:
    """Private SQLite store for imported aggregates; raw logs are never copied."""

    def __init__(self, database_path: str | Path | None = None) -> None:
        self.database_path = Path(database_path) if database_path is not None else game_sessions_database_path()

    @staticmethod
    def _secure_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise OSError("O diretório das sessões não pode ser um link simbólico.")
        os.chmod(path, 0o700)

    def _connect(self) -> sqlite3.Connection:
        self._secure_directory(self.database_path.parent)
        if self.database_path.is_symlink():
            raise OSError("O banco das sessões não pode ser um link simbólico.")
        connection = sqlite3.connect(self.database_path, timeout=2.0)
        connection.execute("PRAGMA busy_timeout=2000")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                imported_at REAL NOT NULL,
                game_label TEXT NOT NULL,
                source_name TEXT NOT NULL,
                format TEXT NOT NULL,
                duration_seconds REAL,
                sample_count INTEGER,
                average_fps REAL,
                low_1pct_fps REAL,
                low_01pct_fps REAL,
                fps_p97 REAL,
                avg_frametime_ms REAL,
                p99_frametime_ms REAL,
                avg_gpu_load_pct REAL,
                avg_cpu_load_pct REAL,
                avg_gpu_temp_c REAL,
                avg_cpu_temp_c REAL,
                avg_vram_used_gib REAL,
                avg_ram_used_gib REAL,
                avg_swap_used_gib REAL,
                peak_gpu_load_pct REAL,
                peak_cpu_load_pct REAL,
                peak_gpu_temp_c REAL,
                peak_cpu_temp_c REAL,
                peak_vram_used_gib REAL,
                peak_ram_used_gib REAL,
                peak_swap_used_gib REAL,
                avg_cpu_power_w REAL,
                avg_gpu_power_w REAL,
                avg_cpu_clock_mhz REAL,
                avg_gpu_core_clock_mhz REAL,
                avg_gpu_mem_clock_mhz REAL,
                avg_process_rss_gib REAL,
                peak_process_rss_gib REAL
            )"""
        )
        connection.execute("CREATE INDEX IF NOT EXISTS sessions_imported_at ON sessions(imported_at DESC)")
        os.chmod(self.database_path, 0o600)
        return connection

    def import_file(self, path: str | Path, game_label: str | None = None) -> dict[str, Any]:
        parsed = parse_mangohud_csv(path)
        label = sanitize_game_label(game_label) if game_label is not None else game_label_from_filename(path)
        if not label:
            raise MangoHudImportError("Informe um nome de jogo com pelo menos um caractere visível.")
        imported_at = time.time()
        columns = (
            "imported_at", "game_label", "source_name", "format", "duration_seconds", "sample_count",
            "average_fps", "low_1pct_fps", "low_01pct_fps", "fps_p97", "avg_frametime_ms",
            "p99_frametime_ms", "avg_gpu_load_pct", "avg_cpu_load_pct", "avg_gpu_temp_c",
            "avg_cpu_temp_c", "avg_vram_used_gib", "avg_ram_used_gib", "avg_swap_used_gib",
            "peak_gpu_load_pct", "peak_cpu_load_pct", "peak_gpu_temp_c", "peak_cpu_temp_c",
            "peak_vram_used_gib", "peak_ram_used_gib", "peak_swap_used_gib", "avg_cpu_power_w",
            "avg_gpu_power_w", "avg_cpu_clock_mhz", "avg_gpu_core_clock_mhz", "avg_gpu_mem_clock_mhz",
            "avg_process_rss_gib", "peak_process_rss_gib",
        )
        values = [imported_at, label, *[parsed.get(column) for column in columns[2:]]]
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            previous_rows = connection.execute(
                "SELECT * FROM sessions ORDER BY imported_at DESC, id DESC LIMIT ?",
                (MAX_STORED_SESSIONS,),
            ).fetchall()
            previous = next((
                self._row_to_dict(row) for row in previous_rows
                if str(row[2]).casefold() == label.casefold()
            ), None)
            placeholders = ",".join("?" for _ in columns)
            cursor = connection.execute(
                f"INSERT INTO sessions ({','.join(columns)}) VALUES ({placeholders})", values,
            )
            connection.execute(
                "DELETE FROM sessions WHERE id IN (SELECT id FROM sessions ORDER BY imported_at DESC, id DESC LIMIT -1 OFFSET ?)",
                (MAX_STORED_SESSIONS,),
            )
            row = connection.execute("SELECT * FROM sessions WHERE id=?", (cursor.lastrowid,)).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        current = self._row_to_dict(row)
        current["comparison"] = _compare_sessions(current, previous)
        return current

    @staticmethod
    def _row_to_dict(row: tuple[Any, ...]) -> dict[str, Any]:
        columns = (
            "id", "imported_at", "game_label", "source_name", "format", "duration_seconds", "sample_count",
            "average_fps", "low_1pct_fps", "low_01pct_fps", "fps_p97", "avg_frametime_ms",
            "p99_frametime_ms", "avg_gpu_load_pct", "avg_cpu_load_pct", "avg_gpu_temp_c",
            "avg_cpu_temp_c", "avg_vram_used_gib", "avg_ram_used_gib", "avg_swap_used_gib",
            "peak_gpu_load_pct", "peak_cpu_load_pct", "peak_gpu_temp_c", "peak_cpu_temp_c",
            "peak_vram_used_gib", "peak_ram_used_gib", "peak_swap_used_gib", "avg_cpu_power_w",
            "avg_gpu_power_w", "avg_cpu_clock_mhz", "avg_gpu_core_clock_mhz", "avg_gpu_mem_clock_mhz",
            "avg_process_rss_gib", "peak_process_rss_gib",
        )
        return dict(zip(columns, row))

    def sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), MAX_STORED_SESSIONS))
        if not self.database_path.exists():
            return []
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM sessions ORDER BY imported_at DESC, id DESC LIMIT ?", (bounded_limit,),
            ).fetchall()
        finally:
            connection.close()
        return [self._row_to_dict(row) for row in rows]

    def clear(self) -> int:
        connection = self._connect()
        try:
            cursor = connection.execute("DELETE FROM sessions")
            deleted = cursor.rowcount
            connection.commit()
            return max(0, deleted)
        finally:
            connection.close()

    def status(self) -> dict[str, Any]:
        if not self.database_path.exists():
            return {"database": str(self.database_path), "session_count": 0, "latest_imported_at": None}
        connection = self._connect()
        try:
            count = int(connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
            latest = connection.execute("SELECT MAX(imported_at) FROM sessions").fetchone()[0]
        finally:
            connection.close()
        return {"database": str(self.database_path), "session_count": count, "latest_imported_at": latest}


def _compare_sessions(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any] | None:
    if previous is None:
        return None
    comparison: dict[str, Any] = {"previous_imported_at": previous.get("imported_at")}
    for metric in ("average_fps", "low_1pct_fps"):
        old = previous.get(metric)
        new = current.get(metric)
        if isinstance(old, (int, float)) and old > 0 and isinstance(new, (int, float)):
            comparison[metric] = {
                "previous": old,
                "current": new,
                "delta_percent": round((new - old) / old * 100, 2),
            }
    return comparison if len(comparison) > 1 else None
