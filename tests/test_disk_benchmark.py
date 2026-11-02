import json
import signal
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import boom_disk_benchmark as disk
from boom_test_workflow import TEST_ACTIONS, TestSessions, test_action_for_shortcut, test_action_legend


def mount_entry(mountpoint: str, filesystem: str = "ext4", source: str = "/dev/test") -> str:
    return f"42 1 8:1 / {mountpoint} rw,relatime - {filesystem} {source} rw\n"


class DiskBenchmarkTests(unittest.TestCase):
    def test_job_file_is_bounded_sequential_direct_file_io(self):
        self.assertIn("direct=1", disk.DISK_BENCHMARK_JOB)
        self.assertIn("size=128M", disk.DISK_BENCHMARK_JOB)
        self.assertIn("end_fsync=1", disk.DISK_BENCHMARK_JOB)
        self.assertIn("rw=write", disk.DISK_BENCHMARK_JOB)
        self.assertIn("rw=read", disk.DISK_BENCHMARK_JOB)
        self.assertIn("filename=payload.bin", disk.DISK_BENCHMARK_JOB)
        self.assertNotIn("/dev/", disk.DISK_BENCHMARK_JOB)

    def test_command_passes_only_a_booM_job_file_to_fio(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / f"{disk.DISK_BENCHMARK_TEMP_PREFIX}case"
            directory.mkdir()
            job_file = directory / "boom.fio"
            job_file.write_text(disk.DISK_BENCHMARK_JOB, encoding="utf-8")
            command = disk.build_fio_command("/usr/bin/fio", directory)
        self.assertEqual(command[0:2], ["/usr/bin/fio", "--output-format=json"])
        self.assertIn("--output=", command[2])
        self.assertTrue(command[-1].endswith("/boom.fio"))
        self.assertFalse(any(argument.startswith("/dev/") for argument in command))

    def test_command_rejects_non_private_or_device_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            regular = Path(temporary) / "ordinary-directory"
            regular.mkdir()
            with self.assertRaises(ValueError):
                disk.build_fio_command("/usr/bin/fio", regular)

    def test_parser_reads_separate_write_and_read_metrics(self):
        payload = json.dumps({
            "jobs": [
                {"jobname": "boom-sequential-write", "write": {"bw_bytes": 52_428_800, "iops": 50, "clat_ns": {"mean": 2_000_000}}},
                {"jobname": "boom-sequential-read", "read": {"bw": 51_200, "iops": 50, "lat_ns": {"mean": 1_000_000}}},
            ]
        })
        result = disk.parse_fio_json(payload)
        self.assertEqual(result["write"]["bytes_per_second"], 52_428_800)
        self.assertEqual(result["read"]["bytes_per_second"], 51_200 * 1024)
        self.assertEqual(result["write"]["latency_mean_ms"], 2.0)
        self.assertIn("Escrita sequencial", disk.format_fio_summary(result))
        self.assertIn("Leitura sequencial", disk.format_fio_summary(result))
        self.assertIn("50 IOPS", disk.format_fio_summary(result))

    def test_parser_rejects_invalid_or_incomplete_fio_output(self):
        with self.assertRaises(ValueError):
            disk.parse_fio_json("not json")
        with self.assertRaisesRegex(ValueError, "leitura e escrita"):
            disk.parse_fio_json('{"jobs": []}')

    def test_readiness_requires_fio_and_existing_writable_persistent_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            mountinfo = target / "mountinfo"
            mountinfo.write_text(mount_entry("/"), encoding="utf-8")
            with patch("boom_disk_benchmark.shutil.which", return_value=None):
                missing = disk.disk_benchmark_readiness(target, mountinfo_path=mountinfo)
            self.assertFalse(missing["ready"])
            self.assertIn("fio", missing["message"])

            with patch("boom_disk_benchmark.shutil.which", return_value="/usr/bin/fio"), patch(
                "boom_disk_benchmark.shutil.disk_usage", return_value=SimpleNamespace(free=disk.DISK_BENCHMARK_REQUIRED_FREE_BYTES - 1)
            ):
                low_space = disk.disk_benchmark_readiness(target, mountinfo_path=mountinfo)
            self.assertFalse(low_space["ready"])
            self.assertIn("160 MiB", low_space["message"])

    def test_readiness_refuses_tmpfs_and_device_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            mountinfo = target / "mountinfo"
            mountinfo.write_text(mount_entry("/", "tmpfs", "tmpfs"), encoding="utf-8")
            with patch("boom_disk_benchmark.shutil.which", return_value="/usr/bin/fio"):
                result = disk.disk_benchmark_readiness(target, mountinfo_path=mountinfo)
            self.assertFalse(result["ready"])
            self.assertIn("não mede armazenamento persistente", result["message"])

    def test_session_cancel_removes_only_its_private_temp_directory(self):
        class FakeProcess:
            pid = 7442

            def __init__(self):
                self.returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout):
                self.returncode = -signal.SIGTERM
                return self.returncode

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary)
            process = FakeProcess()
            session = disk.DiskBenchmarkSession()
            with patch("boom_disk_benchmark.shutil.which", return_value="/usr/bin/fio"), patch(
                "boom_disk_benchmark.shutil.disk_usage", return_value=SimpleNamespace(free=disk.DISK_BENCHMARK_REQUIRED_FREE_BYTES)
            ), patch("boom_disk_benchmark.mount_info_for_path", return_value={"mountpoint": temporary, "filesystem": "ext4", "source": "/dev/test"}), patch(
                "boom_disk_benchmark.subprocess.Popen", return_value=process
            ) as popen, patch("boom_disk_benchmark.os.killpg"):
                session.start(str(target))
                private_directory = Path(popen.call_args.kwargs["cwd"])
                self.assertTrue(private_directory.name.startswith(disk.DISK_BENCHMARK_TEMP_PREFIX))
                self.assertEqual(private_directory.stat().st_mode & 0o777, 0o700)
                self.assertEqual(popen.call_args.args[0][0], "/usr/bin/fio")
                self.assertEqual(popen.call_args.args[0][-1], str(private_directory / "boom.fio"))
                session.cancel()
                session._thread.join(timeout=2)
            snapshot = session.snapshot()
            self.assertEqual(snapshot["status"], "cancelled")
            self.assertTrue(snapshot["temporary_files_removed"])
            self.assertFalse(private_directory.exists())


class SharedTestWorkflowTests(unittest.TestCase):
    def test_action_catalog_is_unique_and_has_a_disk_benchmark_shortcut(self):
        self.assertEqual(len({action.kind for action in TEST_ACTIONS}), len(TEST_ACTIONS))
        self.assertEqual(len({action.shortcut for action in TEST_ACTIONS}), len(TEST_ACTIONS))
        self.assertEqual(test_action_for_shortcut("b").kind, "disk")
        self.assertIn("B disco", test_action_legend())

    def test_coordinator_allows_only_one_active_test_and_routes_cancel(self):
        class FakeSession:
            def __init__(self):
                self.active = False
                self.cancelled = False
                self.stopped = False

            def is_active(self):
                return self.active

            def snapshot(self):
                return {"status": "running" if self.active else "idle", "kind": "fake"}

            def start(self, *args):
                self.active = True
                return "started"

            def cancel(self):
                self.cancelled = True
                self.active = False

            def stop_and_wait(self, timeout=4.0):
                self.stopped = True
                self.active = False

        stress = FakeSession()
        disk_session = FakeSession()
        coordinator = TestSessions(stress, disk_session)
        self.assertEqual(coordinator.start_stress("cpu"), "started")
        with self.assertRaisesRegex(RuntimeError, "Já existe"):
            coordinator.start_disk("/tmp")
        coordinator.cancel()
        self.assertTrue(stress.cancelled)
        self.assertFalse(disk_session.cancelled)
        coordinator.stop_and_wait()
        self.assertTrue(stress.stopped)
        self.assertTrue(disk_session.stopped)


if __name__ == "__main__":
    unittest.main()
