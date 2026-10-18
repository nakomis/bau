import { useEffect, useRef, useState } from 'react';
import { ageLabel } from '../lib/format';
import { groupByRepo } from '../lib/group';
import type { OpenPr } from '../types';
import Badge, { type Tone } from './Badge';
import ExtLink from './ExtLink';

const ciTone: Record<OpenPr['ci'], Tone> = {
  green: 'ok',
  red: 'bad',
  pending: 'warn',
  none: 'neutral',
};

const CONFIRM_MS = 5000;

type Props = {
  rows: OpenPr[];
  onAbandon: (ref: string, note: string) => Promise<string>;
};

function AbandonControl({
  pr,
  armed,
  onArm,
  onDisarm,
  onAbandon,
}: {
  pr: string;
  armed: boolean;
  onArm: (ref: string) => void;
  onDisarm: () => void;
  onAbandon: Props['onAbandon'];
}) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [outcome, setOutcome] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    if (!armed) setNote('');
  }, [armed]);

  const click = async () => {
    if (!armed) {
      setOutcome(null);
      onArm(pr);
      return;
    }
    onDisarm();
    setBusy(true);
    try {
      const restored = await onAbandon(pr, note);
      setOutcome({ ok: true, text: `restored to ${restored}` });
    } catch (e) {
      setOutcome({ ok: false, text: e instanceof Error ? e.message : 'abandon failed' });
    } finally {
      setBusy(false);
    }
  };

  if (outcome?.ok) return <span className="text-ok">{outcome.text}</span>;
  return (
    <div className="flex flex-col gap-1 sm:items-end">
      {outcome && <span className="text-bad">{outcome.text}</span>}
      {armed && (
        <label className="flex flex-col text-xs text-dim">
          Note (optional)
          <input
            className="rounded-lg border border-line bg-ground px-2 py-1 text-sm text-fg"
            type="text"
            maxLength={500}
            value={note}
            onChange={(e) => {
              setNote(e.target.value);
              onArm(pr);
            }}
          />
        </label>
      )}
      <button
        type="button"
        disabled={busy}
        className="self-start text-bad underline disabled:opacity-50 sm:self-end"
        onClick={click}
      >
        {armed ? 'Confirm abandon' : 'Abandon'}
      </button>
    </div>
  );
}

export default function PrTable({ rows, onAbandon }: Props) {
  const [armedRef, setArmedRef] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const disarm = () => {
    clearTimeout(timer.current);
    setArmedRef(null);
  };
  const arm = (ref: string) => {
    clearTimeout(timer.current);
    setArmedRef(ref);
    timer.current = setTimeout(() => setArmedRef(null), CONFIRM_MS);
  };
  useEffect(() => () => clearTimeout(timer.current), []);

  if (rows.length === 0) return <p className="text-dim">Nothing awaiting review.</p>;
  const label = 'before:mr-2 before:text-dim before:content-[attr(data-label)] sm:before:hidden';
  return (
    <div className="space-y-4">
      {groupByRepo(rows).map((g) => (
        <section
          key={g.repo}
          aria-label={g.repo}
          className="rounded-[14px] border border-line bg-card p-4"
        >
          <h3 className="mb-2 text-base font-semibold">
            {g.repo.replace('nakomis/', '')}{' '}
            <span className="text-sm font-normal text-dim">({g.prs.length})</span>
          </h3>
          <table className="block w-full text-sm sm:table">
            <thead className="hidden text-left text-dim sm:table-header-group">
              <tr>
                <th className="font-normal">PR</th>
                <th className="font-normal">Age</th>
                <th className="font-normal">CI</th>
                <th className="font-normal">Comments</th>
                <th />
              </tr>
            </thead>
            <tbody className="block sm:table-row-group">
              {g.prs.map((r) => (
                <tr
                  key={r.ref}
                  className="mb-3 block rounded-lg border border-line p-3 sm:mb-0 sm:table-row sm:rounded-none sm:border-0 sm:border-t sm:p-0"
                >
                  <td className="block py-1 sm:table-cell sm:py-2">
                    <ExtLink className="underline" href={r.url}>
                      {r.ref}: {r.title}
                    </ExtLink>{' '}
                    <ExtLink className="text-dim underline" href={r.plane_url}>
                      Plane
                    </ExtLink>
                    {r.draft && <span className="ml-2 text-xs text-dim">draft</span>}
                  </td>
                  <td className={`block sm:table-cell ${label}`} data-label="Age:">
                    {ageLabel(r.age_days)}
                  </td>
                  <td className={`block sm:table-cell ${label}`} data-label="CI:">
                    <Badge tone={ciTone[r.ci] ?? 'neutral'} title={r.ci}>
                      {r.ci === 'none' ? 'no CI' : r.ci}
                    </Badge>
                  </td>
                  <td className={`block sm:table-cell ${label}`} data-label="Comments:">
                    {r.unresolved_comments > 0 ? `${r.unresolved_comments} unresolved` : '—'}
                  </td>
                  <td className="block pt-2 sm:table-cell sm:pt-0 sm:text-right">
                    <AbandonControl
                      pr={r.ref}
                      armed={armedRef === r.ref}
                      onArm={arm}
                      onDisarm={disarm}
                      onAbandon={onAbandon}
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      ))}
    </div>
  );
}
