import Dashboard from "./dashboard";
import styles from "./dashboard.module.css";
import { fetchClinics, fetchState } from "@/lib/api";
import type { DashState } from "@/lib/types";

// Entirely live data — there is nothing to prerender, and a build-time snapshot of a clinic
// week would be wrong the moment anyone looked at it.
export const dynamic = "force-dynamic";

type Boot = { clinicId: string; initial: DashState } | { failure: string };

/**
 * All the fetching and all the failure handling, and deliberately no JSX.
 *
 * Rendering a component inside a `try` does not do what it looks like it does: React renders
 * lazily, so an error thrown *during* the render escapes the catch entirely. Keeping the
 * try/catch in a plain async function makes the distinction impossible to get wrong.
 */
async function boot(): Promise<Boot> {
  let clinicId: string;
  try {
    const clinics = await fetchClinics();
    const chosen = clinics.find((c) => c.slug === "demo") ?? clinics[0];
    if (!chosen) return { failure: "Ingen klinik finns än. Kör `make seed` i projektroten." };
    clinicId = chosen.id;
  } catch {
    return { failure: "Når inte API:et. Kör `make run` och ladda om." };
  }

  try {
    return { clinicId, initial: await fetchState(clinicId) };
  } catch {
    return { failure: "API:et svarar men klinikvyn gick inte att hämta." };
  }
}

/**
 * The snapshot is fetched here, on the server, and handed to the client component as props.
 *
 * Doing it in an effect instead cost two things: a visible "laddar…" flash on every open, and
 * a `setState` inside `useEffect` that React 19 flags as a cascading render. Fetching in the
 * Server Component removes both, and the socket in the client component then only ever
 * *updates* state it was given rather than creating it.
 */
export default async function Page() {
  const result = await boot();

  if ("failure" in result) {
    return (
      <main className={styles.loading}>
        <p>{result.failure}</p>
      </main>
    );
  }

  return <Dashboard clinicId={result.clinicId} initial={result.initial} />;
}
