"use client";

import { formatSek } from "@/lib/api";
import type { DashCascade, DashKpis } from "@/lib/types";
import styles from "@/app/dashboard.module.css";

/**
 * The top of the screen, and the only part a clinic owner is guaranteed to read.
 *
 * It answers one question in a sentence — *what just happened?* — and then shows the two
 * numbers that make the argument. Everything else on the page is evidence for this line.
 *
 * The previous version led with a row of five equal-weight KPI cards, which is what you build
 * when you are showing data rather than making a point. HIG Typography: "Adjust font weight,
 * size, and color as needed to emphasize important information." Not everything can be
 * important.
 */

function headline(cascades: DashCascade[]): { text: string; tone: string } {
  const live = cascades.find((c) => c.outcome === "live");
  if (live) {
    const waiting = live.offers.find((o) => o.status === "sent");
    return {
      text: `En tid blev ledig ${live.slot_label}. Vi har sms:at ${
        waiting?.patient_name ?? "nästa i kön"
      } — väntar på svar.`,
      tone: styles.heroHeld,
    };
  }

  const refilled = cascades.find((c) => c.outcome === "refilled");
  if (refilled) {
    const winner = refilled.offers.find((o) => o.status === "accepted");
    return {
      text: `${winner?.patient_name ?? "En patient"} tog den lediga tiden ${
        refilled.slot_label
      }. Luckan är fylld.`,
      tone: styles.heroSuccess,
    };
  }

  if (cascades.length > 0) {
    return {
      text: "Ingen på väntelistan kunde ta den lediga tiden. Den ligger kvar som bokningsbar.",
      tone: styles.heroMuted,
    };
  }

  return {
    text: "Allt lugnt. Tryck ”Avboka en tid” för att se vad som händer när ett återbud kommer in.",
    tone: styles.heroMuted,
  };
}

export function Hero({ kpis, cascades }: { kpis: DashKpis; cascades: DashCascade[] }) {
  const { text, tone } = headline(cascades);

  return (
    <section className={`${styles.hero} ${tone}`} aria-labelledby="hero-heading">
      <h2 id="hero-heading" className={styles.heroLead}>
        {text}
      </h2>

      <div className={styles.heroMoney}>
        <div className={styles.money}>
          <span className={styles.moneyValue}>{formatSek(kpis.recovered_sek)}</span>
          <span className={styles.moneyLabel}>räddad intäkt</span>
        </div>
        <div className={`${styles.money} ${styles.moneyRisk}`}>
          <span className={styles.moneyValue}>{formatSek(kpis.at_risk_sek)}</span>
          <span className={styles.moneyLabel}>står tom just nu</span>
        </div>
      </div>

      {/* Supporting numbers, deliberately small. They answer follow-up questions; they do not
          compete with the line above. */}
      <dl className={styles.heroStats}>
        <div className={styles.stat}>
          <dt>Beläggning</dt>
          <dd>
            {Math.round(kpis.fill_rate * 100)}% <span>· {kpis.slots_open} lediga tider</span>
          </dd>
        </div>
        <div className={styles.stat}>
          <dt>Bokat av agenten</dt>
          <dd>
            {kpis.bookings_by_voice} <span>· {kpis.bookings_by_refill} via återbud</span>
          </dd>
        </div>
        <div className={styles.stat}>
          <dt>Hög risk att utebli</dt>
          <dd>
            {kpis.risk_red} <span>av {kpis.slots_booked} bokade</span>
          </dd>
        </div>
      </dl>
    </section>
  );
}
