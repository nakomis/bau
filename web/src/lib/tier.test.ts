import { describe, expect, it } from 'vitest';
import type { OpenPr, Snapshot } from '../types';
import { computeTier } from './tier';

const NOW = Date.parse('2026-10-14T12:00:00Z');
const FRESH = '2026-10-14T11:00:00Z';

const pr = (i: number): OpenPr => ({
  repo: 'nakomis/x',
  ref: `X-${i}`,
  title: 't',
  url: 'https://github.com/nakomis/home-infra/pull/1',
  opened: '2026-10-13',
  age_days: 1,
  ci: 'green',
  draft: false,
  unresolved_comments: 0,
  plane_url: 'https://plane.home.nakomis.com/nakomis/browse/X/',
});

const snap = (n: number, extra: Partial<Snapshot> = {}): Snapshot => ({
  schema: 1,
  generated_at: FRESH,
  open_prs: Array.from({ length: n }, (_, i) => pr(i)),
  ...extra,
});

const run = (stop_reason: string, telemetry = 'ok') => ({
  started: FRESH,
  ended: FRESH,
  sessions: 1,
  prs_opened: 0,
  feedback_handled: 0,
  stop_reason,
  telemetry,
});

// Mirrors tray.rs bau_line_thresholds and the failure/staleness tests.
describe('computeTier', () => {
  it.each([
    [0, 'green'],
    [9, 'green'],
    [10, 'amber'],
    [29, 'amber'],
    [30, 'red'],
    [31, 'red'],
  ])('%i open PRs is %s', (n, tier) => {
    expect(computeTier(snap(n), null, NOW).tier).toBe(tier);
  });

  it.each(['failed: SystemExit: 143', 'failed', 'preflight: Timeout', 'rejected'])(
    'a run stopped with "%s" is red',
    (reason) => {
      const r = computeTier(snap(1, { last_run: run(reason) }), null, NOW);
      expect(r.tier).toBe('red');
      expect(r.reasons[0].text).toContain(reason);
    },
  );

  it.each(['ok', 'cutoff', 'no work'])('a run stopped with "%s" is green', (reason) => {
    expect(computeTier(snap(1, { last_run: run(reason) }), null, NOW).tier).toBe('green');
  });

  it('is stale beyond 26 hours exactly', () => {
    const at = (iso: string) => computeTier(snap(0, { generated_at: iso }), null, NOW).tier;
    expect(at('2026-10-13T10:00:00Z')).toBe('green');
    expect(at('2026-10-13T09:59:59Z')).toBe('red');
  });

  it('tolerates five minutes of clock skew, not six', () => {
    const at = (iso: string) => computeTier(snap(0, { generated_at: iso }), null, NOW).tier;
    expect(at('2026-10-14T12:05:00Z')).toBe('green');
    expect(at('2026-10-14T12:06:00Z')).toBe('red');
  });

  it('treats an absent or unparseable timestamp as stale', () => {
    expect(computeTier(snap(0, { generated_at: undefined }), null, NOW).tier).toBe('red');
    expect(computeTier(snap(0, { generated_at: 'nonsense' }), null, NOW).tier).toBe('red');
  });

  it('is amber with no snapshot yet, whatever the age', () => {
    const r = computeTier({ schema: 1, missing: true }, null, NOW);
    expect(r.tier).toBe('amber');
    expect(r.reasons).toHaveLength(1);
  });

  it('only an explicit missing:true means no snapshot', () => {
    const r = computeTier({ schema: 1, missing: 'yes' } as unknown as Snapshot, null, NOW);
    expect(r.tier).toBe('red');
  });

  it('accepts the producer microsecond timestamp', () => {
    const r = computeTier(snap(0, { generated_at: '2026-10-14T11:00:00.123456Z' }), null, NOW);
    expect(r.tier).toBe('green');
  });

  it('a non-string stop reason is no reason', () => {
    const bad = { ...run('ok'), stop_reason: { x: 1 } } as unknown as ReturnType<typeof run>;
    expect(computeTier(snap(0, { last_run: bad }), null, NOW).tier).toBe('green');
  });

  it('is amber when telemetry is missing', () => {
    expect(computeTier(snap(1, { last_run: run('ok', 'missing') }), null, NOW).tier).toBe('amber');
  });

  it('is red when the state could not be loaded', () => {
    const r = computeTier(null, 'offline', NOW);
    expect(r.tier).toBe('red');
    expect(r.reasons[0].text).toContain('offline');
  });

  it('stays red on a failed refresh even with an earlier snapshot', () => {
    expect(computeTier(snap(0), 'offline', NOW).tier).toBe('red');
  });

  it('lists every reason, worst first', () => {
    const r = computeTier(snap(12, { last_run: run('failed: X', 'missing') }), null, NOW);
    expect(r.tier).toBe('red');
    expect(r.reasons.map((x) => x.tier)).toEqual(['red', 'amber', 'amber']);
  });
});
