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

/** How far ahead of our own clock a snapshot's timestamp may be before it is not trusted. */
export const CLOCK_SKEW_MS = 5 * 60_000;

/** The producer writes RFC 3339 (`...Z`, with microseconds when non-zero); BD-1 parses nothing looser. */
const RFC3339 = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)$/i;

/**
 * Stale as BD-1 judges it (status.rs `bau_status_from`): older than 26 hours,
 * more than five minutes in the future, or absent / not strict RFC 3339 (which
 * cannot be trusted, so counts as stale).
 */
export function isStale(iso: string | undefined, now: number = Date.now()): boolean {
  if (typeof iso !== 'string' || !RFC3339.test(iso)) return true;
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return true;
  if (then > now) return then - now > CLOCK_SKEW_MS;
  return now - then > STALE_HOURS * 3_600_000;
}

/** A run counts as failed when it stopped for a `failed*` or `preflight*` reason, or was `rejected`. */
export function isFailedStop(reason: unknown): boolean {
  if (typeof reason !== 'string') return false;
  return reason.startsWith('failed') || reason.startsWith('preflight') || reason === 'rejected';
}

/** The only hosts the snapshot's links may point at: GitHub pull requests and Plane items. */
const LINK_HOSTS = new Set(['github.com', 'plane.home.nakomis.com']);

/** Returns the URL only if it is https on an allowed host; anything else (javascript:, data:, http:, other hosts, garbage) is dropped. */
export function safeHref(url: string | undefined): string | undefined {
  if (!url) return undefined;
  try {
    const u = new URL(url);
    return u.protocol === 'https:' && LINK_HOSTS.has(u.hostname) ? url : undefined;
  } catch {
    return undefined;
  }
}

/**
 * Coerces a snapshot into something the UI can render without crashing. Only
 * `missing: true` means "no snapshot yet"; a body that is not an object at all
 * is a bad snapshot and throws, which the page shows as red (BD-1: unreachable).
 */
export function normaliseSnapshot(raw: unknown): Snapshot {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    throw new Error('state.json was not a snapshot object');
  }
  const s = raw as Snapshot;
  // A non-string stop reason is no reason (BD-1 reads it as None), and must not reach the DOM.
  const last_run =
    s.last_run && typeof s.last_run === 'object' && typeof s.last_run.stop_reason !== 'string'
      ? { ...s.last_run, stop_reason: '' }
      : s.last_run;
  const list = <T>(v: T[] | undefined): T[] => (Array.isArray(v) ? v : []);
  return {
    ...s,
    last_run,
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
