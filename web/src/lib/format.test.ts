import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  abandon,
  ageLabel,
  fetchState,
  isFailedStop,
  isStale,
  markNotBau,
  normaliseSnapshot,
  pct,
  relativeTime,
  safeHref,
  submitAnswers,
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
    expect(isStale('2026-10-13T10:00:00Z', now)).toBe(false);
    expect(isStale('2026-10-13T09:59:59Z', now)).toBe(true);
  });
  it('accepts the producers microsecond timestamps', () => {
    expect(isStale('2026-10-14T11:48:00.123456Z', now)).toBe(false);
    expect(isStale('2026-10-13T09:00:00.123456Z', now)).toBe(true);
    expect(isStale('2026-10-14T12:48:00.5+01:00', now)).toBe(false);
  });
  it('requires strict RFC 3339, as BD-1 does', () => {
    for (const loose of [
      '2026-10-14',
      '2026-10-14 11:48:00Z',
      '2026-10-14T11:48:00',
      '2026-10-14T11:48Z',
      'Tue, 14 Oct 2026 11:48:00 GMT',
      '1791978480',
    ]) {
      expect(isStale(loose, now)).toBe(true);
    }
  });
  it('treats an absent or unparseable timestamp as stale, as BD-1 does', () => {
    expect(isStale('nonsense', now)).toBe(true);
    expect(isStale('', now)).toBe(true);
    expect(isStale(undefined, now)).toBe(true);
  });
  it('allows five minutes of clock skew into the future, no more', () => {
    expect(isStale('2026-10-14T12:05:00Z', now)).toBe(false);
    expect(isStale('2026-10-14T12:05:01Z', now)).toBe(true);
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
    expect(safeHref('https://github.com/nakomis/home-infra/pull/7')).toBe(
      'https://github.com/nakomis/home-infra/pull/7',
    );
    expect(safeHref('javascript:alert(1)')).toBeUndefined();
    expect(safeHref('data:text/html,x')).toBeUndefined();
    expect(safeHref('http://x')).toBeUndefined();
    expect(safeHref('not a url')).toBeUndefined();
    expect(safeHref(undefined)).toBeUndefined();
  });

  it('allows only GitHub and Plane hosts', () => {
    const plane = 'https://plane.home.nakomis.com/nakomis/browse/HOME-42/';
    expect(safeHref(plane)).toBe(plane);
    for (const bad of [
      'https://example.com/x',
      'https://github.com.evil.example/x',
      'https://evil.example/github.com',
      'https://github.com@evil.example/x',
      'https://gist.github.com/x',
      'https://plane.home.nakomis.com.evil.example/x',
    ]) {
      expect(safeHref(bad)).toBeUndefined();
    }
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

  it('rejects a body that is not an object, so the page goes red', async () => {
    for (const body of ['oops', [], null, 3]) {
      vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => body }));
      await expect(fetchState()).rejects.toThrow('not a snapshot object');
    }
    expect(() => normaliseSnapshot(null)).toThrow();
    expect(() => normaliseSnapshot([1])).toThrow();
  });

  it('keeps an explicit missing snapshot as it is', () => {
    expect(normaliseSnapshot({ schema: 1, missing: true }).missing).toBe(true);
  });

  it('drops a non-string stop reason rather than rendering it', () => {
    const s = normaliseSnapshot({ schema: 1, last_run: { stop_reason: { a: 1 } } });
    expect(s.last_run?.stop_reason).toBe('');
  });

  it('treats non-array lists as empty', () => {
    const s = normaliseSnapshot({ schema: 1, open_prs: {}, deploy_pending: 'x', declined: 3 });
    expect(s.open_prs).toEqual([]);
    expect(s.deploy_pending).toEqual([]);
    expect(s.declined).toEqual([]);
  });
});

describe('submitAnswers and markNotBau', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('posts the answers with round and asked_at', async () => {
    const f = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200, json: async () => ({ ref: 'X-1' }) });
    vi.stubGlobal('fetch', f);
    const answers = [{ id: 'q1', accepted: true, text: 'Yes' }];
    await submitAnswers('X-1', 2, '2026-10-14T01:00:00Z', answers);
    expect(f.mock.calls[0][0]).toBe('/answers/X-1');
    expect(f.mock.calls[0][1].method).toBe('POST');
    expect(JSON.parse(f.mock.calls[0][1].body)).toEqual({
      round: 2,
      asked_at: '2026-10-14T01:00:00Z',
      answers,
    });
  });

  it('throws the API error text, or a status message for non-JSON', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 409, json: async () => ({ error: 'stale' }) }),
    );
    await expect(submitAnswers('X-1', 1, 'a', [])).rejects.toThrow('stale');
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 502,
        json: async () => {
          throw new SyntaxError('x');
        },
      }),
    );
    await expect(markNotBau('X-1', '')).rejects.toThrow('not-bau failed (502)');
  });

  it('posts the not-bau note', async () => {
    const f = vi
      .fn()
      .mockResolvedValue({ ok: true, status: 200, json: async () => ({ ref: 'X-1' }) });
    vi.stubGlobal('fetch', f);
    await markNotBau('X-1', 'nope');
    expect(f.mock.calls[0][0]).toBe('/not-bau/X-1');
    expect(JSON.parse(f.mock.calls[0][1].body)).toEqual({ note: 'nope' });
  });

  it('normalises absent questions and uninstalled to empty lists', () => {
    const s = normaliseSnapshot({ schema: 2 });
    expect(s.questions).toEqual([]);
    expect(s.uninstalled).toEqual([]);
  });
});

describe('isFailedStop', () => {
  it('matches failed*, preflight* and rejected only', () => {
    for (const r of ['failed: SystemExit: 143', 'failed', 'preflight: Timeout', 'rejected']) {
      expect(isFailedStop(r)).toBe(true);
    }
    for (const r of [
      'ok',
      'cutoff',
      'no work',
      '',
      undefined,
      'unrejected',
      7,
      null,
      {},
      ['failed'],
    ]) {
      expect(isFailedStop(r)).toBe(false);
    }
  });
});
