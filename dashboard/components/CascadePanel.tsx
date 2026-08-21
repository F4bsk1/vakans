"use client";

import { useSyncExternalStore } from "react";

import { formatSek } from "@/lib/api";
import type { DashCascade } from "@/lib/types";
import { OFFER_TOKENS } from "@/lib/vocabulary";
import styles from "@/app/dashboard.module.css";

/**
 * The refill cascade, drawn as a queue going downward.
 *
 * This is the panel the demo is sold on, so it earns the one countdown on the page. HIG
 * Accessibility warns against time-boxed UI ("problematic for people who need longer to process
 * information") — the mitigation here is that the countdown *reports* a deadline the backend
 * owns rather than imposing one on the viewer: nothing in this UI disappears when it hits zero,
 * and no user action depends on beating it.
 */

/**
 * The wall clock, read as what it actually is: an external store that changes on its own.
 *
 * `Date.now()` called during render is impure — two renders in the same commit can disagree.
 * `useSyncExternalStore` is the sanctioned way to read a changing external value: the snapshot
 * is whole seconds, so it is stable between ticks and React's `Object.is` comparison settles.
 * The server snapshot is 0, so the countdown renders nothing until the browser takes over
 * rather than shipping a server-rendered number that is already wrong.
 */
function subscribeToSeconds(onTick: () => void): () => void {
  const timer = setInterval(onTick, 1000);
  return () => clearInterval(timer);
}

const subscribeToNothing = () => () => {};

function useCountdown(expiresAt: string | null): number | null {
  const nowSeconds = useSyncExternalStore(
    expiresAt ? subscribeToSeconds : subscribeToNothing,
    () => Math.floor(Date.now() / 1000),
    () => 0,
  );

  if (!expiresAt || nowSeconds === 0) return null;
  return Math.max(0, Math.floor(Date.parse(expiresAt) / 1000) - nowSeconds);
}

function Offer({
  offer,
  isLast,
}: {
  offer: DashCascade["offers"][number];
  isLast: boolean;
}) {
  const token = OFFER_TOKENS[offer.status] ?? OFFER_TOKENS.expired;
  const live = offer.status === "sent";
  const remaining = useCountdown(live ? offer.expires_at : null);

  return (
    <li className={styles.offer}>
      <span
        className={`${styles.offerMark} ${live ? styles.offerLive : ""} ${
          offer.status === "accepted" ? styles.offerAccepted : ""
        }`}
        aria-hidden="true"
      >
        {token.glyph}
      </span>
      <span className={styles.offerName}>
        {offer.patient_name ?? `Kandidat ${offer.candidate_n}`}
      </span>
      <span className={styles.offerState}>
        {live && remaining !== null ? (
          <span className={styles.countdown}>{remaining}s kvar</span>
        ) : (
          token.label
        )}
      </span>
      <span className={styles.srOnly}>
        Kandidat {offer.candidate_n}: {offer.patient_name ?? "okänd"}, {token.label}
        {isLast ? ", sist i kön" : ""}
      </span>
    </li>
  );
}

export function CascadePanel({ cascades }: { cascades: DashCascade[] }) {
  return (
    <section className={styles.panel} aria-labelledby="cascade-heading">
      <div className={styles.panelHead}>
        <h2 id="cascade-heading" className={styles.panelTitle}>
          Återbudsflöde
        </h2>
        {/* Says what the kronor figure on each card means. It is deliberately *not* the same
            number as "Står tom just nu": that one is the revenue the cancellation lost, this
            one is what the clinic earns if the person currently being texted says yes. Two
            honest numbers that can differ, so both are labelled. */}
        <span className={styles.panelHint}>värde om någon tackar ja</span>
      </div>

      <div className={styles.panelBody}>
        {cascades.length === 0 ? (
          <p className={styles.empty}>
            Inget återbud än. Avboka en tid för att starta kaskaden.
          </p>
        ) : (
          cascades.map((cascade) => (
            <div key={cascade.cascade_id} className={styles.cascade}>
              <div className={styles.cascadeHead}>
                <div>
                  {/* Pre-rendered on the server: "på måndag kl 8 hos Priya". */}
                  <div className={styles.cascadeSlot}>{cascade.slot_label}</div>
                  <div className={styles.cascadeService}>
                    {cascade.service ?? "Tid"} ·{" "}
                    {cascade.outcome === "refilled"
                      ? "fylld"
                      : cascade.outcome === "live"
                        ? "pågår"
                        : "ingen tog den"}
                  </div>
                </div>
                <div
                  className={`${styles.cascadePrice} ${
                    cascade.outcome === "refilled" ? styles.toneSuccess : styles.toneHeld
                  }`}
                >
                  {formatSek(cascade.price_sek)}
                </div>
              </div>

              <ol className={styles.offers}>
                {cascade.offers.map((offer, i) => (
                  <Offer
                    key={offer.candidate_n}
                    offer={offer}
                    isLast={i === cascade.offers.length - 1}
                  />
                ))}
              </ol>
            </div>
          ))
        )}
      </div>
    </section>
  );
}
