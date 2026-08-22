"use client";

import { formatSek } from "@/lib/api";
import type { DashClinic, VakansEvent } from "@/lib/types";
import { describeEvent, eventTone } from "@/lib/vocabulary";
import styles from "@/app/dashboard.module.css";

/**
 * The event log — the charter's "åtgärdslogg", which is simultaneously the dashboard feed, the
 * ROI report and the future audit log.
 *
 * Worth saying out loud during a demo: nothing on this list is decorative. Each row is a
 * committed database row, which is why the agent cannot claim to have booked something that
 * does not appear here.
 *
 * `aria-live="polite"` rather than `assertive`: new rows should be announced when the screen
 * reader reaches a natural pause, not interrupt whatever is being read.
 */

const TONE_CLASS: Record<string, string> = {
  success: styles.toneSuccess,
  danger: styles.toneDanger,
  held: styles.toneHeld,
  muted: styles.toneMuted,
  open: styles.toneOpen,
  booked: styles.toneBooked,
};

const GLYPH: Record<string, string> = {
  booking_created: "●",
  booking_cancelled: "✕",
  booking_rescheduled: "→",
  slot_freed: "○",
  refill_sms_sent: "✉",
  refill_reply: "↩",
  refill_offer_expired: "⏱",
  refill_cascade_ended: "—",
  slot_refilled: "✓",
  escalation: "!",
  call_transcript: "☎",
  sms_sent: "✉",
};

function clock(iso: string, timeZone: string): string {
  return new Intl.DateTimeFormat("sv-SE", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    timeZone,
  }).format(new Date(iso));
}

export function EventFeed({
  events,
  clinic,
  snapshotEventId,
}: {
  events: VakansEvent[];
  clinic: DashClinic;
  /** Anything with a larger id arrived over the socket while someone was watching. */
  snapshotEventId: number;
}) {
  return (
    <section className={styles.panel} aria-labelledby="feed-heading">
      <div className={styles.panelHead}>
        <h2 id="feed-heading" className={styles.panelTitle}>
          Händelselogg
        </h2>
        <span className={styles.panelHint}>varje rad är en databasrad</span>
      </div>

      <ul className={styles.feed} aria-live="polite" aria-relevant="additions">
        {events.length === 0 ? (
          <li className={styles.empty}>Inget har hänt än.</li>
        ) : (
          events.map((event) => {
            const tone = eventTone(event.type);
            const sek = Number(event.sek_value ?? 0);
            return (
              <li
                key={event.id}
                className={`${styles.feedItem} ${event.id > snapshotEventId ? styles.feedFresh : ""}`}
              >
                <span className={`${styles.feedGlyph} ${TONE_CLASS[tone]}`} aria-hidden="true">
                  {GLYPH[event.type] ?? "·"}
                </span>
                <span className={styles.feedText}>
                  {describeEvent(event.type, event.payload)}
                  {/* Money is shown only where it was actually recorded, so the feed and the
                      counter can never disagree. */}
                  {event.type === "slot_refilled" && sek > 0 ? (
                    <>
                      {" "}
                      <span className={styles.feedSek}>+{formatSek(sek)}</span>
                    </>
                  ) : null}
                </span>
                {/* The clinic's timezone, not the browser's: a demo run from a laptop still on
                    another timezone must show the clinic's clock. */}
                <time className={styles.feedTime} dateTime={event.ts}>
                  {clock(event.ts, clinic.timezone)}
                </time>
              </li>
            );
          })
        )}
      </ul>
    </section>
  );
}
