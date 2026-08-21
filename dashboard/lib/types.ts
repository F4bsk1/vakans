/**
 * Mirrors `backend/app/schemas/dash.py`. Kept hand-written rather than generated: it is one
 * file, and the point of the mirror is that changing the Python shape should break the
 * TypeScript build rather than silently render `undefined` in front of a customer.
 *
 * Note what is *not* here: no date formatting types, no Swedish strings assembled in the
 * browser. Every human-readable label arrives pre-rendered from the backend (`label`,
 * `slot_label`, `speech`), for the same reason the agent tools return `speech`.
 */

export type SlotStatus = "open" | "booked" | "held" | "blocked";
export type RiskBand = "green" | "yellow" | "red";

export interface DashClinic {
  id: string;
  slug: string;
  name: string;
  timezone: string;
  demo_phone: string | null;
  seeded_at: string | null;
}

export interface DashResource {
  id: string;
  name: string;
  first_name: string;
  title: string | null;
  sort_order: number;
}

export interface DashSlot {
  id: string;
  resource_id: string;
  starts_at: string;
  ends_at: string;
  label: string;
  status: SlotStatus;
  booking_ref: string | null;
  booking_status: string | null;
  patient_name: string | null;
  service: string | null;
  price_sek: number | null;
  risk: number | null;
  risk_band: RiskBand | null;
  booked_via: string | null;
  is_past: boolean;
}

export interface DashDay {
  date: string;
  label: string;
  is_today: boolean;
  slots: DashSlot[];
}

export interface DashKpis {
  slots_total: number;
  slots_open: number;
  slots_booked: number;
  slots_held: number;
  fill_rate: number;
  bookings_by_voice: number;
  bookings_by_refill: number;
  recovered_sek: number;
  at_risk_sek: number;
  risk_green: number;
  risk_yellow: number;
  risk_red: number;
}

export interface DashOffer {
  candidate_n: number;
  patient_name: string | null;
  status: "sent" | "accepted" | "declined" | "expired" | "superseded";
  sent_at: string;
  expires_at: string;
  responded_at: string | null;
}

export interface DashCascade {
  cascade_id: string;
  slot_id: string;
  starts_at: string;
  slot_label: string;
  service: string | null;
  price_sek: number | null;
  is_live: boolean;
  expires_at: string | null;
  outcome: "live" | "refilled" | "exhausted";
  offers: DashOffer[];
}

export interface VakansEvent {
  id: number;
  clinic_id: string;
  ts: string;
  type: string;
  payload: Record<string, unknown>;
  sek_value: string | number | null;
  correlation_id: string | null;
  actor: string | null;
}

export interface DashState {
  clinic: DashClinic;
  generated_at: string;
  last_event_id: number;
  range_start: string;
  range_end: string;
  /** Pre-rendered Swedish, e.g. "fre 21 aug – tor 27 aug". */
  range_label: string;
  resources: DashResource[];
  days: DashDay[];
  kpis: DashKpis;
  cascades: DashCascade[];
  events: VakansEvent[];
}

export interface ClinicSummary {
  id: string;
  slug: string;
  name: string;
  seeded_at: string | null;
}
