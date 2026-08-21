import { formatSek } from "@/lib/api";
import type { DashKpis } from "@/lib/types";
import styles from "@/app/dashboard.module.css";

/**
 * The five numbers a clinic owner reacts to, with "räddad intäkt" given the display type size.
 *
 * HIG Typography: "Adjust font weight, size, and color as needed to emphasize important
 * information." Exactly one number on this page is the argument for buying the product, so
 * exactly one gets 44px and a tinted card.
 */
export function Kpis({ kpis }: { kpis: DashKpis }) {
  return (
    <div className={styles.kpiRow}>
      <div className={`${styles.kpi} ${styles.kpiHero}`}>
        <div className={styles.kpiLabel}>Räddad intäkt</div>
        <div className={styles.kpiValue}>{formatSek(kpis.recovered_sek)}</div>
        <div className={styles.kpiFoot}>
          {kpis.bookings_by_refill} tid{kpis.bookings_by_refill === 1 ? "" : "er"} fylld
          {kpis.bookings_by_refill === 1 ? "" : "a"} via återbud
        </div>
      </div>

      <div className={`${styles.kpi} ${styles.kpiAtRisk}`}>
        <div className={styles.kpiLabel}>Står tom just nu</div>
        <div className={styles.kpiValue}>{formatSek(kpis.at_risk_sek)}</div>
        <div className={styles.kpiFoot}>
          {kpis.slots_held > 0
            ? `${kpis.slots_held} lucka i återbudsflödet`
            : "inga öppna återbud"}
        </div>
      </div>

      <div className={styles.kpi}>
        <div className={styles.kpiLabel}>Beläggning</div>
        <div className={styles.kpiValue}>{Math.round(kpis.fill_rate * 100)}%</div>
        <div className={styles.kpiFoot}>
          {kpis.slots_booked} bokade · {kpis.slots_open} lediga
        </div>
      </div>

      <div className={styles.kpi}>
        <div className={styles.kpiLabel}>Bokat av agenten</div>
        <div className={styles.kpiValue}>{kpis.bookings_by_voice}</div>
        <div className={styles.kpiFoot}>samtal som blev en tid</div>
      </div>

      <div className={styles.kpi}>
        <div className={styles.kpiLabel}>Risk för uteblivande</div>
        <div className={styles.kpiValue}>{kpis.risk_red}</div>
        {/* Counts are spelled out rather than shown as three coloured dots: the band is the
            information, and the colour is only a reinforcement of it. */}
        <div className={styles.kpiFoot}>
          hög · {kpis.risk_yellow} medel · {kpis.risk_green} låg
        </div>
      </div>
    </div>
  );
}
