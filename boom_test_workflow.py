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
)


def test_action_for_shortcut(shortcut: str) -> TestAction | None:
    normalized = str(shortcut).upper()
    return next((action for action in TEST_ACTIONS if action.shortcut == normalized), None)


def test_action_legend() -> str:
    return " · ".join(f"{action.shortcut} {action.label}" for action in TEST_ACTIONS)


class TestSessions:
    """Serialize stress and disk runs and expose one result to both UIs."""

    def __init__(self, stress_session: Any, disk_session: Any) -> None:
        self.stress_session = stress_session
        self.disk_session = disk_session
        self._lock = threading.Lock()
        self._last_kind = "stress"

    def is_active(self) -> bool:
        return self.stress_session.is_active() or self.disk_session.is_active()

    def snapshot(self) -> dict[str, Any]:
        stress = self.stress_session.snapshot()
        disk = self.disk_session.snapshot()
        if disk.get("status") in {"starting", "running", "stopping"}:
            return disk
        if stress.get("status") in {"starting", "running", "stopping"}:
            return stress
        return disk if self._last_kind == "disk" else stress

    def _require_idle(self) -> None:
        if self.stress_session.is_active() or self.disk_session.is_active():
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

    def cancel(self) -> None:
        if self.disk_session.is_active():
            self.disk_session.cancel()
        else:
            self.stress_session.cancel()

    def stop_and_wait(self, timeout: float = 4.0) -> None:
        self.stress_session.stop_and_wait(timeout)
        self.disk_session.stop_and_wait(timeout)
