import { useEffect, useRef, useState } from 'react';
import { ageLabel } from '../lib/format';
import type { OpenPr } from '../types';

const ciColour: Record<OpenPr['ci'], string> = {
  green: 'bg-green-600',
  red: 'bg-red-600',
  pending: 'bg-amber-500',
  none: 'bg-neutral-400',
};

const CONFIRM_MS = 5000;

type Props = {
  rows: OpenPr[];
  onAbandon: (ref: string, note: string) => Promise<string>;
};

function AbandonControl({ pr, onAbandon }: { pr: string; onAbandon: Props['onAbandon'] }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [outcome, setOutcome] = useState<{ ok: boolean; text: string } | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const arm = () => {
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setConfirming(false), CONFIRM_MS);
  };
  useEffect(() => () => clearTimeout(timer.current), []);

  const click = async () => {
    if (!confirming) {
      setOutcome(null);
      setConfirming(true);
      arm();
      return;
    }
    clearTimeout(timer.current);
    setBusy(true);
    try {
      const restored = await onAbandon(pr, note);
      setOutcome({ ok: true, text: `restored to ${restored}` });
    } catch (e) {
      setOutcome({ ok: false, text: e instanceof Error ? e.message : 'abandon failed' });
    } finally {
      setBusy(false);
      setConfirming(false);
      setNote('');
    }
  };

  if (outcome?.ok)
    return <span className="text-green-700 dark:text-green-400">{outcome.text}</span>;
  return (
    <div className="flex flex-col gap-1 sm:items-end">
      {outcome && <span className="text-red-700 dark:text-red-400">{outcome.text}</span>}
      {confirming && (
        <label className="flex flex-col text-xs text-neutral-500">
          Note (optional)
          <input
            className="rounded border border-neutral-300 bg-white px-2 py-1 text-sm text-neutral-900 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-100"
            type="text"
            maxLength={500}
            value={note}
            onChange={(e) => {
              setNote(e.target.value);
              arm();
            }}
          />
        </label>
      )}
      <button
        type="button"
        disabled={busy}
        className="self-start text-red-700 underline disabled:opacity-50 sm:self-end dark:text-red-400"
        onClick={click}
      >
        {confirming ? 'Confirm abandon' : 'Abandon'}
      </button>
    </div>
  );
}

export default function PrTable({ rows, onAbandon }: Props) {
  if (rows.length === 0) return <p className="text-neutral-500">Nothing awaiting review.</p>;
  const label =
    'before:mr-2 before:text-neutral-500 before:content-[attr(data-label)] sm:before:hidden';
  return (
    <table className="block w-full text-sm sm:table">
      <thead className="hidden text-left text-neutral-500 sm:table-header-group">
        <tr>
          <th>PR</th>
          <th>Repo</th>
          <th>Age</th>
          <th>CI</th>
          <th>Comments</th>
          <th />
        </tr>
      </thead>
      <tbody className="block sm:table-row-group">
        {rows.map((r) => (
          <tr
            key={r.ref}
            className="mb-3 block rounded border border-neutral-200 p-3 sm:mb-0 sm:table-row sm:rounded-none sm:border-0 sm:border-t sm:p-0 dark:border-neutral-800"
          >
            <td className="block py-1 sm:table-cell sm:py-2">
              <a className="underline" href={r.url}>
                {r.ref}: {r.title}
              </a>{' '}
              <a className="text-neutral-500 underline" href={r.plane_url}>
                Plane
              </a>
              {r.draft && <span className="ml-2 text-xs text-neutral-500">draft</span>}
            </td>
            <td className={`block sm:table-cell ${label}`} data-label="Repo:">
              {r.repo.replace('nakomis/', '')}
            </td>
            <td className={`block sm:table-cell ${label}`} data-label="Age:">
              {ageLabel(r.age_days)}
            </td>
            <td className={`block sm:table-cell ${label}`} data-label="CI:">
              <span
                className={`inline-block h-3 w-3 rounded-full ${ciColour[r.ci] ?? ciColour.none}`}
                title={r.ci}
              />
            </td>
            <td className={`block sm:table-cell ${label}`} data-label="Comments:">
              {r.unresolved_comments > 0 ? `${r.unresolved_comments} unresolved` : '—'}
            </td>
            <td className="block pt-2 sm:table-cell sm:pt-0 sm:text-right">
              <AbandonControl pr={r.ref} onAbandon={onAbandon} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
