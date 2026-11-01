# Contributing

Thanks for helping improve BooM. Keep changes focused and preserve the rule that
system data comes from the host rather than fabricated placeholders.

## Before opening a pull request

1. Describe the user problem and the behavior you changed.
2. Keep privileged operations explicit and narrow. Do not add arbitrary command
   execution, background network access or silent process termination.
3. Run the checks below and include their results in the pull request.

```bash
python3 -m py_compile boom.py tests/test_boom.py
python3 -m unittest discover -s tests -v
python3 boom.py --snapshot
```

For GTK or tray changes, also check the interaction in a graphical Linux session
with a StatusNotifier host. State clearly when that visual check is unavailable.

## Project conventions

- Python 3.10 or newer; prefer the standard library and system-provided GTK/Gio.
- Keep user-facing strings in Portuguese and encode source files as UTF-8.
- Update `CHANGELOG.md` for user-visible changes and keep the README accurate.
- Do not commit machine-specific configuration, hardware snapshots or personal
  paths.

## Licensing

By contributing, you agree that your contributions are distributed under the
MIT License in this repository. Include attribution and license details for any
third-party material you propose to add.
