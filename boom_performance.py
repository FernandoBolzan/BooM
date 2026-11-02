"""Opt-in, local performance history and evidence-based diagnostics for BooM."""

from __future__ import annotations

import math
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path
from statistics import median
from typing import Any


HISTORY_RETENTION_SECONDS = 7 * 24 * 60 * 60
HISTORY_MAX_SAMPLES = 120_960  # 7 days sampled every 5 seconds.
MIN_HISTORY_SAMPLES = 12
MIN_HISTORY_SPAN_SECONDS = 55.0
HISTORY_SAMPLE_INTERVAL_SECONDS = 5.0
MIN_SAMPLE_GAP_SECONDS = 4.0
MAX_HISTORY_WINDOW_MINUTES = 7 * 24 * 60

SAMPLE_COLUMNS = (
    "cpu_percent",
    "ram_percent",
    "swap_percent",
    "gpu_percent",
    "vram_percent",
    "cpu_temp_celsius",
    "gpu_temp_celsius",
    "disk_busy_percent",
    "disk_read_bytes_per_second",
    "disk_write_bytes_per_second",
    "disk_average_io_wait_ms",
)

CPU_SENSOR_PATTERN = re.compile(r"(?:k10temp|coretemp|zenpower|\bcpu\b|tctl|tdie|package id)", re.I)


def _xdg_directory(environment_name: str, fallback: Path) -> Path:
    configured = os.environ.get(environment_name, "")
    path = Path(configured).expanduser() if configured else fallback
    return path if path.is_absolute() else fallback


def history_database_path() -> Path:
    state_home = _xdg_directory("XDG_STATE_HOME", Path.home() / ".local" / "state")
    return state_home / "boom" / "performance.sqlite3"


def history_enabled_path() -> Path:
    config_home = _xdg_directory("XDG_CONFIG_HOME", Path.home() / ".config")
    return config_home / "boom" / "performance-history.enabled"


def _safe_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _bounded_number(value: Any, minimum: float, maximum: float) -> float | None:
    number = _safe_number(value)
    return number if number is not None and minimum <= number <= maximum else None


def _percentage(used: Any, total: Any) -> float | None:
    used_value = _safe_number(used)
    total_value = _safe_number(total)
    if used_value is None or total_value is None or total_value <= 0 or used_value < 0:
        return None
    return min(100.0, max(0.0, used_value * 100.0 / total_value))


def _max_value(records: Any, field: str) -> float | None:
    if not isinstance(records, list):
        return None
    values = [value for item in records if isinstance(item, dict) if (value := _safe_number(item.get(field))) is not None]
    return max(values) if values else None


def extract_performance_sample(metrics: dict[str, Any], sampled_at: float | None = None) -> dict[str, Any]:
    """Keep only aggregate, non-identifying metrics suitable for local history."""
    memory = metrics.get("memory") if isinstance(metrics.get("memory"), dict) else {}
    gpu_metrics = metrics.get("gpu_metrics") if isinstance(metrics.get("gpu_metrics"), list) else []
    disk_io = metrics.get("disk_io") if isinstance(metrics.get("disk_io"), dict) else {}
    temperatures = metrics.get("temperatures") if isinstance(metrics.get("temperatures"), list) else []

    cpu_temperatures = [
        value
        for item in temperatures
        if isinstance(item, dict) and CPU_SENSOR_PATTERN.search(str(item.get("name", "")))
        if (value := _safe_number(item.get("celsius"))) is not None
    ]
    gpu_temperatures = [
        value
        for item in gpu_metrics
        if isinstance(item, dict)
        if (value := _safe_number(item.get("temperature_celsius"))) is not None
    ]
    gpu_usage = [
        value
        for item in gpu_metrics
        if isinstance(item, dict)
        if (value := _safe_number(item.get("gpu_utilization_percent"))) is not None and 0 <= value <= 100
    ]
    vram_usage = [
        value
        for item in gpu_metrics
        if isinstance(item, dict)
        if (value := _percentage(item.get("vram_used_mib"), item.get("vram_total_mib"))) is not None
    ]

    timestamp = _safe_number(sampled_at)
    if timestamp is None or timestamp < 0:
        timestamp = time.time()
    sample: dict[str, Any] = {
        "sampled_at": timestamp,
        "cpu_percent": _bounded_number(metrics.get("cpu_percent"), 0, 100),
        "ram_percent": _percentage(memory.get("used"), memory.get("total")),
        "swap_percent": _percentage(memory.get("swap_used"), memory.get("swap_total")),
        "gpu_percent": max(gpu_usage) if gpu_usage else None,
        "vram_percent": max(vram_usage) if vram_usage else None,
        "cpu_temp_celsius": max(cpu_temperatures) if cpu_temperatures else None,
        "gpu_temp_celsius": max(gpu_temperatures) if gpu_temperatures else None,
    }
    for field in (
        "busy_percent",
        "read_bytes_per_second",
        "write_bytes_per_second",
        "average_io_wait_ms",
    ):
        sample_field = {
            "busy_percent": "disk_busy_percent",
            "read_bytes_per_second": "disk_read_bytes_per_second",
            "write_bytes_per_second": "disk_write_bytes_per_second",
            "average_io_wait_ms": "disk_average_io_wait_ms",
        }[field]
        sample[sample_field] = _bounded_number(disk_io.get(field), 0, 100) if field == "busy_percent" else _safe_number(disk_io.get(field))
    return sample


def parse_proc_diskstats(payload: str, whole_devices: set[str] | None = None) -> dict[str, dict[str, int]]:
    """Parse whole-device counters; partition rows are excluded to avoid double counting."""
    result: dict[str, dict[str, int]] = {}
    for line in (payload or "").splitlines():
        fields = line.split()
        if len(fields) < 14:
            continue
        name = fields[2]
        if whole_devices is not None:
            if name not in whole_devices:
                continue
        else:
            block_path = Path("/sys/class/block") / name
            if name.startswith(("loop", "ram", "zram")) or (block_path / "partition").exists():
                continue
            slaves = block_path / "slaves"
            try:
                if any(slaves.iterdir()):
                    continue
            except OSError:
                pass
        try:
            counters = [int(value) for value in fields[3:]]
        except ValueError:
            continue
        if len(counters) < 11 or min(counters[0], counters[2], counters[4], counters[6], counters[9], counters[10]) < 0:
            continue
        result[name] = {
            "reads_completed": counters[0],
            "sectors_read": counters[2],
            "writes_completed": counters[4],
            "sectors_written": counters[6],
            "io_milliseconds": counters[9],
            "weighted_io_milliseconds": counters[10],
        }
    return result


class DiskIoSampler:
    """Turn cumulative `/proc/diskstats` counters into interval rates."""

    def __init__(self, proc_path: str | Path = "/proc/diskstats") -> None:
        self.proc_path = Path(proc_path)
        self._previous: dict[str, dict[str, int]] = {}
        self._previous_at: float | None = None

    def sample(
        self,
        payload: str | None = None,
        sampled_at: float | None = None,
        whole_devices: set[str] | None = None,
    ) -> dict[str, Any] | None:
        if payload is None:
            try:
                payload = self.proc_path.read_text(encoding="ascii")
            except (OSError, UnicodeError):
                return None
        current_at = float(sampled_at if sampled_at is not None else time.monotonic())
        current = parse_proc_diskstats(payload, whole_devices=whole_devices)
        previous = self._previous
        previous_at = self._previous_at
        self._previous = current
        self._previous_at = current_at
        if previous_at is None:
            return None
        elapsed = current_at - previous_at
        if elapsed < 0.5:
            return None

        samples: list[dict[str, float]] = []
        for name, counters in current.items():
            before = previous.get(name)
            if before is None:
                continue
            deltas = {key: counters[key] - before[key] for key in counters}
            if any(value < 0 for value in deltas.values()):
                continue  # Counter reset or device reinitialization.
            completed = deltas["reads_completed"] + deltas["writes_completed"]
            samples.append({
                "busy_percent": min(100.0, deltas["io_milliseconds"] * 100.0 / (elapsed * 1000.0)),
                "read_bytes_per_second": deltas["sectors_read"] * 512.0 / elapsed,
                "write_bytes_per_second": deltas["sectors_written"] * 512.0 / elapsed,
                "average_io_wait_ms": (
                    deltas["weighted_io_milliseconds"] / completed if completed else 0.0
                ),
            })
        if not samples:
            return {
                "busy_percent": None,
                "read_bytes_per_second": None,
                "write_bytes_per_second": None,
                "average_io_wait_ms": None,
                "source": "/proc/diskstats · sem variação de contador utilizável",
            }
        busiest = max(samples, key=lambda item: (item["busy_percent"], item["average_io_wait_ms"]))
        return {
            **{key: round(value, 2) for key, value in busiest.items()},
            "source": "/proc/diskstats · dispositivo inteiro mais ocupado",
        }


class PerformanceHistoryStore:
    """Private SQLite history; collection only starts after explicit opt-in."""

    def __init__(
        self,
        database_path: str | Path | None = None,
        enabled_path: str | Path | None = None,
    ) -> None:
        self.database_path = Path(database_path) if database_path is not None else history_database_path()
        self.enabled_path = Path(enabled_path) if enabled_path is not None else history_enabled_path()
        self._last_recorded_at: float | None = None
        self._last_cleanup_at: float | None = None

    def enabled(self) -> bool:
        try:
            return self.enabled_path.read_text(encoding="utf-8").strip() == "enabled"
        except OSError:
            return False

    @staticmethod
    def _secure_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise OSError("O diretório do histórico não pode ser um link simbólico.")
        os.chmod(path, 0o700)

    def set_enabled(self, enabled: bool) -> Path:
        if enabled:
            self._secure_directory(self.enabled_path.parent)
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", dir=self.enabled_path.parent,
                    prefix=".boom-history-", delete=False,
                ) as temporary:
                    temporary.write("enabled\n")
                    temporary.flush()
                    os.fsync(temporary.fileno())
                    temporary_path = Path(temporary.name)
                os.chmod(temporary_path, 0o600)
                os.replace(temporary_path, self.enabled_path)
            finally:
                if temporary_path is not None:
                    temporary_path.unlink(missing_ok=True)
        else:
            self.enabled_path.unlink(missing_ok=True)
        return self.enabled_path

    def _connect(self) -> sqlite3.Connection:
        self._secure_directory(self.database_path.parent)
        if self.database_path.is_symlink():
            raise OSError("O arquivo do histórico não pode ser um link simbólico.")
        connection = sqlite3.connect(self.database_path, timeout=2.0)
        connection.execute("PRAGMA busy_timeout=2000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS samples (
                sampled_at REAL PRIMARY KEY,
                cpu_percent REAL,
                ram_percent REAL,
                swap_percent REAL,
                gpu_percent REAL,
                vram_percent REAL,
                cpu_temp_celsius REAL,
                gpu_temp_celsius REAL,
                disk_busy_percent REAL,
                disk_read_bytes_per_second REAL,
                disk_write_bytes_per_second REAL,
                disk_average_io_wait_ms REAL
            )"""
        )
        connection.execute("CREATE INDEX IF NOT EXISTS samples_sampled_at ON samples(sampled_at)")
        os.chmod(self.database_path, 0o600)
        return connection

    def record(self, metrics: dict[str, Any], sampled_at: float | None = None) -> bool:
        if not self.enabled():
            return False
        sample = extract_performance_sample(metrics, sampled_at)
        timestamp = sample.pop("sampled_at")
        if self._last_recorded_at is not None and timestamp - self._last_recorded_at < MIN_SAMPLE_GAP_SECONDS:
            return False
        values = [sample.get(column) for column in SAMPLE_COLUMNS]
        if all(value is None for value in values):
            return False

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            # Recheck while holding SQLite's write lock so a concurrent disable/clear wins.
            if not self.enabled():
                connection.rollback()
                return False
            columns = ", ".join(("sampled_at", *SAMPLE_COLUMNS))
            placeholders = ", ".join("?" for _ in range(len(SAMPLE_COLUMNS) + 1))
            connection.execute(
                f"INSERT OR REPLACE INTO samples ({columns}) VALUES ({placeholders})",
                (timestamp, *values),
            )
            if self._last_cleanup_at is None or timestamp - self._last_cleanup_at >= 3600:
                cutoff = timestamp - HISTORY_RETENTION_SECONDS
                connection.execute("DELETE FROM samples WHERE sampled_at < ?", (cutoff,))
                connection.execute(
                    "DELETE FROM samples WHERE sampled_at IN "
                    "(SELECT sampled_at FROM samples ORDER BY sampled_at DESC LIMIT -1 OFFSET ?)",
                    (HISTORY_MAX_SAMPLES,),
                )
                self._last_cleanup_at = timestamp
            connection.commit()
        finally:
            connection.close()
            os.chmod(self.database_path, 0o600)
        self._last_recorded_at = timestamp
        return True

    def samples(self, window_minutes: float = 15, now: float | None = None) -> list[dict[str, Any]]:
        if not math.isfinite(window_minutes) or not 1 <= window_minutes <= MAX_HISTORY_WINDOW_MINUTES:
            raise ValueError(f"A janela deve ficar entre 1 e {MAX_HISTORY_WINDOW_MINUTES} minutos.")
        if not self.database_path.exists() or self.database_path.is_symlink():
            return []
        current = float(now if now is not None else time.time())
        cutoff = current - window_minutes * 60
        uri = f"{self.database_path.resolve().as_uri()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=2.0)
            connection.row_factory = sqlite3.Row
            try:
                rows = connection.execute(
                    f"SELECT sampled_at, {', '.join(SAMPLE_COLUMNS)} FROM samples "
                    "WHERE sampled_at >= ? AND sampled_at <= ? ORDER BY sampled_at",
                    (cutoff, current),
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.Error:
            return []
        return [dict(row) for row in rows]

    def status(self, now: float | None = None) -> dict[str, Any]:
        current = float(now if now is not None else time.time())
        result: dict[str, Any] = {
            "enabled": self.enabled(),
            "database": str(self.database_path),
            "retention_days": HISTORY_RETENTION_SECONDS // (24 * 60 * 60),
            "sample_interval_seconds": HISTORY_SAMPLE_INTERVAL_SECONDS,
            "sample_count": 0,
            "first_sample_at": None,
            "last_sample_at": None,
        }
        if not self.database_path.exists() or self.database_path.is_symlink():
            return result
        uri = f"{self.database_path.resolve().as_uri()}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=2.0) as connection:
                row = connection.execute("SELECT COUNT(*), MIN(sampled_at), MAX(sampled_at) FROM samples").fetchone()
        except sqlite3.Error:
            result["error"] = "O banco local não pôde ser lido."
            return result
        if row:
            result.update({
                "sample_count": int(row[0]),
                "first_sample_at": row[1],
                "last_sample_at": row[2],
                "latest_age_seconds": max(0.0, current - float(row[2])) if row[2] is not None else None,
            })
        return result

    def clear(self) -> None:
        if self.database_path.is_symlink():
            raise OSError("O arquivo do histórico não pode ser um link simbólico.")
        if self.database_path.exists():
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("PRAGMA secure_delete=ON")
                connection.execute("DELETE FROM samples")
                connection.commit()
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("VACUUM")
            finally:
                connection.close()
        self._last_recorded_at = None
        self._last_cleanup_at = None


def _metric_values(samples: list[dict[str, Any]], field: str) -> list[float]:
    values = [_safe_number(sample.get(field)) for sample in samples]
    return [value for value in values if value is not None]


def _sustained(
    samples: list[dict[str, Any]],
    field: str,
    threshold: float,
    minimum_ratio: float = 0.65,
) -> tuple[bool, float | None, int, int]:
    values = _metric_values(samples, field)
    if not values or len(values) < max(4, math.ceil(len(samples) * 0.65)):
        return False, median(values) if values else None, 0, len(values)
    above = sum(value >= threshold for value in values)
    return above / len(values) >= minimum_ratio, median(values), above, len(values)


def analyze_performance_history(samples: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(
        (sample for sample in samples if _safe_number(sample.get("sampled_at")) is not None),
        key=lambda sample: float(sample["sampled_at"]),
    )
    if not ordered:
        return {
            "status": "insufficient_data",
            "sample_count": 0,
            "window_seconds": 0,
            "confidence": "none",
            "summary": "Ainda não há amostras locais. Ative o histórico e deixe o BooM monitorando.",
            "findings": [],
        }
    start = float(ordered[0]["sampled_at"])
    end = float(ordered[-1]["sampled_at"])
    span = max(0.0, end - start)
    count = len(ordered)
    coverage = min(1.0, count / (span / HISTORY_SAMPLE_INTERVAL_SECONDS + 1.0)) if span else 0.0
    if count < MIN_HISTORY_SAMPLES or span < MIN_HISTORY_SPAN_SECONDS:
        return {
            "status": "insufficient_data",
            "sample_count": count,
            "window_seconds": round(span, 1),
            "coverage_percent": round(coverage * 100, 1),
            "confidence": "none",
            "summary": (
                f"Amostras insuficientes: são necessárias pelo menos {MIN_HISTORY_SAMPLES} leituras "
                f"ao longo de {MIN_HISTORY_SPAN_SECONDS:.0f} segundos."
            ),
            "findings": [],
        }

    if count >= 120 and span >= 600 and coverage >= 0.85:
        confidence = "high"
    elif count >= 36 and span >= 180 and coverage >= 0.70:
        confidence = "moderate"
    else:
        confidence = "low"

    findings: list[dict[str, Any]] = []

    def add_finding(
        finding_id: str,
        severity: str,
        title: str,
        message: str,
        suggestion: str,
        evidence: dict[str, Any],
    ) -> None:
        findings.append({
            "id": finding_id,
            "severity": severity,
            "confidence": confidence,
            "title": title,
            "message": message,
            "suggestion": suggestion,
            "evidence": evidence,
        })

    cpu_hot, cpu_median, cpu_above, cpu_count = _sustained(ordered, "cpu_percent", 85)
    gpu_hot, gpu_median, gpu_above, gpu_count = _sustained(ordered, "gpu_percent", 90)
    if cpu_hot:
        if gpu_median is not None and gpu_median < 70:
            add_finding(
                "possible_cpu_limit", "warning", "Possível limite de CPU",
                "A CPU ficou muito ocupada enquanto a GPU teve folga nesta janela. Isso é um indício, não prova de gargalo de um aplicativo.",
                "Compare a carga por processo e medições durante o aplicativo ou jogo que parece lento.",
                {"cpu_median_percent": round(cpu_median or 0, 1), "cpu_samples_over_85_percent": cpu_above,
                 "cpu_valid_samples": cpu_count, "gpu_median_percent": round(gpu_median, 1)},
            )
        else:
            add_finding(
                "sustained_cpu_load", "info", "Carga alta e sustentada de CPU",
                "A utilização da CPU permaneceu alta durante boa parte da janela.",
                "Veja os processos mais ativos e repita a análise durante a tarefa que deseja investigar.",
                {"cpu_median_percent": round(cpu_median or 0, 1), "cpu_samples_over_85_percent": cpu_above,
                 "cpu_valid_samples": cpu_count},
            )

    if gpu_hot:
        if cpu_median is not None and cpu_median < 70:
            add_finding(
                "possible_gpu_limit", "warning", "Possível limite de GPU",
                "A GPU permaneceu muito ocupada enquanto a CPU teve folga nesta janela. Isso é um indício e depende do aplicativo medido.",
                "Confira temperatura, potência, VRAM e métricas durante a mesma tarefa antes de mudar configurações.",
                {"gpu_median_percent": round(gpu_median or 0, 1), "gpu_samples_over_90_percent": gpu_above,
                 "gpu_valid_samples": gpu_count, "cpu_median_percent": round(cpu_median, 1)},
            )
        else:
            add_finding(
                "sustained_gpu_load", "info", "Carga alta e sustentada de GPU",
                "A GPU permaneceu muito ocupada durante boa parte da janela.",
                "Compare a utilização de VRAM e a temperatura com a tarefa em execução.",
                {"gpu_median_percent": round(gpu_median or 0, 1), "gpu_samples_over_90_percent": gpu_above,
                 "gpu_valid_samples": gpu_count},
            )

    ram_hot, ram_median, ram_above, ram_count = _sustained(ordered, "ram_percent", 90, minimum_ratio=0.5)
    if ram_hot:
        add_finding(
            "memory_pressure", "warning", "Pouca RAM disponível",
            "O uso de RAM ficou acima de 90% durante pelo menos metade das leituras válidas.",
            "Confira aplicativos com maior RSS e a evolução da swap antes de fechar processos.",
            {"ram_median_percent": round(ram_median or 0, 1), "ram_samples_over_90_percent": ram_above,
             "ram_valid_samples": ram_count},
        )

    swap_hot, swap_median, swap_above, swap_count = _sustained(ordered, "swap_percent", 20, minimum_ratio=0.5)
    if swap_hot:
        add_finding(
            "swap_usage", "info", "Swap em uso de forma sustentada",
            "Uma parte relevante da swap permaneceu ocupada nesta janela; isso, isoladamente, não comprova pressão atual de memória.",
            "Confira RAM disponível e atividade de disco; páginas antigas podem permanecer na swap sem impacto perceptível.",
            {"swap_median_percent": round(swap_median or 0, 1), "swap_samples_over_20_percent": swap_above,
             "swap_valid_samples": swap_count},
        )

    vram_hot, vram_median, vram_above, vram_count = _sustained(ordered, "vram_percent", 90, minimum_ratio=0.5)
    if vram_hot:
        add_finding(
            "vram_pressure", "warning", "VRAM quase cheia",
            "A VRAM dedicada reportada pelo driver ficou acima de 90% durante pelo menos metade das leituras válidas.",
            "Confira a resolução e as configurações de textura do aplicativo; não reduza qualidade automaticamente.",
            {"vram_median_percent": round(vram_median or 0, 1), "vram_samples_over_90_percent": vram_above,
             "vram_valid_samples": vram_count},
        )

    disk_hot, disk_median, disk_above, disk_count = _sustained(ordered, "disk_busy_percent", 85, minimum_ratio=0.5)
    wait_values = _metric_values(ordered, "disk_average_io_wait_ms")
    wait_median = median(wait_values) if wait_values else None
    if disk_hot and wait_median is not None and wait_median >= 20:
        add_finding(
            "possible_storage_pressure", "warning", "Possível pressão de armazenamento",
            "O dispositivo inteiro mais ocupado reportou atividade alta e tempo médio de I/O elevado. O contador do kernel é aproximado e não identifica qual aplicativo originou a fila.",
            "Confira processos com I/O e a saúde SMART; não execute testes de escrita automaticamente.",
            {"busiest_disk_median_percent": round(disk_median or 0, 1),
             "disk_samples_over_85_percent": disk_above, "disk_valid_samples": disk_count,
             "average_io_wait_median_ms": round(wait_median, 2)},
        )

    cpu_temp_hot, cpu_temp_median, cpu_temp_above, cpu_temp_count = _sustained(
        ordered, "cpu_temp_celsius", 80, minimum_ratio=0.5
    )
    if cpu_temp_hot:
        add_finding(
            "elevated_cpu_temperature", "warning", "Temperatura de CPU elevada",
            "Um sensor identificado como CPU permaneceu em 80 °C ou mais durante parte relevante da janela. O BooM não consegue confirmar throttling apenas por esta leitura.",
            "Confira fluxo de ar, perfil de energia e sensores do fabricante antes de iniciar testes de carga.",
            {"cpu_temperature_median_celsius": round(cpu_temp_median or 0, 1),
             "cpu_temperature_samples_over_80_celsius": cpu_temp_above,
             "cpu_temperature_valid_samples": cpu_temp_count},
        )

    summary = (
        "Nenhuma saturação sustentada foi identificada nos sinais disponíveis nesta janela. "
        "Isso não exclui gargalos por aplicativo, latência, rede ou eventos fora do período medido."
        if not findings
        else f"Encontrado(s) {len(findings)} sinal(is) sustentado(s); trate-os como hipóteses para investigar, não como diagnóstico definitivo."
    )
    return {
        "status": "analyzed",
        "sample_count": count,
        "window_seconds": round(span, 1),
        "coverage_percent": round(coverage * 100, 1),
        "confidence": confidence,
        "summary": summary,
        "findings": findings,
    }
