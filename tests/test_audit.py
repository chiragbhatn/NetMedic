import json

from netmedic.audit import AuditLog


def test_record_appends_json_lines(tmp_path):
    path = tmp_path / "logs" / "audit.jsonl"
    log = AuditLog(path)
    log.record("run_show", {"device": "r1", "command": "show ip route"}, "r1", 42)
    log.record("apply_change", {"change_id": "abc"}, "r2", 10, approved=True)
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["tool"] == "run_show"
    assert lines[0]["device"] == "r1"
    assert lines[0]["result_chars"] == 42
    assert lines[0]["approved"] is None
    assert lines[1]["approved"] is True
    assert "ts" in lines[0]
