import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { readFile } from "node:fs/promises";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const MAX_BODY_BYTES = 64 * 1024;
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "::1", "[::1]"]);

function apiConfiguration() {
  const base = new URL(process.env.GALAXY_API_URL || "http://127.0.0.1:8765");
  if (base.protocol !== "http:" || !LOOPBACK_HOSTS.has(base.hostname.toLowerCase())) {
    throw new Error("Galaxy API must use a local HTTP address");
  }
  const port = base.port || "80";
  let tokenPath = process.env.GALAXY_API_TOKEN_FILE;
  if (!tokenPath) {
    const override = process.env.GALAXY_HOME;
    let dataDir;
    if (override) {
      dataDir = override.startsWith("~/") ? join(homedir(), override.slice(2)) : resolve(override);
    } else if (process.platform === "win32") {
      dataDir = join(process.env.LOCALAPPDATA || process.env.APPDATA || join(homedir(), "AppData", "Local"), "Galaxy");
    } else if (process.platform === "darwin") {
      dataDir = join(homedir(), "Library", "Application Support", "Galaxy");
    } else {
      dataDir = join(process.env.XDG_DATA_HOME || join(homedir(), ".local", "share"), "galaxy");
    }
    tokenPath = join(dataDir, `local-api-${port}.token`);
  }
  return { base, tokenPath };
}

function jsonError(status, code, message) {
  return Response.json({ error: { code, message } }, {
    status,
    headers: { "Cache-Control": "no-store" },
  });
}

function isSameLocalOrigin(request) {
  const origin = request.headers.get("origin");
  const host = request.headers.get("host");
  if (!origin || !host) return false;
  try {
    const originUrl = new URL(origin);
    const requestUrl = new URL(request.url);
    return originUrl.origin === requestUrl.origin
      && originUrl.host.toLowerCase() === host.toLowerCase()
      && LOOPBACK_HOSTS.has(originUrl.hostname.toLowerCase());
  } catch {
    return false;
  }
}

async function proxy(request, context) {
  if (!["GET", "POST", "PATCH"].includes(request.method)) {
    return jsonError(405, "method_not_allowed", "Method not allowed");
  }
  if (request.method !== "GET" && !isSameLocalOrigin(request)) {
    return jsonError(403, "origin_not_allowed", "Request origin is not allowed");
  }

  try {
    const { base, tokenPath } = apiConfiguration();
    const token = (await readFile(tokenPath, "utf8")).trim();
    if (!token) return jsonError(503, "service_unavailable", "Galaxy local service is not ready");

    const { path = [] } = await context.params;
    const target = new URL(base);
    target.pathname = `/api/v1/${path.map(encodeURIComponent).join("/")}`;
    target.search = new URL(request.url).search;

    const headers = {
      Accept: "application/json",
      Authorization: `Bearer ${token}`,
    };
    const init = { method: request.method, headers, cache: "no-store", redirect: "error", signal: AbortSignal.timeout(10_000) };
    if (request.method !== "GET") {
      const contentType = request.headers.get("content-type") || "";
      if (!contentType.toLowerCase().includes("application/json")) {
        return jsonError(415, "unsupported_media_type", "Content-Type must be application/json");
      }
      const body = await request.arrayBuffer();
      if (body.byteLength > MAX_BODY_BYTES) {
        return jsonError(413, "body_too_large", "Request body is too large");
      }
      headers["Content-Type"] = "application/json";
      init.body = body;
    }

    const upstream = await fetch(target, init);
    const payload = await upstream.arrayBuffer();
    return new Response(payload, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("content-type") || "application/json; charset=utf-8",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
      },
    });
  } catch (error) {
    if (error?.name === "TimeoutError" || error?.name === "AbortError") {
      return jsonError(504, "local_api_timeout", "Galaxy local service did not respond in time");
    }
    return jsonError(503, "service_unavailable", "Start the Galaxy local service with `galaxy serve`");
  }
}

export const GET = proxy;
export const POST = proxy;
export const PATCH = proxy;
