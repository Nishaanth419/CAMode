/**
 * CA Mode code runner: runs one model-written snippet per WebSocket connection in a fresh Dynamic Worker.
 *
 * The snippet gets an `api` object whose functions are the tools CA Mode offered for that answer. It has
 * no network access (globalOutbound: null); each `api.<name>(args)` call is sent back to CA Mode over the
 * WebSocket, which runs the tool (INDmoney or local data) and replies with the result. This Worker never
 * holds an INDmoney token. Connections need `Authorization: Bearer <CODE_RUNNER_SECRET>`.
 */

import { DynamicWorkerExecutor } from "@cloudflare/codemode";
import { CallBridge, jsonSafe, parseRun, secretMatches } from "./bridge.ts";

interface Env {
  LOADER: WorkerLoader;
  CODE_RUNNER_SECRET?: string;
}

const MAX_LOG_LINES = 50;
// How long to wait for the run request after the connection opens.
const FIRST_MESSAGE_MS = 10_000;

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/healthz") return new Response("ok");
    if (!(await secretMatches(request.headers.get("Authorization"), env.CODE_RUNNER_SECRET ?? ""))) {
      return new Response("Unauthorized", { status: 401 });
    }
    if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") {
      return new Response("Expected a WebSocket upgrade", { status: 426 });
    }
    const [client, server] = Object.values(new WebSocketPair());
    server.accept();
    ctx.waitUntil(serve(server, env));
    return new Response(null, { status: 101, webSocket: client });
  },
} satisfies ExportedHandler<Env>;

function send(socket: WebSocket, message: object): void {
  socket.send(JSON.stringify(message));
}

async function serve(socket: WebSocket, env: Env): Promise<void> {
  let bridge: CallBridge | null = null;
  let resolveRun: (message: unknown) => void = () => {};
  const firstMessage = new Promise<unknown>((resolve) => {
    resolveRun = resolve;
    setTimeout(() => resolve(null), FIRST_MESSAGE_MS);
  });

  socket.addEventListener("message", (event) => {
    let message: unknown;
    try {
      message = JSON.parse(typeof event.data === "string" ? event.data : "");
    } catch {
      return;
    }
    if (bridge) bridge.receive(message);
    else resolveRun(message);
  });
  socket.addEventListener("close", () => bridge?.close("The connection to CA Mode closed."));
  socket.addEventListener("error", () => bridge?.close("The connection to CA Mode failed."));

  const run = parseRun(await firstMessage);
  if (typeof run === "string") {
    send(socket, { type: "error", error: run });
    socket.close(1008, "refused");
    return;
  }
  bridge = new CallBridge((message) => send(socket, message), run.maxCalls);
  const calls = bridge;
  const fns = Object.fromEntries(run.api.map((name) => [name, (args: unknown) => calls.call(name, args)]));
  const executor = new DynamicWorkerExecutor({ loader: env.LOADER, timeout: run.timeoutMs, globalOutbound: null });
  let outcome: { result?: unknown; error?: string; logs?: string[] };
  try {
    outcome = await executor.execute(run.code, [{ name: "api", fns }]);
  } catch (error) {
    // Code that does not compile fails while the isolate starts, which the executor throws rather than returns.
    const message = error instanceof Error ? error.message : String(error);
    outcome = { error: message.replace(/^Failed to start Worker:\s*/, "") };
  }
  calls.close("The snippet has finished.");
  try {
    send(socket, {
      type: "done",
      result: outcome.error ? null : jsonSafe(outcome.result),
      error: outcome.error ?? null,
      logs: (outcome.logs ?? []).slice(0, MAX_LOG_LINES),
    });
    socket.close(1000, "done");
  } catch {
    // CA Mode already closed the connection (it aborted the answer); nothing is left to report.
  }
}
