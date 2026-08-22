"use client";

import { useCallback, useState } from "react";

import { CascadePanel } from "@/components/CascadePanel";
import { EventFeed } from "@/components/EventFeed";
import { Hero } from "@/components/Hero";
import { WeekOverview } from "@/components/WeekOverview";
import { sim } from "@/lib/api";
import type { DashState } from "@/lib/types";
import { useDashboard, type Connection } from "@/lib/useDashboard";
import styles from "./dashboard.module.css";

const CONNECTION_LABEL: Record<Connection, string> = {
  connecting: "Ansluter",
  live: "Live",
  reconnecting: "Återansluter",
};

/**
 * The whole dashboard. One client component because every panel reacts to the same event
 * stream, and splitting the socket across panels would mean four sockets or a context that
 * exists only to avoid one.
 */
export default function Dashboard({
  clinicId,
  initial,
}: {
  clinicId: string;
  initial: DashState;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const { state, feed, connection, error, snapshotEventId, refresh } = useDashboard(
    clinicId,
    initial,
  );

  const run = useCallback(
    async (name: string, action: () => Promise<unknown>) => {
      setBusy(name);
      try {
        await action();
        await refresh();
      } catch {
        // The event feed and the connection pill already show whether the API is answering;
        // a modal on top of that would be noise mid-demo.
      } finally {
        setBusy(null);
      }
    },
    [refresh],
  );

  const statusClass =
    connection === "live"
      ? styles.statusLive
      : connection === "reconnecting"
        ? styles.statusReconnecting
        : "";

  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <div className={styles.brand}>
          <h1 className={styles.clinicName}>{state.clinic.name}</h1>
          <span className={styles.clinicMeta}>
            {state.range_label} · {state.clinic.timezone}
          </span>
        </div>

        {/* Dot plus word: the dot alone would be colour-as-only-signal. */}
        <span className={`${styles.status} ${statusClass}`}>
          <span className={styles.statusDot} aria-hidden="true" />
          {CONNECTION_LABEL[connection]}
        </span>

        <div className={styles.controls}>
          <button
            type="button"
            className={`${styles.button} ${styles.buttonPrimary}`}
            disabled={busy !== null}
            onClick={() => run("cancel", () => sim.cancelRandom(clinicId))}
          >
            {busy === "cancel" ? "Avbokar…" : "Avboka en tid"}
          </button>
          <button
            type="button"
            className={styles.button}
            disabled={busy !== null}
            onClick={() => run("reply", () => sim.reply(clinicId, "JA"))}
          >
            {busy === "reply" ? "Svarar…" : "Svara JA"}
          </button>
          <button
            type="button"
            className={styles.button}
            disabled={busy !== null}
            // HIG Accessibility: "Always ask for confirmation whenever people perform an action
            // that's difficult to recover from." A reset throws away the running demo.
            onClick={() => {
              if (confirm("Återställ sandlådan? Allt som hänt i demon försvinner.")) {
                void run("reset", () => sim.reset(clinicId));
              }
            }}
          >
            {busy === "reset" ? "Återställer…" : "Återställ"}
          </button>
        </div>
      </header>

      {error ? <p className={styles.banner}>{error}</p> : null}

      <main className={styles.main}>
        <div className={styles.column}>
          <Hero kpis={state.kpis} cascades={state.cascades} />
          <WeekOverview days={state.days} resources={state.resources} />
        </div>
        <div className={styles.column}>
          <CascadePanel cascades={state.cascades} />
          <EventFeed events={feed} clinic={state.clinic} snapshotEventId={snapshotEventId} />
        </div>
      </main>
    </div>
  );
}
