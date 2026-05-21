import { readCookie } from "@/lib/utils";

/**
 * Base fetch wrapper for /api/*.
 *
 * - credentials: 'include' so the JWT HttpOnly cookie is always sent.
 * - On state-changing methods (POST/PUT/PATCH/DELETE), echoes the
 *   `csrf_token` cookie into the `X-CSRF-Token` header. The backend's CSRF
 *   middleware compares the two (double-submit cookie pattern).
 * - Throws ApiError on non-2xx responses; the caller (React Query) handles it.
 */

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, message: string, body?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

export interface ApiRequestInit extends Omit<RequestInit, "body"> {
  /** Will be JSON-encoded if not already a string/FormData/Blob/URLSearchParams. */
  body?: unknown;
  /** Optional URL query parameters (added to the path). */
  params?: Record<string, string | number | boolean | null | undefined>;
}

const CSRF_COOKIE = "csrf_token";
const CSRF_HEADER = "X-CSRF-Token";
const WRITE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

function buildUrl(path: string, params?: ApiRequestInit["params"]): string {
  // Absolute URL passes through; otherwise we anchor at /api (callers can pass either).
  const url = path.startsWith("http")
    ? new URL(path)
    : new URL(path, window.location.origin);
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v == null) continue;
      url.searchParams.set(k, String(v));
    }
  }
  return url.pathname + url.search + (url.hash || "");
}

function encodeBody(body: unknown, headers: Headers): BodyInit | null {
  if (body == null) return null;
  if (
    typeof body === "string" ||
    body instanceof FormData ||
    body instanceof Blob ||
    body instanceof URLSearchParams ||
    body instanceof ArrayBuffer
  ) {
    return body as BodyInit;
  }
  if (!headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  return JSON.stringify(body);
}

export async function apiFetch<T = unknown>(
  path: string,
  init: ApiRequestInit = {},
): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers ?? {});
  if (!headers.has("Accept")) {
    headers.set("Accept", "application/json");
  }
  if (WRITE_METHODS.has(method)) {
    const csrf = readCookie(CSRF_COOKIE);
    if (csrf) headers.set(CSRF_HEADER, csrf);
  }

  const body = encodeBody(init.body, headers);
  const url = buildUrl(path, init.params);

  const res = await fetch(url, {
    ...init,
    method,
    headers,
    body,
    credentials: "include",
  });

  if (res.status === 204) {
    return undefined as T;
  }

  const contentType = res.headers.get("content-type") ?? "";
  const isJson = contentType.includes("application/json");
  const payload = isJson ? await res.json().catch(() => null) : await res.text();

  if (!res.ok) {
    const message =
      (isJson && payload && typeof payload === "object" && "detail" in payload
        ? String((payload as { detail: unknown }).detail)
        : null) ?? `Request failed: ${res.status} ${res.statusText}`;
    throw new ApiError(res.status, message, payload);
  }

  return payload as T;
}

// ----- Convenience helpers -----

export const api = {
  get: <T>(path: string, params?: ApiRequestInit["params"]) =>
    apiFetch<T>(path, { method: "GET", params }),
  post: <T>(path: string, body?: unknown) =>
    apiFetch<T>(path, { method: "POST", body }),
  put: <T>(path: string, body?: unknown) =>
    apiFetch<T>(path, { method: "PUT", body }),
  patch: <T>(path: string, body?: unknown) =>
    apiFetch<T>(path, { method: "PATCH", body }),
  delete: <T>(path: string) => apiFetch<T>(path, { method: "DELETE" }),
};
