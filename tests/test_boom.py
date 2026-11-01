import contextlib
import inspect
import io
import signal
import sys
import tempfile
import types
import unittest
import subprocess
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

    def test_uptime(self):
        self.assertEqual(boom.format_uptime(90061), "1d 01h 01min")


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
        self.assertEqual(labels["updated"], "Atualizado às 12:34:56")
        self.assertIn("CPU 12% · RAM 50%", labels["panel"])


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
        self.assertIn("GLib.idle_add(self.render_scheduled_page)", navigation)
        self.assertNotIn("set_active(", navigation)
        self.assertIn("HANDLES_COMMAND_LINE", inspect.getsource(boom.launch_gtk))


if __name__ == "__main__":
    unittest.main()
