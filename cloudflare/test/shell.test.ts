import assert from "node:assert/strict";
import { test } from "node:test";

import { canonicalRedirect, containerEnv, escapeHtml, fillOrigin, isAppShell, missingSettings } from "../src/shell.ts";

test("fillOrigin escapes like html.escape(quote=True) and drops a trailing slash", () => {
  const html = '<link rel="canonical" href="{{origin}}/" /><meta content="{{origin}}/og-image.jpg">';
  assert.equal(fillOrigin(html, "https://camode.example.workers.dev/"),
    '<link rel="canonical" href="https://camode.example.workers.dev/" /><meta content="https://camode.example.workers.dev/og-image.jpg">');
  assert.equal(escapeHtml(`a&b<c>"d'`), "a&amp;b&lt;c&gt;&quot;d&#x27;");
  assert.equal(fillOrigin("{{origin}}", 'https://x"><script>'), "https://x&quot;&gt;&lt;script&gt;");
});

test("only GET/HEAD of / and /index.html are the app shell", () => {
  assert.equal(isAppShell("GET", "/"), true);
  assert.equal(isAppShell("HEAD", "/index.html"), true);
  assert.equal(isAppShell("POST", "/"), false);
  assert.equal(isAppShell("GET", "/api/session"), false);
  assert.equal(isAppShell("GET", "/privacy"), false);
});

test("the container gets only listed settings, and the deployment's fixed values win", () => {
  const env = containerEnv({
    APP_ORIGIN: "https://o", GROQ_API_KEY: "k", OPENAI_API_KEY: "", ASSETS: {}, SOMETHING_ELSE: "x",
    BACKUP_ENABLED: "true", APP_DATA_DIR: "/elsewhere",
  });
  assert.deepEqual(env, {
    APP_ORIGIN: "https://o", GROQ_API_KEY: "k", APP_DATA_DIR: "/data", PORT: "8080", BACKUP_ENABLED: "false", OLLAMA_MODEL: "",
  });
});

test("missing settings are listed before the container would start without storage or a model", () => {
  assert.deepEqual(missingSettings({ APP_ORIGIN: "https://o" }), [
    "APP_MASTER_KEY", "LITESTREAM_ENDPOINT", "LITESTREAM_BUCKET", "LITESTREAM_ACCESS_KEY_ID",
    "LITESTREAM_SECRET_ACCESS_KEY", "GROQ_API_KEY or OPENAI_API_KEY",
  ]);
  assert.deepEqual(missingSettings({
    APP_ORIGIN: "o", APP_MASTER_KEY: "m", LITESTREAM_ENDPOINT: "e", LITESTREAM_BUCKET: "b",
    LITESTREAM_ACCESS_KEY_ID: "a", LITESTREAM_SECRET_ACCESS_KEY: "s", OPENAI_API_KEY: "k",
  }), []);
});

test("requests on another host go to APP_ORIGIN, keeping path and query", () => {
  assert.equal(canonicalRedirect("GET", "http://127.0.0.1:8787/#/chat", "http://localhost:8787"), "http://localhost:8787/");
  assert.equal(canonicalRedirect("GET", "http://127.0.0.1:8787/auth/indmoney/connect?x=1", "http://localhost:8787"),
    "http://localhost:8787/auth/indmoney/connect?x=1");
  assert.equal(canonicalRedirect("GET", "https://camode.example.workers.dev/api/session", "https://camode.example.workers.dev/"), null);
  assert.equal(canonicalRedirect("POST", "http://127.0.0.1:8787/api/users", "http://localhost:8787"), null);
  assert.equal(canonicalRedirect("GET", "http://127.0.0.1:8787/", undefined), null);
  assert.equal(canonicalRedirect("GET", "http://127.0.0.1:8787/", "not a url"), null);
});
