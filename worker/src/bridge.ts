/**
 * The WebSocket protocol between CA Mode (app/code_runner.py) and this Worker, without any Workers APIs,
 * so it runs under `node --test` as well.
 *
 *   app → worker  {type: "run", code, api: [names], timeoutMs, maxCalls}
 *   worker → app  {type: "call", id, name, args}
 *   app → worker  {type: "result", id, ok, value | error}
 *   worker → app  {type: "done", result, error, logs}   (or {type: "error", error} for a refused run)
 *   app → worker  {type: "abort", reason}               every pending and later call fails
 */

export const MAX_CODE_CHARS = 20_000;
export const MAX_API_NAMES = 64;
export const MAX_CALLS_LIMIT = 20;
export const MAX_TIMEOUT_MS = 120_000;
export const DEFAULT_TIMEOUT_MS = 60_000;

export interface RunRequest {
  code: string;
  api: string[];
  timeoutMs: number;
  maxCalls: number;
}

const IDENTIFIER = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;

/** A validated run request, or the reason it is refused. */
export function parseRun(message: unknown): RunRequest | string {
  if (!message || typeof message !== "object") return "The first message must be a run request.";
  const data = message as Record<string, unknown>;
  if (data.type !== "run") return "The first message must be a run request.";
  if (typeof data.code !== "string" || !data.code.trim()) return "The run request has no code.";
  if (data.code.length > MAX_CODE_CHARS) return `The code is longer than ${MAX_CODE_CHARS} characters.`;
  if (!Array.isArray(data.api) || data.api.length > MAX_API_NAMES) return "The run request must list the api functions.";
  const api = data.api.filter((name): name is string => typeof name === "string" && IDENTIFIER.test(name));
  if (api.length !== data.api.length) return "An api function name is not a plain identifier.";
  const timeout = Number(data.timeoutMs);
  const maxCalls = Number(data.maxCalls);
  return {
    code: data.code,
    api: [...new Set(api)],
    timeoutMs: Number.isFinite(timeout) && timeout > 0 ? Math.min(timeout, MAX_TIMEOUT_MS) : DEFAULT_TIMEOUT_MS,
    maxCalls: Number.isFinite(maxCalls) && maxCalls > 0 ? Math.min(Math.floor(maxCalls), MAX_CALLS_LIMIT) : MAX_CALLS_LIMIT,
  };
}

type Pending = { resolve: (value: unknown) => void; reject: (error: Error) => void };

/**
 * Turns each `api.<name>(args)` call in the sandbox into a `call` frame and waits for its `result`.
 * Once closed (abort, disconnect), every pending and later call rejects with the reason.
 */
export class CallBridge {
  #send: (message: object) => void;
  #maxCalls: number;
  #pending = new Map<number, Pending>();
  #nextId = 1;
  #closed: string | null = null;

  constructor(send: (message: object) => void, maxCalls: number) {
    this.#send = send;
    this.#maxCalls = maxCalls;
  }

  get calls(): number {
    return this.#nextId - 1;
  }

  call(name: string, args: unknown): Promise<unknown> {
    if (this.#closed) return Promise.reject(new Error(this.#closed));
    if (this.calls >= this.#maxCalls) {
      return Promise.reject(new Error(`More than ${this.#maxCalls} api calls in one snippet.`));
    }
    const id = this.#nextId++;
    return new Promise((resolve, reject) => {
      this.#pending.set(id, { resolve, reject });
      try {
        this.#send({ type: "call", id, name, args: args === undefined ? {} : args });
      } catch {
        this.#pending.delete(id);
        reject(new Error("The connection to CA Mode was lost."));
      }
    });
  }

  /** Handle a frame from the app after the run request. */
  receive(message: unknown): void {
    if (!message || typeof message !== "object") return;
    const data = message as Record<string, unknown>;
    if (data.type === "abort") {
      this.close(typeof data.reason === "string" ? `Stopped: ${data.reason}` : "Stopped by CA Mode.");
      return;
    }
    if (data.type !== "result" || typeof data.id !== "number") return;
    const pending = this.#pending.get(data.id);
    if (!pending) return;
    this.#pending.delete(data.id);
    if (data.ok === true) pending.resolve(data.value);
    else pending.reject(new Error(typeof data.error === "string" && data.error ? data.error : "The call failed."));
  }

  close(reason: string): void {
    if (this.#closed) return;
    this.#closed = reason;
    for (const pending of this.#pending.values()) pending.reject(new Error(reason));
    this.#pending.clear();
  }
}

/** Constant-time comparison of an `Authorization: Bearer <secret>` header with the configured secret. */
export async function secretMatches(header: string | null, secret: string): Promise<boolean> {
  if (!secret || !header?.startsWith("Bearer ")) return false;
  const encoder = new TextEncoder();
  const [given, expected] = await Promise.all([
    crypto.subtle.digest("SHA-256", encoder.encode(header.slice(7))),
    crypto.subtle.digest("SHA-256", encoder.encode(secret)),
  ]);
  const a = new Uint8Array(given);
  const b = new Uint8Array(expected);
  let difference = 0;
  for (let index = 0; index < a.length; index++) difference |= a[index] ^ b[index];
  return difference === 0;
}

/** A sandbox result as JSON-safe data (undefined becomes null; values that cannot be serialised become text). */
export function jsonSafe(value: unknown): unknown {
  if (value === undefined) return null;
  try {
    return JSON.parse(JSON.stringify(value)) ?? null;
  } catch {
    return String(value);
  }
}
