import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  abandon,
  ageLabel,
  fetchState,
  isStale,
  normaliseSnapshot,
  pct,
  relativeTime,
  safeHref,
} from './format';

describe('ageLabel', () => {
  it('reads naturally', () => {
    expect(ageLabel(0)).toBe('today');
    expect(ageLabel(1)).toBe('1 day');
    expect(ageLabel(5)).toBe('5 days');
  });
});

describe('pct', () => {
  it('formats a fraction as a whole percentage', () => {
    expect(pct(0.544)).toBe('54%');
  });
});

describe('relativeTime', () => {
  const now = Date.parse('2026-10-14T12:00:00Z');
  it('describes minutes, hours and days', () => {
    expect(relativeTime('2026-10-14T11:59:40Z', now)).toBe('just now');
    expect(relativeTime('2026-10-14T11:48:00Z', now)).toBe('12 minutes ago');
    expect(relativeTime('2026-10-14T11:59:00Z', now)).toBe('1 minute ago');
    expect(relativeTime('2026-10-14T10:00:00Z', now)).toBe('2 hours ago');
    expect(relativeTime('2026-10-14T11:00:00Z', now)).toBe('1 hour ago');
    expect(relativeTime('2026-10-11T12:00:00Z', now)).toBe('3 days ago');
  });
  it('copes with unparseable input', () => {
    expect(relativeTime('nonsense', now)).toBe('at an unknown time');
  });
});

describe('isStale', () => {
  const now = Date.parse('2026-10-14T12:00:00Z');
  it('is stale beyond 26 hours only', () => {
    expect(isStale('2026-10-13T10:30:00Z', now)).toBe(false);
    expect(isStale('2026-10-13T09:00:00Z', now)).toBe(true);
    expect(isStale('nonsense', now)).toBe(false);
  });
});

describe('api helpers', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('fetchState returns the snapshot', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: true, json: async () => ({ schema: 1 }) }),
    );
    expect(await fetchState()).toMatchObject({ schema: 1 });
  });

  it('fetchState throws on a non-OK response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 502, json: async () => ({}) }),
    );
    await expect(fetchState()).rejects.toThrow('502');
  });

  it('abandon posts the note and returns the restored state', async () => {
    const f = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => ({ restored_state: 'Todo' }) });
    vi.stubGlobal('fetch', f);
    expect(await abandon('HOME-42', 'nope')).toBe('Todo');
    expect(f).toHaveBeenCalledWith(
      '/abandon/HOME-42',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ note: 'nope' }) }),
    );
  });

  it('abandon surfaces the JSON error text', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 409,
        json: async () => ({ error: 'run in progress' }),
      }),
    );
    await expect(abandon('HOME-42', '')).rejects.toThrow('run in progress');
  });

  it('abandon falls back to the status when the body is not JSON', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        json: async () => {
          throw new Error('bad');
        },
      }),
    );
    await expect(abandon('HOME-42', '')).rejects.toThrow('abandon failed (500)');
  });
});

describe('safeHref', () => {
  it('allows only https URLs', () => {
    expect(safeHref('https://gh/pull/7')).toBe('https://gh/pull/7');
    expect(safeHref('javascript:alert(1)')).toBeUndefined();
    expect(safeHref('data:text/html,x')).toBeUndefined();
    expect(safeHref('http://x')).toBeUndefined();
    expect(safeHref('not a url')).toBeUndefined();
    expect(safeHref(undefined)).toBeUndefined();
  });
});

describe('snapshot hardening', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('explains a non-JSON state response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => {
          throw new SyntaxError('Unexpected token <');
        },
      }),
    );
    await expect(fetchState()).rejects.toThrow(
      'state.json was not JSON (is the client certificate installed?)',
    );
  });

  it('treats a non-object body as a missing snapshot', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => 'oops' }));
    expect(await fetchState()).toEqual({ schema: 1, missing: true });
    expect(normaliseSnapshot(null)).toEqual({ schema: 1, missing: true });
    expect(normaliseSnapshot([1])).toEqual({ schema: 1, missing: true });
  });

  it('treats non-array lists as empty', () => {
    const s = normaliseSnapshot({ schema: 1, open_prs: {}, deploy_pending: 'x', declined: 3 });
    expect(s.open_prs).toEqual([]);
    expect(s.deploy_pending).toEqual([]);
    expect(s.declined).toEqual([]);
  });
});
