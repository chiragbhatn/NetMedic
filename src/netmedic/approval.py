"""Human approval for change tools.

`prompt_approver` asks on the terminal (CLI). `file_approver` checks for a marker file
written by `approve_change` (MCP server, where stdin/stdout belong to the protocol).
Applying a change needs `<id>.approved`; rolling it back needs `<id>.rollback-approved`.
"""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console
from rich.prompt import Confirm

from netmedic.errors import ApprovalPending, NetMedicError
from netmedic.tools.toolbox import CHANGE_ID_RE, Approver, ChangeProposal

MARKER_SUFFIXES = {"proposed": ".approved", "applied": ".rollback-approved"}


def _marker_suffix(status: str) -> str | None:
    return MARKER_SUFFIXES.get(status)


def prompt_approver(console: Console | None = None) -> Approver:
    """Show the change and ask yes/no on the terminal. Default is No."""
    console = console or Console()

    def approve(proposal: ChangeProposal) -> bool:
        action = "ROLL BACK" if proposal.status == "applied" else "APPLY"
        console.print(
            f"\n[bold yellow]{action} change {proposal.id}[/] on [bold]{proposal.device}[/]"
        )
        console.print(f"Reason: {proposal.reason}", markup=False)
        if proposal.status == "applied":
            console.print("This restores the running config saved before the change.")
        else:
            console.print("Commands:")
            for command in proposal.commands:
                console.print(f"  {command}", markup=False, highlight=False)
        return Confirm.ask(f"{action.capitalize()} this change?", default=False, console=console)

    return approve


def file_approver(changes_dir: Path) -> Approver:
    """Approve only if the marker for the proposal's next action exists in `changes_dir`.

    A missing marker raises ApprovalPending (the change keeps its status and can be retried
    after `netmedic approve <id>`); any status other than proposed/applied returns False.
    """
    changes_dir = Path(changes_dir)

    def approve(proposal: ChangeProposal) -> bool:
        suffix = _marker_suffix(proposal.status)
        if suffix is None:
            return False
        if not (changes_dir / f"{proposal.id}{suffix}").is_file():
            action = "rollback" if proposal.status == "applied" else "apply"
            raise ApprovalPending(
                f"change {proposal.id} ({action}) is awaiting human approval; "
                f"ask the operator to run `netmedic approve {proposal.id}`, then retry"
            )
        return True

    return approve


def approve_change(changes_dir: Path, change_id: str) -> Path:
    """Write the approval marker for a change's next action and return its path.

    Status `proposed` -> `<id>.approved` (apply); `applied` -> `<id>.rollback-approved`.
    """
    if not CHANGE_ID_RE.match(change_id):
        raise NetMedicError(f"invalid change_id '{change_id}'")
    changes_dir = Path(changes_dir)
    path = changes_dir / f"{change_id}.json"
    if not path.is_file():
        raise NetMedicError(f"unknown change_id '{change_id}'")
    status = json.loads(path.read_text(encoding="utf-8")).get("status")
    suffix = _marker_suffix(status)
    if suffix is None:
        raise NetMedicError(
            f"change {change_id} is '{status}'; only 'proposed' or 'applied' can be approved"
        )
    marker = changes_dir / f"{change_id}{suffix}"
    marker.write_text("approved\n", encoding="utf-8")
    return marker
