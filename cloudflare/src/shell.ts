/**
 * Pieces of the front Worker with no Workers APIs, so `node --test` covers them.
 */

const ORIGIN_PLACEHOLDER = "{{origin}}";

/** The same escaping as Python's html.escape(origin, quote=True) in app/public_pages.py. */
export function escapeHtml(text: string): string {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#x27;");
}

/** index.html with absolute Open Graph URLs under `origin` (what app_shell() does in the container). */
export function fillOrigin(html: string, origin: string): string {
  return html.replaceAll(ORIGIN_PLACEHOLDER, escapeHtml(origin.replace(/\/+$/, "")));
}

/**
 * Where to send a GET/HEAD that arrived on another host than APP_ORIGIN, or null. Cookies are kept per host,
 * and INDmoney's OAuth callback always returns to APP_ORIGIN, so a session started elsewhere (127.0.0.1 vs
 * localhost, workers.dev vs a custom domain) could never finish connecting INDmoney.
 */
export function canonicalRedirect(method: string, requestUrl: string, appOrigin: string | undefined): string | null {
  if (!appOrigin || (method !== "GET" && method !== "HEAD")) return null;
  let canonical: URL;
  try {
    canonical = new URL(appOrigin);
  } catch {
    return null;
  }
  const url = new URL(requestUrl);
  if (url.origin === canonical.origin) return null;
  return canonical.origin + url.pathname + url.search;
}

/** Whether a request is for the app shell, which the Worker serves without waking the container. */
export function isAppShell(method: string, pathname: string): boolean {
  return (method === "GET" || method === "HEAD") && (pathname === "/" || pathname === "/index.html");
}

/** Settings passed from the Worker's vars and secrets into the container's environment. */
export const CONTAINER_ENV_KEYS = [
  "APP_ORIGIN", "APP_MASTER_KEY",
  "AI_PROVIDER", "GROQ_API_KEY", "GROQ_MODEL", "GROQ_BASE_URL", "GROQ_INPUT_TOKEN_BUDGET", "GROQ_COMPLETION_TOKEN_LIMIT",
  "GROQ_ROUTER_MODEL", "OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL", "OPENAI_ROUTER_MODEL",
  "REQUEST_ROUTER", "TOOL_MODE", "CODE_RUNNER_URL", "CODE_RUNNER_SECRET", "CODE_RUNNER_TIMEOUT_SECONDS",
  "MCP_SERVER_URL", "SNAPSHOT_TIME", "SNAPSHOT_TIMEZONE", "SNAPSHOT_RETENTION_DAYS", "LOG_LEVEL",
  "LITESTREAM_ENDPOINT", "LITESTREAM_BUCKET", "LITESTREAM_ACCESS_KEY_ID", "LITESTREAM_SECRET_ACCESS_KEY",
] as const;

/** Values fixed by this deployment, whatever the Worker's vars say. */
export const CONTAINER_FIXED_ENV: Record<string, string> = {
  APP_DATA_DIR: "/data",
  PORT: "8080",
  // The container disk is erased on every restart; Litestream keeps the database in R2 instead of daily files.
  BACKUP_ENABLED: "false",
  // Ollama cannot run in the container.
  OLLAMA_MODEL: "",
};

export function containerEnv(env: Record<string, unknown>): Record<string, string> {
  const values: Record<string, string> = {};
  for (const key of CONTAINER_ENV_KEYS) {
    const value = env[key];
    if (typeof value === "string" && value !== "") values[key] = value;
  }
  return { ...values, ...CONTAINER_FIXED_ENV };
}

/** Settings without which the app would start but could not keep data or answer. */
export function missingSettings(env: Record<string, unknown>): string[] {
  const required = ["APP_ORIGIN", "APP_MASTER_KEY", "LITESTREAM_ENDPOINT", "LITESTREAM_BUCKET",
    "LITESTREAM_ACCESS_KEY_ID", "LITESTREAM_SECRET_ACCESS_KEY"];
  const missing = required.filter((key) => typeof env[key] !== "string" || env[key] === "");
  if (!env.GROQ_API_KEY && !env.OPENAI_API_KEY) missing.push("GROQ_API_KEY or OPENAI_API_KEY");
  return missing;
}
