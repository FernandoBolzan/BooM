import contextlib
import inspect
import io
import json
import os
import signal
import stat
import sys
import tempfile
import types
import unittest
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import boom
import boom_performance


class FormatTests(unittest.TestCase):
    def test_release_version_is_declared(self):
        self.assertEqual(boom.__version__, "0.2.1")

    def test_version_command_prints_release_version(self):
        output = io.StringIO()
        with patch.object(boom.sys, "argv", ["boom.py", "--version"]):
            with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as result:
                boom.main()
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), "BooM 0.2.1")

    def test_byte_units_include_mb_and_gb(self):
        self.assertEqual(boom.format_bytes(10 * 1024**2), "10.0 MB")
        self.assertEqual(boom.format_bytes(10 * 1024**3), "10.0 GB")

    def test_format_unavailable_values(self):
        self.assertEqual(boom.format_bytes(None), boom.UNAVAILABLE)
        labels = boom.format_tray_metrics({"gpu_metrics": []})
        self.assertEqual(labels["gpu"], f"GPU: {boom.UNAVAILABLE}")
        self.assertEqual(labels["cpu_clock"], f"Frequência reportada média da CPU: {boom.UNAVAILABLE}")

    def test_navigation_has_dedicated_memory_and_graphics_areas(self):
        self.assertEqual(boom.NAVIGATION_PAGES, (
            "Visão geral", "Sistema", "Memória", "Gráficos", "Discos",
            "Rede", "Processos", "Programas", "Inicialização", "Testes",
        ))
        self.assertEqual(boom.PAGE_MEMORY, 2)
        self.assertEqual(boom.PAGE_GRAPHICS, 3)
        self.assertEqual(boom.PAGE_DISKS, 4)
        self.assertEqual(boom.PAGE_TESTS, 9)
        self.assertEqual(len(boom.NAVIGATION_PAGES), len(boom.NAVIGATION_DESCRIPTIONS))

    def test_uptime(self):
        self.assertEqual(boom.format_uptime(90061), "1d 01h 01min")


class PerformanceHistoryTests(unittest.TestCase):
    def test_performance_sample_keeps_aggregate_metrics_without_hardware_names(self):
        sample = boom_performance.extract_performance_sample({
            "cpu_percent": 42,
            "memory": {"used": 75, "total": 100, "swap_used": 10, "swap_total": 50},
            "gpu_metrics": [{
                "name": "PRIVATE GPU NAME", "gpu_utilization_percent": 60,
                "vram_used_mib": 9, "vram_total_mib": 10, "temperature_celsius": 70,
            }],
            "temperatures": [
                {"name": "k10temp · Tctl", "celsius": 65},
                {"name": "nvme · Composite", "celsius": 80},
            ],
            "disk_io": {"busy_percent": 45, "read_bytes_per_second": 512},
        }, sampled_at=1234)
        self.assertEqual(sample["sampled_at"], 1234)
        self.assertEqual(sample["cpu_percent"], 42)
        self.assertEqual(sample["ram_percent"], 75)
        self.assertEqual(sample["swap_percent"], 20)
        self.assertEqual(sample["gpu_percent"], 60)
        self.assertEqual(sample["vram_percent"], 90)
        self.assertEqual(sample["cpu_temp_celsius"], 65)
        self.assertEqual(sample["gpu_temp_celsius"], 70)
        self.assertNotIn("PRIVATE GPU NAME", str(sample))
        self.assertNotIn("name", sample)

    def test_diskstats_parser_uses_only_whole_devices_and_correct_fields(self):
        payload = (
            "259 0 nvme0n1 100 0 200 0 150 0 300 0 0 50 70 0 0 0 0 0 0\n"
            "259 1 nvme0n1p1 10 0 20 0 15 0 30 0 0 5 7 0 0 0 0 0 0\n"
            "8 0 sda not-a-number 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n"
        )
        parsed = boom_performance.parse_proc_diskstats(payload, whole_devices={"nvme0n1", "sda"})
        self.assertEqual(list(parsed), ["nvme0n1"])
        self.assertEqual(parsed["nvme0n1"]["sectors_read"], 200)
        self.assertEqual(parsed["nvme0n1"]["sectors_written"], 300)
        self.assertEqual(parsed["nvme0n1"]["io_milliseconds"], 50)
        self.assertEqual(parsed["nvme0n1"]["weighted_io_milliseconds"], 70)


class FirmwareTests(unittest.TestCase):
    def test_fwupd_inventory_keeps_safe_fields_and_matches_available_releases(self):
        inventory = boom.parse_fwupd_inventory(
            {"Devices": [{
                "DeviceId": "device-1",
                "Name": "System Firmware",
                "Vendor": "Example Vendor",
                "Version": "1.0",
                "Serial": "PRIVATE-SERIAL",
                "Flags": ["updatable", "needs-reboot", "require-ac"],
            }]},
            {"Devices": [{
                "DeviceId": "device-1",
                "Name": "System Firmware",
                "Releases": [{"Version": "2.0"}],
            }]},
        )
        self.assertEqual(inventory["devices"], [{
            "device_id": "device-1",
            "name": "System Firmware",
            "vendor": "Example Vendor",
            "version": "1.0",
            "updatable": True,
            "needs_reboot": True,
            "requires_ac": True,
        }])
        self.assertEqual(inventory["updates"], [{
            "device_id": "device-1",
            "name": "System Firmware",
            "current_version": "1.0",
            "update_version": "2.0",
            "needs_reboot": True,
            "requires_ac": True,
        }])
        self.assertNotIn("PRIVATE-SERIAL", str(inventory))

    def test_fwupd_query_uses_read_only_json_commands_and_cached_metadata(self):
        responses = [
            subprocess.CompletedProcess([], 0, '{"Devices": [{"DeviceId": "d1", "Name": "BIOS", "Flags": ["updatable"]}]}', ""),
            subprocess.CompletedProcess([], 0, '{"Devices": []}', ""),
        ]
        with patch("boom.shutil.which", return_value="/usr/bin/fwupdmgr"), patch(
            "boom.run_command", side_effect=responses
        ) as run_command:
            result = boom.firmware_inventory_info()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["devices"][0]["name"], "BIOS")
        self.assertFalse(result["writes_firmware"])
        self.assertIn("metadados locais", result["source"])
        self.assertEqual(run_command.call_args_list[0].args[0], ["/usr/bin/fwupdmgr", "get-devices", "--json"])
        self.assertEqual(run_command.call_args_list[1].args[0], ["/usr/bin/fwupdmgr", "get-updates", "--json"])

    def test_fwupd_query_reports_missing_optional_package(self):
        with patch("boom.shutil.which", return_value=None), patch("boom.run_command") as run_command:
            result = boom.firmware_inventory_info()
        self.assertEqual(result["state"], "missing")
        self.assertIn("fwupd", result["message"])
        run_command.assert_not_called()

    def test_fwupd_is_offered_by_the_optional_dependency_installer(self):
        with patch("boom.intel_i915_adapter_present", return_value=False), patch(
            "boom.power_profile_control_supported", return_value=False
        ), patch("boom.gtk3_runtime_available", return_value=True), patch(
            "boom.appindicator_runtime_available", return_value=True
        ), patch("boom.shutil.which", return_value=None):
            packages = {item["package"] for item in boom.optional_tool_status()}
        self.assertIn("fwupd", packages)


class PerformanceHistoryContinuationTests(unittest.TestCase):
    def test_disk_sampler_derives_rates_from_counter_deltas(self):
        sampler = boom_performance.DiskIoSampler()
        before = "259 0 nvme0n1 100 0 200 0 150 0 300 0 0 50 70 0 0 0 0 0 0"
        after = "259 0 nvme0n1 110 0 210 0 155 0 320 0 0 300 570 0 0 0 0 0 0"
        self.assertIsNone(sampler.sample(before, sampled_at=10, whole_devices={"nvme0n1"}))
        result = sampler.sample(after, sampled_at=15, whole_devices={"nvme0n1"})
        self.assertEqual(result["busy_percent"], 5)
        self.assertEqual(result["read_bytes_per_second"], 1024)
        self.assertEqual(result["write_bytes_per_second"], 2048)
        self.assertEqual(result["average_io_wait_ms"], 33.33)

    def test_local_history_is_opt_in_private_bounded_and_clearable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = boom_performance.PerformanceHistoryStore(
                database_path=root / "state" / "boom" / "performance.sqlite3",
                enabled_path=root / "config" / "boom" / "performance-history.enabled",
            )
            metrics = {
                "cpu_percent": 42,
                "memory": {"used": 8, "total": 10},
                "gpu_metrics": [{"name": "not persisted", "gpu_utilization_percent": 20}],
            }
            self.assertFalse(store.enabled())
            self.assertFalse(store.record(metrics, sampled_at=1000))
            self.assertFalse(store.database_path.exists())

            store.set_enabled(True)
            self.assertTrue(store.enabled())
            self.assertEqual(stat.S_IMODE(store.enabled_path.stat().st_mode), 0o600)
            self.assertTrue(store.record(metrics, sampled_at=1000))
            self.assertFalse(store.record(metrics, sampled_at=1002))
            self.assertTrue(store.record(metrics, sampled_at=1005))
            self.assertEqual(stat.S_IMODE(store.database_path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(store.database_path.stat().st_mode), 0o600)
            samples = store.samples(window_minutes=1, now=1005)
            self.assertEqual(len(samples), 2)
            self.assertNotIn("gpu_name", samples[0])
            self.assertEqual(store.status(now=1005)["sample_count"], 2)

            store.set_enabled(False)
            self.assertFalse(store.record(metrics, sampled_at=1010))
            self.assertEqual(store.status(now=1010)["sample_count"], 2)
            store.clear()
            self.assertEqual(store.status(now=1010)["sample_count"], 0)
            self.assertFalse(store.enabled())

    def test_history_analyzer_requires_a_real_time_window(self):
        result = boom_performance.analyze_performance_history([
            {"sampled_at": 1, "cpu_percent": 95}
        ])
        self.assertEqual(result["status"], "insufficient_data")
        self.assertEqual(result["confidence"], "none")

    def test_history_analyzer_flags_sustained_cpu_pressure_as_a_hypothesis(self):
        samples = [
            {"sampled_at": index * 5, "cpu_percent": 92, "gpu_percent": 30, "ram_percent": 60}
            for index in range(121)
        ]
        result = boom_performance.analyze_performance_history(samples)
        self.assertEqual(result["status"], "analyzed")
        self.assertEqual(result["confidence"], "high")
        self.assertEqual(result["findings"][0]["id"], "possible_cpu_limit")
        self.assertIn("não prova", result["findings"][0]["message"])

    def test_history_analyzer_ignores_a_short_cpu_spike(self):
        samples = [
            {"sampled_at": index * 5, "cpu_percent": 100 if index < 4 else 25, "gpu_percent": 20}
            for index in range(30)
        ]
        result = boom_performance.analyze_performance_history(samples)
        self.assertEqual(result["status"], "analyzed")
        self.assertEqual(result["findings"], [])
        self.assertIn("Nenhuma saturação sustentada", result["summary"])

    def test_history_analyzer_separates_vram_and_temperature_evidence(self):
        samples = [
            {"sampled_at": index * 5, "vram_percent": 95, "cpu_temp_celsius": 82}
            for index in range(20)
        ]
        result = boom_performance.analyze_performance_history(samples)
        finding_ids = {finding["id"] for finding in result["findings"]}
        self.assertIn("vram_pressure", finding_ids)
        thermal = next(finding for finding in result["findings"] if finding["id"] == "elevated_cpu_temperature")
        self.assertIn("não consegue confirmar throttling", thermal["message"])

    def test_history_cli_enables_and_disables_opt_in_recording(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                "XDG_STATE_HOME": str(root / "state"),
                "XDG_CONFIG_HOME": str(root / "config"),
            }
            output = io.StringIO()
            with patch.dict(os.environ, environment, clear=False), patch.object(
                boom.sys, "argv", ["boom.py", "--history-enable"]
            ), contextlib.redirect_stdout(output):
                self.assertEqual(boom.main(), 0)
            self.assertTrue(json.loads(output.getvalue())["enabled"])
            self.assertTrue((root / "config" / "boom" / "performance-history.enabled").exists())

            output = io.StringIO()
            with patch.dict(os.environ, environment, clear=False), patch.object(
                boom.sys, "argv", ["boom.py", "--history-status"]
            ), contextlib.redirect_stdout(output):
                self.assertEqual(boom.main(), 0)
            self.assertTrue(json.loads(output.getvalue())["enabled"])

            output = io.StringIO()
            with patch.dict(os.environ, environment, clear=False), patch.object(
                boom.sys, "argv", ["boom.py", "--history-disable"]
            ), contextlib.redirect_stdout(output):
                self.assertEqual(boom.main(), 0)
            self.assertFalse(json.loads(output.getvalue())["enabled"])


class SensorAndStressTests(unittest.TestCase):
    def test_lm_sensors_parser_reads_live_values_and_ignores_thresholds_and_alarms(self):
        readings = boom.parse_lm_sensors_json({
            "k10temp-pci-00c3": {
                "Adapter": "PCI adapter",
                "Tctl": {"temp1_input": 55.9, "temp1_crit": 95.0, "temp1_crit_alarm": 1.0},
                "Tccd1": {"temp2_input": 55.9},
                "fan1": {"fan1_input": 1250.0},
                "in0": {"in0_input": 1.1},
                "power1": {"power1_input": 65.2},
                "invalid": {"temp9_input": 65261.85},
            }
        })
        self.assertEqual([(item["kind"], item["value"], item["unit"]) for item in readings], [
            ("temperature", 55.9, "°C"), ("temperature", 55.9, "°C"), ("fan", 1250.0, "RPM"),
            ("voltage", 1.1, "V"), ("power", 65.2, "W"),
        ])
        self.assertTrue(all(item["source"] == "lm-sensors" for item in readings))

    def test_stress_cpu_command_is_fixed_and_bounded(self):
        with patch("boom.shutil.which", return_value="/usr/bin/stress-ng"), patch("boom.os.cpu_count", return_value=12):
            command = boom.stress_test_command("cpu", 30, workers=2)
        self.assertEqual(command, [
            "/usr/bin/stress-ng", "--timeout", "30s", "--metrics-brief", "--oom-avoid", "--abort",
            "--cpu", "2", "--cpu-load", "80",
        ])
        self.assertNotIn("--shell", command)

    def test_stress_ram_command_caps_allocation(self):
        with patch("boom.shutil.which", return_value="/usr/bin/stress-ng"):
            command = boom.stress_test_command("memory", 60, available_memory_bytes=8 * 1024**3)
        self.assertEqual(command[command.index("--vm-bytes") + 1], str(1024**3))
        with patch("boom.shutil.which", return_value="/usr/bin/stress-ng"):
            command = boom.stress_test_command("memory", 60, available_memory_bytes=512 * 1024**2)
        self.assertEqual(command[command.index("--vm-bytes") + 1], str(128 * 1024**2))

    def test_stress_gpu_command_uses_one_fixed_render_node_worker(self):
        with patch("boom.shutil.which", return_value="/usr/bin/stress-ng"):
            command = boom.stress_test_command("gpu", 30, gpu_device="/dev/dri/renderD128")
        self.assertEqual(command, [
            "/usr/bin/stress-ng", "--timeout", "30s", "--metrics-brief", "--oom-avoid", "--abort",
            "--gpu", "1", "--gpu-devnode", "/dev/dri/renderD128",
        ])
        with patch("boom.shutil.which", return_value="/usr/bin/stress-ng"):
            with self.assertRaises(ValueError):
                boom.stress_test_command("gpu", 30, gpu_device="/dev/dri/card0")
            with self.assertRaises(ValueError):
                boom.stress_test_command("gpu", 180, gpu_device="/dev/dri/renderD128")

    def test_stress_command_rejects_unsafe_duration_workers_and_memory(self):
        with patch("boom.shutil.which", return_value="/usr/bin/stress-ng"), patch("boom.os.cpu_count", return_value=2):
            with self.assertRaises(ValueError):
                boom.stress_test_command("cpu", 180, workers=1)
            with self.assertRaises(ValueError):
                boom.stress_test_command("cpu", 30, workers=3)
            with self.assertRaises(ValueError):
                boom.stress_test_command("memory", 30, available_memory_bytes=480 * 1024**2)

    def test_stress_readiness_requires_cpu_sensor_and_cool_hardware(self):
        temperatures = [{"name": "k10temp · Tctl", "celsius": 55.9}]
        with patch("boom.shutil.which", return_value="/usr/bin/stress-ng"):
            self.assertTrue(boom.stress_test_readiness(temperatures)["ready"])
            hot = boom.stress_test_readiness([{"name": "k10temp · Tctl", "celsius": 80.0}])
            self.assertFalse(hot["ready"])
            no_cpu = boom.stress_test_readiness([{"name": "nvme · Composite", "celsius": 40.0}])
            self.assertFalse(no_cpu["ready"])

    def test_gpu_stress_readiness_requires_live_gpu_temperature_and_safe_thermal_margin(self):
        temperatures = [{"name": "k10temp · Tctl", "celsius": 55.0}]
        gpu = {"name": "RTX Test", "pci_address": "0000:0b:00.0", "temperature_celsius": 48.0}
        with patch("boom.shutil.which", return_value="/usr/bin/stress-ng"), patch(
            "boom.gpu_render_device", return_value=("/dev/dri/renderD128", None)
        ):
            ready = boom.stress_test_readiness(temperatures, "gpu", gpu)
            self.assertTrue(ready["ready"])
            self.assertEqual(ready["gpu_device"], "/dev/dri/renderD128")
            hot_gpu = boom.stress_test_readiness(temperatures, "gpu", gpu, [80.0])
            self.assertFalse(hot_gpu["ready"])
            missing_gpu_sensor = boom.stress_test_readiness(temperatures, "gpu", gpu, [])
            self.assertFalse(missing_gpu_sensor["ready"])
            self.assertIn("leitura térmica", missing_gpu_sensor["message"])

    def test_gpu_render_node_is_matched_to_adapter_pci_address(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pci_device = root / "sys" / "devices" / "pci0000:00" / "0000:0b:00.0"
            pci_device.mkdir(parents=True)
            drm_root = root / "sys" / "class" / "drm"
            render = drm_root / "renderD128"
            render.mkdir(parents=True)
            (render / "device").symlink_to(pci_device)
            device_root = root / "dev" / "dri"
            device_root.mkdir(parents=True)
            (device_root / "renderD128").touch()
            result = boom.gpu_render_device(
                {"pci_address": "00000000:0B:00.0"},
                drm_root=drm_root,
                device_root=device_root,
            )
        self.assertEqual(result, (str(device_root / "renderD128"), None))

    def test_nvidia_gpu_temperature_reader_matches_the_selected_pci_adapter(self):
        result = subprocess.CompletedProcess(
            args=["nvidia-smi"],
            returncode=0,
            stdout="00000000:0B:00.0, 61\n00000000:0C:00.0, 72\n",
            stderr="",
        )
        with patch("boom.shutil.which", return_value="/usr/bin/nvidia-smi"), patch(
            "boom.run_command", return_value=result
        ) as run:
            temperatures = boom.gpu_temperature_readings({
                "source": "nvidia-smi/NVML",
                "pci_address": "0000:0b:00.0",
            })
        self.assertEqual(temperatures, [61.0])
        self.assertIn("temperature.gpu", run.call_args.args[0][1])

    def test_amd_gpu_temperature_reader_uses_its_pci_hwmon(self):
        with tempfile.TemporaryDirectory() as temporary:
            pci_root = Path(temporary)
            hwmon = pci_root / "0000:0b:00.0" / "hwmon" / "hwmon0"
            hwmon.mkdir(parents=True)
            (hwmon / "temp1_input").write_text("67250", encoding="utf-8")
            (hwmon / "temp2_input").write_text("68375", encoding="utf-8")
            temperatures = boom.gpu_temperature_readings(
                {"source": "DRM/sysfs · amdgpu", "pci_address": "0000:0b:00.0"},
                pci_root=pci_root,
            )
        self.assertEqual(temperatures, [67.25, 68.375])

    def test_hot_cpu_prevents_stress_process_launch(self):
        session = boom.StressTestSession()
        with patch("boom.shutil.which", return_value="/usr/bin/stress-ng"), patch(
            "boom.temperature_info", return_value=[{"name": "k10temp · Tctl", "celsius": 85.1}]
        ), patch("boom.subprocess.Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "abaixo de 80"):
                session.start("cpu", 10, 1, 8 * 1024**3)
        popen.assert_not_called()

    def test_gpu_start_refuses_to_trust_a_stale_cached_temperature(self):
        session = boom.StressTestSession()
        gpu = {
            "name": "RTX Test",
            "source": "nvidia-smi/NVML",
            "pci_address": "0000:0b:00.0",
            "temperature_celsius": 48,
        }
        with patch("boom.shutil.which", return_value="/usr/bin/stress-ng"), patch(
            "boom.temperature_info", return_value=[{"name": "k10temp · Tctl", "celsius": 45.0}]
        ), patch("boom.gpu_temperature_readings", return_value=[]), patch("boom.subprocess.Popen") as popen:
            with self.assertRaisesRegex(RuntimeError, "leitura térmica ao vivo"):
                session.start("gpu", 10, 1, None, gpu)
        popen.assert_not_called()

    def test_stress_watchdog_stops_process_group_at_thermal_limit(self):
        class FakeProcess:
            pid = 4242
            returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout):
                self.returncode = -signal.SIGTERM
                return self.returncode

            def communicate(self, timeout):
                return "stress metrics", None

        session = boom.StressTestSession()
        process = FakeProcess()
        session._state = {"status": "running"}
        hot_cpu = [{"name": "k10temp · Tctl", "celsius": 85.1}]
        with patch("boom.temperature_info", return_value=hot_cpu), patch("boom.os.killpg") as killpg, patch.object(
            boom.threading.Event, "wait", return_value=False
        ):
            session._supervise(process, "cpu", 30, boom.time.monotonic())
        self.assertEqual(session.snapshot()["status"], "cancelled")
        self.assertIn("85.1 °C", session.snapshot()["message"])
        killpg.assert_called_once_with(process.pid, signal.SIGTERM)

    def test_gpu_watchdog_stops_when_gpu_reaches_thermal_limit(self):
        class FakeProcess:
            pid = 4243
            returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout):
                self.returncode = -signal.SIGTERM
                return self.returncode

            def communicate(self, timeout):
                return "stress metrics", None

        session = boom.StressTestSession()
        process = FakeProcess()
        session._state = {"status": "running"}
        gpu = {"name": "RTX Test", "pci_address": "0000:0b:00.0"}
        with patch("boom.temperature_info", return_value=[{"name": "k10temp · Tctl", "celsius": 57.0}]), patch(
            "boom.gpu_temperature_readings", return_value=[85.1]
        ), patch("boom.os.killpg") as killpg, patch.object(boom.threading.Event, "wait", return_value=False):
            session._supervise(process, "gpu", 30, boom.time.monotonic(), gpu)
        self.assertEqual(session.snapshot()["status"], "cancelled")
        self.assertIn("85.1 °C", session.snapshot()["message"])
        killpg.assert_called_once_with(process.pid, signal.SIGTERM)

    def test_gpu_watchdog_stops_when_gpu_temperature_sensor_disappears(self):
        class FakeProcess:
            pid = 4244
            returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout):
                self.returncode = -signal.SIGTERM
                return self.returncode

            def communicate(self, timeout):
                return "stress metrics", None

        session = boom.StressTestSession()
        process = FakeProcess()
        session._state = {"status": "running"}
        with patch("boom.temperature_info", return_value=[{"name": "k10temp · Tctl", "celsius": 57.0}]), patch(
            "boom.gpu_temperature_readings", return_value=[]
        ), patch("boom.os.killpg") as killpg, patch.object(boom.threading.Event, "wait", return_value=False):
            session._supervise(process, "gpu", 30, boom.time.monotonic(), {"pci_address": "0000:0b:00.0"})
        self.assertEqual(session.snapshot()["status"], "cancelled")
        self.assertIn("GPU ficou indisponível", session.snapshot()["message"])
        killpg.assert_called_once_with(process.pid, signal.SIGTERM)

    def test_optional_installer_requests_only_missing_fixed_apt_packages(self):
        tools = {
            "lspci": "/usr/bin/lspci",
            "sensors": None,
            "stress-ng": "/usr/bin/stress-ng",
            "smartctl": "/usr/sbin/smartctl",
            "dmidecode": "/usr/sbin/dmidecode",
            "fwupdmgr": "/usr/bin/fwupdmgr",
            "ping": "/usr/bin/ping",
            "apt-get": "/usr/bin/apt-get",
            "pkexec": "/usr/bin/pkexec",
            "fio": "/usr/bin/fio",
        }
        update_process = MagicMock()
        update_process.returncode = 0
        update_process.communicate.return_value = ("Package lists updated", None)
        install_process = MagicMock()
        install_process.returncode = 0

        def install_package(timeout=None):
            tools["sensors"] = "/usr/bin/sensors"
            return "installed", None

        install_process.communicate.side_effect = install_package
        with patch("boom.shutil.which", side_effect=lambda name: tools.get(name)), patch(
            "boom.gtk3_runtime_available", return_value=True
        ), patch("boom.appindicator_runtime_available", return_value=True), patch(
            "boom.power_profile_control_supported", return_value=False
        ), patch(
            "boom.subprocess.Popen", side_effect=[update_process, install_process]
        ) as popen:
            result = boom.install_missing_optional_dependencies()
        self.assertTrue(result["ok"])
        self.assertEqual(result["packages"], ["lm-sensors"])
        self.assertEqual(popen.call_args_list[0].args[0], [
            "/usr/bin/pkexec", "/usr/bin/apt-get", "update",
        ])
        self.assertEqual(popen.call_args_list[1].args[0], [
            "/usr/bin/pkexec", "/usr/bin/apt-get", "install", "--yes", "--no-install-recommends", "lm-sensors",
        ])
        self.assertTrue(all(call.kwargs["start_new_session"] for call in popen.call_args_list))

    def test_optional_tool_list_offers_fio_when_disk_benchmark_dependency_is_missing(self):
        with patch("boom.shutil.which", side_effect=lambda name: None if name == "fio" else f"/usr/bin/{name}"), patch(
            "boom.intel_i915_adapter_present", return_value=False
        ), patch("boom.power_profile_control_supported", return_value=False), patch(
            "boom.gtk3_runtime_available", return_value=True
        ), patch("boom.appindicator_runtime_available", return_value=True):
            self.assertEqual(boom.missing_optional_packages(), ["fio"])

    def test_optional_installer_stops_when_apt_index_refresh_fails(self):
        tools = {"sensors": None, "apt-get": "/usr/bin/apt-get", "pkexec": "/usr/bin/pkexec"}
        process = MagicMock()
        process.returncode = 1
        process.communicate.return_value = ("Permission denied", None)
        with patch("boom.shutil.which", side_effect=lambda name: tools.get(name)), patch(
            "boom.gtk3_runtime_available", return_value=True
        ), patch("boom.appindicator_runtime_available", return_value=True), patch(
            "boom.power_profile_control_supported", return_value=False
        ), patch(
            "boom.subprocess.Popen", return_value=process
        ) as popen:
            result = boom.install_missing_optional_dependencies()
        self.assertFalse(result["ok"])
        self.assertIn("Atualização dos índices do APT", result["message"])
        self.assertEqual(popen.call_count, 1)

    def test_optional_installer_does_not_spawn_when_tools_are_present(self):
        with patch("boom.shutil.which", return_value="/usr/bin/tool"), patch(
            "boom.gtk3_runtime_available", return_value=True
        ), patch("boom.appindicator_runtime_available", return_value=True), patch(
            "boom.power_profile_control_supported", return_value=False
        ), patch("boom.subprocess.Popen") as popen:
            result = boom.install_missing_optional_dependencies()
        self.assertTrue(result["ok"])
        popen.assert_not_called()

    def test_optional_installer_offers_missing_gtk_and_tray_packages(self):
        with patch("boom.shutil.which", return_value="/usr/bin/tool"), patch(
            "boom.gtk3_runtime_available", return_value=False
        ), patch("boom.appindicator_runtime_available", return_value=False):
            packages = set(boom.missing_optional_packages())
        self.assertTrue({"python3-gi", "gir1.2-gtk-3.0", "gir1.2-ayatanaappindicator3-0.1"}.issubset(packages))

    def test_intel_gpu_tools_are_optional_only_for_detected_i915_hardware(self):
        with patch("boom.intel_i915_adapter_present", return_value=True), patch("boom.shutil.which", return_value=None):
            intel_packages = {item["package"] for item in boom.optional_tool_status()}
        with patch("boom.intel_i915_adapter_present", return_value=False), patch("boom.shutil.which", return_value=None):
            other_packages = {item["package"] for item in boom.optional_tool_status()}
        self.assertIn("intel-gpu-tools", intel_packages)
        self.assertNotIn("intel-gpu-tools", other_packages)

    def test_power_profiles_daemon_is_offered_only_when_cpu_or_platform_profiles_are_supported(self):
        with patch("boom.power_profile_control_supported", return_value=True), patch(
            "boom.power_profile_manager_conflict", return_value=False
        ), patch("boom.shutil.which", side_effect=lambda name: None if name == "powerprofilesctl" else "/usr/bin/tool"), patch(
            "boom.gtk3_runtime_available", return_value=True
        ), patch("boom.appindicator_runtime_available", return_value=True):
            supported = {item["package"] for item in boom.optional_tool_status()}
        with patch("boom.power_profile_control_supported", return_value=False), patch(
            "boom.power_profile_manager_conflict", return_value=False
        ), patch("boom.shutil.which", return_value=None), patch(
            "boom.gtk3_runtime_available", return_value=True
        ), patch("boom.appindicator_runtime_available", return_value=True):
            unsupported = {item["package"] for item in boom.optional_tool_status()}
        self.assertIn("power-profiles-daemon", supported)
        self.assertNotIn("power-profiles-daemon", unsupported)


class HtmlReportTests(unittest.TestCase):
    def test_html_report_escapes_content_redacts_identifiers_and_has_no_remote_assets(self):
        report = boom.generate_html_report({
            "updated_at": "2026-11-01T12:34:56-03:00",
            "cpu": {"model": "CPU <modelo>", "usage_percent": 25.5},
            "memory": {
                "used": 1024**3, "total": 2 * 1024**3,
                "swap_used": 512 * 1024**2, "swap_total": 1024**3,
                "swap_available": 512 * 1024**2,
            },
            "platform_details": {
                "board_name": "Board",
                "system_uuid": "PRIVATE-UUID",
                "serial_number": "PRIVATE-SERIAL",
            },
            "network": {"mac_address": "00:11:22:33:44:55"},
            "gpu_metrics": [{
                "name": "GPU <teste>",
                "gpu_utilization_percent": 25.5,
                "memory_utilization_percent": 55,
                "vram_used_mib": 1024,
                "vram_total_mib": 4096,
            }],
            "note": "<script>alert('x')</script>",
        })
        self.assertIn("<html lang=\"pt-BR\">", report)
        self.assertIn("CPU &lt;modelo&gt;", report)
        self.assertIn("GPU &lt;teste&gt;", report)
        self.assertIn("1,0 GB", report)
        self.assertIn("Swap usada", report)
        self.assertIn("512,0 MB", report)
        self.assertIn("1.024 MiB", report)
        self.assertIn("VRAM usada", report)
        self.assertIn("Uso do núcleo gráfico", report)
        self.assertNotIn("PRIVATE-UUID", report)
        self.assertNotIn("PRIVATE-SERIAL", report)
        self.assertNotIn("00:11:22:33:44:55", report)
        self.assertIn("Ocultado por privacidade", report)
        self.assertNotIn("<script>", report)
        self.assertNotIn("http://", report)
        self.assertNotIn("https://", report)

    def test_html_report_writes_selected_local_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "system-report.html"
            saved = boom.write_html_report(destination, {"updated_at": "agora", "cpu": {"cores": 12}})
            self.assertEqual(saved, destination.resolve())
            self.assertTrue(destination.read_text(encoding="utf-8").startswith("<!doctype html>"))
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_html_report_appends_html_extension_when_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "system-report"
            saved = boom.write_html_report(destination, {"updated_at": "agora"})
            self.assertEqual(saved.name, "system-report.html")
            self.assertTrue(saved.exists())

    def test_report_html_cli_exports_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "boom.html"
            output = io.StringIO()
            with patch.object(boom.sys, "argv", ["boom.py", "--report-html", str(destination)]), \
                 patch.object(boom, "collect_snapshot", return_value={"updated_at": "now", "cpu": {"cores": 12}}), \
                 contextlib.redirect_stdout(output):
                result = boom.main()
            report_contents = destination.read_text(encoding="utf-8") if destination.exists() else ""
        self.assertEqual(result, 0)
        self.assertIn("Relatório HTML salvo localmente", output.getvalue())
        self.assertTrue(report_contents)
        self.assertIn("Processador", report_contents)


class LinuxSnapshotTests(unittest.TestCase):
    def test_snapshot_contains_linux_inventory(self):
        snapshot = boom.collect_snapshot()
        self.assertIn("cpu", snapshot)
        self.assertIn("memory", snapshot)
        self.assertIn("os", snapshot)
        self.assertIn("disks", snapshot)
        self.assertIn("network", snapshot)
        self.assertGreater(snapshot["cpu"]["cores"], 0)
        self.assertTrue(snapshot["os"]["name"])
        self.assertIsInstance(snapshot["disks"], list)
        self.assertIn("interfaces", snapshot["network"])
        self.assertIn("platform_details", snapshot)
        self.assertIn("memory_modules", snapshot)
        self.assertIn("frequency", snapshot["cpu"])
        self.assertIn("gpu_metrics", snapshot)
        self.assertIn("gpu_metrics_status", snapshot)
        self.assertIn("block_devices", snapshot)
        self.assertIn("smart_devices", snapshot)
        self.assertIn("power", snapshot)
        self.assertIn("sensor_readings", snapshot)
        self.assertIn("temperatures", snapshot)
        self.assertIn("swap_total", snapshot["memory"])
        self.assertIn("swap_used", snapshot["memory"])

    def test_network_diagnostic_uses_only_fixed_targets(self):
        snapshot = {"network": {"gateway": "192.0.2.1"}}
        with patch("boom.ping_target", side_effect=lambda label, address: {"target": label, "address": address}) as ping:
            results = boom.run_network_test(snapshot)
        self.assertCountEqual(
            ping.call_args_list,
            [
                unittest.mock.call("Gateway", "192.0.2.1"),
                unittest.mock.call("Internet · Cloudflare DNS", "1.1.1.1"),
            ],
        )
        self.assertEqual(len(results), 2)

    def test_ping_reports_sandbox_permission_failure(self):
        denied = subprocess.CompletedProcess(
            args=["ping"], returncode=2, stdout="", stderr="ping: socket: Operation not permitted"
        )
        with patch("boom.run_command", return_value=denied):
            result = boom.ping_target("Internet", "1.1.1.1")
        self.assertEqual(result["status"], "Sem permissão para enviar ICMP")
        self.assertEqual(result["sent"], 0)


class HardwareDetailTests(unittest.TestCase):
    def _make_drm_device(self, root, card_name, pci_address, vendor, driver, product_name):
        card = root / card_name
        card.mkdir(parents=True)
        pci_device = root.parent / "devices" / pci_address
        pci_device.mkdir(parents=True)
        drivers = root.parent / "drivers"
        driver_path = drivers / driver
        driver_path.mkdir(parents=True, exist_ok=True)
        (pci_device / "driver").symlink_to(driver_path, target_is_directory=True)
        (card / "device").symlink_to(pci_device, target_is_directory=True)
        (pci_device / "vendor").write_text(vendor, encoding="utf-8")
        (pci_device / "device").write_text("0x1234", encoding="utf-8")
        if product_name:
            (pci_device / "product_name").write_text(product_name, encoding="utf-8")
        return pci_device

    def test_meminfo_parser_reports_ram_and_swap_bytes(self):
        parsed = boom.parse_meminfo("""MemTotal:        8192 kB
MemAvailable:    3072 kB
SwapTotal:       4096 kB
SwapFree:        1024 kB
""")
        self.assertEqual(parsed, {
            "total": 8192 * 1024,
            "available": 3072 * 1024,
            "used": 5120 * 1024,
            "swap_total": 4096 * 1024,
            "swap_available": 1024 * 1024,
            "swap_used": 3072 * 1024,
        })
        self.assertIsNone(boom.parse_meminfo("MemTotal: 1024 kB\n"))

    def test_gpu_terminal_rows_separate_graphics_and_memory_metrics(self):
        gpu = {
            "name": "GPU teste", "driver_version": "1", "gpu_utilization_percent": 55,
            "memory_utilization_percent": 35, "temperature_celsius": 61,
            "graphics_clock_mhz": 1800, "memory_clock_mhz": 7000,
            "vram_used_mib": 1024, "vram_total_mib": 4096,
        }
        graphics_rows = "\n".join(boom.format_gpu_graphics_terminal_rows(gpu))
        memory_rows = "\n".join(boom.format_gpu_memory_terminal_rows(gpu))
        self.assertIn("Uso do núcleo gráfico: 55%", graphics_rows)
        self.assertNotIn("VRAM", graphics_rows)
        self.assertNotIn("controlador de memória", graphics_rows.casefold())
        self.assertIn("VRAM usada / total: 1024 / 4096 MiB · 25.0%", memory_rows)
        self.assertIn("Atividade do controlador de memória: 35%", memory_rows)
        self.assertNotIn("Uso do núcleo gráfico", memory_rows)

    def test_cpu_frequency_parser_converts_khz_to_mhz_and_skips_bad_values(self):
        frequencies = boom.parse_cpu_frequency_readings({
            0: "3700000\n",
            1: "2200000",
            2: "0",
            3: "not-a-number",
        })
        self.assertEqual(frequencies, [{"core": 0, "mhz": 3700.0}, {"core": 1, "mhz": 2200.0}])
        self.assertEqual(boom.format_frequency(3700), "3.70 GHz")
        self.assertEqual(boom.format_frequency(None), boom.UNAVAILABLE)

    def test_cpu_energy_preference_parser_reads_policy_values_and_common_choices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, current, available in (
                ("policy0", "balance_performance", "default performance balance_performance power"),
                ("policy4", "power", "default performance balance_performance power"),
            ):
                policy = root / name
                policy.mkdir()
                (policy / "energy_performance_preference").write_text(current, encoding="utf-8")
                (policy / "energy_performance_available_preferences").write_text(available, encoding="utf-8")

            result = boom.cpu_energy_preference_info(root)

        self.assertTrue(result["available"])
        self.assertIsNone(result["current"])
        self.assertFalse(result["consistent"])
        self.assertEqual(result["available_preferences"], ["default", "performance", "balance_performance", "power"])
        self.assertEqual(len(result["policies"]), 2)

    def test_power_profile_list_parser_accepts_only_known_profile_ids(self):
        output = "* balanced: Balanced\n  power-saver: Saver\n  performance: Performance\n  unknown: Unknown\n"
        self.assertEqual(
            boom.parse_power_profiles_list(output),
            ["balanced", "power-saver", "performance"],
        )

    def test_power_profile_set_saves_private_restore_state_and_restores(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "boom" / "power-profile-restore.json"
            initial = {
                "available": True,
                "profiles": ["balanced", "performance"],
                "current": "balanced",
                "message": "ready",
            }
            changed = {**initial, "current": "performance"}
            calls = [
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, "performance\n", ""),
                subprocess.CompletedProcess([], 0, "", ""),
                subprocess.CompletedProcess([], 0, "balanced\n", ""),
            ]
            with patch("boom.power_profile_info", side_effect=[initial, changed]), patch(
                "boom.shutil.which", return_value="/usr/bin/powerprofilesctl"
            ), patch("boom.run_command", side_effect=calls) as run:
                applied = boom.set_power_profile("performance", state_path)
                self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
                saved = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(saved, {"restore_profile": "balanced", "applied_profile": "performance"})
                restored = boom.restore_power_profile(state_path)

        self.assertTrue(applied["ok"])
        self.assertTrue(restored["ok"])
        self.assertFalse(state_path.exists())
        self.assertEqual(run.call_args_list[0].args[0], ["/usr/bin/powerprofilesctl", "set", "performance"])
        self.assertEqual(run.call_args_list[2].args[0], ["/usr/bin/powerprofilesctl", "set", "balanced"])

    def test_power_profile_set_rejects_profile_not_advertised_by_system(self):
        with patch("boom.power_profile_info", return_value={
            "available": True, "profiles": ["balanced"], "current": "balanced", "message": "ready",
        }), patch("boom.run_command") as run:
            result = boom.set_power_profile("performance", Path("unused"))
        self.assertFalse(result["ok"])
        run.assert_not_called()

    def test_power_profile_failed_change_keeps_or_restores_known_state(self):
        info = {
            "available": True,
            "profiles": ["balanced", "performance"],
            "current": "balanced",
            "message": "ready",
        }
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            failed_set = subprocess.CompletedProcess([], 1, "", "daemon refused")
            unchanged = subprocess.CompletedProcess([], 0, "balanced\n", "")
            with patch("boom.power_profile_info", return_value=info), patch(
                "boom.shutil.which", return_value="/usr/bin/powerprofilesctl"
            ), patch("boom.run_command", side_effect=[failed_set, unchanged]):
                result = boom.set_power_profile("performance", state_path)
        self.assertFalse(result["ok"])
        self.assertIn("revertido", result["message"])
        self.assertFalse(state_path.exists())

    def test_power_profile_failed_rollback_preserves_manual_restore_path(self):
        info = {
            "available": True,
            "profiles": ["balanced", "performance"],
            "current": "balanced",
            "message": "ready",
        }
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            responses = [
                subprocess.CompletedProcess([], 1, "", "set failed"),
                subprocess.CompletedProcess([], 0, "performance\n", ""),
                subprocess.CompletedProcess([], 1, "", "rollback failed"),
                subprocess.CompletedProcess([], 0, "performance\n", ""),
            ]
            with patch("boom.power_profile_info", return_value=info), patch(
                "boom.shutil.which", return_value="/usr/bin/powerprofilesctl"
            ), patch("boom.run_command", side_effect=responses):
                result = boom.set_power_profile("performance", state_path)
            saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertFalse(result["ok"])
        self.assertIn("restauração", result["message"])
        self.assertEqual(saved, {"restore_profile": "balanced", "applied_profile": "performance"})

    def test_nvidia_smi_parser_keeps_units_and_marks_unsupported_fields(self):
        output = """NVIDIA GeForce RTX 3060, 595.104.02, 18, 21, 11, 9, 48, 2048, 12288, 1830, 7501, 72.50, 170.00, P2, 00000000:0B:00.0
NVIDIA GeForce RTX 4060, 595.104.02, N/A, N/A, N/A, N/A, N/A, N/A, N/A, N/A, N/A, N/A, N/A, P8, 00000000:0C:00.0
"""
        gpus = boom.parse_nvidia_smi_csv(output)
        self.assertEqual(len(gpus), 2)
        self.assertEqual(gpus[0]["name"], "NVIDIA GeForce RTX 3060")
        self.assertEqual(gpus[0]["gpu_utilization_percent"], 18)
        self.assertEqual(gpus[0]["encoder_utilization_percent"], 11)
        self.assertEqual(gpus[0]["decoder_utilization_percent"], 9)
        self.assertEqual(gpus[0]["temperature_celsius"], 48)
        self.assertEqual(gpus[0]["graphics_clock_mhz"], 1830)
        self.assertEqual(gpus[0]["power_draw_watts"], 72.5)
        self.assertEqual(gpus[0]["pci_address"], "00000000:0B:00.0")
        self.assertEqual(gpus[0]["source"], "nvidia-smi/NVML")
        self.assertIsNone(gpus[1]["gpu_utilization_percent"])

    def test_intel_gpu_top_parser_handles_truncated_json_stream_without_inventing_total_usage(self):
        output = '''[{"engines":{"Render/3D/0":{"busy":47.2,"sema":0.0},"Video/0":{"busy":8.0}},"frequency":{"actual":1250,"unit":"MHz"},"power":{"GPU":18.25,"unit":"W"}}'''
        metrics = boom.parse_intel_gpu_top_json(output)
        self.assertEqual(metrics["gpu_utilization_percent"], 47.2)
        self.assertEqual(metrics["gpu_utilization_label"], "Motor mais ativo")
        self.assertEqual(metrics["engine_utilization"], {"Render/3D/0": 47.2, "Video/0": 8.0})
        self.assertEqual(metrics["graphics_clock_mhz"], 1250)
        self.assertEqual(metrics["power_draw_watts"], 18.25)
        self.assertNotIn("memory_utilization_percent", metrics)

    def test_intel_gpu_top_uses_fixed_unprivileged_device_query(self):
        payload = json.dumps({"engines": {"Render/3D/0": {"busy": 22.0}}})
        process = MagicMock()
        process.communicate.return_value = (payload, "")
        process.poll.return_value = 0
        with patch("boom.shutil.which", return_value="/usr/bin/intel_gpu_top"), patch("boom.subprocess.Popen", return_value=process) as popen:
            metrics, error = boom.intel_gpu_top_sample("/dev/dri/card3")
        self.assertIsNone(error)
        self.assertEqual(metrics["gpu_utilization_percent"], 22)
        self.assertEqual(popen.call_args.args[0], [
            "/usr/bin/intel_gpu_top", "-J", "-o", "-", "-s", "500", "-d", "drm:/dev/dri/card3",
        ])
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertNotIn("shell", popen.call_args.kwargs)

    def test_drm_gpu_telemetry_reads_amd_sysfs_units_and_validates_ranges(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "class" / "drm"
            root.mkdir(parents=True)
            device = self._make_drm_device(
                root, "card0", "0000:03:00.0", "0x1002", "amdgpu", "AMD Radeon Test",
            )
            values = {
                "gpu_busy_percent": "67\n",
                "mem_busy_percent": "43\n",
                "mem_info_vram_used": str(3 * 1024**3),
                "mem_info_vram_total": str(8 * 1024**3),
                "mem_info_gtt_used": str(512 * 1024**2),
                "mem_info_gtt_total": str(16 * 1024**3),
                "current_link_speed": "16.0 GT/s PCIe\n",
                "current_link_width": "16\n",
                "pp_dpm_sclk": "0: 300Mhz\n1: 2400Mhz *\n",
                "pp_dpm_mclk": "0: 96Mhz\n1: 2000Mhz *\n",
            }
            for name, value in values.items():
                (device / name).write_text(value, encoding="utf-8")
            hwmon = device / "hwmon" / "hwmon0"
            hwmon.mkdir(parents=True)
            for name, value in {
                "temp1_input": "73400\n",
                "fan1_input": "1120\n",
                "pwm1": "128\n",
                "pwm1_max": "255\n",
                "power1_average": "52000000\n",
                "power1_cap": "200000000\n",
                "in0_input": "1030\n",
                "freq1_input": "2400000000\n",
                "freq2_input": "2000000000\n",
            }.items():
                (hwmon / name).write_text(value, encoding="utf-8")

            metrics, status = boom.drm_gpu_telemetry_info(root)

        gpu = metrics[0]
        self.assertIn("DRM/sysfs", status)
        self.assertEqual(gpu["name"], "AMD Radeon Test")
        self.assertEqual(gpu["driver_name"], "amdgpu")
        self.assertEqual(gpu["gpu_utilization_percent"], 67)
        self.assertEqual(gpu["memory_utilization_percent"], 43)
        self.assertEqual(gpu["vram_used_mib"], 3072)
        self.assertEqual(gpu["vram_total_mib"], 8192)
        self.assertEqual(gpu["shared_memory_used_mib"], 512)
        self.assertEqual(gpu["temperature_celsius"], 73.4)
        self.assertEqual(gpu["fan_rpm"], 1120)
        self.assertAlmostEqual(gpu["fan_pwm_percent"], 128 * 100 / 255, places=1)
        self.assertEqual(gpu["power_draw_watts"], 52)
        self.assertEqual(gpu["power_limit_watts"], 200)
        self.assertEqual(gpu["gpu_voltage_volts"], 1.03)
        self.assertEqual(gpu["graphics_clock_mhz"], 2400)
        self.assertEqual(gpu["memory_clock_mhz"], 2000)
        self.assertEqual(gpu["pcie_link_speed"], "16.0 GT/s")
        self.assertEqual(gpu["pcie_link_width"], 16)

    def test_drm_gpu_telemetry_merges_intel_engine_sample_and_keeps_missing_fields_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "class" / "drm"
            root.mkdir(parents=True)
            device = self._make_drm_device(
                root, "card0", "0000:00:02.0", "0x8086", "i915", "Intel Graphics Test",
            )
            (device / "mem_info_vram_total").write_text("0\n", encoding="utf-8")
            (device / "gpu_busy_percent").write_text("125\n", encoding="utf-8")
            sample = {
                "source": "intel_gpu_top/PMU",
                "gpu_utilization_label": "Motor mais ativo",
                "gpu_utilization_basis": "maior percentual entre motores gráficos",
                "gpu_utilization_percent": 47.2,
                "engine_utilization": {"Render/3D/0": 47.2},
                "graphics_clock_mhz": 1250,
            }
            with patch("boom.intel_gpu_top_sample", return_value=(sample, None)) as sample_gpu:
                metrics, _ = boom.drm_gpu_telemetry_info(root)

        gpu = metrics[0]
        self.assertEqual(gpu["gpu_utilization_percent"], 47.2)
        self.assertEqual(gpu["gpu_utilization_label"], "Motor mais ativo")
        self.assertEqual(gpu["engine_utilization"], {"Render/3D/0": 47.2})
        self.assertIsNone(gpu["vram_total_mib"])
        self.assertNotIn("gpu_busy_percent", gpu)
        self.assertEqual(gpu["gpu_utilization_basis"], "maior percentual entre motores gráficos")
        sample_gpu.assert_called_once_with("/dev/dri/card0")

    def test_gpu_telemetry_queries_nvidia_backend_and_merges_optional_fields(self):
        main = subprocess.CompletedProcess(args=["nvidia-smi"], returncode=0, stdout=(
            "NVIDIA GeForce RTX 3060, 595.104.02, 18, 21, 12, 6, 48, 2048, 12288, 1830, 7501, 72.50, 170.00, P2, 00000000:0B:00.0\n"
        ), stderr="")
        optional = subprocess.CompletedProcess(args=["nvidia-smi"], returncode=0, stdout="43, 4, 16\n", stderr="")
        with patch("boom.shutil.which", return_value="/usr/bin/nvidia-smi"), patch("boom.run_command", side_effect=[main, optional]) as run:
            metrics, status = boom.gpu_telemetry_info()
        self.assertIn("nvidia-smi/NVML", status)
        self.assertEqual(metrics[0]["encoder_utilization_percent"], 12)
        self.assertEqual(metrics[0]["decoder_utilization_percent"], 6)
        self.assertEqual(metrics[0]["pci_address"], "00000000:0B:00.0")
        self.assertEqual(metrics[0]["fan_percent"], 43)
        self.assertEqual(metrics[0]["pcie_generation"], 4)
        self.assertEqual(metrics[0]["pcie_link_width"], 16)
        self.assertEqual(run.call_count, 2)

    def test_gpu_telemetry_reports_missing_optional_backend(self):
        with patch("boom.shutil.which", return_value=None):
            metrics, status = boom.gpu_telemetry_info()
        self.assertEqual(metrics, [])
        self.assertIn("nvidia-smi", status)

    def test_smartctl_parser_normalizes_health_and_nvme_life_without_serial(self):
        payload = {
            "model_name": "Example NVMe",
            "serial_number": "PRIVATE-SERIAL",
            "smart_status": {"passed": True},
            "temperature": {"current": 42},
            "power_on_time": {"hours": 1530},
            "nvme_smart_health_information_log": {
                "percentage_used": 7,
                "available_spare": 100,
                "critical_warning": 0,
            },
        }
        result = boom.parse_smartctl_json("/dev/nvme1", json.dumps(payload))
        self.assertTrue(result["available"])
        self.assertTrue(result["health_passed"])
        self.assertEqual(result["temperature_celsius"], 42)
        self.assertEqual(result["power_on_hours"], 1530)
        self.assertEqual(result["percentage_used"], 7)
        self.assertNotIn("serial_number", result)

    def test_smartctl_read_uses_pkexec_with_validated_fixed_arguments(self):
        payload = json.dumps({"smart_status": {"passed": True}})
        completed = subprocess.CompletedProcess(args=["smartctl"], returncode=0, stdout=payload, stderr="")
        which = {"pkexec": "/usr/bin/pkexec", "smartctl": "/usr/sbin/smartctl"}
        with patch("boom.shutil.which", side_effect=which.get), patch("boom.run_command", return_value=completed) as run:
            result = boom.read_smart_device({"device": "/dev/nvme1n1", "smart_device": "/dev/nvme1", "device_type": "nvme"})
        self.assertTrue(result["health_passed"])
        self.assertEqual(run.call_args.args[0], [
            "/usr/bin/pkexec", "/usr/sbin/smartctl", "--json", "--all", "--device=nvme", "/dev/nvme1",
        ])
        self.assertEqual(run.call_args.kwargs["timeout"], 15.0)

    def test_smartctl_rejects_invalid_device_paths_without_running_command(self):
        with patch("boom.shutil.which", return_value="/usr/bin/unused"), patch("boom.run_command") as run:
            result = boom.read_smart_device({"device": "/dev/../../etc/passwd"})
        self.assertFalse(result["available"])
        self.assertIn("inválido", result["message"])
        run.assert_not_called()

    def test_smart_scan_maps_nvme_controller_to_physical_block_device(self):
        scan = subprocess.CompletedProcess(
            args=["smartctl", "--scan"], returncode=0,
            stdout="/dev/nvme1 -d nvme # NVMe device\n/dev/sda -d scsi # SATA disk\n", stderr="",
        )
        devices = [
            {"device": "/dev/nvme1n1", "name": "nvme1n1", "model": "Example NVMe", "size": 1_000, "transport": "nvme"},
            {"device": "/dev/sda", "name": "sda", "model": "Example SATA", "size": 2_000, "transport": "sata"},
        ]
        with patch("boom.shutil.which", return_value="/usr/sbin/smartctl"), patch("boom.run_command", return_value=scan):
            found, status = boom.smart_device_info(devices)
        self.assertEqual(found[0]["smart_device"], "/dev/nvme1")
        self.assertEqual(found[0]["device_type"], "nvme")
        self.assertTrue(found[0]["smart_supported"])
        self.assertEqual(found[1]["device_type"], "scsi")
        self.assertIn("ação explícita", status)


class PowerSupplyTests(unittest.TestCase):
    def test_power_supply_parser_reads_battery_and_external_power(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            battery = root / "BAT0"
            battery.mkdir()
            for name, value in {
                "type": "Battery",
                "present": "1",
                "status": "Discharging",
                "capacity": "75",
                "energy_now": "45000000",
                "energy_full": "60000000",
                "power_now": "12000000",
                "voltage_now": "12000000",
            }.items():
                (battery / name).write_text(value, encoding="utf-8")
            mains = root / "AC"
            mains.mkdir()
            (mains / "type").write_text("Mains", encoding="utf-8")
            (mains / "online").write_text("1", encoding="utf-8")
            result = boom.power_supply_info(root)
        self.assertEqual(result["batteries"][0]["capacity_percent"], 75)
        self.assertEqual(result["batteries"][0]["energy_now_wh"], 45)
        self.assertEqual(result["batteries"][0]["power_watts"], 12)
        self.assertTrue(result["ac_online"])

    def test_power_supply_reports_no_battery_on_empty_desktop(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = boom.power_supply_info(Path(temporary))
        self.assertEqual(result["batteries"], [])
        self.assertEqual(result["status"], "Nenhuma bateria detectada.")

class MemoryModuleTests(unittest.TestCase):
    def test_dmidecode_memory_parser_reads_configured_and_nominal_speed(self):
        output = """Handle 0x0011, DMI type 17, 92 bytes
Memory Device
\tSize: 16 GB
\tForm Factor: DIMM
\tLocator: DIMM_A2
\tType: DDR4
\tSpeed: 3200 MT/s
\tManufacturer: Example Memory
\tPart Number: EX16G3200
\tConfigured Memory Speed: 2666 MT/s
\tConfigured Voltage: 1.2 V

Handle 0x0012, DMI type 17, 92 bytes
Memory Device
\tSize: No Module Installed
"""
        modules = boom.parse_dmidecode_memory(output)
        self.assertEqual(len(modules), 1)
        self.assertEqual(modules[0]["configured_speed"], "2666 MT/s")
        self.assertEqual(modules[0]["speed"], "3200 MT/s")
        self.assertEqual(modules[0]["manufacturer"], "Example Memory")
        self.assertEqual(modules[0]["part_number"], "EX16G3200")
        self.assertEqual(boom.format_memory_speed("3200 MT/s", "DDR4"), "3200 MT/s (≈ 1600 MHz)")
        rows = boom.memory_module_terminal_rows(modules[0], 1)
        self.assertTrue(any("Configurada: 2666 MT/s" in row for row in rows))
        self.assertTrue(any("Nominal: 3200 MT/s" in row for row in rows))
        self.assertTrue(any("Modelo: EX16G3200" in row for row in rows))

class PlatformInfoTests(unittest.TestCase):
    def test_platform_details_are_read_as_real_strings(self):
        details = boom.platform_detail_info()
        self.assertIsInstance(details, dict)
        self.assertTrue(all(isinstance(key, str) and isinstance(value, str) for key, value in details.items()))


class ProcessManagerTests(unittest.TestCase):
    def test_proc_stat_parser_handles_spaces_in_process_name(self):
        fields = ["S", "1", "0", "0", "0", "0", "0", "0", "0", "0", "0", "123", "45", "0", "0", "0", "0", "0", "0", "6789", "2048", "-1"]
        parsed = boom.parse_proc_stat("123 (worker with spaces) " + " ".join(fields))
        self.assertEqual(parsed["comm"], "worker with spaces")
        self.assertEqual(parsed["utime"], 123)
        self.assertEqual(parsed["start_ticks"], 6789)

    def test_process_list_marks_boom_as_protected(self):
        processes = boom.process_list(sample_seconds=0.01)
        current = next(item for item in processes if item["pid"] == __import__("os").getpid())
        self.assertFalse(current["can_terminate"])
        self.assertGreaterEqual(current["memory_bytes"], 0)

    def test_termination_sends_sigterm_to_owned_child(self):
        child = subprocess.Popen(["sleep", "30"], start_new_session=True)
        try:
            record = boom.read_process_record(child.pid)
            self.assertIsNotNone(record)
            self.assertTrue(record["can_terminate"])
            boom.terminate_process(child.pid, record["start_ticks"])
            self.assertEqual(child.wait(timeout=3), -signal.SIGTERM)
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=1)

    def test_termination_rejects_init_process(self):
        with self.assertRaises(PermissionError):
            boom.terminate_process(1, 0)


class ApplicationInventoryTests(unittest.TestCase):
    def test_applications_and_startup_inventory_have_expected_shape(self):
        applications = boom.desktop_application_list()
        startup = boom.autostart_list()
        self.assertIsInstance(applications, list)
        self.assertIsInstance(startup, list)
        if applications:
            self.assertIn("uninstallable", applications[0])
            self.assertIn("source", applications[0])
        if startup:
            self.assertIn(startup[0]["enabled"], {"Ativo", "Desativado"})

    def test_flatpak_export_is_only_removable_for_matching_installed_scope(self):
        with tempfile.TemporaryDirectory() as home:
            launcher_dir = Path(home) / ".local/share/flatpak/exports/share/applications"
            launcher_dir.mkdir(parents=True)
            launcher = launcher_dir / "org.example.App.desktop"
            launcher.write_text("[Desktop Entry]\nName=Example\n", encoding="utf-8")
            with patch.object(boom.Path, "home", return_value=Path(home)):
                target = boom.desktop_application_uninstall_target(
                    str(launcher), {"org.example.App": {"user"}}, {},
                )
                mismatch = boom.desktop_application_uninstall_target(
                    str(launcher), {"org.example.App": {"system"}}, {},
                )
        self.assertEqual(target, {
            "manager": "flatpak", "app_id": "org.example.App", "installation": "user",
        })
        self.assertIsNone(mismatch)

    def test_apt_preview_blocks_removing_other_packages(self):
        package = {"manager": "apt", "package": "example-app"}
        with patch("boom.shutil.which", side_effect=lambda name: "/usr/bin/apt-get" if name == "apt-get" else None), patch(
            "boom.run_command",
            return_value=subprocess.CompletedProcess(
                args=[], returncode=0, stdout="Purg example-app [1.0]\nRemv shared-consumer [2.0]\n", stderr="",
            ),
        ):
            preview = boom.preview_desktop_application_uninstall(package)
        self.assertFalse(preview["ok"])
        self.assertIn("shared-consumer", preview["message"])

    def test_apt_uninstall_purges_only_confirmed_package(self):
        package = {"manager": "apt", "package": "example-app"}
        simulation = subprocess.CompletedProcess(args=[], returncode=0, stdout="Purg example-app [1.0]\n", stderr="")
        with patch("boom.shutil.which", side_effect=lambda name: {
            "apt-get": "/usr/bin/apt-get", "pkexec": "/usr/bin/pkexec",
        }.get(name)), patch("boom.run_command", return_value=simulation), patch(
            "boom.run_privileged_apt_step", return_value={"ok": True},
        ) as execute:
            result = boom.uninstall_desktop_application(package)
        self.assertTrue(result["ok"])
        self.assertEqual(execute.call_args.args[2], [
            "purge", "--yes", "--no-auto-remove", "example-app",
        ])

    def test_flatpak_uninstall_also_deletes_application_data(self):
        target = {"manager": "flatpak", "app_id": "org.example.App", "installation": "user"}
        with patch("boom.shutil.which", side_effect=lambda name: "/usr/bin/flatpak" if name == "flatpak" else None), patch(
            "boom.run_command",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ) as run:
            result = boom.uninstall_desktop_application(target)
        self.assertTrue(result["ok"])
        self.assertEqual(run.call_args.args[0], [
            "/usr/bin/flatpak", "uninstall", "--user", "--delete-data", "--assumeyes", "org.example.App",
        ])

    def test_uninstall_target_rejects_unrecognized_desktop_sources(self):
        self.assertIsNone(boom.desktop_application_uninstall_target(
            "/tmp/app.desktop", {}, {},
        ))

    def test_dpkg_owner_lookup_maps_only_single_package_matches(self):
        launcher = "/usr/share/applications/example.desktop"
        output = f"example-package: {launcher}\nshared-one, shared-two: /usr/share/applications/ambiguous.desktop\n"
        with patch("boom.shutil.which", side_effect=lambda name: "/usr/bin/dpkg-query" if name == "dpkg-query" else None), patch(
            "boom.run_command",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=output, stderr=""),
        ):
            owners = boom.dpkg_desktop_owners([launcher, "/usr/share/applications/ambiguous.desktop"])
        self.assertEqual(owners, {launcher: "example-package"})

    def test_uninstall_rejects_invalid_package_and_flatpak_identifiers(self):
        with patch("boom.shutil.which") as which, patch("boom.run_command") as run:
            apt_result = boom.preview_desktop_application_uninstall({
                "manager": "apt", "package": "example;command",
            })
            flatpak_result = boom.preview_desktop_application_uninstall({
                "manager": "flatpak", "app_id": "--help", "installation": "user",
            })
        self.assertFalse(apt_result["ok"])
        self.assertFalse(flatpak_result["ok"])
        which.assert_not_called()
        run.assert_not_called()

    def test_application_launch_uses_gio_without_shell(self):
        app = MagicMock()
        app.launch.return_value = True
        desktop_app_info = MagicMock()
        desktop_app_info.new_from_filename.return_value = app
        fake_gio = types.SimpleNamespace(DesktopAppInfo=desktop_app_info)
        fake_repository = types.ModuleType("gi.repository")
        fake_repository.Gio = fake_gio
        fake_gi = types.ModuleType("gi")
        fake_gi.require_version = MagicMock()
        with patch.dict(sys.modules, {"gi": fake_gi, "gi.repository": fake_repository}):
            result = boom.launch_desktop_application("/tmp/example.desktop")
        self.assertEqual(result, "Aplicativo iniciado.")
        desktop_app_info.new_from_filename.assert_called_once_with("/tmp/example.desktop")
        app.launch.assert_called_once_with([], None)

    def test_boom_autostart_entry_can_be_enabled_and_removed(self):
        with tempfile.TemporaryDirectory() as config_home:
            with patch.dict("boom.os.environ", {"XDG_CONFIG_HOME": config_home}):
                path = boom.set_autostart(True)
                contents = path.read_text(encoding="utf-8")
                self.assertTrue(boom.autostart_enabled())
                self.assertIn("--tray", contents)
                self.assertIn('"' + boom.sys.executable + '"', contents)
                boom_entries = [entry for entry in boom.autostart_list() if entry["id"] == "boom-monitor"]
                self.assertEqual(len(boom_entries), 1)
                self.assertEqual(boom_entries[0]["enabled"], "Ativo")
                boom.set_autostart(False)
                self.assertFalse(path.exists())
                self.assertFalse(boom.autostart_enabled())

    def test_generated_autostart_desktop_entry_is_accepted_by_gio(self):
        try:
            import gi
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio
        except (ImportError, ValueError):
            self.skipTest("Gio indisponível")
        with tempfile.TemporaryDirectory() as config_home:
            with patch.dict("boom.os.environ", {"XDG_CONFIG_HOME": config_home}):
                path = boom.set_autostart(True)
                app = Gio.DesktopAppInfo.new_from_filename(str(path))
                self.assertIsNotNone(app)
                self.assertEqual(app.get_name(), "BooM")


class TrayMonitoringTests(unittest.TestCase):
    def test_tray_collector_reads_local_cpu_memory_and_temperature(self):
        metrics = boom.collect_tray_metrics()
        self.assertIn("cpu_percent", metrics)
        self.assertIn("memory", metrics)
        self.assertIn("temperatures", metrics)
        self.assertIn("sensor_readings", metrics)
        self.assertIn("cpu_frequency", metrics)
        self.assertIn("gpu_metrics", metrics)
        self.assertIn("power", metrics)
        self.assertIn("battery", boom.format_tray_metrics(metrics))
        self.assertTrue(metrics["updated_at"])
        self.assertGreater(metrics["memory"]["total"], 0)

    def test_tray_summary_formats_cpu_memory_and_hottest_temperature(self):
        labels = boom.format_tray_metrics({
            "cpu_percent": 12.34,
            "memory": {"used": 4 * 1024**3, "total": 8 * 1024**3},
            "temperatures": [{"name": "CPU", "celsius": 52.1}, {"name": "GPU", "celsius": 67.4}],
            "updated_at": "12:34:56",
        })
        self.assertEqual(labels["cpu"], "CPU: 12.3%")
        self.assertIn("RAM: 4.0 GB / 8.0 GB (50%)", labels["memory"])
        self.assertEqual(labels["temperature"], "Temperatura máxima: 67.4 °C")
        labels = boom.format_tray_metrics({
            "cpu_percent": 12.34,
            "cpu_frequency": {"average_mhz": 3700},
            "memory": {"used": 4 * 1024**3, "total": 8 * 1024**3},
            "gpu_metrics": [{
                "name": "NVIDIA GeForce RTX 3060",
                "gpu_utilization_percent": 35,
                "memory_utilization_percent": 57,
                "temperature_celsius": 64,
                "graphics_clock_mhz": 1800,
                "memory_clock_mhz": 7500,
                "vram_used_mib": 4096,
                "vram_total_mib": 12288,
                "power_draw_watts": 110,
                "power_limit_watts": 170,
                "fan_percent": 45,
                "pcie_generation": 4,
                "pcie_link_width": 16,
            }],
            "temperatures": [],
            "updated_at": "12:34:56",
        })
        self.assertEqual(labels["cpu_clock"], "Frequência reportada média da CPU: 3.70 GHz")
        self.assertIn("35%", labels["gpu"])
        self.assertIn("NVIDIA GeForce RTX 3060", labels["gpu"])
        self.assertEqual(labels["gpu_memory_activity"], "Atividade do controlador de memória: 57%")
        self.assertIn("núcleo 1.80 GHz", labels["gpu_clock"])
        self.assertIn("4096 / 12288 MiB", labels["vram"])
        self.assertIn("33.3%", labels["vram"])
        self.assertIn("110.0 W / 170.0 W", labels["gpu_power"])
        self.assertEqual(labels["gpu_fan"], "Ventoinha da GPU: 45%")
        self.assertIn("geração 4 · x16", labels["gpu_link"])
        self.assertIn("GPU 35%", labels["panel"])
        self.assertIn("VRAM 4.0/12.0 GB · 33%", labels["panel"])
        self.assertEqual(labels["updated"], "Atualizado às 12:34:56")


class TrayRegistrationTests(unittest.TestCase):
    def probe_with_owner(self, has_owner):
        connection = MagicMock()
        reply = MagicMock()
        reply.unpack.return_value = (has_owner,)
        connection.call_sync.return_value = reply
        gio = types.SimpleNamespace(
            BusType=types.SimpleNamespace(SESSION=object()),
            DBusCallFlags=types.SimpleNamespace(NO_AUTO_START=object()),
            bus_get_sync=MagicMock(return_value=connection),
        )
        glib = types.SimpleNamespace(
            Variant=MagicMock(),
            VariantType=types.SimpleNamespace(new=MagicMock()),
        )
        fake_repository = types.ModuleType("gi.repository")
        fake_repository.Gio = gio
        fake_repository.GLib = glib
        fake_gi = types.ModuleType("gi")
        fake_gi.require_version = MagicMock()
        with patch.dict(sys.modules, {"gi": fake_gi, "gi.repository": fake_repository}):
            result = boom.status_notifier_watcher_status()
        return result, connection

    def test_tray_probe_checks_watcher_name_with_bus_daemon(self):
        result, connection = self.probe_with_owner(True)
        self.assertEqual(result, (True, None))
        self.assertEqual(connection.call_sync.call_args.args[:4], (
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
        ))

    def test_tray_probe_reports_missing_cosmic_watcher(self):
        result, _connection = self.probe_with_owner(False)
        self.assertFalse(result[0])
        self.assertIn("COSMIC", result[1])


class GtkThemeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import gi
            gi.require_version("Gtk", "3.0")
            from gi.repository import Gtk
        except (ImportError, ValueError):
            raise unittest.SkipTest("GTK 3/PyGObject não instalado")
        cls.Gtk = Gtk

    def test_theme_css_parses(self):
        provider = self.Gtk.CssProvider()
        errors = []
        provider.connect("parsing-error", lambda _provider, _section, error: errors.append(str(error)))
        provider.load_from_data(boom.GTK_CSS)
        self.assertEqual(errors, [])

    def test_navigation_defers_render_and_avoids_toggle_buttons(self):
        source = inspect.getsource(boom.launch_gtk)
        navigation = source.split("def on_navigation", 1)[1].split("def render_scheduled_page", 1)[0]
        self.assertIn("Gtk.Button(label=title)", source)
        self.assertIn("titles = list(NAVIGATION_PAGES)", source)
        self.assertIn("GLib.idle_add(self.render_scheduled_page)", navigation)
        self.assertNotIn("set_active(", navigation)
        self.assertIn("HANDLES_COMMAND_LINE", inspect.getsource(boom.launch_gtk))


if __name__ == "__main__":
    unittest.main()
