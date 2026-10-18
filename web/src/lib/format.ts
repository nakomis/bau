import type { Answer, Snapshot } from '../types';

export const STALE_HOURS = 26;

export function ageLabel(days: number): string {
  if (days <= 0) return 'today';
  return days === 1 ? '1 day' : `${days} days`;
}

export function pct(fraction: number): string {
  return `${Math.round(fraction * 100)}%`;
}

const plural = (n: number, unit: string) => `${n} ${unit}${n === 1 ? '' : 's'} ago`;

export function relativeTime(iso: string, now: number = Date.now()): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return 'at an unknown time';
  const seconds = Math.max(0, Math.round((now - then) / 1000));
  if (seconds < 60) return 'just now';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return plural(minutes, 'minute');
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return plural(hours, 'hour');
  return plural(Math.floor(hours / 24), 'day');
}

export function isStale(iso: string, now: number = Date.now()): boolean {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return false;
  return now - then > STALE_HOURS * 3_600_000;
}

/** Returns the URL only if it is https; anything else (javascript:, data:, http:, garbage) is dropped. */
export function safeHref(url: string | undefined): string | undefined {
  if (!url) return undefined;
  try {
    return new URL(url).protocol === 'https:' ? url : undefined;
  } catch {
    return undefined;
  }
}

/** Coerces whatever came back into something the UI can render without crashing. */
export function normaliseSnapshot(raw: unknown): Snapshot {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    return { schema: 1, missing: true };
  }
  const s = raw as Snapshot;
  const list = <T>(v: T[] | undefined): T[] => (Array.isArray(v) ? v : []);
  return {
    ...s,
    open_prs: list(s.open_prs),
    deploy_pending: list(s.deploy_pending),
    declined: list(s.declined),
    questions: list(s.questions),
    uninstalled: list(s.uninstalled),
  };
}

export async function fetchState(): Promise<Snapshot> {
  const r = await fetch('/state.json', { cache: 'no-store' });
  if (!r.ok) throw new Error(`state fetch failed (${r.status})`);
  let raw: unknown;
  try {
    raw = await r.json();
  } catch {
    throw new Error('state.json was not JSON (is the client certificate installed?)');
  }
  return normaliseSnapshot(raw);
}

/** POST /abandon/{ref}; resolves to the Plane state the item was restored to. */
export async function abandon(ref: string, note: string): Promise<string> {
  const r = await fetch(`/abandon/${encodeURIComponent(ref)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ note }),
  });
  let body: { error?: string; restored_state?: string } = {};
  try {
    body = await r.json();
  } catch {
    // non-JSON body; fall through to the status-based message
  }
  if (!r.ok) throw new Error(body.error ?? `abandon failed (${r.status})`);
  return body.restored_state ?? 'its previous state';
}

async function post(path: string, payload: unknown, what: string): Promise<void> {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  let body: { error?: string } = {};
  try {
    body = await r.json();
  } catch {
    // non-JSON body; fall through to the status-based message
  }
  if (!r.ok) throw new Error(body.error ?? `${what} failed (${r.status})`);
}

/** POST /answers/{ref}; accepted answers carry the recommendation text verbatim. */
export function submitAnswers(
  ref: string,
  round: number,
  asked_at: string,
  answers: Answer[],
): Promise<void> {
  return post(`/answers/${encodeURIComponent(ref)}`, { round, asked_at, answers }, 'send');
}

/** POST /not-bau/{ref}. */
export function markNotBau(ref: string, note: string): Promise<void> {
  return post(`/not-bau/${encodeURIComponent(ref)}`, { note }, 'not-bau');
}
