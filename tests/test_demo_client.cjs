"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { PlanningClient, checkedState } = require("../mpa/demo_assets/app.js");
const state = (revision, time = 2) => ({ revision, snapshot: { currentTime: time, operations: [] }, snapshotHash: "hash" + time, history: [], provenance: {} });
const reply = data => ({ ok: true, json: async () => data });
function client(fetcher) { const published = []; const c = new PlanningClient(fetcher, s => published.push(s), () => {}); c.state = state(0); c.sessionId = "session"; c.token = "token"; return { c, published }; }

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
