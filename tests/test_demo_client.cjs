"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const crypto = require("node:crypto");
const { PlanningClient, checkedState, evidenceBlob } = require("../mpa/demo_assets/app.js");
const state = (revision, time = 2) => ({ revision, snapshot: { currentTime: time, operations: [] }, snapshotHash: "hash" + time, history: [], provenance: {} });
const reply = data => ({ ok: true, json: async () => data });
function client(fetcher) { const published = []; const c = new PlanningClient(fetcher, s => published.push(s), () => {}); c.state = state(0); c.sessionId = "session"; c.token = "token"; return { c, published }; }
const rawSnapshot = '{"currentTime":8.0,"operations":[{"processedHours":0.0}]}';
const snapshotHash = crypto.createHash("sha256").update(rawSnapshot).digest("hex");
const exactEvidence = '{"revision":4,"snapshot":' + rawSnapshot + ',"snapshotHash":"' + snapshotHash + '","history":[],"provenance":{}}\n';

test("local export preserves raw Python 8.0/0.0 JSON through the actual Blob", async () => {
  let request;
  const { c, published } = client(async (path, init) => { request = { path, init }; return new Response(exactEvidence, { headers: { "Content-Type": "application/json" } }); });
  const text = await c.exportText();
  assert.equal(await evidenceBlob(text).text(), exactEvidence);
  assert.equal(request.path, "./api/export"); assert.equal(request.init.method, "GET");
  assert.equal(request.init.headers["X-Demo-Session"], "session"); assert.equal(request.init.headers["X-Demo-Token"], "token");
  assert.equal(published.length, 0); assert.equal(c.state.revision, 0);
  // This is the failure control: parse/stringify cannot retain the hash input.
  const rewritten = JSON.stringify(JSON.parse(rawSnapshot));
  assert.notEqual(crypto.createHash("sha256").update(rewritten).digest("hex"), snapshotHash);
  assert.match(text, /"currentTime":8\.0/); assert.match(text, /"processedHours":0\.0/);
});

test("failed, malformed or non-JSON export cannot become downloadable evidence", async () => {
  for (const response of [new Response('{"error":{"message":"Not available"}}', { status: 409, headers: { "Content-Type": "application/json" } }),
                          new Response("{", { headers: { "Content-Type": "application/json" } }),
                          new Response("{}", { headers: { "Content-Type": "application/json" } }),
                          new Response(exactEvidence, { headers: { "Content-Type": "text/html" } })]) {
    const { c, published } = client(async () => response);
    await assert.rejects(c.exportText()); assert.equal(published.length, 0); assert.equal(c.state.revision, 0);
  }
  assert.throws(() => evidenceBlob({}), /evidence could not be read/);
});

test("recorded page's download button uses the embedded exact evidence text", async () => {
  const nodes = new Map(); let captured, clicks = 0;
  function node(id) { if (!nodes.has(id)) nodes.set(id, { textContent: "", hidden: false, disabled: true, options: [], events: {}, addEventListener(type, fn) { this.events[type] = fn; } }); return nodes.get(id); }
  const recordedState = { ...state(4, 8), horizon: 10, machines: ["M1"], metrics: { onTimeUnits: 4, tardyUnits: 0 }, notice: "Recorded", provenance: { sourceCommit: "source", workingTreeClean: true, inputHash: "input" } };
  const document = { documentElement: { dataset: { mode: "recorded" } }, getElementById: node, querySelector: () => node("originalChoice"), createElement: () => ({ click() { clicks++; } }) };
  const context = vm.createContext({ document, Blob, URL: { createObjectURL(blob) { captured = blob; return "blob:fixture"; }, revokeObjectURL() {} }, setTimeout() {},
    fetch: async () => ({ ok: true, json: async () => ({ frames: [{ title: "Final", description: "Recorded execution", state: recordedState, evidence: JSON.parse(exactEvidence), evidenceJSON: exactEvidence }] }) }) });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../mpa/demo_assets/app.js"), "utf8"), context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(Boolean(node("download").disabled), false);
  await node("download").events.click();
  const downloaded = await captured.text();
  assert.equal(clicks, 1);
  assert.match(downloaded, /"currentTime"\s*:\s*8\.0\b/);
  assert.match(downloaded, /"processedHours"\s*:\s*0\.0\b/);
  assert.equal(downloaded, exactEvidence);
  assert.equal(node("error").hidden, true);
});

test("single-flight admission uses one current revision", async () => {
  let resolve, calls = 0, body;
  const { c, published } = client(async (_, init) => { calls++; body = JSON.parse(init.body); return new Promise(r => resolve = r); });
  const first = c.act("propose", { event: {} });
  assert.equal(await c.act("reset"), false);
  assert.equal(calls, 1); assert.equal(body.revision, 0);
  resolve(reply(state(1))); await first;
  assert.equal(published.length, 1); assert.equal(c.state.revision, 1);
});

test("a lost advance response reconciles actual committed state without a second advance", async () => {
  const calls = [];
  const { c, published } = client(async (path, init) => { calls.push({ path, init }); if (init.method === "POST") throw new Error("connection lost"); return reply(state(2, 3.5)); });
  await assert.rejects(c.act("advance", { confirmed: true }), /connection lost/);
  assert.equal(calls.filter(r => r.init.method === "POST").length, 1);
  assert.equal(c.state.snapshot.currentTime, 3.5); assert.equal(c.uncertain, false);
  assert.equal(published.length, 1);
});

test("unconfirmed network outcomes keep the old state and block further mutations", async () => {
  let calls = 0;
  const { c, published } = client(async () => { calls++; throw new Error("offline"); });
  await assert.rejects(c.act("advance", { confirmed: true }), /offline/);
  assert.equal(c.uncertain, true); assert.equal(c.state.snapshot.currentTime, 2); assert.equal(published.length, 0);
  assert.equal(await c.act("advance", { confirmed: true }), false); assert.equal(calls, 2);
  c.fetch = async () => reply(state(2, 3.5));
  await c.refresh(); assert.equal(c.uncertain, false); assert.equal(c.state.revision, 2);
});

test("invalid successful JSON cannot authorize a new execution state", async () => {
  const { c } = client(async () => reply({}));
  await assert.rejects(c.act("reset"), /execution state/);
  assert.equal(c.state.revision, 0); assert.equal(c.uncertain, true);
  assert.throws(() => checkedState({ ...state(0), revision: false }), /execution state/);
});

test("refresh cannot race an active decision or publish an earlier read", async () => {
  let resolve, calls = 0;
  const { c, published } = client(async () => { calls++; return new Promise(r => resolve = r); });
  const action = c.act("advance", { confirmed: true });
  assert.equal(await c.refresh(), false); assert.equal(calls, 1);
  resolve(reply(state(2, 3.5))); await action;
  assert.equal(published.length, 1); assert.equal(c.state.snapshot.currentTime, 3.5);
});

test("page initialization resumes into fresh per-page credentials", async () => {
  let request;
  const { c } = client(async (_, init) => { request = init; return reply({ ...state(7, 5), sessionId: "new", token: "new-token" }); });
  const receipt = await c.open({ id: "old", token: "old-token" }, "bootstrap");
  assert.deepEqual(JSON.parse(request.body), { resumeId: "old", resumeToken: "old-token" });
  assert.equal(request.headers["X-Demo-Token"], "bootstrap");
  assert.deepEqual(receipt, { id: "new", token: "new-token" }); assert.equal(c.state.snapshot.currentTime, 5);
});

test("recorded 404 leaves steps disabled and cannot advance an undefined recording", async () => {
  const nodes = new Map();
  function node(id) { if (!nodes.has(id)) nodes.set(id, { textContent: "", hidden: false, disabled: true, events: {}, addEventListener(type, fn) { this.events[type] = fn; } }); return nodes.get(id); }
  const document = { documentElement: { dataset: { mode: "recorded" } }, getElementById: node, querySelector: () => node("originalChoice") };
  const context = vm.createContext({ document, fetch: async () => ({ ok: false }) });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../mpa/demo_assets/app.js"), "utf8"), context);
  await new Promise(resolve => setImmediate(resolve));
  assert.match(node("error").textContent, /could not be loaded/);
  assert.equal(node("error").hidden, false);
  for (const id of ["previousFrame", "nextFrame"]) { assert.equal(node(id).disabled, true); assert.doesNotThrow(() => node(id).events.click()); }
  assert.equal(node("frameTitle").textContent, "");
});
