# Changelog

All notable changes to BooM are documented here. This project follows
[Semantic Versioning](https://semver.org/).

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
