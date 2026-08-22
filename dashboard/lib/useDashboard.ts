"use client";

/**
 * The dashboard's live half: the snapshot arrives as a prop from the Server Component, and
 * everything after it comes down one WebSocket.
 *
 * The snapshot is the only expensive query. Everything after it is an append to the same
 * `events` table the ROI report is built from — which is why the charter insists nothing
 * bypasses it.
 *
 * Three things here are load-bearing during a live demo:
 *
 * 1. **Resume, don't restart.** The socket opens with `?after_id=` from the snapshot, so an
 *    event emitted during the handshake is replayed instead of lost.
 * 2. **Reconnect with backoff.** A laptop that sleeps mid-meeting must come back on its own,
 *    and come back at the right event rather than from scratch.
 * 3. **Refetch on state-changing events only.** A refill reshapes the calendar; a ping does
 *    not. Refetching on every event would put a snapshot query behind every SMS.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { eventsSocketUrl, fetchState } from "./api";
import type { DashState, VakansEvent } from "./types";

/** Event types that change something the snapshot renders. Anything else is feed-only. */
const RESHAPES_THE_BOARD = new Set([
  "booking_created",
  "booking_cancelled",
  "booking_rescheduled",
  "slot_freed",
  "slot_refilled",
  "refill_sms_sent",
  "refill_offer_expired",
  "refill_cascade_ended",
  "sim_reset",
  "seed_completed",
]);

const MAX_FEED = 80;
const RECONNECT_MIN_MS = 500;
const RECONNECT_MAX_MS = 10_000;
// Events arrive in bursts — an expiring offer emits refill_offer_expired, refill_sms_sent and
// sometimes refill_cascade_ended within the same tick. Refetching per event fired a snapshot
// query for each of them, in parallel, which is visible in the API log as eight identical
// requests in the same instant. One refetch per burst is all the UI needs.
const REFRESH_DEBOUNCE_MS = 250;

export type Connection = "connecting" | "live" | "reconnecting";

export interface Dashboard {
  state: DashState;
  feed: VakansEvent[];
  connection: Connection;
  error: string | null;
  /** Every event with a larger id arrived live rather than in the snapshot, so the UI can
   *  highlight it. Derived from the snapshot rather than tracked in a ref: a ref mutation does
   *  not trigger the render that would show the highlight. */
  snapshotEventId: number;
  refresh: () => Promise<void>;
}

export function useDashboard(clinicId: string, initial: DashState): Dashboard {
  const [state, setState] = useState<DashState>(initial);
  const [feed, setFeed] = useState<VakansEvent[]>(() => [...initial.events].reverse());
  const [connection, setConnection] = useState<Connection>("connecting");
  const [error, setError] = useState<string | null>(null);

  const lastEventId = useRef(initial.last_event_id);
  const socketRef = useRef<WebSocket | null>(null);
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const retryRef = useRef(RECONNECT_MIN_MS);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const closedByUs = useRef(false);

  const refresh = useCallback(async () => {
    try {
      const next = await fetchState(clinicId);
      setState(next);
      setError(null);
      // Only ever move forward: a snapshot that raced behind a live event must not make the
      // socket replay events the feed already shows.
      lastEventId.current = Math.max(lastEventId.current, next.last_event_id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Kunde inte hämta data");
    }
  }, [clinicId]);

  useEffect(() => {
    closedByUs.current = false;

    const connect = () => {
      const socket = new WebSocket(eventsSocketUrl(clinicId, lastEventId.current));
      socketRef.current = socket;

      socket.onopen = () => {
        retryRef.current = RECONNECT_MIN_MS;
        setConnection("live");
      };

      socket.onmessage = (message) => {
        const data = JSON.parse(message.data as string);
        if (data.type === "ping") return; // keepalive, not an event
        const event = data as VakansEvent;

        lastEventId.current = Math.max(lastEventId.current, event.id);
        setFeed((current) =>
          // De-duplicate on id: a reconnect replays from `after_id`, and an event already on
          // screen must not appear twice in the log.
          current.some((e) => e.id === event.id)
            ? current
            : [event, ...current].slice(0, MAX_FEED),
        );

        if (RESHAPES_THE_BOARD.has(event.type)) {
          if (refreshTimer.current) clearTimeout(refreshTimer.current);
          refreshTimer.current = setTimeout(() => void refresh(), REFRESH_DEBOUNCE_MS);
        }
      };

      socket.onclose = () => {
        if (closedByUs.current) return;
        setConnection("reconnecting");
        timerRef.current = setTimeout(connect, retryRef.current);
        // Exponential backoff, capped: a laptop that slept through lunch should not hammer the
        // API when it wakes, but it must recover within ten seconds.
        retryRef.current = Math.min(retryRef.current * 2, RECONNECT_MAX_MS);
      };

      socket.onerror = () => setConnection("reconnecting");
    };

    connect();

    return () => {
      closedByUs.current = true;
      if (timerRef.current) clearTimeout(timerRef.current);
      if (refreshTimer.current) clearTimeout(refreshTimer.current);
      socketRef.current?.close();
    };
  }, [clinicId, refresh]);

  return { state, feed, connection, error, snapshotEventId: initial.last_event_id, refresh };
}
