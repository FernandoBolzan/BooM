"""Bounded CPU and RAM benchmarks backed by the distribution's sysbench."""

from __future__ import annotations

import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
from typing import Any, Callable


SYSBENCH_CPU_DURATION_SECONDS = 10
SYSBENCH_MEMORY_PASS_SECONDS = 5
SYSBENCH_MAX_CPU_THREADS = 4
SYSBENCH_MEMORY_MIN_AVAILABLE_BYTES = 512 * 1024**2
SYSBENCH_MEMORY_MIN_BUFFER_BYTES = 32 * 1024**2
SYSBENCH_MEMORY_MAX_BUFFER_BYTES = 256 * 1024**2
SYSBENCH_MEMORY_BUFFER_DIVISOR = 16
SYSBENCH_PRECHECK_TEMP_C = 80.0
SYSBENCH_ABORT_TEMP_C = 85.0
SYSBENCH_KINDS = {"cpu-benchmark": "cpu", "memory-benchmark": "memory"}
_CPU_SCORE = re.compile(r"^\s*events per second:\s*([0-9]+(?:\.[0-9]+)?)\s*$", re.M)
_MEMORY_SCORE = re.compile(
    r"^\s*[0-9]+(?:\.[0-9]+)? MiB transferred \(([0-9]+(?:\.[0-9]+)?) MiB/sec\)\s*$",
    re.M,
)


def _positive_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _temperature_values(snapshot: dict[str, Any] | None) -> tuple[list[float], list[float]]:
    if not isinstance(snapshot, dict):
        return [], []

    def accepted(values: Any) -> list[float]:
        if not isinstance(values, (list, tuple)):
            return []
        result = []
        for value in values:
            if isinstance(value, dict):
                value = value.get("celsius")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            number = float(value)
            if math.isfinite(number) and -20 <= number <= 150:
                result.append(number)
        return result

    cpu = accepted(snapshot.get("cpu_temperatures"))
    system = accepted(snapshot.get("temperatures"))
    return cpu, system or cpu


def sysbench_temperature_readiness(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    cpu, system = _temperature_values(snapshot)
    if not cpu:
        return {
            "ready": False,
            "message": "Benchmark bloqueado: o BooM precisa identificar a temperatura da CPU para vigiar a execução.",
            "cpu_celsius": None,
            "current_celsius": max(system, default=None),
        }
    cpu_hottest = max(cpu)
    hottest = max(system, default=cpu_hottest)
    if hottest >= SYSBENCH_PRECHECK_TEMP_C:
        return {
            "ready": False,
            "message": f"Aguarde a temperatura cair abaixo de {SYSBENCH_PRECHECK_TEMP_C:.0f} °C antes do benchmark.",
            "cpu_celsius": cpu_hottest,
            "current_celsius": hottest,
        }
    return {
        "ready": True,
        "message": f"Sensor da CPU disponível; início abaixo de {SYSBENCH_PRECHECK_TEMP_C:.0f} °C e parada em {SYSBENCH_ABORT_TEMP_C:.0f} °C.",
        "cpu_celsius": cpu_hottest,
        "current_celsius": hottest,
    }


def sysbench_memory_buffer_bytes(available_memory_bytes: int | None) -> int:
    if isinstance(available_memory_bytes, bool) or not isinstance(available_memory_bytes, int):
        raise ValueError("Não foi possível consultar a RAM disponível para o benchmark.")
    if available_memory_bytes < SYSBENCH_MEMORY_MIN_AVAILABLE_BYTES:
        raise ValueError("Benchmark de RAM bloqueado: são necessários pelo menos 512 MiB disponíveis.")
    buffer_bytes = min(
        SYSBENCH_MEMORY_MAX_BUFFER_BYTES,
        available_memory_bytes // SYSBENCH_MEMORY_BUFFER_DIVISOR,
    )
    buffer_bytes = buffer_bytes // (1024**2) * 1024**2
    if buffer_bytes < SYSBENCH_MEMORY_MIN_BUFFER_BYTES:
        raise ValueError("A RAM disponível é insuficiente para criar o buffer limitado do benchmark.")
    return buffer_bytes


def sysbench_benchmark_plan(
    kind: str,
    available_memory_bytes: int | None,
    *,
    executable: str | None = None,
    cpu_count: int | None = None,
) -> list[dict[str, Any]]:
    """Build fixed, shell-free sysbench commands with bounded duration and memory."""
    mode = SYSBENCH_KINDS.get(kind)
    if mode is None:
        raise ValueError("Tipo de benchmark sysbench desconhecido.")
    executable = executable or shutil.which("sysbench")
    if not executable:
        raise RuntimeError("sysbench está ausente; o BooM pode oferecê-lo na instalação assistida.")

    if mode == "cpu":
        reported_cpus = cpu_count if isinstance(cpu_count, int) and cpu_count > 0 else (os.cpu_count() or 1)
        threads = max(1, min(SYSBENCH_MAX_CPU_THREADS, reported_cpus))
        return [{
            "label": "CPU",
            "metric": "cpu-events-per-second",
            "unit": "eventos/s",
            "duration_seconds": SYSBENCH_CPU_DURATION_SECONDS,
            "threads": threads,
            "buffer_bytes": None,
            "command": [
                executable,
                f"--threads={threads}",
                f"--time={SYSBENCH_CPU_DURATION_SECONDS}",
                "--report-interval=0",
                "--cpu-max-prime=20000",
                "cpu",
                "run",
            ],
        }]

    buffer_bytes = sysbench_memory_buffer_bytes(available_memory_bytes)
    plan = []
    for operation, label in (("read", "RAM · leitura sequencial"), ("write", "RAM · escrita sequencial")):
        plan.append({
            "label": label,
            "metric": f"memory-{operation}-mib-per-second",
            "unit": "MiB/s",
            "operation": operation,
            "duration_seconds": SYSBENCH_MEMORY_PASS_SECONDS,
            "threads": 1,
            "buffer_bytes": buffer_bytes,
            "command": [
                executable,
                "--threads=1",
                f"--time={SYSBENCH_MEMORY_PASS_SECONDS}",
                "--report-interval=0",
                f"--memory-block-size={buffer_bytes}",
                "--memory-total-size=1T",
                f"--memory-oper={operation}",
                "--memory-access-mode=seq",
                "--memory-scope=global",
                "memory",
                "run",
            ],
        })
    return plan


def sysbench_benchmark_readiness(
    kind: str,
    available_memory_bytes: int | None,
    temperature_snapshot: dict[str, Any] | None,
    *,
    executable: str | None = None,
) -> dict[str, Any]:
    try:
        plan = sysbench_benchmark_plan(kind, available_memory_bytes, executable=executable)
    except (RuntimeError, ValueError) as exc:
        return {"ready": False, "message": str(exc), "duration_seconds": None, "buffer_bytes": None}
    thermal = sysbench_temperature_readiness(temperature_snapshot)
    return {
        "ready": thermal["ready"],
        "message": thermal["message"],
        "duration_seconds": sum(step["duration_seconds"] for step in plan),
        "buffer_bytes": plan[0].get("buffer_bytes"),
        "threads": plan[0]["threads"],
        "steps": [step["label"] for step in plan],
        "current_celsius": thermal.get("current_celsius"),
        "cpu_celsius": thermal.get("cpu_celsius"),
    }


def parse_sysbench_result(step: dict[str, Any], output: str) -> dict[str, Any] | None:
    if step.get("metric") == "cpu-events-per-second":
        match = _CPU_SCORE.search(output)
    elif str(step.get("metric", "")).startswith("memory-"):
        match = _MEMORY_SCORE.search(output)
    else:
        return None
    value = _positive_number(float(match.group(1))) if match else None
    if value is None:
        return None
    return {
        "label": str(step["label"]),
        "metric": str(step["metric"]),
        "value": value,
        "unit": str(step["unit"]),
        "threads": int(step["threads"]),
        "buffer_bytes": step.get("buffer_bytes"),
        "duration_seconds": int(step["duration_seconds"]),
    }


def format_sysbench_results(results: list[dict[str, Any]], *, partial: bool = False) -> str:
    lines = []
    for item in results:
        value = f"{float(item['value']):,.2f}"
        value = value.replace(",", "\0").replace(".", ",").replace("\0", ".")
        detail = f"{item['label']}: {value} {item['unit']} · {item['threads']} thread(s)"
        if item.get("buffer_bytes"):
            detail += f" · buffer {int(item['buffer_bytes']) // 1024**2} MiB"
        lines.append(detail)
    summary = "\n".join(lines)
    return f"Resultados parciais (benchmark interrompido; não compare):\n{summary}" if partial and summary else summary


class SysbenchBenchmarkSession:
    """Run CPU or sequential RAM benchmarks with a live CPU thermal guard."""

    def __init__(self, temperature_provider: Callable[[], dict[str, Any]]) -> None:
        self._temperature_provider = temperature_provider
        self._lock = threading.Lock()
        self._cancel_requested = threading.Event()
        self._process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {"status": "idle", "message": "Nenhum benchmark executado."}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
        if state.get("status") == "running":
            elapsed = max(0.0, time.monotonic() - state["started_at"])
            duration = float(state["duration_seconds"])
            state["elapsed_seconds"] = min(elapsed, duration)
            state["remaining_seconds"] = max(0, int(duration - elapsed + 0.999))
            current = state.get("current_celsius")
            temperature = f" · CPU {current:.1f} °C" if isinstance(current, (int, float)) else ""
            step = state.get("step_label") or state.get("kind_label") or "sysbench"
            state["message"] = f"{step} em execução · {state['remaining_seconds']} s restantes{temperature}"
        return state

    def is_active(self) -> bool:
        return self.snapshot().get("status") in {"starting", "running", "stopping"}

    def start(self, kind: str, available_memory_bytes: int | None) -> str:
        executable = shutil.which("sysbench")
        plan = sysbench_benchmark_plan(kind, available_memory_bytes, executable=executable)
        duration = sum(step["duration_seconds"] for step in plan)
        mode = SYSBENCH_KINDS[kind]
        kind_label = "CPU" if mode == "cpu" else "RAM"
        with self._lock:
            if self._state.get("status") in {"starting", "running", "stopping"}:
                raise RuntimeError("Já existe um benchmark em andamento.")
            self._cancel_requested.clear()
            self._state = {
                "status": "starting",
                "kind": kind,
                "kind_label": f"Benchmark de {kind_label}",
                "duration_seconds": duration,
                "current_celsius": None,
                "cpu_celsius": None,
                "buffer_bytes": plan[0].get("buffer_bytes"),
                "results": [],
                "summary": None,
                "message": f"Preparando benchmark de {kind_label}…",
            }
            self._thread = threading.Thread(
                target=self._run,
                args=(kind, plan),
                name="boom-sysbench-benchmark",
                daemon=True,
            )
            self._thread.start()
        return f"Benchmark de {kind_label} iniciado por até {duration} s."

    def _thermal_stop_reason(self) -> tuple[str | None, float | None, float | None]:
        try:
            snapshot = self._temperature_provider()
        except Exception:
            return "Benchmark interrompido: não foi possível atualizar os sensores térmicos.", None, None
        cpu, system = _temperature_values(snapshot)
        if not cpu:
            return "Benchmark interrompido: a temperatura da CPU ficou indisponível.", None, None
        hottest = max(system, default=max(cpu))
        cpu_hottest = max(cpu)
        if hottest >= SYSBENCH_ABORT_TEMP_C:
            return (
                f"Benchmark interrompido ao atingir {hottest:.1f} °C (limite {SYSBENCH_ABORT_TEMP_C:.0f} °C).",
                hottest,
                cpu_hottest,
            )
        return None, hottest, cpu_hottest

    @staticmethod
    def _signal(process: subprocess.Popen[str], *, force: bool = False) -> None:
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                process.kill() if force else process.terminate()
            except OSError:
                pass

    def _stop_process(self, process: subprocess.Popen[str]) -> str:
        self._signal(process)
        try:
            output, _ = process.communicate(timeout=3.0)
            return output or ""
        except subprocess.TimeoutExpired:
            self._signal(process, force=True)
            try:
                output, _ = process.communicate(timeout=2.0)
            except subprocess.TimeoutExpired:
                output = ""
            return output or ""

    def _run(self, kind: str, plan: list[dict[str, Any]]) -> None:
        started_at = time.monotonic()
        results: list[dict[str, Any]] = []
        stop_reason: str | None = None
        failed_reason: str | None = None
        completed_steps = 0

        try:
            readiness = sysbench_temperature_readiness(self._temperature_provider())
        except Exception:
            readiness = {"ready": False, "message": "Não foi possível consultar os sensores térmicos antes do benchmark."}
        if self._cancel_requested.is_set():
            with self._lock:
                self._state.update({
                    "status": "cancelled",
                    "message": "Benchmark cancelado antes de iniciar.",
                    "finished_at": time.monotonic(),
                })
            return
        if not readiness["ready"]:
            with self._lock:
                self._state.update({
                    "status": "failed",
                    "message": readiness["message"],
                    "finished_at": time.monotonic(),
                })
            return
        with self._lock:
            self._state["current_celsius"] = readiness.get("current_celsius")
            self._state["cpu_celsius"] = readiness.get("cpu_celsius")

        for index, step in enumerate(plan):
            if self._cancel_requested.is_set():
                stop_reason = "Benchmark cancelado pelo usuário."
                break
            command = list(step["command"])
            environment = os.environ.copy()
            environment.update({"LC_ALL": "C", "LANG": "C"})
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=environment,
                    start_new_session=True,
                    close_fds=True,
                )
            except OSError as exc:
                failed_reason = f"Não foi possível iniciar sysbench: {exc}"
                break

            step_started = time.monotonic()
            with self._lock:
                self._process = process
                self._state.update({
                    "status": "running",
                    "started_at": started_at,
                    "step_label": step["label"],
                    "step_index": index,
                    "current_celsius": None,
                })
            step_deadline = step_started + float(step["duration_seconds"]) + 3.0
            while process.poll() is None:
                if self._cancel_requested.wait(1.0):
                    stop_reason = "Benchmark cancelado pelo usuário."
                    break
                thermal_reason, hottest, cpu_hottest = self._thermal_stop_reason()
                with self._lock:
                    self._state["current_celsius"] = hottest
                    self._state["cpu_celsius"] = cpu_hottest
                if thermal_reason:
                    stop_reason = thermal_reason
                    break
                if time.monotonic() >= step_deadline:
                    stop_reason = "Benchmark interrompido ao exceder o limite de duração."
                    break
            if self._cancel_requested.is_set() and stop_reason is None:
                stop_reason = "Benchmark cancelado pelo usuário."
            if stop_reason:
                output = self._stop_process(process) if process.poll() is None else (process.communicate()[0] or "")
                break
            try:
                output, _ = process.communicate(timeout=3.0)
            except subprocess.TimeoutExpired:
                self._signal(process, force=True)
                output, _ = process.communicate()
            if process.returncode != 0:
                failed_reason = f"sysbench encerrou com código {process.returncode}: {(output or '').strip()[-800:]}".strip()
                break
            result = parse_sysbench_result(step, output or "")
            if result is None:
                failed_reason = "sysbench terminou sem fornecer uma métrica reconhecida; nenhum resultado será estimado."
                break
            results.append(result)
            completed_steps += 1
            with self._lock:
                self._state["results"] = list(results)
                self._state["summary"] = format_sysbench_results(results)
            if index + 1 < len(plan):
                time.sleep(0.25)

        partial = bool(results) and (stop_reason is not None or failed_reason is not None or completed_steps < len(plan))
        with self._lock:
            if stop_reason:
                status = "cancelled" if self._cancel_requested.is_set() else "aborted"
                message = stop_reason
            elif failed_reason:
                status, message = "failed", failed_reason
            elif completed_steps == len(plan):
                status = "completed"
                message = f"Benchmark de {'CPU' if SYSBENCH_KINDS[kind] == 'cpu' else 'RAM'} concluído."
            else:
                status, message = "failed", "Benchmark encerrado sem resultados completos."
            self._state.update({
                "status": status,
                "message": message,
                "results": list(results),
                "summary": format_sysbench_results(results, partial=partial) if results else failed_reason,
                "finished_at": time.monotonic(),
                "return_code": self._process.returncode if self._process else None,
            })
            self._process = None

    def cancel(self) -> None:
        with self._lock:
            if self._state.get("status") not in {"starting", "running", "stopping"}:
                return
            self._cancel_requested.set()
            self._state["status"] = "stopping"
            self._state["message"] = "Cancelando benchmark…"
            process = self._process
        if process is not None and process.poll() is None:
            self._signal(process)

    def stop_and_wait(self, timeout: float = 4.0) -> None:
        self.cancel()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            self._signal(process, force=True)
