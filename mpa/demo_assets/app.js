/* One serialized page client. The server remains authoritative after lost responses. */
"use strict";
function checkedState(value) {
  if (!value || !Number.isInteger(value.revision) || value.revision < 0 || !value.snapshot ||
      !Number.isFinite(value.snapshot.currentTime) || !Array.isArray(value.snapshot.operations) ||
      !Array.isArray(value.history) || !value.provenance || typeof value.snapshotHash !== "string") {
    throw new Error("The execution state could not be read. Refresh before acting.");
  }
  return value;
}
function evidenceBlob(text) {
  try { if (typeof text !== "string") throw new Error(); checkedState(JSON.parse(text)); }
  catch (_) { throw new Error("The evidence could not be read. Refresh before downloading."); }
  // Preserve Python's JSON number spellings: 8.0 and 8 have different snapshot hashes.
  return new Blob([text], { type: "application/json" });
}
class PlanningClient {
  constructor(fetcher, onState, onBusy) { this.fetch = fetcher; this.onState = onState; this.onBusy = onBusy; this.state = null; this.busy = false; this.uncertain = false; this.sessionId = null; this.token = null; }
  async request(path, body, token = this.token) {
    const headers = { "X-Demo-Token": token || "", "X-Demo-Session": this.sessionId || "" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await this.fetch("./api/" + path, { method: body === undefined ? "GET" : "POST", headers, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store" });
    let data; try { data = await response.json(); } catch (_) { throw new Error("The response was incomplete. Refresh state before acting."); }
    if (!response.ok) throw new Error(data.error?.message || "The action could not be confirmed. Refresh state.");
    return data;
  }
  async exportText() {
    const response = await this.fetch("./api/export", { method: "GET", headers: { "X-Demo-Token": this.token || "", "X-Demo-Session": this.sessionId || "" }, cache: "no-store" });
    let text, data;
    try { text = await response.text(); data = JSON.parse(text); }
    catch (_) { throw new Error("The evidence response was incomplete. Refresh before downloading."); }
    if (!response.ok) throw new Error(data.error?.message || "The evidence could not be downloaded. Refresh state.");
    if (!/^application\/json(?:\s*;|$)/i.test(response.headers.get("Content-Type") || "")) throw new Error("The evidence response was not JSON.");
    checkedState(data);
    return text;
  }
  publish(data) { this.state = checkedState(data); this.uncertain = false; this.onState(this.state); }
  async open(resume, bootstrap) {
    const data = await this.request("session", { resumeId: resume?.id || null, resumeToken: resume?.token || null }, bootstrap);
    if (typeof data.sessionId !== "string" || typeof data.token !== "string") throw new Error("The local exercise could not be opened.");
    this.sessionId = data.sessionId; this.token = data.token; this.publish(data);
    return { id: this.sessionId, token: this.token };
  }
  async refresh() {
    if (this.busy) return false;
    this.busy = true; this.onBusy(true);
    try { this.publish(await this.request("state")); return true; }
    catch (error) { this.uncertain = true; throw error; }
    finally { this.busy = false; this.onBusy(false); }
  }
  async act(action, values = {}) {
    if (this.busy || this.uncertain || !this.state) return false;
    this.busy = true; this.onBusy(true);
    try { this.publish(await this.request(action, { ...values, revision: this.state.revision })); return true; }
    catch (error) {
      this.uncertain = true;
      try { this.publish(await this.request("state")); } catch (_) { /* preserve unknown outcome; never optimistically advance */ }
      throw error;
    } finally { this.busy = false; this.onBusy(false); }
  }
}
if (typeof module !== "undefined") module.exports = { PlanningClient, checkedState, evidenceBlob };
if (typeof document !== "undefined") boot();

async function boot() {
  const $ = id => document.getElementById(id);
  const mode = document.documentElement.dataset.mode;
  const recorded = mode === "recorded";
  let state, client, recording, frame = 0;
  const n = value => Number(value).toLocaleString("en", { maximumFractionDigits: 4 });
  const escape = value => String(value).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  function failure(error) { $("error").textContent = error.message || "The action could not be completed."; $("error").hidden = false; }
  function clearError() { $("error").hidden = true; }
  function controls() {
    const disabled = recorded || !state || client?.busy || client?.uncertain;
    for (const id of ["compare", "reset", "machine", "eventStart", "eventEnd", "advanceUntil", "explicitConfirm"]) $(id).disabled = disabled;
    $("refreshState").hidden = recorded; $("refreshState").disabled = client?.busy || !state;
    $("download").disabled = !state || client?.busy;
    const pending = state?.comparison?.phase === "pending";
    $("confirmationPanel").hidden = recorded || !pending;
    const recommended = state?.comparison?.report?.recommendation === "candidate";
    $("candidateChoice").disabled = disabled || !recommended;
    document.querySelector('input[name="choice"][value="original"]').disabled = disabled;
    $("advance").disabled = disabled || !pending || !$("explicitConfirm").checked;
    $("compare").textContent = client?.busy ? "Working…" : "Compare remaining routes →";
  }
  function arm(target, data) {
    if (!data) { $(target).textContent = "No audited candidate. Keep original routes."; return; }
    const m = data.metrics;
    $(target).innerHTML = [["On time", m.onTimeUnits], ["Tardy", m.tardyUnits], ["Unfinished", m.unfinishedUnits]].map(([label, value]) => `<div><strong>${n(value)}</strong><span>${label}</span></div>`).join("") + `<p>Reserved OT ${n(m.reservedOvertimeHours)} h · used OT ${n(m.actualOvertimeHours)} h</p>`;
  }
  function render(next) {
    const oldTime = state?.snapshot.currentTime;
    state = checkedState(next);
    const now = state.snapshot.currentTime, ops = state.snapshot.operations;
    $("clock").textContent = "Hour " + n(now) + " / " + n(state.horizon);
    $("revision").textContent = recorded ? "Recorded state " + (frame + 1) + " / " + recording.frames.length : "Session revision " + state.revision;
    $("notice").textContent = state.notice;
    $("finished").textContent = n(state.metrics.onTimeUnits + state.metrics.tardyUnits);
    for (const [id, name] of [["completed", "completed"], ["wipCount", "wip"], ["unstarted", "unstarted"]]) $(id).textContent = ops.filter(o => o.state === name).length;
    const wip = ops.filter(o => o.state === "wip");
    $("wipRows").innerHTML = wip.map(o => `<tr><td>${escape(o.order)}/${o.unit} ${escape(o.stage)}</td><td>${escape(o.machine)}</td><td>${n(o.processedHours)} / ${n(o.remainingHours)} h</td></tr>`).join("") || '<tr><td colspan="3">No operation is in progress.</td></tr>';
    if (!$("machine").options.length) $("machine").innerHTML = state.machines.map(m => `<option>${escape(m)}</option>`).join("");
    if (oldTime !== now) {
      $("eventStart").value = now; $("eventStart").min = now; $("eventEnd").value = Math.min(now + (now === 2 ? 4 : 2), state.horizon);
      $("machine").value = now === 2 ? "M1" : "M2";
      $("advanceUntil").min = now; $("advanceUntil").value = Math.min(now + 1.5, state.horizon);
      $("explicitConfirm").checked = false;
    }
    const comparison = state.comparison;
    $("emptyComparison").hidden = !!comparison; $("comparison").hidden = !comparison;
    $("comparisonPhase").textContent = comparison ? ({ pending: "Awaiting your confirmation", confirmed: "Confirmed comparison", expired: "Proposal expired" }[comparison.phase] || "Recorded comparison") : "No proposal yet";
    if (comparison) {
      const r = comparison.report;
      arm("originalMetrics", r.baseline); arm("candidateMetrics", r.candidate);
      $("recommendation").textContent = comparison.phase === "confirmed" ? `Confirmed ${comparison.choice} routes; advanced to hour ${n(comparison.advancedTo)}. This chart records the decision's compared futures.` : comparison.phase === "expired" ? "This comparison expired without advancing execution. Compare the event again before confirming." : r.recommendation === "candidate" ? "Candidate is no worse under the fixed cost policy. Only unstarted work may move; confirmation is still required." : "Keep original routes. " + r.reason;
      const event = r.events[r.events.length - 1];
      $("recommendation").textContent = `Compared ${event.machine} outage ${n(event.start)}–${n(event.end)} h. ` + $("recommendation").textContent;
      $("gantt").innerHTML = comparison.svg;
      if (r.recommendation !== "candidate") document.querySelector('input[name="choice"][value="original"]').checked = true;
    }
    $("history").innerHTML = state.history.map(h => `<li><strong>${escape(h.event.machine)} outage ${n(h.event.start)}–${n(h.event.end)} h</strong> · ${escape(h.choice)} routes · advanced ${n(h.from)} → ${n(h.until)} h</li>`).join("") || "<li>No route has been confirmed.</li>";
    $("sourceCommit").textContent = state.provenance.sourceCommit;
    $("sourceStatus").textContent = (state.provenance.workingTreeClean ? "Clean source snapshot" : "Source includes local changes") + " · input " + state.provenance.inputHash.slice(0, 12) + "…";
    if (recorded) {
      $("frameTitle").textContent = recording.frames[frame].title;
      $("frameDetail").textContent = recording.frames[frame].description;
      $("previousFrame").disabled = frame === 0; $("nextFrame").disabled = frame === recording.frames.length - 1;
    }
    controls();
  }
  $("modeLabel").textContent = recorded ? "Recorded replay · no live solver" : "Local live solve · no API key";
  $("modeDescription").textContent = recorded ? "Inspect a recorded HiGHS / SimPy exercise. These controls step through saved computations; they cannot run Python or change the event." : "HiGHS computes the future. SimPy executes confirmed routes. This is a synthetic exercise with no production commands.";
  $("replayControls").hidden = !recorded;
  controls();
  function download(text) {
    const url = URL.createObjectURL(evidenceBlob(text));
    const a = document.createElement("a"); a.href = url; a.download = "planning-evidence.json"; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  $("download").addEventListener("click", async () => { clearError(); try { download(recorded ? recording.frames[frame].evidenceJSON : await client.exportText()); } catch (e) { failure(e); } });
  if (recorded) {
    try { const reply = await fetch("./recording.json", { cache: "no-store" }); if (!reply.ok) throw new Error("The recorded exercise could not be loaded."); recording = await reply.json(); if (!Array.isArray(recording.frames) || !recording.frames.length) throw new Error("The recorded exercise is incomplete."); render(recording.frames[0].state); }
    catch (e) { recording = null; $("previousFrame").disabled = $("nextFrame").disabled = true; failure(e); }
    for (const [id, delta] of [["previousFrame", -1], ["nextFrame", 1]]) $(id).addEventListener("click", () => {
      if (!recording || frame + delta < 0 || frame + delta >= recording.frames.length) return;
      const previous = frame;
      try { checkedState(recording.frames[frame + delta].state); frame += delta; render(recording.frames[frame].state); }
      catch (e) { frame = previous; failure(e); }
    });
    return;
  }
  client = new PlanningClient(window.fetch.bind(window), render, controls);
  try {
    let resume; try { resume = JSON.parse(sessionStorage.getItem("planning-exercise")); } catch (_) {}
    const receipt = await client.open(resume, document.querySelector('meta[name="demo-bootstrap"]').content);
    try { sessionStorage.setItem("planning-exercise", JSON.stringify(receipt)); } catch (_) {}
  } catch (e) { failure(e); controls(); }
  async function action(name, body) { clearError(); try { await client.act(name, body); } catch (e) { failure(e); } controls(); }
  $("eventForm").addEventListener("submit", e => { e.preventDefault(); $("explicitConfirm").checked = false; action("propose", { event: { machine: $("machine").value, start: Number($("eventStart").value), end: Number($("eventEnd").value) } }); });
  $("confirmForm").addEventListener("submit", e => { e.preventDefault(); action("advance", { proposalId: state.proposalId, choice: document.querySelector('input[name="choice"]:checked').value, confirmed: $("explicitConfirm").checked, until: Number($("advanceUntil").value) }); });
  $("explicitConfirm").addEventListener("change", controls);
  $("reset").addEventListener("click", () => action("reset", {}));
  $("refreshState").addEventListener("click", async () => { clearError(); try { await client.refresh(); } catch (e) { failure(e); } controls(); });
}
