import { pct } from '../lib/format';
import type { Pace, LastRun as Run } from '../types';

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
        <p>
          {run.started} to {run.ended}: {run.sessions} sessions, {run.prs_opened} PRs opened,{' '}
          {run.feedback_handled} feedback handled, stopped: {run.stop_reason}.
        </p>
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
