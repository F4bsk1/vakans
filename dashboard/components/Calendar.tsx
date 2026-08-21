import type { DashDay, DashResource, DashSlot } from "@/lib/types";
import { BOOKING_TOKENS, RISK_TOKENS, SLOT_TOKENS } from "@/lib/vocabulary";
import styles from "@/app/dashboard.module.css";

/**
 * The clinic's week — the one panel a clinic owner recognises instantly, so it has to read as
 * *their* calendar rather than as a data grid.
 *
 * Grouped by practitioner inside each day. The first version listed slots flat, which put four
 * rows labelled "08:00" under one date with nothing to say they were four different people.
 * Whose column it is turns out to be the first question anyone asks.
 *
 * Accessibility decisions worth keeping:
 *
 * - Every slot states its status with a **distinct glyph** as well as a colour, and the legend
 *   below names each one. Printed in greyscale this calendar still reads correctly (HIG
 *   Accessibility: "convey information with more than color alone").
 * - Each row carries a full sentence for screen readers, because "08:00 ●" spoken aloud is
 *   meaningless. It is visually hidden, not omitted.
 * - Rows are list items, not buttons: nothing here is clickable yet, and dressing static
 *   content as a control is its own accessibility problem.
 */

const TONE_CLASS: Record<string, string> = {
  success: styles.toneSuccess,
  danger: styles.toneDanger,
  held: styles.toneHeld,
  muted: styles.toneMuted,
  open: styles.toneOpen,
  booked: styles.toneBooked,
};

const RISK_CLASS = {
  green: styles.riskGreen,
  yellow: styles.riskYellow,
  red: styles.riskRed,
} as const;

function Slot({ slot }: { slot: DashSlot }) {
  // A past slot reports what happened; a future slot reports what it is.
  const past =
    slot.is_past && slot.booking_status ? BOOKING_TOKENS[slot.booking_status] : undefined;
  const token = past ?? SLOT_TOKENS[slot.status];

  const shape =
    slot.status === "held"
      ? styles.slotHeld
      : slot.status === "open"
        ? styles.slotOpen
        : slot.booking_status === "noshow"
          ? styles.slotNoshow
          : slot.booking_status === "completed"
            ? styles.slotCompleted
            : "";

  const risk = slot.risk_band ? RISK_TOKENS[slot.risk_band] : null;
  const showRisk = risk && slot.status === "booked" && !slot.is_past;
  const spoken = [slot.label, token.label, slot.patient_name, slot.service, risk?.label]
    .filter(Boolean)
    .join(", ");

  return (
    <li
      className={`${styles.slot} ${shape} ${slot.is_past ? styles.slotPast : ""}`}
      // Native tooltip carries the detail that does not fit on one line — service and risk —
      // so the row can stay dense without hiding anything.
      title={spoken}
    >
      <span className={styles.slotTime} aria-hidden="true">
        {slot.label}
      </span>
      <span className={styles.slotGlyph} aria-hidden="true">
        {token.glyph}
      </span>
      <span className={styles.slotWho} aria-hidden="true">
        {slot.patient_name ?? token.label}
      </span>
      <span
        className={`${styles.riskMark} ${showRisk ? RISK_CLASS[slot.risk_band!] : ""}`}
        aria-hidden="true"
      >
        {showRisk ? risk.glyph : ""}
      </span>
      <span className={styles.srOnly}>{spoken}</span>
    </li>
  );
}

export function Calendar({
  days,
  resources,
}: {
  days: DashDay[];
  resources: DashResource[];
}) {
  const total = days.reduce((n, d) => n + d.slots.length, 0);

  return (
    <section className={styles.panel} aria-labelledby="calendar-heading">
      <div className={styles.panelHead}>
        <h2 id="calendar-heading" className={styles.panelTitle}>
          Kalender
        </h2>
        <span className={styles.panelHint}>{total} tider</span>
      </div>

      <div className={styles.calendarScroll}>
        <div className={styles.calendar}>
          {days.map((day) => (
            <div key={day.date} className={styles.day}>
              <h3 className={`${styles.dayHead} ${day.is_today ? styles.dayToday : ""}`}>
                {day.label}
              </h3>

              {day.slots.length === 0 ? (
                <p className={styles.panelHint}>Inga tider</p>
              ) : (
                resources.map((resource) => {
                  const slots = day.slots.filter((s) => s.resource_id === resource.id);
                  if (slots.length === 0) return null;
                  return (
                    <div key={resource.id} className={styles.resourceGroup}>
                      <h4 className={styles.resourceName}>
                        {resource.first_name}
                        <span className={styles.srOnly}>
                          {resource.title ? `, ${resource.title}` : ""}, {day.label}
                        </span>
                      </h4>
                      <ul className={styles.slotList}>
                        {slots.map((slot) => (
                          <Slot key={slot.id} slot={slot} />
                        ))}
                      </ul>
                    </div>
                  );
                })
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Not decoration: the legend is what makes the glyphs self-explanatory the first time a
          clinic owner sees this screen. */}
      <div className={styles.legend}>
        {(["open", "booked", "held"] as const).map((key) => (
          <span key={key} className={styles.legendItem}>
            <span className={TONE_CLASS[SLOT_TOKENS[key].tone]}>{SLOT_TOKENS[key].glyph}</span>
            {SLOT_TOKENS[key].label}
          </span>
        ))}
        {Object.entries(BOOKING_TOKENS).map(([key, token]) => (
          <span key={key} className={styles.legendItem}>
            <span className={TONE_CLASS[token.tone]}>{token.glyph}</span>
            {token.label}
          </span>
        ))}
        <span className={styles.legendItem}>
          <span className={styles.riskRed}>{RISK_TOKENS.red.glyph}</span> Hög risk för
          uteblivande
        </span>
      </div>
    </section>
  );
}
