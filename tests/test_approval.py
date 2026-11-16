import io
import json

import pytest
from rich.console import Console

from netmedic.approval import approve_change, file_approver, prompt_approver
from netmedic.errors import ApprovalPending, NetMedicError
from netmedic.tools.toolbox import ChangeProposal


def proposal(status: str = "proposed") -> ChangeProposal:
    return ChangeProposal(
        id="abc123", device="r2", commands=["interface eth2", "no shutdown"],
        reason="eth2 is admin down", status=status, rollback_config=None,
    )


def write(changes_dir, status: str) -> None:
    changes_dir.mkdir(exist_ok=True)
    p = proposal(status)
    (changes_dir / f"{p.id}.json").write_text(json.dumps(p.__dict__))


@pytest.mark.parametrize(("answer", "expected"), [("y", True), ("n", False), ("", False)])
def test_prompt_approver(monkeypatch, answer, expected):
    out = io.StringIO()
    monkeypatch.setattr("builtins.input", lambda *a: answer)
    approve = prompt_approver(Console(file=out, width=100))
    assert approve(proposal()) is expected
    text = out.getvalue()
    assert "r2" in text and "eth2 is admin down" in text and "no shutdown" in text


def test_file_approver_needs_marker(tmp_path):
    approve = file_approver(tmp_path)
    write(tmp_path, "proposed")
    with pytest.raises(ApprovalPending, match="netmedic approve abc123"):
        approve(proposal("proposed"))
    marker = approve_change(tmp_path, "abc123")
    assert marker == tmp_path / "abc123.approved"
    assert approve(proposal("proposed")) is True


def test_apply_approval_does_not_approve_rollback(tmp_path):
    approve = file_approver(tmp_path)
    write(tmp_path, "proposed")
    approve_change(tmp_path, "abc123")
    with pytest.raises(ApprovalPending):
        approve(proposal("applied"))
    write(tmp_path, "applied")
    assert approve_change(tmp_path, "abc123") == tmp_path / "abc123.rollback-approved"
    assert approve(proposal("applied")) is True


@pytest.mark.parametrize("status", ["denied", "rolled_back"])
def test_file_approver_rejects_final_states(tmp_path, status):
    write(tmp_path, status)
    with pytest.raises(NetMedicError):
        approve_change(tmp_path, "abc123")
    assert file_approver(tmp_path)(proposal(status)) is False


@pytest.mark.parametrize("change_id", ["../etc", "ABC", "", "nope99"])
def test_approve_change_rejects_bad_ids(tmp_path, change_id):
    with pytest.raises(NetMedicError):
        approve_change(tmp_path, change_id)
