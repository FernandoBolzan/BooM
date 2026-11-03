"""Shared action catalog and single-active-session coordinator for Testes."""

from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any


@dataclass(frozen=True)
class TestAction:
    kind: str
    label: str
    shortcut: str


TEST_ACTIONS = (
    TestAction("cpu", "CPU", "C"),
    TestAction("memory", "RAM", "M"),
    TestAction("gpu", "GPU", "G"),
    TestAction("disk", "disco", "B"),
    TestAction("cpu-benchmark", "bench CPU", "A"),
    TestAction("memory-benchmark", "bench RAM", "E"),
)


def test_action_for_shortcut(shortcut: str) -> TestAction | None:
    normalized = str(shortcut).upper()
    return next((action for action in TEST_ACTIONS if action.shortcut == normalized), None)


def test_action_legend() -> str:
    return " · ".join(f"{action.shortcut} {action.label}" for action in TEST_ACTIONS)


class TestSessions:
    """Serialize stress, benchmark, and disk runs across the two user interfaces."""

    def __init__(self, stress_session: Any, disk_session: Any, benchmark_session: Any | None = None) -> None:
        self.stress_session = stress_session
        self.disk_session = disk_session
        self.benchmark_session = benchmark_session
        self._lock = threading.Lock()
        self._last_kind = "stress"

    def _sessions(self) -> tuple[Any, ...]:
        sessions = (self.stress_session, self.disk_session)
        return sessions + ((self.benchmark_session,) if self.benchmark_session is not None else ())

    def is_active(self) -> bool:
        return any(session.is_active() for session in self._sessions())

    def snapshot(self) -> dict[str, Any]:
        stress = self.stress_session.snapshot()
        disk = self.disk_session.snapshot()
        benchmark = self.benchmark_session.snapshot() if self.benchmark_session is not None else None
        if benchmark and benchmark.get("status") in {"starting", "running", "stopping"}:
            return benchmark
        if disk.get("status") in {"starting", "running", "stopping"}:
            return disk
        if stress.get("status") in {"starting", "running", "stopping"}:
            return stress
        if self._last_kind == "benchmark" and benchmark is not None:
            return benchmark
        return disk if self._last_kind == "disk" else stress

    def _require_idle(self) -> None:
        if self.is_active():
            raise RuntimeError("Já existe um teste em andamento.")

    def start_stress(self, *args: Any, **kwargs: Any) -> str:
        with self._lock:
            self._require_idle()
            self._last_kind = "stress"
            return self.stress_session.start(*args, **kwargs)

    def start_disk(self, target_directory: str) -> str:
        with self._lock:
            self._require_idle()
            self._last_kind = "disk"
            return self.disk_session.start(target_directory)

    def start_benchmark(self, kind: str, available_memory_bytes: int | None) -> str:
        if self.benchmark_session is None:
            raise RuntimeError("Os benchmarks sysbench não estão disponíveis nesta sessão.")
        with self._lock:
            self._require_idle()
            self._last_kind = "benchmark"
            return self.benchmark_session.start(kind, available_memory_bytes)

    def cancel(self) -> None:
        if self.benchmark_session is not None and self.benchmark_session.is_active():
            self.benchmark_session.cancel()
        elif self.disk_session.is_active():
            self.disk_session.cancel()
        else:
            self.stress_session.cancel()

    def stop_and_wait(self, timeout: float = 4.0) -> None:
        self.stress_session.stop_and_wait(timeout)
        self.disk_session.stop_and_wait(timeout)
        if self.benchmark_session is not None:
            self.benchmark_session.stop_and_wait(timeout)
