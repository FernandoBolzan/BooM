"""Bounded filesystem benchmark backed by the distribution's fio executable."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any


DISK_BENCHMARK_SIZE_BYTES = 128 * 1024**2
DISK_BENCHMARK_SIZE_MIB = DISK_BENCHMARK_SIZE_BYTES // 1024**2
DISK_BENCHMARK_REQUIRED_FREE_BYTES = DISK_BENCHMARK_SIZE_BYTES + 32 * 1024**2
DISK_BENCHMARK_TIMEOUT_SECONDS = 120
DISK_BENCHMARK_TEMP_PREFIX = ".boom-disk-benchmark-"
DISK_BENCHMARK_JOB = """[global]
ioengine=psync
direct=1
bs=1M
size=128M
iodepth=1
numjobs=1
end_fsync=1
filename=payload.bin

[boom-sequential-write]
rw=write

[boom-sequential-read]
stonewall
rw=read
"""
_NON_STORAGE_FILESYSTEMS = {"cgroup", "cgroup2", "devpts", "devtmpfs", "proc", "ramfs", "sysfs", "tmpfs"}


def _unescape_mount_field(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def mount_info_for_path(
    directory: Path,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
) -> dict[str, str] | None:
    """Return the longest matching mount entry for a resolved directory."""
    try:
        resolved = directory.resolve(strict=True)
        text = mountinfo_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, RuntimeError):
        return None

    best: dict[str, str] | None = None
    best_length = -1
    for line in text.splitlines():
        if " - " not in line:
            continue
        left, right = line.split(" - ", 1)
        fields = left.split()
        mount_fields = right.split()
        if len(fields) < 5 or len(mount_fields) < 2:
            continue
        mountpoint = Path(_unescape_mount_field(fields[4]))
        if resolved != mountpoint and mountpoint not in resolved.parents:
            continue
        if len(str(mountpoint)) <= best_length:
            continue
        best_length = len(str(mountpoint))
        best = {
            "mountpoint": str(mountpoint),
            "filesystem": mount_fields[0],
            "source": _unescape_mount_field(mount_fields[1]),
        }
    return best


def disk_benchmark_readiness(
    target_directory: str | os.PathLike[str] | None,
    *,
    mountinfo_path: Path = Path("/proc/self/mountinfo"),
) -> dict[str, Any]:
    """Validate a user-selected filesystem target without creating files."""
    executable = shutil.which("fio")
    if not executable:
        return {"ready": False, "message": "fio não está instalado; o BooM pode oferecê-lo na instalação assistida."}
    if target_directory is None or not str(target_directory).strip():
        return {"ready": False, "message": "Escolha uma pasta no volume que deseja avaliar."}
    try:
        target = Path(target_directory).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return {"ready": False, "message": f"Pasta de destino inválida: {exc}"}
    if not target.is_dir():
        return {"ready": False, "message": "O destino do benchmark precisa ser uma pasta existente."}
    if not os.access(target, os.W_OK | os.X_OK):
        return {"ready": False, "message": "Sem permissão para criar e remover arquivos nessa pasta."}
    if target == Path("/dev") or Path("/dev") in target.parents:
        return {"ready": False, "message": "Escolha uma pasta de armazenamento; o BooM não escreve em /dev."}

    mount = mount_info_for_path(target, mountinfo_path)
    if mount is None:
        return {"ready": False, "message": "Não foi possível identificar o sistema de arquivos do destino."}
    filesystem = mount["filesystem"].casefold()
    if filesystem in _NON_STORAGE_FILESYSTEMS:
        return {
            "ready": False,
            "message": f"O destino usa {mount['filesystem']}, que não mede armazenamento persistente.",
            "target": str(target),
            **mount,
        }
    try:
        free_bytes = shutil.disk_usage(target).free
    except OSError as exc:
        return {"ready": False, "message": f"Não foi possível consultar o espaço livre: {exc}"}
    if free_bytes < DISK_BENCHMARK_REQUIRED_FREE_BYTES:
        return {
            "ready": False,
            "message": "São necessários 160 MiB livres para o arquivo temporário de 128 MiB e margem de segurança.",
            "target": str(target),
            "free_bytes": free_bytes,
            **mount,
        }
    return {
        "ready": True,
        "message": "Destino gravável; o teste usa arquivo temporário e não acessa o dispositivo bruto.",
        "target": str(target),
        "free_bytes": free_bytes,
        "size_bytes": DISK_BENCHMARK_SIZE_BYTES,
        **mount,
    }


def build_fio_command(
    executable: str,
    private_directory: str | os.PathLike[str],
) -> list[str]:
    directory = Path(private_directory).resolve(strict=True)
    if not directory.is_dir() or not directory.name.startswith(DISK_BENCHMARK_TEMP_PREFIX):
        raise ValueError("O benchmark exige o diretório temporário privado criado pelo BooM.")
    if directory == Path("/dev") or Path("/dev") in directory.parents:
        raise ValueError("O benchmark nunca acessa caminhos de dispositivo.")
    job_file = directory / "boom.fio"
    output_file = directory / "result.json"
    if not job_file.is_file() or job_file.is_symlink():
        raise ValueError("Arquivo de configuração temporário ausente ou inválido.")
    return [executable, "--output-format=json", f"--output={output_file}", str(job_file)]


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _direction_stats(job: dict[str, Any], direction: str) -> dict[str, Any] | None:
    stats = job.get(direction)
    if not isinstance(stats, dict):
        return None
    byte_rate = _finite_number(stats.get("bw_bytes"))
    if byte_rate is None:
        kibibytes_per_second = _finite_number(stats.get("bw"))
        byte_rate = kibibytes_per_second * 1024 if kibibytes_per_second is not None else None
    if byte_rate is None or byte_rate <= 0:
        return None
    latency = stats.get("clat_ns")
    if not isinstance(latency, dict):
        latency = stats.get("lat_ns")
    latency_mean = _finite_number(latency.get("mean")) if isinstance(latency, dict) else None
    return {
        "bytes_per_second": byte_rate,
        "iops": _finite_number(stats.get("iops")),
        "latency_mean_ms": latency_mean / 1_000_000 if latency_mean is not None else None,
    }


def parse_fio_json(payload: str | bytes) -> dict[str, Any]:
    try:
        document = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        raise ValueError("A saída JSON do fio está inválida.") from exc
    jobs = document.get("jobs") if isinstance(document, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("A saída do fio não contém os trabalhos de leitura e escrita.")
    result: dict[str, Any] = {"size_bytes": DISK_BENCHMARK_SIZE_BYTES, "mode": "I/O sequencial direto"}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        name = str(job.get("jobname", "")).casefold()
        if "write" in name:
            direction = "write"
        elif "read" in name:
            direction = "read"
        else:
            continue
        stats = _direction_stats(job, direction)
        if stats:
            result[direction] = stats
    if "write" not in result or "read" not in result:
        raise ValueError("O fio não retornou métricas válidas para leitura e escrita sequenciais.")
    return result


def format_fio_summary(result: dict[str, Any]) -> str:
    def direction(label: str, key: str) -> str:
        stats = result[key]
        speed = stats["bytes_per_second"] / 1024**2
        iops = stats.get("iops")
        iops_text = f" · {iops:.0f} IOPS" if isinstance(iops, (int, float)) else ""
        latency = stats.get("latency_mean_ms")
        latency_text = f" · latência média {latency:.2f} ms" if isinstance(latency, (int, float)) else ""
        return f"{label} sequencial: {speed:.1f} MiB/s{iops_text}{latency_text}"

    return direction("Escrita", "write") + " · " + direction("Leitura", "read")


def _signal_process(process: subprocess.Popen[Any], force: bool = False) -> None:
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


def _remove_private_directory(directory: Path | None, target: Path | None) -> None:
    if directory is None or target is None or directory.is_symlink():
        return
    try:
        if directory.parent.resolve(strict=True) != target.resolve(strict=True):
            return
        if directory.name.startswith(DISK_BENCHMARK_TEMP_PREFIX) and directory.is_dir():
            shutil.rmtree(directory)
    except OSError:
        pass


class DiskBenchmarkSession:
    """Run fio only in a fresh private directory under the selected filesystem."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancel_requested = threading.Event()
        self._process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._private_directory: Path | None = None
        self._target: Path | None = None
        self._state: dict[str, Any] = {"status": "idle", "kind": "disk", "message": "Nenhum benchmark de disco executado."}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
        if state.get("status") in {"starting", "running"} and state.get("started_at"):
            elapsed = max(0.0, time.monotonic() - state["started_at"])
            state["elapsed_seconds"] = min(elapsed, DISK_BENCHMARK_TIMEOUT_SECONDS)
            state["remaining_seconds"] = max(0, int(DISK_BENCHMARK_TIMEOUT_SECONDS - elapsed + 0.999))
            state["message"] = f"Benchmark sequencial em andamento · {state['remaining_seconds']} s restantes"
        return state

    def is_active(self) -> bool:
        return self.snapshot().get("status") in {"starting", "running", "stopping"}

    def start(self, target_directory: str | os.PathLike[str]) -> str:
        with self._lock:
            if self._state.get("status") in {"starting", "running", "stopping"}:
                raise RuntimeError("Já existe um teste em andamento.")
            self._cancel_requested.clear()
            self._state = {"status": "starting", "kind": "disk", "message": "Validando o volume e preparando arquivo temporário…"}

        private_directory: Path | None = None
        target: Path | None = None
        process: subprocess.Popen[str] | None = None
        try:
            readiness = disk_benchmark_readiness(target_directory)
            if not readiness.get("ready"):
                raise RuntimeError(str(readiness.get("message") or "Destino de benchmark indisponível."))
            executable = shutil.which("fio")
            if not executable:
                raise RuntimeError("fio não está instalado; use a instalação assistida do BooM.")
            target = Path(str(readiness["target"]))
            private_directory = Path(tempfile.mkdtemp(prefix=DISK_BENCHMARK_TEMP_PREFIX, dir=target))
            os.chmod(private_directory, 0o700)
            job_file = private_directory / "boom.fio"
            with job_file.open("x", encoding="utf-8") as stream:
                stream.write(DISK_BENCHMARK_JOB)
            os.chmod(job_file, 0o600)
            result_file = private_directory / "result.json"
            log_file = private_directory / "fio.log"
            command = build_fio_command(executable, private_directory)
            with self._lock:
                if self._cancel_requested.is_set():
                    self._state.update({"status": "cancelled", "message": "Benchmark cancelado antes de iniciar."})
                    raise RuntimeError("Benchmark cancelado antes de iniciar.")
                self._state.update({
                    "status": "starting",
                    "target": str(target),
                    "mountpoint": readiness.get("mountpoint"),
                    "filesystem": readiness.get("filesystem"),
                    "size_bytes": DISK_BENCHMARK_SIZE_BYTES,
                    "duration_seconds": DISK_BENCHMARK_TIMEOUT_SECONDS,
                    "message": "Iniciando fio no arquivo temporário…",
                })
                with log_file.open("x", encoding="utf-8") as diagnostics:
                    process = subprocess.Popen(
                        command,
                        cwd=private_directory,
                        stdin=subprocess.DEVNULL,
                        stdout=diagnostics,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                started_at = time.monotonic()
                self._process = process
                self._private_directory = private_directory
                self._target = target
                self._state.update({
                    "status": "running",
                    "started_at": started_at,
                    "message": "Benchmark de disco iniciado.",
                })
                self._thread = threading.Thread(
                    target=self._supervise,
                    args=(process, private_directory, target, result_file, log_file, started_at),
                    name="boom-disk-benchmark-watchdog",
                    daemon=True,
                )
                try:
                    self._thread.start()
                except RuntimeError:
                    _signal_process(process, force=True)
                    try:
                        process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        pass
                    self._thread = None
                    self._process = None
                    self._private_directory = None
                    self._target = None
                    raise
        except (OSError, RuntimeError, ValueError) as exc:
            if process is not None and process.poll() is None:
                _signal_process(process, force=True)
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
            process_stopped = process is None or process.poll() is not None
            if process_stopped:
                _remove_private_directory(private_directory, target)
            with self._lock:
                if self._state.get("status") in {"starting", "stopping"}:
                    status = "cancelled" if self._cancel_requested.is_set() else "failed" if process_stopped else "stopping"
                    message = str(exc) if process_stopped else f"{exc} O processo ainda está encerrando; arquivos temporários foram preservados."
                    self._state.update({"status": status, "message": message})
                if not process_stopped:
                    self._process = process
                    self._private_directory = private_directory
                    self._target = target
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError(f"Não foi possível preparar o benchmark: {exc}") from exc
        return f"Benchmark sequencial iniciado em {target}; arquivo temporário de 128 MiB, removido ao terminar."

    def _supervise(
        self,
        process: subprocess.Popen[str],
        private_directory: Path,
        target: Path,
        result_file: Path,
        log_file: Path,
        started_at: float,
    ) -> None:
        reason: str | None = None
        deadline = started_at + DISK_BENCHMARK_TIMEOUT_SECONDS
        while process.poll() is None:
            if self._cancel_requested.wait(0.25):
                reason = "Benchmark cancelado pelo usuário."
                break
            if time.monotonic() >= deadline:
                reason = "Benchmark interrompido ao atingir o limite de 120 segundos."
                break
        if reason and process.poll() is None:
            _signal_process(process)
            try:
                process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                _signal_process(process, force=True)
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    reason += " O processo não encerrou dentro do prazo de segurança."
            if process.poll() is None:
                with self._lock:
                    self._state.update({
                        "status": "stopping",
                        "message": reason + " O diretório temporário será preservado até o encerramento do processo.",
                    })
                while process.poll() is None:
                    _signal_process(process, force=True)
                    try:
                        process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        continue

        return_code = process.poll()
        result: dict[str, Any] | None = None
        if reason is None and return_code == 0:
            try:
                result = parse_fio_json(result_file.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                reason = str(exc)
        elif reason is None:
            try:
                detail = log_file.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-3:]
            except OSError:
                detail = []
            reason = "fio encerrou com erro" + (": " + " · ".join(detail) if detail else ".")

        if process.poll() is not None:
            _remove_private_directory(private_directory, target)
        with self._lock:
            if self._cancel_requested.is_set() and reason is None:
                reason = "Benchmark cancelado pelo usuário."
            if reason:
                status = "cancelled" if self._cancel_requested.is_set() else "failed"
                message = reason
            elif result is not None:
                status = "completed"
                message = "Benchmark sequencial concluído; o arquivo temporário foi removido."
            else:
                status = "failed"
                message = "O fio terminou sem um resultado de leitura/escrita válido."
            self._state.update({
                "status": status,
                "message": message,
                "finished_at": time.monotonic(),
                "return_code": return_code,
                "result": result,
                "summary": format_fio_summary(result) if result else None,
                "temporary_files_removed": not private_directory.exists(),
            })
            self._private_directory = None
            self._process = None

    def cancel(self) -> None:
        with self._lock:
            if self._state.get("status") not in {"starting", "running", "stopping"}:
                return
            self._cancel_requested.set()
            self._state.update({"status": "stopping", "message": "Cancelando benchmark e removendo o arquivo temporário…"})
            process = self._process
        if process is not None:
            _signal_process(process)

    def stop_and_wait(self, timeout: float = 4.0) -> None:
        self.cancel()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        process = self._process
        if process is not None and process.poll() is None:
            _signal_process(process, force=True)
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                return
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        if thread is None and process is not None and process.poll() is not None:
            private_directory = self._private_directory
            target = self._target
            _remove_private_directory(private_directory, target)
            with self._lock:
                self._process = None
                self._private_directory = None
                self._target = None
                self._state.update({
                    "status": "cancelled" if self._cancel_requested.is_set() else "failed",
                    "message": "Benchmark encerrado e arquivos temporários removidos.",
                    "temporary_files_removed": not bool(private_directory and private_directory.exists()),
                })
