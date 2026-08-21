/**
 * The single place where a status becomes something a human sees.
 *
 * HIG Accessibility: "Convey information with more than color alone… offer visual indicators,
 * like distinct shapes or icons, in addition to color." So every status here carries three
 * things — a colour token, a **distinct glyph**, and a **Swedish word** — and the components
 * are built so that removing the colour still leaves the meaning intact. Print this dashboard
 * in greyscale and it still reads correctly.
 *
 * The glyphs are chosen to differ in *shape*, not just fill: ○ ● ◑ ✓ ✕ are distinguishable to
 * someone with red-green colour blindness and at small sizes.
 */

import type { RiskBand, SlotStatus } from "./types";

export interface Token {
  glyph: string;
  /** Shown next to the glyph, or as the screen-reader label when space is tight. */
  label: string;
  /** CSS custom property suffix: `--{tone}-fg` / `--{tone}-tint`. */
  tone: "open" | "booked" | "held" | "success" | "danger" | "muted";
}

export const SLOT_TOKENS: Record<SlotStatus, Token> = {
  open: { glyph: "○", label: "Ledig", tone: "open" },
  booked: { glyph: "●", label: "Bokad", tone: "booked" },
  // The cascade's slot. Amber and half-filled: reserved, not yet anyone's.
  held: { glyph: "◑", label: "SMS ute", tone: "held" },
  blocked: { glyph: "—", label: "Spärrad", tone: "muted" },
};

/** A past slot says what happened, not what it is. A no-show is the loss this product exists
 *  to prevent, so it gets the loudest treatment on the calendar. */
export const BOOKING_TOKENS: Record<string, Token> = {
  completed: { glyph: "✓", label: "Genomförd", tone: "success" },
  noshow: { glyph: "✕", label: "Uteblev", tone: "danger" },
};

export const RISK_TOKENS: Record<RiskBand, Token> = {
  green: { glyph: "▪", label: "Låg risk", tone: "success" },
  yellow: { glyph: "◆", label: "Medel risk", tone: "held" },
  red: { glyph: "▲", label: "Hög risk", tone: "danger" },
};

export const OFFER_TOKENS: Record<string, Token> = {
  sent: { glyph: "◑", label: "Väntar på svar", tone: "held" },
  accepted: { glyph: "✓", label: "Svarade JA", tone: "success" },
  declined: { glyph: "✕", label: "Tackade nej", tone: "danger" },
  expired: { glyph: "○", label: "Svarade inte", tone: "muted" },
  superseded: { glyph: "—", label: "Någon annan tog tiden", tone: "muted" },
};

/**
 * Swedish sentences for the event feed.
 *
 * Deliberately assembled here and not on the server, unlike times and prices: these read from
 * a payload the backend already shaped, and no date or currency is being formatted. Anything
 * that *is* a time or a price arrives pre-rendered and is passed through untouched.
 */
export function describeEvent(type: string, payload: Record<string, unknown>): string {
  const p = payload as Record<string, string | number | null | undefined>;
  switch (type) {
    case "booking_created":
      return p.via === "refill"
        ? `${p.patient_name ?? "Någon"} tog den lediga tiden — ${p.service ?? ""}`
        : `Ny bokning: ${p.patient_name ?? "okänd"} — ${p.service ?? ""}`;
    case "booking_cancelled":
      return `Avbokning: ${p.service ?? "en tid"} (${p.booking_ref ?? ""})`;
    case "booking_rescheduled":
      return `Ombokad: ${p.booking_ref ?? ""}`;
    case "slot_freed":
      return `Lucka ledig: ${p.service ?? "en tid"} — återbudsmotorn startar`;
    case "refill_sms_sent":
      return `SMS #${p.candidate_n} till ${p.patient_name ?? "kandidat"}`;
    case "refill_reply":
      return p.answer === "ja"
        ? "Svar: JA"
        : p.answer === "nej"
          ? "Svar: NEJ — går vidare i kön"
          : "Svar som inte gick att tolka";
    case "refill_offer_expired":
      return `Kandidat #${p.candidate_n} svarade inte — nästa i kön`;
    case "refill_cascade_ended":
      return p.reason === "no_candidates"
        ? "Ingen på väntelistan passade luckan"
        : "Återbudsflödet avslutat";
    case "slot_refilled":
      return `Luckan fylld av ${p.patient_name ?? "en patient"} — ${p.booking_ref ?? ""}`;
    case "escalation":
      return `Eskalerad till människa: ${p.reason ?? ""}`;
    case "sms_sent":
      return "SMS skickat";
    case "sim_reset":
    case "seed_completed":
      return "Sandlådan återställd";
    case "call_started":
      return "Samtal påbörjat";
    default:
      return type.replace(/_/g, " ");
  }
}

/** Events that deserve to stand out in the feed — the beats of the demo. */
export function eventTone(type: string): Token["tone"] {
  if (type === "slot_refilled" || type === "booking_created") return "success";
  if (type === "booking_cancelled" || type === "escalation") return "danger";
  if (type === "slot_freed" || type === "refill_sms_sent" || type === "refill_reply")
    return "held";
  return "muted";
}
