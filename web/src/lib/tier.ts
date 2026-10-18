import type { Snapshot } from '../types';
import { isFailedStop, isStale } from './format';

/*
 * The overall tier, mirroring BD-1's BAU row so the two never disagree:
 * home-servers mac/conversation-memory-forwarder/src/status.rs
 * (`BauStatus::health`, `bau_status_from`) and src/tray.rs (`bau_line`).
 * If the rules change there, change them here.
 */
export const AMBER_FROM_OPEN_PRS = 10;
export const RED_FROM_OPEN_PRS = 30;

export type Tier = 'green' | 'amber' | 'red';
export type Reason = { tier: Exclude<Tier, 'green'>; text: string };
export type TierResult = { tier: Tier; reasons: Reason[] };

const RANK: Record<Tier, number> = { green: 0, amber: 1, red: 2 };

export function computeTier(
  snap: Snapshot | null,
  fetchError: string | null,
  now: number = Date.now(),
): TierResult {
  const reasons: Reason[] = [];
  const add = (tier: Reason['tier'], text: string) => reasons.push({ tier, text });

  // BD-1: no reachable endpoint is red.
  if (fetchError !== null) add('red', `State could not be loaded: ${fetchError}`);

  if (snap?.missing) {
    add('amber', 'No snapshot has been published yet');
  } else if (snap) {
    if (isStale(snap.generated_at, now)) {
      add('red', 'The snapshot is stale (older than 26 hours, or its timestamp is unusable)');
    }
    const run = snap.last_run;
    if (run && isFailedStop(run.stop_reason)) {
      add('red', `The last run failed (${run.stop_reason})`);
    }
    const open = snap.open_prs?.length ?? 0;
    if (open >= RED_FROM_OPEN_PRS) {
      add('red', `${open} open PRs (red from ${RED_FROM_OPEN_PRS})`);
    } else if (open >= AMBER_FROM_OPEN_PRS) {
      add('amber', `${open} open PRs (amber from ${AMBER_FROM_OPEN_PRS})`);
    }
    if (run?.telemetry === 'missing') add('amber', 'Usage telemetry is missing');
  }

  reasons.sort((a, b) => RANK[b.tier] - RANK[a.tier]);
  return { tier: reasons[0]?.tier ?? 'green', reasons };
}
