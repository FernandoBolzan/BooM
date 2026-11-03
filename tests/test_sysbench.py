import unittest
from unittest.mock import MagicMock, patch

import boom_sysbench


class SysbenchPlanTests(unittest.TestCase):
    def test_cpu_plan_caps_workers_and_uses_a_fixed_short_shell_free_command(self):
        plan = boom_sysbench.sysbench_benchmark_plan(
            "cpu-benchmark", None, executable="/usr/bin/sysbench", cpu_count=32,
        )
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["duration_seconds"], 10)
        self.assertEqual(plan[0]["threads"], 4)
        self.assertIn("--threads=4", plan[0]["command"])
        self.assertIn("--time=10", plan[0]["command"])
        self.assertEqual(plan[0]["command"][-2:], ["cpu", "run"])
        self.assertNotIn("shell", plan[0])

    def test_cpu_plan_uses_one_thread_on_single_core_hardware(self):
        plan = boom_sysbench.sysbench_benchmark_plan(
            "cpu-benchmark", None, executable="/usr/bin/sysbench", cpu_count=1,
        )
        self.assertEqual(plan[0]["threads"], 1)
        self.assertIn("--threads=1", plan[0]["command"])

    def test_memory_plan_runs_read_and_write_with_a_bounded_private_buffer(self):
        plan = boom_sysbench.sysbench_benchmark_plan(
            "memory-benchmark", 8 * 1024**3, executable="/usr/bin/sysbench",
        )
        self.assertEqual([step["operation"] for step in plan], ["read", "write"])
        self.assertEqual(sum(step["duration_seconds"] for step in plan), 10)
        self.assertEqual(plan[0]["buffer_bytes"], 256 * 1024**2)
        for step in plan:
            self.assertEqual(step["threads"], 1)
            self.assertIn("--memory-access-mode=seq", step["command"])
            self.assertIn("--memory-scope=global", step["command"])
            self.assertIn("--memory-total-size=1T", step["command"])
            self.assertIn(f"--memory-block-size={step['buffer_bytes']}", step["command"])
            self.assertEqual(step["command"][-2:], ["memory", "run"])

    def test_memory_buffer_is_one_sixteenth_of_available_until_the_cap(self):
        self.assertEqual(
            boom_sysbench.sysbench_memory_buffer_bytes(1024**3),
            64 * 1024**2,
        )
        self.assertEqual(
            boom_sysbench.sysbench_memory_buffer_bytes(512 * 1024**2),
            32 * 1024**2,
        )
        with self.assertRaisesRegex(ValueError, "512 MiB"):
            boom_sysbench.sysbench_memory_buffer_bytes(511 * 1024**2)

    def test_plan_rejects_unknown_kind_and_missing_binary(self):
        with self.assertRaises(ValueError):
            boom_sysbench.sysbench_benchmark_plan("gpu-benchmark", None, executable="/usr/bin/sysbench")
        with patch("boom_sysbench.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "sysbench está ausente"):
                boom_sysbench.sysbench_benchmark_plan("cpu-benchmark", None)


class SysbenchReadinessTests(unittest.TestCase):
    def test_cpu_benchmark_requires_a_valid_identified_cpu_sensor(self):
        result = boom_sysbench.sysbench_benchmark_readiness(
            "cpu-benchmark", None, {"cpu_temperatures": [], "temperatures": [52]},
            executable="/usr/bin/sysbench",
        )
        self.assertFalse(result["ready"])
        self.assertIn("identificar a temperatura", result["message"])

    def test_benchmark_refuses_hot_system_before_start(self):
        result = boom_sysbench.sysbench_benchmark_readiness(
            "cpu-benchmark", None,
            {"cpu_temperatures": [82], "temperatures": [82]},
            executable="/usr/bin/sysbench",
        )
        self.assertFalse(result["ready"])
        self.assertIn("abaixo de 80 °C", result["message"])

    def test_memory_readiness_enforces_the_minimum_available_memory(self):
        result = boom_sysbench.sysbench_benchmark_readiness(
            "memory-benchmark", 400 * 1024**2,
            {"cpu_temperatures": [48], "temperatures": [48]},
            executable="/usr/bin/sysbench",
        )
        self.assertFalse(result["ready"])
        self.assertIn("512 MiB", result["message"])

    def test_readiness_reports_duration_buffer_and_guard_limits(self):
        result = boom_sysbench.sysbench_benchmark_readiness(
            "memory-benchmark", 8 * 1024**3,
            {"cpu_temperatures": [55], "temperatures": [55, 62]},
            executable="/usr/bin/sysbench",
        )
        self.assertTrue(result["ready"])
        self.assertEqual(result["duration_seconds"], 10)
        self.assertEqual(result["buffer_bytes"], 256 * 1024**2)
        self.assertEqual(result["current_celsius"], 62)


class SysbenchOutputTests(unittest.TestCase):
    def test_cpu_events_per_second_is_parsed_as_a_finite_positive_score(self):
        plan = boom_sysbench.sysbench_benchmark_plan(
            "cpu-benchmark", None, executable="/usr/bin/sysbench", cpu_count=2,
        )[0]
        result = boom_sysbench.parse_sysbench_result(
            plan, "CPU speed:\n    events per second: 18319.98\n",
        )
        self.assertEqual(result["value"], 18319.98)
        self.assertEqual(result["unit"], "eventos/s")

    def test_memory_mib_per_second_is_parsed(self):
        plan = boom_sysbench.sysbench_benchmark_plan(
            "memory-benchmark", 2 * 1024**3, executable="/usr/bin/sysbench",
        )[0]
        result = boom_sysbench.parse_sysbench_result(
            plan, "1024.00 MiB transferred (8123.45 MiB/sec)\n",
        )
        self.assertEqual(result["value"], 8123.45)
        self.assertEqual(result["buffer_bytes"], 128 * 1024**2)

    def test_parser_rejects_missing_zero_infinite_and_unknown_metrics(self):
        cpu = boom_sysbench.sysbench_benchmark_plan(
            "cpu-benchmark", None, executable="/usr/bin/sysbench",
        )[0]
        memory = boom_sysbench.sysbench_benchmark_plan(
            "memory-benchmark", 2 * 1024**3, executable="/usr/bin/sysbench",
        )[0]
        for step, output in (
            (cpu, "events per second: 0"),
            (cpu, "events per second: nan"),
            (cpu, "no known score"),
            (memory, "1024.0 MiB transferred (inf MiB/sec)"),
            ({"metric": "unknown"}, "events per second: 10"),
        ):
            with self.subTest(output=output):
                self.assertIsNone(boom_sysbench.parse_sysbench_result(step, output))

    def test_results_are_reported_with_units_and_partial_runs_are_explicit(self):
        memory = boom_sysbench.sysbench_benchmark_plan(
            "memory-benchmark", 2 * 1024**3, executable="/usr/bin/sysbench",
        )[0]
        result = boom_sysbench.parse_sysbench_result(
            memory, "1024.00 MiB transferred (8123.45 MiB/sec)",
        )
        summary = boom_sysbench.format_sysbench_results([result])
        partial = boom_sysbench.format_sysbench_results([result], partial=True)
        self.assertIn("RAM · leitura sequencial", summary)
        self.assertIn("8.123,45 MiB/s", summary)
        self.assertIn("buffer 128 MiB", summary)
        self.assertIn("não compare", partial)


class SysbenchSessionTests(unittest.TestCase):
    @staticmethod
    def _process(output):
        process = MagicMock()
        process.pid = 111
        process.returncode = 0
        process.poll.return_value = 0
        process.communicate.return_value = (output, None)
        return process

    def test_cpu_session_uses_fixed_environment_and_returns_measured_result(self):
        process = self._process("CPU speed:\n    events per second: 12000.25\n")
        session = boom_sysbench.SysbenchBenchmarkSession(
            lambda: {"cpu_temperatures": [55], "temperatures": [55, 61]},
        )
        with patch("boom_sysbench.shutil.which", return_value="/usr/bin/sysbench"), patch(
            "boom_sysbench.subprocess.Popen", return_value=process,
        ) as popen:
            session.start("cpu-benchmark", None)
            session._thread.join(timeout=2)
        result = session.snapshot()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["results"][0]["value"], 12000.25)
        self.assertIn("12.000,25 eventos/s", result["summary"])
        self.assertEqual(popen.call_args.args[0][0], "/usr/bin/sysbench")
        self.assertEqual(popen.call_args.kwargs["env"]["LC_ALL"], "C")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertNotIn("shell", popen.call_args.kwargs)

    def test_ram_session_runs_both_read_and_write_passes(self):
        processes = [
            self._process("1024.00 MiB transferred (7000.00 MiB/sec)"),
            self._process("1024.00 MiB transferred (6500.00 MiB/sec)"),
        ]
        session = boom_sysbench.SysbenchBenchmarkSession(
            lambda: {"cpu_temperatures": [51], "temperatures": [51]},
        )
        with patch("boom_sysbench.shutil.which", return_value="/usr/bin/sysbench"), patch(
            "boom_sysbench.subprocess.Popen", side_effect=processes,
        ) as popen:
            session.start("memory-benchmark", 8 * 1024**3)
            session._thread.join(timeout=3)
        result = session.snapshot()
        self.assertEqual(result["status"], "completed")
        self.assertEqual([item["metric"] for item in result["results"]], [
            "memory-read-mib-per-second", "memory-write-mib-per-second",
        ])
        self.assertEqual(popen.call_count, 2)

    def test_session_refuses_to_start_without_cpu_thermal_telemetry(self):
        session = boom_sysbench.SysbenchBenchmarkSession(lambda: {"cpu_temperatures": [], "temperatures": []})
        with patch("boom_sysbench.shutil.which", return_value="/usr/bin/sysbench"), patch(
            "boom_sysbench.subprocess.Popen",
        ) as popen:
            session.start("cpu-benchmark", None)
            session._thread.join(timeout=2)
        self.assertEqual(session.snapshot()["status"], "failed")
        self.assertIn("temperatura da CPU", session.snapshot()["message"])
        popen.assert_not_called()

    def test_session_aborts_when_the_live_temperature_crosses_the_limit(self):
        process = self._process("")
        process.poll.side_effect = [None, None, None]
        thermal_states = iter((
            {"cpu_temperatures": [50], "temperatures": [50]},
            {"cpu_temperatures": [86], "temperatures": [86]},
        ))
        session = boom_sysbench.SysbenchBenchmarkSession(lambda: next(thermal_states))
        with patch("boom_sysbench.shutil.which", return_value="/usr/bin/sysbench"), patch(
            "boom_sysbench.subprocess.Popen", return_value=process,
        ), patch.object(boom_sysbench.SysbenchBenchmarkSession, "_signal") as signal_process:
            session.start("cpu-benchmark", None)
            session._thread.join(timeout=3)
        result = session.snapshot()
        self.assertEqual(result["status"], "aborted")
        self.assertIn("86.0 °C", result["message"])
        signal_process.assert_called_once_with(process)


if __name__ == "__main__":
    unittest.main()
