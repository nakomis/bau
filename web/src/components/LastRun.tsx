import { isFailedStop, pct, relativeTime } from '../lib/format';
import type { Pace, LastRun as Run } from '../types';
import Badge from './Badge';

export default function LastRun({
  run,
  pace,
  paused,
}: {
  run: Run | null | undefined;
  pace: Pace | null | undefined;
  paused: boolean;
}) {
  return (
    <div className="space-y-1 rounded-[14px] border border-line bg-card p-4 text-sm">
      {paused && <p className="font-semibold text-warn">Worker is paused.</p>}
      {run ? (
        <>
          {isFailedStop(run.stop_reason) && (
            <p>
              <Badge tone="bad">Last run failed</Badge>
            </p>
          )}
          {run.telemetry === 'missing' && (
            <p className="font-semibold text-warn">
              Telemetry missing: fail-closed, at most two sessions
            </p>
          )}
          <p>
            <time title={run.started}>{relativeTime(run.started)}</time> to{' '}
            <time title={run.ended}>{relativeTime(run.ended)}</time>: {run.sessions} sessions,{' '}
            {run.prs_opened} PRs opened, {run.feedback_handled} feedback handled, stopped:{' '}
            {run.stop_reason}.
          </p>
        </>
      ) : (
        <p>No run recorded yet.</p>
      )}
      {pace ? (
        <p>
          Weekly {pct(pace.seven_day_used)} used, {pct(pace.elapsed_pct)} of the week elapsed;
          five-hour {pct(pace.five_hour_used)}.
        </p>
      ) : (
        <p className="text-warn">Usage figures unavailable (telemetry missing).</p>
      )}
    </div>
  );
}
