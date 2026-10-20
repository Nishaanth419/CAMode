/**
 * CA Mode on Cloudflare.
 *
 * - The app shell (index.html) comes from static assets with {{origin}} filled in, so opening the page
 *   never waits for the container. Other files in frontend/ are served as assets before this Worker runs.
 * - Everything else (/api/*, /auth/*, /privacy, /healthz, robots.txt, sitemap.xml, unknown pages) goes
 *   to ONE container, `getContainer(env.APP, "main")`, running the FastAPI app from ../Dockerfile's code.
 * - The container keeps SQLite on its (ephemeral) disk and Litestream streams it to R2; on start it
 *   restores from R2 first (container/entrypoint.sh).
 * - A daily cron wakes the container at 09:00 IST so the snapshot job runs.
 */

import { Container, getContainer } from "@cloudflare/containers";
import { canonicalRedirect, containerEnv, fillOrigin, isAppShell, missingSettings } from "./shell.ts";

interface Env {
  ASSETS: Fetcher;
  APP: DurableObjectNamespace<CaModeApp>;
  APP_ORIGIN: string;
  SLEEP_AFTER?: string;
  [key: string]: unknown;
}

export class CaModeApp extends Container<Env> {
  defaultPort = 8080;
  sleepAfter = "30m";
  enableInternet = true; // INDmoney MCP, the model provider, R2 and the code runner.

  constructor(ctx: DurableObjectState<{}>, env: Env) {
    super(ctx, env);
    this.sleepAfter = env.SLEEP_AFTER || "30m";
    this.envVars = containerEnv(env);
  }

  override onStart(): void {
    console.log("ca-mode container started");
  }

  override onStop({ exitCode, reason }: { exitCode: number; reason: string }): void {
    console.log("ca-mode container stopped", { exitCode, reason });
  }

  override onError(error: unknown): never {
    console.error("ca-mode container error", error instanceof Error ? error.message : String(error));
    throw error;
  }
}

function app(env: Env) {
  // One name, so there is only ever one instance of the app.
  return getContainer(env.APP, "main");
}

async function appShell(request: Request, env: Env): Promise<Response> {
  // Asset handling serves index.html at "/" (and redirects "/index.html" there), so ask for "/".
  const asset = await env.ASSETS.fetch(new Request(new URL("/", request.url), { method: "GET", headers: request.headers }));
  if (!asset.ok) return asset;
  const html = fillOrigin(await asset.text(), env.APP_ORIGIN || new URL(request.url).origin);
  const headers = new Headers(asset.headers);
  headers.set("content-type", "text/html; charset=utf-8");
  headers.set("cache-control", "no-cache");
  headers.delete("content-length");
  headers.delete("etag");
  return new Response(request.method === "HEAD" ? null : html, { status: 200, headers });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const canonical = canonicalRedirect(request.method, request.url, env.APP_ORIGIN);
    if (canonical) return Response.redirect(canonical, 308);
    if (isAppShell(request.method, url.pathname)) return appShell(request, env);

    const missing = missingSettings(env);
    if (missing.length) {
      // Fail loudly rather than start an app that would lose every write.
      return Response.json(
        { error: "CA Mode is not configured on Cloudflare yet.", missing },
        { status: 503, headers: { "cache-control": "no-store" } },
      );
    }
    // The app reads X-Forwarded-Proto to mark its session cookie Secure.
    const forwarded = new Request(request);
    forwarded.headers.set("x-forwarded-proto", url.protocol.replace(":", ""));
    forwarded.headers.set("x-forwarded-host", url.host);
    try {
      return await app(env).fetch(forwarded);
    } catch (error) {
      console.error("container request failed", error instanceof Error ? error.message : String(error));
      return Response.json(
        { error: "CA Mode is starting or unavailable. Try again in a few seconds." },
        { status: 503, headers: { "retry-after": "5", "cache-control": "no-store" } },
      );
    }
  },

  async scheduled(_controller: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    if (missingSettings(env).length) return;
    // Starting the container is enough: the app catches up on a missed snapshot at startup, and sleepAfter
    // keeps it awake long enough for the scheduler's first pass.
    ctx.waitUntil(app(env).fetch(new Request(`${env.APP_ORIGIN}/healthz`)).then((response) => {
      console.log("snapshot wake-up", response.status);
    }));
  },
} satisfies ExportedHandler<Env>;
