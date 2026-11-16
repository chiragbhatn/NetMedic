# Media

These are real captures of real model runs in replay mode on synthetic fixtures: the lab outputs are hand-written, not from live devices. Model: `anthropic / claude-sonnet-5-5`. Captured 2026-11-16 with `scripts/capture_demo.py` (spends API tokens; see its docstring).

| File | Shows | Scenario | Theme | Viewport |
|---|---|---|---|---|
| `web-ui-idle.png` | Page before a run | `srl_interface_down` | dark | 1440x900 @2x |
| `web-ui-running.png` | Mid-run: step cards streaming, queried devices highlighted | `srl_interface_down` | dark | 1440x900 @2x |
| `web-ui-report.png` | Finished run, report panel in view ("Fault found on r4") | `srl_interface_down` | dark | 1440x900 @2x |
| `web-ui-full.png` | Full page of the finished run | `srl_interface_down` | dark | 1440 wide, full page @2x |
| `web-ui-report-light.png` | Same finished run reloaded via `#run=`, report in view | `srl_interface_down` | light | 1440x900 @2x |
| `web-ui-mobile.png` | Same finished run, full page on a phone | `srl_interface_down` | dark | 390x844 @2x, full page |
| `web-ui-injection.png` | Timeline card with the hostile interface description shown as plain text | `injection` | dark | 1440x900 @2x |
| `web-ui-demo.webm` | One continuous real run: load, Run, cards stream, report, hold | `srl_interface_down` | dark | 1280x720, ~29 s |
| `web-ui-demo.gif` | Sped-up subset of a run for the README, ends on the report | `srl_interface_down` | dark | 960x600, ~8 s |
