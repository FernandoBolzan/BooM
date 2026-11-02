# Changelog

All notable changes to BooM are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Dedicated Memória and Gráficos areas in GTK and `curses`; RAM, swap and per-GPU VRAM telemetry are separated from GPU-core telemetry. `/proc/meminfo` parsing now exposes swap totals and availability.
- Per-core CPU frequencies from Linux CPUFreq/sysfs, with source labels and live updates.
- NVIDIA GPU telemetry from the installed `nvidia-smi`/NVML driver, including GPU-core and memory-controller utilization, used/total VRAM and percentage, temperature, graphics and memory clocks, power, fan speed, P-state and current PCIe link when supported.
- Live CPU, memory, GPU and temperature updates in the open window as well as the StatusNotifier menu.
- Physical disk inventory with SMART JSON parsing and a user-triggered, authorization-gated, read-only health query.
- Battery and external power information from Linux `power_supply` sysfs, including an explicit no-battery state.
- Offline HTML system report from the desktop or CLI; serial numbers, UUIDs and MAC addresses are redacted, IPs/routes may appear, and the saved file is created with owner-only permissions.

### Notes

- GPU metrics are vendor-specific in this iteration; non-NVIDIA adapters remain visible in the PCI inventory while live telemetry support is pending.
- CPUFreq values are identified by source because `scaling_cur_freq` may report the last frequency requested by the driver rather than an instantaneous hardware measurement.

## [0.1.0] - 2026-11-01

Initial public Linux release.

### Added

- GTK 3 desktop interface and `curses` terminal interface with seven areas.
- Local hardware and system inventory, including RAM module details when SMBIOS provides them.
- Disk and network inventory with an explicit, fixed-target network diagnostic.
- Process manager with confirmation, PID-reuse protection and `SIGTERM`.
- Desktop application inventory and safe launch through `.desktop` metadata.
- Session autostart inventory and BooM autostart control.
- Ayatana StatusNotifier tray indicator with background CPU, memory and temperature monitoring.
- JSON system snapshot, unit tests, usage documentation and CI workflow.
- MIT license.

### Notes

- The tray icon requires a working StatusNotifier/AppIndicator host in the desktop session.
- Hardware details depend on firmware, sensors and permissions. Missing information is reported as unavailable.
- GTK tray interaction has not been visually verified in every supported desktop environment.
