import type { DashDay, DashResource, DashSlot } from "@/lib/types";
import { BOOKING_TOKENS, SLOT_TOKENS } from "@/lib/vocabulary";
import styles from "@/app/dashboard.module.css";

/**
 * The whole week, at a glance: practitioners down, days across, one small block per slot.
 *
 * This replaced a scrolling list of 320 rows. The list was accurate and useless — nobody in a
 * fifteen-minute meeting reads three hundred rows, and the one slot that mattered (the one a
 * cascade was texting people about) was reliably below the fold. Compressed like this, the
 * whole clinic week fits on screen and the freed slot is the only thing that moves.
 *
 * Accessibility, since the blocks are too small for glyphs:
 *
 * - Free slots are **hollow**, booked are **filled**, the held one is **larger with a ring**.
 *   Shape, not just colour (HIG Accessibility: "more than color alone").
 * - Every row ends with a written count — "3 av 16 lediga" — which is the same information in
 *   words for anyone who cannot resolve the blocks at all.
 * - Every block carries a full sentence in `title` and an `aria-label`.
 */

function slotClass(slot: DashSlot): string {
  if (slot.status === "held") return styles.blockHeld;
  if (slot.status === "open") return styles.blockOpen;
  if (slot.booking_status === "noshow") return styles.blockNoshow;
  if (slot.is_past) return styles.blockPast;
  return styles.blockBooked;
}

function describe(slot: DashSlot): string {
  const past = slot.is_past && slot.booking_status ? BOOKING_TOKENS[slot.booking_status] : null;
  const status = past?.label ?? SLOT_TOKENS[slot.status].label;
  return [slot.label, status, slot.patient_name, slot.service].filter(Boolean).join(" · ");
}

export function WeekOverview({
  days,
  resources,
}: {
  days: DashDay[];
  resources: DashResource[];
}) {
  return (
    <section className={styles.panel} aria-labelledby="week-heading">
      <div className={styles.panelHead}>
        <h2 id="week-heading" className={styles.panelTitle}>
          Veckan
        </h2>
        <span className={styles.panelHint}>varje ruta är en tid · hålrum = ledigt</span>
      </div>

      <div className={styles.weekScroll}>
        <table className={styles.week}>
          <thead>
            <tr>
              <th scope="col" className={styles.weekCorner}>
                <span className={styles.srOnly}>Behandlare</span>
              </th>
              {days.map((day) => (
                <th
                  key={day.date}
                  scope="col"
                  className={`${styles.weekDay} ${day.is_today ? styles.weekToday : ""}`}
                >
                  {day.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {resources.map((resource) => {
              const week = days.map((day) =>
                day.slots.filter((s) => s.resource_id === resource.id),
              );
              const all = week.flat();
              const free = all.filter((s) => s.status === "open").length;

              return (
                <tr key={resource.id}>
                  <th scope="row" className={styles.weekName}>
                    {resource.first_name}
                    <span className={styles.weekTitle}>{resource.title}</span>
                  </th>
                  {week.map((slots, i) => (
                    <td key={days[i].date} className={styles.weekCell}>
                      <div className={styles.blocks}>
                        {slots.map((slot) => (
                          <span
                            key={slot.id}
                            className={`${styles.block} ${slotClass(slot)}`}
                            title={describe(slot)}
                            aria-label={describe(slot)}
                            role="img"
                          />
                        ))}
                      </div>
                    </td>
                  ))}
                  <td className={styles.weekCount}>
                    {/* The same information in words, for anyone who cannot resolve the blocks. */}
                    {free} av {all.length} lediga
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className={styles.legend}>
        <span className={styles.legendItem}>
          <span className={`${styles.block} ${styles.blockOpen}`} aria-hidden="true" /> Ledig
        </span>
        <span className={styles.legendItem}>
          <span className={`${styles.block} ${styles.blockBooked}`} aria-hidden="true" /> Bokad
        </span>
        <span className={styles.legendItem}>
          <span className={`${styles.block} ${styles.blockHeld}`} aria-hidden="true" /> SMS ute
          just nu
        </span>
        <span className={styles.legendItem}>
          <span className={`${styles.block} ${styles.blockNoshow}`} aria-hidden="true" /> Patienten
          uteblev
        </span>
        <span className={styles.legendItem}>
          <span className={`${styles.block} ${styles.blockPast}`} aria-hidden="true" /> Genomförd
        </span>
      </div>
    </section>
  );
}
