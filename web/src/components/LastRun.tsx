import { pct, relativeTime } from '../lib/format';
import type { Pace, LastRun as Run } from '../types';

const failed = (reason: string) =>
  reason.startsWith('failed') || reason.startsWith('preflight') || reason === 'rejected';

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
    <div className="space-y-1 text-sm">
      {paused && (
        <p className="font-semibold text-amber-600 dark:text-amber-400">Worker is paused.</p>
      )}
      {run ? (
        <>
          {failed(run.stop_reason) && (
            <p>
              <span className="rounded bg-red-600 px-2 py-0.5 font-semibold text-white">
                Last run failed
              </span>
            </p>
          )}
          {run.telemetry === 'missing' && (
            <p className="font-semibold text-amber-600 dark:text-amber-400">
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
        <p className="text-amber-600 dark:text-amber-400">
          Usage figures unavailable (telemetry missing).
        </p>
      )}
    </div>
  );
}
