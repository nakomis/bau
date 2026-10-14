import { useCallback, useEffect, useState } from 'react';
import DeployList from './components/DeployList';
import LastRun from './components/LastRun';
import PrTable from './components/PrTable';
import { abandon, fetchState, isStale, relativeTime } from './lib/format';
import type { Snapshot } from './types';

const REFRESH_MS = 5 * 60 * 1000;

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-8">
      <h2 className="mb-3 text-lg font-semibold">{title}</h2>
      {children}
    </section>
  );
}

export default function App() {
  const [snap, setSnap] = useState<Snapshot | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notices, setNotices] = useState<string[]>([]);

  const refresh = useCallback(async () => {
    try {
      setSnap(await fetchState());
      setLoadError(null);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : 'unknown error');
    }
  }, []);

  useEffect(() => {
    void refresh();
    const id = setInterval(() => void refresh(), REFRESH_MS);
    return () => clearInterval(id);
  }, [refresh]);

  const onAbandon = useCallback(
    async (ref: string, note: string) => {
      const restored = await abandon(ref, note);
      setNotices((n) => [...n, `${ref}: restored to ${restored}`]);
      void refresh();
      return restored;
    },
    [refresh],
  );

  const generated = snap?.generated_at;
  const prs = [...(snap?.open_prs ?? [])].sort((a, b) => b.age_days - a.age_days);

  return (
    <main className="mx-auto max-w-4xl px-4 py-6">
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h1 className="text-2xl font-bold">BAU</h1>
        {generated && (
          <span className="text-sm text-neutral-500" title={generated}>
            updated {relativeTime(generated)}
          </span>
        )}
        {generated && isStale(generated) && (
          <span className="rounded bg-red-600 px-2 py-0.5 text-xs font-semibold text-white">
            stale
          </span>
        )}
        {snap?.paused && (
          <span className="rounded bg-amber-500 px-2 py-0.5 text-xs font-semibold text-black">
            PAUSED
          </span>
        )}
      </header>

      {loadError && (
        <p
          className="mt-4 rounded border border-red-600 p-2 text-sm text-red-700 dark:text-red-400"
          role="alert"
        >
          Could not load state: {loadError}
        </p>
      )}
      {notices.length > 0 && (
        <ul className="mt-4 text-sm text-green-700 dark:text-green-400" role="status">
          {notices.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}

      {snap === null ? (
        !loadError && <p className="mt-8 text-neutral-500">Loading…</p>
      ) : snap.missing ? (
        <p className="mt-8 text-neutral-500">No snapshot yet.</p>
      ) : (
        <>
          <Section title="Awaiting review">
            <PrTable rows={prs} onAbandon={onAbandon} />
          </Section>
          <Section title="To deploy">
            <DeployList rows={snap.deploy_pending ?? []} />
          </Section>
          <Section title="Last run">
            <LastRun run={snap.last_run} pace={snap.pace} paused={snap.paused === true} />
          </Section>
        </>
      )}
    </main>
  );
}
