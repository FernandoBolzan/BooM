import contextlib
import inspect
import io
import json
import signal
import sys
import tempfile
import types
import unittest
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import boom


class FormatTests(unittest.TestCase):
    def test_release_version_is_declared(self):
        self.assertEqual(boom.__version__, "0.1.0")

    def test_version_command_prints_release_version(self):
        output = io.StringIO()
        with patch.object(boom.sys, "argv", ["boom.py", "--version"]):
            with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as result:
                boom.main()
        self.assertEqual(result.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), "BooM 0.1.0")

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
            "Rede", "Processos", "Programas", "Inicialização",
        ))
        self.assertEqual(boom.PAGE_MEMORY, 2)
        self.assertEqual(boom.PAGE_GRAPHICS, 3)
        self.assertEqual(boom.PAGE_DISKS, 4)
        self.assertEqual(len(boom.NAVIGATION_PAGES), len(boom.NAVIGATION_DESCRIPTIONS))

    def test_uptime(self):
        self.assertEqual(boom.format_uptime(90061), "1d 01h 01min")


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

    def test_nvidia_smi_parser_keeps_units_and_marks_unsupported_fields(self):
        output = """NVIDIA GeForce RTX 3060, 595.104.02, 18, 21, 48, 2048, 12288, 1830, 7501, 72.50, 170.00, P2
NVIDIA GeForce RTX 4060, 595.104.02, N/A, N/A, N/A, N/A, N/A, N/A, N/A, N/A, N/A, P8
"""
        gpus = boom.parse_nvidia_smi_csv(output)
        self.assertEqual(len(gpus), 2)
        self.assertEqual(gpus[0]["name"], "NVIDIA GeForce RTX 3060")
        self.assertEqual(gpus[0]["gpu_utilization_percent"], 18)
        self.assertEqual(gpus[0]["temperature_celsius"], 48)
        self.assertEqual(gpus[0]["graphics_clock_mhz"], 1830)
        self.assertEqual(gpus[0]["power_draw_watts"], 72.5)
        self.assertEqual(gpus[0]["source"], "nvidia-smi/NVML")
        self.assertIsNone(gpus[1]["gpu_utilization_percent"])

    def test_gpu_telemetry_queries_nvidia_backend_and_merges_optional_fields(self):
        main = subprocess.CompletedProcess(args=["nvidia-smi"], returncode=0, stdout=(
            "NVIDIA GeForce RTX 3060, 595.104.02, 18, 21, 48, 2048, 12288, 1830, 7501, 72.50, 170.00, P2\n"
        ), stderr="")
        optional = subprocess.CompletedProcess(args=["nvidia-smi"], returncode=0, stdout="43, 4, 16\n", stderr="")
        with patch("boom.shutil.which", return_value="/usr/bin/nvidia-smi"), patch("boom.run_command", side_effect=[main, optional]) as run:
            metrics, status = boom.gpu_telemetry_info()
        self.assertIn("nvidia-smi/NVML", status)
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
            self.assertIn("path", applications[0])
            self.assertIn("available", applications[0])
        if startup:
            self.assertIn(startup[0]["enabled"], {"Ativo", "Desativado"})

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
