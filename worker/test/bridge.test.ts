import assert from "node:assert/strict";
import { test } from "node:test";

import { CallBridge, MAX_CALLS_LIMIT, MAX_CODE_CHARS, MAX_TIMEOUT_MS, jsonSafe, parseRun, secretMatches } from "../src/bridge.ts";

test("parseRun accepts a run request and clamps its limits", () => {
  const run = parseRun({ type: "run", code: "return 1", api: ["a", "b", "a"], timeoutMs: 10 ** 9, maxCalls: 500 });
  assert.deepEqual(run, { code: "return 1", api: ["a", "b"], timeoutMs: MAX_TIMEOUT_MS, maxCalls: MAX_CALLS_LIMIT });
});

test("parseRun refuses anything that is not a well-formed run request", () => {
  assert.equal(typeof parseRun(null), "string");
  assert.equal(typeof parseRun({ type: "call" }), "string");
  assert.equal(typeof parseRun({ type: "run", code: "  ", api: [] }), "string");
  assert.equal(typeof parseRun({ type: "run", code: "x".repeat(MAX_CODE_CHARS + 1), api: [] }), "string");
  assert.equal(typeof parseRun({ type: "run", code: "return 1", api: "a" }), "string");
  assert.equal(typeof parseRun({ type: "run", code: "return 1", api: ["a.b"] }), "string");
  assert.equal(typeof parseRun({ type: "run", code: "return 1", api: ["__proto__; x"] }), "string");
});

test("a call is sent as a frame and resolves with the app's result", async () => {
  const sent: any[] = [];
  const bridge = new CallBridge((message) => sent.push(message), 5);
  const pending = bridge.call("networth_snapshot", { a: 1 });
  assert.deepEqual(sent, [{ type: "call", id: 1, name: "networth_snapshot", args: { a: 1 } }]);
  bridge.receive({ type: "result", id: 1, ok: true, value: { total: 42 } });
  assert.deepEqual(await pending, { total: 42 });
});

test("a failed call rejects with the app's message, and undefined args become an empty object", async () => {
  const sent: any[] = [];
  const bridge = new CallBridge((message) => sent.push(message), 5);
  const pending = bridge.call("x", undefined);
  assert.deepEqual(sent[0].args, {});
  bridge.receive({ type: "result", id: 1, ok: false, error: "rejected: bad asset_type" });
  await assert.rejects(pending, /bad asset_type/);
});

test("results for unknown ids and malformed frames are ignored", async () => {
  const bridge = new CallBridge(() => {}, 5);
  const pending = bridge.call("x", {});
  bridge.receive({ type: "result", id: 99, ok: true, value: 1 });
  bridge.receive("nonsense");
  bridge.receive({ type: "result", id: "1", ok: true, value: 1 });
  bridge.receive({ type: "result", id: 1, ok: true, value: "right" });
  assert.equal(await pending, "right");
});

test("calls over the limit are refused without a frame", async () => {
  const sent: any[] = [];
  const bridge = new CallBridge((message) => sent.push(message), 1);
  bridge.call("x", {});
  await assert.rejects(bridge.call("y", {}), /More than 1 api calls/);
  assert.equal(sent.length, 1);
});

test("abort rejects pending and later calls", async () => {
  const bridge = new CallBridge(() => {}, 5);
  const pending = bridge.call("x", {});
  bridge.receive({ type: "abort", reason: "stopped" });
  await assert.rejects(pending, /Stopped: stopped/);
  await assert.rejects(bridge.call("y", {}), /Stopped/);
});

test("a send failure rejects the call", async () => {
  const bridge = new CallBridge(() => { throw new Error("closed"); }, 5);
  await assert.rejects(bridge.call("x", {}), /connection to CA Mode was lost/);
});

test("secretMatches needs the exact bearer secret", async () => {
  assert.equal(await secretMatches("Bearer s3cret", "s3cret"), true);
  assert.equal(await secretMatches("Bearer s3cret2", "s3cret"), false);
  assert.equal(await secretMatches("s3cret", "s3cret"), false);
  assert.equal(await secretMatches(null, "s3cret"), false);
  assert.equal(await secretMatches("Bearer ", ""), false);
});

test("jsonSafe turns sandbox results into JSON data", () => {
  assert.equal(jsonSafe(undefined), null);
  assert.deepEqual(jsonSafe({ a: 1, b: undefined, c: [1, 2] }), { a: 1, c: [1, 2] });
  const cyclic: any = {};
  cyclic.self = cyclic;
  assert.equal(jsonSafe(cyclic), "[object Object]");
});
