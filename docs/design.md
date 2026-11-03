# BooM GTK design system

Design system: boom/gtk-local

This native Linux application uses its own GTK 3 styles and reusable widget
helpers. The implementation lives in `boom.py` (`GTK_CSS`, `card`, `metric`,
`add_row`, and `make_grid`); interaction labels and safety limits are reflected
in the README and application screens.

## Direction

Technical utility: a polished performance console for a local Linux computer.
Use a high-contrast forest-green navigation rail, a soft sage canvas, clean
white information cards, and BooM's lime accent; use a restrained mint accent
for GPU readings. Give the live CPU/GPU readings the strongest visual weight,
keep hardware detail calm and scannable, and make actions easy to distinguish.
Use depth, gradients, and rounded surfaces only to separate meaningful areas;
avoid decorative effects that compete with hardware readings.

## Composition

- Group navigation by user goal: Monitorar, Gerenciar, Diagnosticar.
- Keep one page title, its explanation, and page actions in a stable header;
  keep the category chip compact instead of stretching it across the toolbar.
- Use large readings only for decision-making metrics; keep sources, units,
  caveats, and detailed inventory in secondary rows or tables.
- Use a two-column overview for live resources and capacity, with distinct CPU
  and GPU treatments, readable utilization tracks, and supporting platform and
  thermal details below it.
- Use a branded navigation rail with GTK symbolic icons, a clear active state,
  and enough spacing to separate each task group. Present the green rail as a
  rounded floating panel with a balanced outer gap on every side; keep a
  separate inner inset around the brand, section labels, navigation items, and
  local status. Use the bomb emoji (💣) as the BooM brand mark.
- Keep action groups on a wrapping flow so buttons and selectors do not collide
  or get clipped at narrower window widths.
- Preserve keyboard navigation, explicit labels, confirmation dialogs, and all
  existing GTK/TUI behavior.

## Platform

- GTK 3 / PyGObject; no external component or font dependency.
- Desktop layout with a 1024 × 680 minimum window and flexible content scrolling.
- The terminal interface remains a separate `curses` surface and shares data and
  action semantics, not GTK presentation.
