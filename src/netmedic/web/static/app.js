// NetMedic web UI. Device output and model text are untrusted: every piece of dynamic
// text goes into the page through textContent or createTextNode, never as markup.
"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
const ARG_LIMIT = 160;

const state = {
  meta: null,
  runId: null,
  source: null,     // EventSource of the active run
  finished: false,  // terminal event received
  counts: {},       // device -> number of queries
  nodes: {},        // device -> SVG group
};

const $ = (id) => document.getElementById(id);

// Build an element; `text` is always set with textContent.
function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function svg(tag, attrs) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, String(value));
  return node;
}

function setStatus(text) {
  $("status").textContent = text;
}

// --- metadata and controls -------------------------------------------------------------

async function loadMeta() {
  const response = await fetch("/api/meta");
  if (!response.ok) throw new Error(await errorText(response));
  const meta = await response.json();
  state.meta = meta;

  const mode = $("mode");
  mode.textContent = meta.mode;
  mode.classList.add(meta.mode === "live" ? "badge-live" : "badge-replay");
  $("model").textContent = meta.llm || "not configured";

  if (meta.mode === "replay" && meta.scenarios.length) {
    const select = $("scenario");
    for (const scenario of meta.scenarios) {
      const option = el("option", null, scenario.id);
      option.value = scenario.id;
      select.appendChild(option);
    }
    const known = meta.scenarios.some((s) => s.id === meta.default_scenario);
    select.value = known ? meta.default_scenario : meta.scenarios[0].id;
    fillProblem();
    select.addEventListener("change", fillProblem);
    $("scenario-row").hidden = false;
  }

  drawTopology(meta.devices, meta.links);
  if (!meta.llm) {
    setStatus("No model configured: " + (meta.llm_error || "unknown error") +
      ". Set NETMEDIC_MODEL and ANTHROPIC_API_KEY and restart.");
  }
  updateRunButton();
}

function fillProblem() {
  const id = $("scenario").value;
  const scenario = state.meta.scenarios.find((s) => s.id === id);
  if (scenario) $("problem").value = scenario.problem;
}

function running() {
  return state.runId !== null && !state.finished;
}

function updateRunButton() {
  $("run").disabled = running() || !state.meta || !state.meta.llm;
}

async function errorText(response) {
  try {
    const body = await response.json();
    if (typeof body.detail === "string") return body.detail;
  } catch (_) { /* not JSON */ }
  return "HTTP " + response.status;
}

async function startRun(event) {
  event.preventDefault();
  const problem = $("problem").value.trim();
  if (!problem) {
    setStatus("Describe the problem first.");
    $("problem").focus();
    return;
  }
  const body = { problem };
  if (state.meta.mode === "replay" && !$("scenario-row").hidden) body.scenario = $("scenario").value;
  $("run").disabled = true;
  setStatus("Starting...");
  let response;
  try {
    response = await fetch("/api/diagnoses", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (err) {
    setStatus("Could not reach the NetMedic server.");
    updateRunButton();
    return;
  }
  if (!response.ok) {
    setStatus("Not started: " + (await errorText(response)));
    updateRunButton();
    return;
  }
  const { id } = await response.json();
  history.replaceState(null, "", "#run=" + encodeURIComponent(id));
  follow(id, problem, body.scenario || null);
}

// --- running a diagnosis -----------------------------------------------------------------

function resetView(problem, scenario) {
  if (state.source) state.source.close();
  state.source = null;
  state.finished = false;
  state.counts = {};
  $("timeline").replaceChildren();
  $("report").hidden = true;
  $("report-body").replaceChildren();
  $("failure").hidden = true;
  $("empty").hidden = true;
  for (const group of Object.values(state.nodes)) {
    group.classList.remove("queried", "fault", "pulse");
    group.querySelector(".count").textContent = "";
  }
  const banner = $("problem-banner");
  banner.replaceChildren(el("span", "banner-label", "Problem"), el("q", null, problem));
  if (scenario) banner.appendChild(el("span", "banner-scenario", "scenario " + scenario));
  banner.hidden = false;
}

function follow(id, problem, scenario) {
  state.runId = id;
  state.problem = problem;
  state.scenario = scenario;
  resetView(problem, scenario);
  updateRunButton();
  setStatus("Running. Waiting for the first tool call...");
  connect();
}

function connect() {
  const source = new EventSource("/api/diagnoses/" + encodeURIComponent(state.runId) + "/events");
  state.source = source;
  source.addEventListener("step", (e) => addStep(JSON.parse(e.data)));
  source.addEventListener("report", (e) => { finish(); showReport(JSON.parse(e.data)); });
  source.addEventListener("error", (e) => {
    if (e.data === undefined) return connectionLost(source);  // transport error, not ours
    finish();
    showFailure("The diagnosis stopped with an error: " + JSON.parse(e.data).message, false);
    setStatus("Stopped with an error.");
  });
  source.addEventListener("open", () => { $("failure").hidden = true; });
}

function connectionLost(source) {
  if (state.finished) return;
  const closed = source.readyState === EventSource.CLOSED;
  showFailure(closed
    ? "The connection to the server was lost. The diagnosis may still be running."
    : "The connection to the server dropped. Retrying...", true);
  setStatus("Connection lost.");
}

function finish() {
  state.finished = true;
  if (state.source) state.source.close();
  updateRunButton();
}

function showFailure(message, offerReload) {
  $("failure-text").textContent = message;
  $("reload").hidden = !offerReload;
  $("failure").hidden = false;
}

// Rebuild the view from GET /api/diagnoses/{id} (page reload or dropped stream).
async function reload(id) {
  let response;
  try {
    response = await fetch("/api/diagnoses/" + encodeURIComponent(id));
  } catch (_) {
    showFailure("The NetMedic server is not reachable. Check that `netmedic web` is running.", true);
    return;
  }
  if (!response.ok) {
    setStatus("Diagnosis " + id + " is not on the server: it keeps the last 20 in memory, " +
      "and forgets them when it restarts.");
    history.replaceState(null, "", location.pathname);
    $("failure").hidden = true;
    state.finished = true;
    updateRunButton();
    return;
  }
  const data = await response.json();
  state.runId = data.id;
  state.problem = data.problem;
  state.scenario = data.scenario;
  if (data.status === "running") {  // the stream replays every earlier step
    follow(data.id, data.problem, data.scenario);
    return;
  }
  resetView(data.problem, data.scenario);
  data.steps.forEach(addStep);
  state.finished = true;
  if (data.report) showReport(data.report);
  else {
    showFailure("The diagnosis stopped with an error: " + data.error, false);
    setStatus("Stopped with an error.");
  }
  updateRunButton();
}

// --- timeline ------------------------------------------------------------------------------

function nearBottom() {
  const doc = document.documentElement;
  return window.innerHeight + window.scrollY >= doc.scrollHeight - 160;
}

function compactArgs(tool, args) {
  if (!args || typeof args !== "object") return "";
  if (tool === "submit_report") return "fault_type=" + args.fault_type + " device=" + args.device;
  const parts = [];
  for (const [key, value] of Object.entries(args)) {
    if (key === "device") continue;
    parts.push(key + "=" + (typeof value === "string" ? value : JSON.stringify(value)));
  }
  const text = parts.join("  ");
  return text.length > ARG_LIMIT ? text.slice(0, ARG_LIMIT) + "..." : text;
}

function addStep(step) {
  const stick = nearBottom();
  const rejected = step.tool === "submit_report" && step.is_error;
  const item = el("li", "card" + (step.is_error ? " is-error" : ""));

  const head = el("div", "card-head");
  head.appendChild(el("span", "stepno", "Step " + step.step));
  head.appendChild(el("span", "tool", step.tool));
  if (step.device) head.appendChild(el("span", "chip", step.device));
  if (rejected) head.appendChild(el("span", "flag", "report rejected"));
  else if (step.is_error) head.appendChild(el("span", "flag", "error"));
  item.appendChild(head);

  const args = compactArgs(step.tool, step.arguments);
  if (args) item.appendChild(el("code", "args", args));

  const result = typeof step.result === "string" ? step.result : "";
  const lines = result.split("\n");
  const first = lines.find((line) => line.trim()) || "(no output)";
  const details = el("details", "output");
  const summary = el("summary");
  summary.appendChild(el("span", "first-line", first));
  const extra = lines.length - 1;
  if (extra > 0) summary.appendChild(el("span", "more", extra + " more line" + (extra === 1 ? "" : "s")));
  details.appendChild(summary);
  details.appendChild(el("pre", null, result));
  item.appendChild(details);

  $("timeline").appendChild(item);
  if (step.device) markQueried(step.device);
  if (!state.finished) {
    setStatus("Running. Step " + step.step + ": " + step.tool + (step.device ? " on " + step.device : "") + ".");
  }
  if (stick) item.scrollIntoView({ block: "end", behavior: reducedMotion() ? "auto" : "smooth" });
}

function reducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

// --- report --------------------------------------------------------------------------------

function cliCommand() {
  const quoted = '"' + String(state.problem || "").replace(/(["\\$`])/g, "\\$1") + '"';
  const where = state.meta && state.meta.mode === "live"
    ? "--live" : "--scenario " + (state.scenario || state.meta.default_scenario);
  return "netmedic diagnose " + quoted + " " + where;
}

function codeBlock(text, label) {
  const wrap = el("div", "codeblock");
  const pre = el("pre", null, text);
  const button = el("button", "copy", "Copy");
  button.type = "button";
  button.setAttribute("aria-label", "Copy " + label);
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "Copied";
    } catch (_) {
      button.textContent = "Copy failed";
    }
    setTimeout(() => { button.textContent = "Copy"; }, 2000);
  });
  wrap.append(pre, button);
  return wrap;
}

function fact(list, term, value, className) {
  const row = el("div", className);
  row.append(el("dt", null, term), el("dd", null, value));
  list.appendChild(row);
}

function showReport(report) {
  const body = $("report-body");
  body.replaceChildren();
  const noFault = report.fault_type === "none";
  const unknown = report.fault_type === "unknown";
  const section = $("report");
  section.classList.toggle("no-fault", noFault);
  section.classList.toggle("has-fault", !noFault && !unknown);

  body.appendChild(el("p", "verdict",
    noFault ? "No fault found" : unknown ? "No diagnosis reached" : "Fault found on " + (report.device || "an unnamed device")));
  body.appendChild(el("p", "root-cause", report.root_cause));

  const facts = el("dl", "facts");
  fact(facts, "Device", report.device || "none");
  fact(facts, "Fault type", report.fault_type);
  fact(facts, "Confidence", report.confidence, "confidence-" + report.confidence);
  body.appendChild(facts);

  body.appendChild(el("h4", null, "Evidence"));
  if (report.evidence.length) {
    const list = el("ul", "evidence");
    for (const line of report.evidence) list.appendChild(el("li", null, line));
    body.appendChild(list);
  } else {
    body.appendChild(el("p", "muted", "The report quotes no evidence."));
  }

  if (report.proposed_fix.length) {
    body.appendChild(el("h4", null, "Proposed fix"));
    body.appendChild(codeBlock(report.proposed_fix.join("\n"), "proposed fix"));
    body.appendChild(el("p", "muted",
      "This page never changes devices. To apply the fix with your approval, run:"));
    body.appendChild(codeBlock(cliCommand(), "command"));
  } else if (!noFault) {
    body.appendChild(el("p", "muted", "The report proposes no fix."));
  }

  body.appendChild(el("p", "totals",
    report.steps_taken + " steps, " + report.input_tokens + " input tokens (" +
    report.cached_input_tokens + " cached), " + report.output_tokens + " output tokens"));

  section.hidden = false;
  if (report.device && !noFault) markFault(report.device);
  setStatus(noFault ? "Finished: no fault found." : "Finished: " + report.fault_type +
    (report.device ? " on " + report.device : "") + ".");
  section.scrollIntoView({ block: "start", behavior: reducedMotion() ? "auto" : "smooth" });
}

// --- topology ------------------------------------------------------------------------------

// Layered left-to-right layout from the links: each connected group starts at its first
// edge host (else at one end of its longest path); columns are hop counts from there.
// Devices without links sit in a row.
function layout(devices, links) {
  const roles = new Map(devices.map((d) => [d.name, d.role]));
  const names = devices.map((d) => d.name);
  const adjacent = new Map(names.map((n) => [n, []]));
  for (const [a, b] of links) {
    if (adjacent.has(a) && adjacent.has(b) && a !== b) {
      adjacent.get(a).push(b);
      adjacent.get(b).push(a);
    }
  }
  const bfs = (start) => {
    const depth = new Map([[start, 0]]);
    const order = [start];
    for (let i = 0; i < order.length; i++) {
      for (const next of adjacent.get(order[i])) {
        if (!depth.has(next)) { depth.set(next, depth.get(order[i]) + 1); order.push(next); }
      }
    }
    return { depth, order };
  };

  const position = new Map();
  const placed = new Set();
  let top = 0;
  const loners = [];
  for (const name of names) {
    if (placed.has(name)) continue;
    if (adjacent.get(name).length === 0) { loners.push(name); placed.add(name); continue; }
    const probe = bfs(name);
    const members = new Set(probe.order);
    const edgeHost = names.find((n) => members.has(n) && roles.get(n) !== "router" &&
      adjacent.get(n).length === 1);
    const start = edgeHost || probe.order[probe.order.length - 1];  // farthest: a path end
    const { depth, order } = bfs(start);
    const rows = new Map();
    for (const node of order) {
      const column = depth.get(node);
      const row = rows.get(column) || 0;
      rows.set(column, row + 1);
      position.set(node, { column, row: top + row });
      placed.add(node);
    }
    top += Math.max(...rows.values());
  }
  loners.forEach((name, i) => position.set(name, { column: i, row: top }));
  return position;
}

function drawTopology(devices, links) {
  const box = $("topology");
  box.replaceChildren();
  state.nodes = {};
  if (!devices.length) {
    box.appendChild(el("p", "muted", "The inventory has no devices."));
    return;
  }
  const position = layout(devices, links);
  const dx = 100, dy = 100, pad = 46;
  const columns = Math.max(...[...position.values()].map((p) => p.column)) + 1;
  const rows = Math.max(...[...position.values()].map((p) => p.row)) + 1;
  const width = pad * 2 + (columns - 1) * dx;
  const height = pad * 2 + (rows - 1) * dy + 24;
  const at = (name) => {
    const p = position.get(name);
    return { x: pad + p.column * dx, y: pad + p.row * dy };
  };

  const root = svg("svg", {
    viewBox: "0 0 " + width + " " + height, role: "img", class: "topology-svg",
  });
  root.appendChild(svg("title")).textContent =
    "Network topology: " + devices.length + " devices, " + links.length + " links";

  const wires = svg("g", { class: "links" });
  for (const [a, b] of links) {
    if (!position.has(a) || !position.has(b)) continue;
    const p = at(a), q = at(b);
    wires.appendChild(svg("line", { x1: p.x, y1: p.y, x2: q.x, y2: q.y }));
  }
  root.appendChild(wires);

  for (const device of devices) {
    const { x, y } = at(device.name);
    const isRouter = device.role === "router";
    const group = svg("g", {
      class: "node " + (isRouter ? "router" : "host"), transform: "translate(" + x + " " + y + ")",
    });
    group.appendChild(svg("circle", { class: "ring", r: 26 }));
    if (isRouter) {
      group.appendChild(svg("circle", { class: "shape", r: 18 }));
      group.appendChild(svg("path", { class: "glyph", d: "M-9 -4 H9 M5 -8 L9 -4 L5 0 M9 4 H-9 M-5 0 L-9 4 L-5 8" }));
    } else {
      group.appendChild(svg("rect", { class: "shape", x: -18, y: -13, width: 36, height: 26, rx: 3 }));
      group.appendChild(svg("path", { class: "glyph", d: "M-10 -5 H10 M-10 1 H4" }));
    }
    const badge = svg("g", { class: "badge-count", transform: "translate(17 -17)" });
    badge.appendChild(svg("circle", { r: 9 }));
    badge.appendChild(svg("text", { class: "count", "text-anchor": "middle", dy: "0.35em" }));
    group.appendChild(badge);
    const name = svg("text", { class: "name", y: 38, "text-anchor": "middle" });
    name.textContent = device.name;
    const platform = svg("text", { class: "platform", y: 53, "text-anchor": "middle" });
    platform.textContent = device.platform;
    group.append(name, platform);
    const tip = svg("title");
    tip.textContent = device.name + " (" + device.role + ", " + device.platform + ")";
    group.appendChild(tip);
    root.appendChild(group);
    state.nodes[device.name] = group;
  }
  box.appendChild(root);
}

function markQueried(name) {
  const group = state.nodes[name];
  if (!group) return;
  state.counts[name] = (state.counts[name] || 0) + 1;
  group.querySelector(".count").textContent = String(state.counts[name]);
  group.classList.add("queried");
  group.classList.remove("pulse");
  void group.getBoundingClientRect();  // restart the animation
  group.classList.add("pulse");
}

function markFault(name) {
  const group = state.nodes[name];
  if (group) group.classList.add("fault");
}

// --- start ------------------------------------------------------------------------------------

document.addEventListener("DOMContentLoaded", async () => {
  $("run-form").addEventListener("submit", startRun);
  $("reload").addEventListener("click", () => {
    if (state.source) state.source.close();
    if (state.runId) reload(state.runId);
  });
  try {
    await loadMeta();
  } catch (err) {
    setStatus("Could not load the inventory: " + err.message);
    return;
  }
  const match = /^#run=([A-Za-z0-9]+)$/.exec(location.hash);
  if (match) reload(match[1]);
});
