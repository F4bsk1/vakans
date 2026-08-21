/**
 * Every call to the Vakans API. One module so the base URL and the demo token are configured
 * in exactly one place.
 *
 * `NEXT_PUBLIC_API_BASE` is read at build time by Next; the fallback is the local dev API.
 */

import type { ClinicSummary, DashState } from "./types";

export const API_BASE = (
  process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000"
).replace(/\/$/, "");

// Only set when the API has VAKANS_DEMO_TOKEN configured. In local development it is unset and
// the endpoints are open — see `_authorize` in backend/app/routers/dash.py.
const DEMO_TOKEN = process.env.NEXT_PUBLIC_DEMO_TOKEN ?? "";

function withToken(url: URL): URL {
  if (DEMO_TOKEN) url.searchParams.set("token", DEMO_TOKEN);
  return url;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function getJson<T>(path: string, params?: Record<string, string>): Promise<T> {
  const url = withToken(new URL(`${API_BASE}${path}`));
  for (const [k, v] of Object.entries(params ?? {})) url.searchParams.set(k, v);

  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    throw new ApiError(`${path} returned ${response.status}`, response.status);
  }
  return response.json() as Promise<T>;
}

export async function fetchClinics(): Promise<ClinicSummary[]> {
  return getJson<ClinicSummary[]>("/sim/clinics");
}

export async function fetchState(clinicId: string): Promise<DashState> {
  return getJson<DashState>(`/dash/${clinicId}/state`);
}

export function eventsSocketUrl(clinicId: string, afterId: number): string {
  const url = withToken(
    new URL(`${API_BASE.replace(/^http/, "ws")}/dash/${clinicId}/events`),
  );
  url.searchParams.set("after_id", String(afterId));
  return url.toString();
}

// --- demo choreography ----------------------------------------------------------------------
// These are the buttons an operator presses during a meeting. They are POSTs to /sim/*, which
// is destructive by design — hence the confirmation on `reset` in the UI.

async function post<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(withToken(new URL(`${API_BASE}${path}`)), {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new ApiError(detail || `${path} returned ${response.status}`, response.status);
  }
  return response.json() as Promise<T>;
}

export const sim = {
  reset: (clinicId: string) => post(`/sim/${clinicId}/reset`),
  cancelRandom: (clinicId: string) => post(`/sim/${clinicId}/cancel_random`),
  reply: (clinicId: string, answer: string) =>
    post(`/sim/${clinicId}/waitlist_reply`, { answer }),
};

/** "1 195 kr" — Swedish grouping, no decimals. The only formatting the browser is trusted with,
 *  because it is a number rather than a date or a sentence. */
export function formatSek(value: number | string | null | undefined): string {
  const n = typeof value === "string" ? Number(value) : (value ?? 0);
  return `${new Intl.NumberFormat("sv-SE", { maximumFractionDigits: 0 }).format(n)} kr`;
}
