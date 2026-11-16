"""NetMedic command line (SPEC section 11)."""

from __future__ import annotations

import json
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.markup import escape
from rich.prompt import Confirm
from rich.table import Table

from netmedic.agent import SUBMIT_REPORT, DiagnosisReport, run_diagnosis
from netmedic.approval import approve_change, file_approver, prompt_approver
from netmedic.audit import AuditLog
from netmedic.drivers import ReplayDriver, slugify
from netmedic.drivers.base import Driver
from netmedic.errors import NetMedicError
from netmedic.evals import (
    DEFAULT_SETTLE_SECONDS,
    RunResult,
    load_scenarios,
    run_eval,
    ssh_driver_from_env,
    write_results,
)
from netmedic.inventory import Inventory, load_inventory
from netmedic.llm import LLMClient, create_llm, describe_llm
from netmedic.platforms import PROBE_TARGETS  # noqa: F401  (re-exported for callers)
from netmedic.platforms import record_commands as _platform_record_commands
from netmedic.tools.toolbox import Approver, Toolbox

app = typer.Typer(help="NetMedic: AI network troubleshooting agent.", no_args_is_help=True)


def make_console(stderr: bool = False, width: int | None = None) -> Console:
    """Line output is soft-wrapped: one logical line stays one line whatever the terminal
    width (or when piped), so output does not depend on where it runs. Tables opt out."""
    return Console(stderr=stderr, soft_wrap=True, width=width)


console = make_console()
err = make_console(stderr=True)

# Logical command sets for `record` live in netmedic.platforms (SPEC sections 6 and 7).


def make_llm() -> LLMClient:
    """LLM factory (Anthropic, model from NETMEDIC_MODEL); tests replace it."""
    return create_llm()


def build_toolbox(
    scenario: str,
    live: bool,
    approver: Approver | None,
    allow_changes: bool,
    root: Path,
) -> Toolbox:
    """The one place commands build a Toolbox. `scenario` is ignored in live mode."""
    inventory = _load_inventory(root)
    if not live:
        _warn_missing_fixtures(inventory, root)
    driver: Driver = ssh_driver_from_env() if live else ReplayDriver(root / "fixtures", scenario)
    return Toolbox(
        inventory, driver, AuditLog(root / "audit.jsonl"),
        golden_dir=root / "lab" / "golden", changes_dir=root / "changes",
        approver=approver, allow_changes=allow_changes,
    )


def _load_inventory(root: Path) -> Inventory:
    """load_inventory() plus its warnings on stderr (never stdout: mcp owns stdout)."""
    inventory = load_inventory(root)
    for warning in inventory.warnings:
        err.print(f"[yellow]inventory: {escape(warning)}[/]")
    return inventory


def _warn_missing_fixtures(inventory: Inventory, root: Path) -> None:
    """Replay mode keys fixtures by device name; NetBox names may not match them."""
    if not inventory.source.startswith("NetBox"):
        return
    missing = [n for n in inventory.devices if not (root / "fixtures" / "healthy" / n).is_dir()]
    if missing:
        err.print(f"[yellow]warning: no fixtures/healthy/ directory for NetBox device(s) "
                  f"{escape(', '.join(missing))}; replay mode cannot answer for them[/]")


def _fail(exc: Exception) -> typer.Exit:
    err.print(f"[red]error:[/] {escape(str(exc))}")
    return typer.Exit(2)


@app.callback()
def main() -> None:
    load_dotenv(Path.cwd() / ".env")


# --- diagnose -------------------------------------------------------------------------

MAX_ARGS_SHOWN = 200


def _show_step(step: int, tool: str, args: dict, result: str) -> None:
    error = result.startswith("ERROR")
    if tool == SUBMIT_REPORT:  # the full report is printed once at the end
        shown = f"fault_type={args.get('fault_type')} device={args.get('device')}"
        summary = result
    else:
        shown = json.dumps(args)
        if len(shown) > MAX_ARGS_SHOWN:
            shown = shown[:MAX_ARGS_SHOWN] + "..."
        first = result.strip().splitlines()[0] if result.strip() else ""
        summary = first if error else f"{len(result)} chars"
    style = "red" if error else "dim"
    console.print(
        f"[bold]{step:>2}[/] [cyan]{escape(tool)}[/] {escape(shown)} "
        f"[{style}]-> {escape(summary[:300] if tool == SUBMIT_REPORT else summary[:120])}[/]"
    )


def _show_report(report: DiagnosisReport) -> None:
    console.rule("Diagnosis")
    console.print(f"[bold]Root cause:[/] {escape(report.root_cause)}")
    console.print(f"[bold]Device:[/] {report.device or '-'}    [bold]Fault type:[/] "
                  f"{report.fault_type}    [bold]Confidence:[/] {report.confidence}")
    console.print("[bold]Evidence:[/]")
    for line in report.evidence or ["(none)"]:
        console.print(f"  - {escape(line)}")
    console.print("[bold]Proposed fix:[/]")
    for line in report.proposed_fix or ["(none)"]:
        console.print(f"  {escape(line)}")
    console.print(f"[dim]{report.steps_taken} steps, {report.input_tokens} input "
                  f"({report.cached_input_tokens} cached) / {report.output_tokens} output "
                  "tokens[/]")


@app.command()
def diagnose(
    problem: str = typer.Argument(..., help="Plain-English problem statement."),
    scenario: str = typer.Option("healthy", help="Fixture scenario (replay mode)."),
    live: bool = typer.Option(False, "--live", help="Use the live lab over SSH."),
    max_steps: int = typer.Option(25, help="Step budget (LLM calls)."),
    no_fix: bool = typer.Option(False, "--no-fix", help="Do not offer to apply the fix."),
) -> None:
    """Diagnose a network problem."""
    root = Path.cwd()
    try:
        toolbox = build_toolbox(scenario, live, None, False, root)
        llm = make_llm()
        console.print(f"[dim]LLM: {escape(describe_llm(llm))}[/]")
        report = run_diagnosis(problem, toolbox, llm, max_steps, on_step=_show_step)
    except NetMedicError as exc:
        raise _fail(exc) from None
    _show_report(report)
    if no_fix or not report.proposed_fix or not report.device:
        return
    if not Confirm.ask(f"Turn the proposed fix into a change on {report.device}?",
                       default=False, console=console):
        return
    try:
        changer = build_toolbox(scenario, live, prompt_approver(console), True, root)
    except NetMedicError as exc:
        raise _fail(exc) from None
    proposed = changer.call("propose_change", {
        "device": report.device, "commands": report.proposed_fix, "reason": report.root_cause,
    })
    if proposed.startswith("ERROR"):
        console.print(f"[red]{escape(proposed)}[/]")
        raise typer.Exit(1)
    change_id = json.loads(proposed)["change_id"]
    result = changer.call("apply_change", {"change_id": change_id})
    if result.startswith("ERROR"):
        console.print(f"[red]{escape(result)}[/]")
        return
    console.print(escape(result))
    console.print(f"Change id: {change_id}")
    console.print(f"To roll back: netmedic rollback {change_id}", markup=False)


# --- inventory ----------------------------------------------------------------------------

@app.command("inventory")
def inventory_command() -> None:
    """Show the devices NetMedic will use (YAML or NetBox); no LLM call."""
    try:
        inventory = _load_inventory(Path.cwd())
    except NetMedicError as exc:
        raise _fail(exc) from None
    # Plain columns sized to the data, not the terminal: nothing is ever cropped, and the
    # output is the same on any terminal or when piped (one device per line).
    rows = [("NAME", "PLATFORM", "ROLE", "HOST", "PORT")] + [
        (d.name, d.platform, d.role, d.host, str(d.port)) for d in inventory.devices.values()
    ]
    widths = [max(len(row[i]) for row in rows) for i in range(5)]
    console.print(f"Source: {inventory.source}", markup=False)
    for row in rows:
        line = "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True))
        console.print(line.rstrip(), markup=False)
    if inventory.links:
        console.print("Links: " + ", ".join(f"{a}-{b}" for a, b in inventory.links),
                      markup=False)


# --- approve / rollback / mcp ------------------------------------------------------------

@app.command()
def rollback(
    change_id: str = typer.Argument(..., help="Id of an applied change."),
    scenario: str = typer.Option("healthy", help="Fixture scenario (replay mode)."),
    live: bool = typer.Option(False, "--live", help="Use the live lab over SSH."),
) -> None:
    """Roll back an applied change (asks for confirmation)."""
    try:
        toolbox = build_toolbox(scenario, live, prompt_approver(console), True, Path.cwd())
    except NetMedicError as exc:
        raise _fail(exc) from None
    result = toolbox.call("rollback_change", {"change_id": change_id})
    if result.startswith("ERROR"):
        err.print(f"[red]{escape(result)}[/]")
        raise typer.Exit(2)
    console.print(escape(result))

@app.command()
def approve(change_id: str = typer.Argument(..., help="Id returned by propose_change.")) -> None:
    """Approve a pending change (or its rollback) for the MCP server."""
    try:
        marker = approve_change(Path.cwd() / "changes", change_id)
    except NetMedicError as exc:
        raise _fail(exc) from None
    action = "rollback" if marker.name.endswith(".rollback-approved") else "apply"
    console.print(f"Approved {action} of change {change_id}: wrote {marker}")


@app.command()
def mcp(
    scenario: str = typer.Option("healthy", help="Fixture scenario (replay mode)."),
    live: bool = typer.Option(False, "--live", help="Use the live lab over SSH."),
    allow_changes: bool = typer.Option(
        False, "--allow-changes", help="Expose apply/rollback (needs `netmedic approve`)."
    ),
) -> None:
    """Serve the device tools over MCP (stdio). Prints nothing to stdout."""
    from netmedic.mcp_server import serve

    root = Path.cwd()
    approver = file_approver(root / "changes") if allow_changes else None
    try:
        toolbox = build_toolbox(scenario, live, approver, allow_changes, root)
    except NetMedicError as exc:
        raise _fail(exc) from None
    serve(toolbox)


@app.command()
def web(
    scenario: str = typer.Option("healthy", help="Default fixture scenario (replay mode)."),
    live: bool = typer.Option(False, "--live", help="Use the live lab over SSH."),
    host: str = typer.Option("127.0.0.1", help="Address to listen on."),
    port: int = typer.Option(8080, help="Port to listen on."),
    max_steps: int = typer.Option(25, help="Step budget (LLM calls) per diagnosis."),
    allow_remote: bool = typer.Option(
        False, "--allow-remote", help="Allow a --host other than a loopback address."
    ),
) -> None:
    """Serve the local web UI: run read-only diagnoses and watch them step by step."""
    import ipaddress

    try:
        import uvicorn

        from netmedic.web.app import LOOPBACK_HOSTS, create_app
    except ImportError:
        raise _fail(NetMedicError(
            'the web UI needs FastAPI and uvicorn: pip install -e ".[web]"')) from None
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        err.print(f"[yellow]warning: --host {escape(host)} is not a loopback address. The web "
                  "UI has no authentication: anyone who can reach it can start diagnoses, and "
                  "each diagnosis spends Anthropic API tokens.[/]")
        if not allow_remote:
            err.print("[red]error:[/] pass --allow-remote to listen on it anyway")
            raise typer.Exit(2)
    root = Path.cwd()
    try:
        _load_inventory(root)  # fail fast on a broken inventory
    except NetMedicError as exc:
        raise _fail(exc) from None
    web_app = create_app(root, live=live, default_scenario=scenario, max_steps=max_steps,
                         trusted_hosts=[*LOOPBACK_HOSTS, host] if loopback else ["*"])
    shown = f"[{host}]" if ":" in host else host
    console.print(f"NetMedic web UI: http://{shown}:{port}/  (Ctrl+C to stop)", markup=False)
    uvicorn.run(web_app, host=host, port=port, log_level="warning")


# --- eval ---------------------------------------------------------------------------------

def _results_table(results: list[RunResult]) -> Table:
    table = Table("Scenario", "Run", "Result", "Reported", "Expected", "Steps",
                  "Tokens in (cached) / out", "Safety")
    for r in results:
        outcome = "[green]pass[/]" if r.passed else "[red]FAIL[/]"
        if r.error:
            outcome += f" [dim]{escape(r.error[:60])}[/]"
        safety = "ok" if r.safety_ok else "[red]FAIL[/]"
        if r.safety_notes:
            safety += f" [dim]{escape('; '.join(r.safety_notes))}[/]"
        table.add_row(r.scenario_id, str(r.run), outcome, f"{r.fault_type} / {r.device}",
                      f"{r.expected_fault_type} / {r.expected_device}", str(r.steps),
                      f"{r.input_tokens} ({r.cached_input_tokens}) / {r.output_tokens}", safety)
    return table


@app.command("eval")
def eval_command(
    runs: int = typer.Option(1, min=1, help="Runs per scenario."),
    scenario: str | None = typer.Option(None, help="Only this scenario id."),
    live: bool = typer.Option(False, "--live", help="Inject faults in the live lab."),
    max_steps: int = typer.Option(25, help="Step budget per run."),
    settle: float = typer.Option(DEFAULT_SETTLE_SECONDS, help="Live mode: seconds to wait "
                                 "after injecting a fault."),
) -> None:
    """Run the evaluation scenarios and write evals/results.md."""
    root = Path.cwd()
    try:
        scenarios = load_scenarios(root / "evals" / "scenarios")
    except NetMedicError as exc:
        raise _fail(exc) from None
    if scenario:
        scenarios = [s for s in scenarios if s.id == scenario]
        if not scenarios:
            raise _fail(NetMedicError(f"no scenario with id '{scenario}'"))
    llms: list[LLMClient] = []

    def factory() -> LLMClient:
        llms.append(make_llm())
        return llms[-1]

    results = run_eval(
        scenarios, factory, root, runs=runs, live=live, max_steps=max_steps,
        settle_seconds=settle,
        on_result=lambda r: console.print(
            f"{r.scenario_id} run {r.run}: {'pass' if r.passed else 'FAIL'}"
            + (f" (error: {r.error})" if r.error else ""), markup=False,
        ),
    )
    out = root / "evals" / "results.md"
    if results and all(r.error for r in results):
        err.print(f"[red]Eval could not run: all {len(results)} run(s) ended in an error.[/]")
        for message in dict.fromkeys(r.error for r in results):
            err.print(f"  {escape(str(message))}")
        err.print(f"{escape(str(out))} was not written.")
        raise typer.Exit(2)
    model = describe_llm(llms[0]) if llms else "unknown"
    console.print(f"LLM: {model}", markup=False)
    write_results(results, out, model, "live" if live else "replay")
    console.print(_results_table(results), soft_wrap=False)  # tables size to the terminal
    passed = sum(r.passed for r in results)
    unsafe = sum(not r.safety_ok for r in results)
    console.print(f"{passed} of {len(results)} passed; {unsafe} safety failure(s). "
                  f"Wrote {out}")
    if unsafe:
        raise typer.Exit(1)


# --- record -------------------------------------------------------------------------------

def record_commands(name: str, platform: str) -> list[str]:
    """Every logical command recorded for one device (SPEC sections 6 and 7)."""
    return _platform_record_commands(name, platform)


@app.command()
def record(scenario: str = typer.Option(..., help="Scenario id to record into fixtures/.")) -> None:
    """Record live lab output into fixtures/<scenario>/ (needs the lab)."""
    root = Path.cwd()
    try:
        inventory = _load_inventory(root)
        driver = ssh_driver_from_env()
    except NetMedicError as exc:
        raise _fail(exc) from None
    failures = 0
    for device in inventory.devices.values():
        out_dir = root / "fixtures" / scenario / device.name
        out_dir.mkdir(parents=True, exist_ok=True)
        for command in record_commands(device.name, device.platform):
            try:
                output = driver.run(device, command)
            except Exception as exc:
                failures += 1
                err.print(f"[yellow]{device.name}: '{escape(command)}' failed: "
                          f"{escape(str(exc))}[/]")
                continue
            (out_dir / f"{slugify(command)}.txt").write_text(output, encoding="utf-8")
        console.print(f"recorded {device.name}")
    if failures:
        raise typer.Exit(1)
