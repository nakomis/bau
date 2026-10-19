import type { TierResult } from '../lib/tier';

const heading = {
  green: { label: 'All clear', colour: 'text-ok', edge: 'border-l-ok' },
  amber: { label: 'Needs a look', colour: 'text-warn', edge: 'border-l-warn' },
  red: { label: 'Needs attention', colour: 'text-bad', edge: 'border-l-bad' },
} as const;

export default function TierBanner({ result, openPrs }: { result: TierResult; openPrs: number }) {
  const h = heading[result.tier];
  return (
    <section
      aria-label="Overall status"
      className={`rounded-[14px] border border-line border-l-4 bg-card p-4 ${h.edge}`}
    >
      <p className={`text-base font-semibold ${h.colour}`}>{h.label}</p>
      <p className="text-sm text-dim">
        {openPrs} open {openPrs === 1 ? 'PR' : 'PRs'} awaiting review
      </p>
      {result.reasons.length > 0 && (
        <ul className="mt-2 list-disc pl-5 text-sm">
          {result.reasons.map((r) => (
            <li key={r.text} className={r.tier === 'red' ? 'text-bad' : 'text-warn'}>
              {r.text}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
