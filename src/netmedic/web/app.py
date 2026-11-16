"""FastAPI app for the NetMedic web UI: start a read-only diagnosis, stream its steps.

One diagnosis runs at a time in a background thread. Its events (`step`, then one
`report` or `error`) are kept in memory and streamed as Server-Sent Events; a client
that connects late gets every earlier event first. No database, no authentication:
the server is meant for 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from netmedic.agent import run_diagnosis
from netmedic.errors import NetMedicError
from netmedic.evals import load_scenarios
from netmedic.inventory import load_inventory
from netmedic.llm import LLMClient, describe_llm

STATIC_DIR = Path(__file__).parent / "static"
RESULT_LIMIT = 4000  # characters of each tool result sent to the browser
MAX_PROBLEM_CHARS = 500
KEEP_DIAGNOSES = 20
HEARTBEAT_SECONDS = 15.0
POLL_SECONDS = 0.2
LOOPBACK_HOSTS = ["127.0.0.1", "localhost", "::1"]
TERMINAL_EVENTS = ("report", "error")

# The page loads only its own files; device output is rendered as text, and this
# blocks inline scripts as a second line of defence.
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
       "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

_SECRET_RE = re.compile(r"sk-[A-Za-z0-9_\-*]{4,}")


def _truncate(text: str) -> str:
    if len(text) <= RESULT_LIMIT:
        return text
    return text[:RESULT_LIMIT] + (
        f"\n[truncated in the web UI: showing {RESULT_LIMIT} of {len(text)} characters]"
    )


class Diagnosis:
    """One run: its inputs, its event log, and its outcome. Thread-safe appends."""

    def __init__(self, problem: str, scenario: str | None, mode: str):
        self.id = uuid.uuid4().hex[:12]
        self.problem = problem
        self.scenario = scenario
        self.mode = mode
        self.status = "running"
        self.events: list[tuple[str, dict]] = []
        self._lock = threading.Lock()

    def add(self, name: str, data: dict) -> None:
        with self._lock:
            if self.status != "running":
                return  # exactly one terminal event
            self.events.append((name, data))
            if name == "report":
                self.status = "done"
            elif name == "error":
                self.status = "error"

    def snapshot(self, start: int = 0) -> list[tuple[str, dict]]:
        with self._lock:
            return list(self.events[start:])

    def state(self) -> dict:
        events = self.snapshot()
        out: dict[str, Any] = {
            "id": self.id, "status": self.status, "problem": self.problem,
            "scenario": self.scenario, "mode": self.mode,
            "steps": [data for name, data in events if name == "step"],
        }
        for name, data in events:
            if name == "report":
                out["report"] = data
            elif name == "error":
                out["error"] = data["message"]
        return out


def _sse(index: int, name: str, data: dict) -> str:
    return f"id: {index}\nevent: {name}\ndata: {json.dumps(data)}\n\n"


def create_app(
    root: Path,
    live: bool = False,
    default_scenario: str = "healthy",
    max_steps: int = 25,
    llm_factory: Callable[[], LLMClient] | None = None,
    trusted_hosts: list[str] | None = None,
) -> FastAPI:
    """The web UI app. `llm_factory` defaults to `cli.make_llm` (tests inject a fake).

    `trusted_hosts` limits the Host header (default: loopback names only), which stops
    DNS-rebinding pages from driving a local server; pass ["*"] to allow any host.
    """
    from netmedic import cli  # the one place Toolboxes and LLM clients are built

    root = Path(root)
    make_llm = llm_factory or (lambda: cli.make_llm())
    mode = "live" if live else "replay"
    diagnoses: OrderedDict[str, Diagnosis] = OrderedDict()
    start_lock = threading.Lock()

    app = FastAPI(title="NetMedic", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts or LOOPBACK_HOSTS)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    def scenarios() -> list[dict]:
        try:
            return [{"id": s.id, "problem": s.problem}
                    for s in load_scenarios(root / "evals" / "scenarios")]
        except NetMedicError:
            return []

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={
            "Content-Security-Policy": CSP, "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
        })

    @app.get("/api/meta")
    def meta() -> dict:
        try:
            inventory = load_inventory(root)
        except NetMedicError as exc:
            raise HTTPException(500, str(exc)) from None
        llm_name, llm_error = None, None
        try:
            llm_name = describe_llm(make_llm())
        except NetMedicError as exc:
            llm_error = str(exc)
        return {
            "mode": mode,
            "llm": llm_name,
            "llm_error": llm_error,
            "devices": [{"name": d.name, "platform": d.platform, "role": d.role}
                        for d in inventory.devices.values()],
            "links": [[a, b] for a, b in inventory.links],
            "scenarios": scenarios(),
            "default_scenario": default_scenario,
            "max_steps": max_steps,
        }

    def run(diagnosis: Diagnosis, toolbox: Any, llm: LLMClient) -> None:
        def on_step(step: int, tool: str, arguments: dict, result: str) -> None:
            device = arguments.get("device") if isinstance(arguments, dict) else None
            diagnosis.add("step", {
                "step": step, "tool": tool, "arguments": arguments,
                "result": _truncate(result), "is_error": result.startswith("ERROR"),
                "device": device if isinstance(device, str) else None,
            })

        try:
            report = run_diagnosis(diagnosis.problem, toolbox, llm, max_steps, on_step=on_step)
            diagnosis.add("report", dataclasses.asdict(report))
        except NetMedicError as exc:
            diagnosis.add("error", {"message": _SECRET_RE.sub("sk-***", str(exc))})
        except Exception as exc:  # never let a run take the server down
            message = f"{type(exc).__name__}: {exc}"
            diagnosis.add("error", {"message": _SECRET_RE.sub("sk-***", message)})

    @app.post("/api/diagnoses")
    def start(request: Request, payload: dict = Body(...)) -> dict:  # noqa: B008
        origin = request.headers.get("origin")
        if origin and origin.split("://", 1)[-1] != request.headers.get("host"):
            raise HTTPException(403, "cross-origin requests are not allowed")
        problem = payload.get("problem")
        if not isinstance(problem, str) or not problem.strip():
            raise HTTPException(400, "problem must be a non-empty string")
        problem = problem.strip()
        if len(problem) > MAX_PROBLEM_CHARS:
            raise HTTPException(400, f"problem is longer than {MAX_PROBLEM_CHARS} characters")
        scenario: str | None = None
        if not live:
            scenario = payload.get("scenario") or default_scenario
            known = {s["id"] for s in scenarios()} | {default_scenario}
            if scenario not in known:
                raise HTTPException(400, f"unknown scenario '{scenario}'")
        with start_lock:
            if any(d.status == "running" for d in diagnoses.values()):
                raise HTTPException(409, "a diagnosis is already running; wait for it to "
                                         "finish (each run calls the Claude API)")
            try:
                llm = make_llm()
                # Read-only: no approver, change tools off. propose_change only records.
                toolbox = cli.build_toolbox(scenario or default_scenario, live, None, False,
                                            root)
            except NetMedicError as exc:
                raise HTTPException(400, str(exc)) from None
            diagnosis = Diagnosis(problem, scenario, mode)
            diagnoses[diagnosis.id] = diagnosis
            while len(diagnoses) > KEEP_DIAGNOSES:
                oldest = next(i for i, d in diagnoses.items() if d.status != "running")
                del diagnoses[oldest]
            threading.Thread(target=run, args=(diagnosis, toolbox, llm), daemon=True,
                             name=f"diagnosis-{diagnosis.id}").start()
        return {"id": diagnosis.id}

    def get(diagnosis_id: str) -> Diagnosis:
        diagnosis = diagnoses.get(diagnosis_id)
        if diagnosis is None:
            raise HTTPException(404, f"no diagnosis '{diagnosis_id}'")
        return diagnosis

    @app.get("/api/diagnoses/{diagnosis_id}")
    def state(diagnosis_id: str) -> dict:
        return get(diagnosis_id).state()

    @app.get("/api/diagnoses/{diagnosis_id}/events")
    def events(diagnosis_id: str, request: Request) -> StreamingResponse:
        diagnosis = get(diagnosis_id)
        # An EventSource that reconnects sends the last id it saw; resume after it.
        # A new connection (page load, late client) gets every event from the start.
        try:
            start_at = int(request.headers.get("last-event-id", "-1")) + 1
        except ValueError:
            start_at = 0

        async def stream():
            sent = max(start_at, 0)
            last_write = time.monotonic()
            yield "retry: 3000\n\n"
            while True:
                for name, data in diagnosis.snapshot(sent):
                    yield _sse(sent, name, data)
                    sent += 1
                    last_write = time.monotonic()
                    if name in TERMINAL_EVENTS:
                        return
                if diagnosis.status != "running" and not diagnosis.snapshot(sent):
                    return  # the terminal event was sent on an earlier connection
                if time.monotonic() - last_write >= HEARTBEAT_SECONDS:
                    yield ": heartbeat\n\n"
                    last_write = time.monotonic()
                await asyncio.sleep(POLL_SECONDS)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        })

    return app
