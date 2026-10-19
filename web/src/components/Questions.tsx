import { useEffect, useMemo, useState } from 'react';
import { ageLabel } from '../lib/format';
import type { Answer, NudgeLevel, QuestionRow } from '../types';
import ExtLink from './ExtLink';

const DRAFTS_KEY = 'bau.drafts';
const MAX_ANSWER = 2000;
const OPEN_ALL_UP_TO = 3;

type Draft = { accepted: boolean; text: string };
type Drafts = Record<string, Record<string, Draft>>;

type Props = {
  rows: QuestionRow[];
  level: NudgeLevel;
  onSend: (ref: string, round: number, asked_at: string, answers: Answer[]) => Promise<void>;
  onNotBau: (ref: string) => Promise<void>;
};

const keyOf = (r: QuestionRow) => `${r.ref}|${r.round}|${r.asked_at}`;

function loadDrafts(): Drafts {
  try {
    const raw = localStorage.getItem(DRAFTS_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : {};
    return typeof parsed === 'object' && parsed !== null && !Array.isArray(parsed)
      ? (parsed as Drafts)
      : {};
  } catch {
    return {};
  }
}

function saveDrafts(drafts: Drafts) {
  try {
    localStorage.setItem(DRAFTS_KEY, JSON.stringify(drafts));
  } catch {
    // storage unavailable: drafts just won't survive a reload
  }
}

const badge: Record<'warn' | 'down', string> = {
  warn: 'bg-warn/20 text-warn',
  down: 'bg-bad/15 text-bad',
};

function Card({
  row,
  open,
  draft,
  onToggle,
  onEngage,
  onDraft,
  onSend,
  onNotBau,
}: {
  row: QuestionRow;
  open: boolean;
  draft: Record<string, Draft>;
  onToggle: () => void;
  onEngage: () => void;
  onDraft: (id: string, d: Draft) => void;
  onSend: () => Promise<void>;
  onNotBau: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const answered = (id: string) => {
    const d = draft[id];
    return d !== undefined && (d.accepted || d.text.trim() !== '');
  };
  const complete = row.questions.every((q) => answered(q.id));

  const run = async (action: () => Promise<void>) => {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'request failed');
      setBusy(false);
    }
  };

  return (
    <div className="mb-3 rounded-[14px] border border-line bg-card p-4">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <button
          type="button"
          aria-expanded={open}
          className="text-left font-medium underline-offset-2 hover:underline"
          onClick={onToggle}
        >
          {row.ref}: {row.title}
        </button>
        <span className="text-xs text-dim">
          {row.repo.replace('nakomis/', '')} · {ageLabel(row.age_days)}
        </span>
        <span className="ml-auto flex gap-3 text-sm">
          <ExtLink className="text-dim underline" href={row.plane_url}>
            Plane ↗
          </ExtLink>
          <ExtLink className="text-dim underline" href={row.repo_url}>
            Repo ↗
          </ExtLink>
        </span>
      </div>
      {open && (
        // Focus inside the open body pins the card open (not on the header, which toggles).
        <fieldset className="min-w-0" aria-label={`Questions for ${row.ref}`} onFocus={onEngage}>
          <p className="mt-2 text-sm text-dim italic">
            {row.summary}
          </p>
          {row.questions.map((q, i) => {
            const d = draft[q.id] ?? { accepted: false, text: '' };
            return (
              <div key={q.id} className="mt-3">
                <p>
                  <b>{i + 1}.</b> {q.text}
                </p>
                <div className="mt-1 rounded-lg bg-accent/15 p-2 text-sm">
                  <div>
                    <b>Recommended:</b> {q.recommendation}
                  </div>
                  <div className="text-dim">Why: {q.why}</div>
                </div>
                <div className="mt-2 flex flex-wrap items-start gap-2">
                  <button
                    type="button"
                    aria-pressed={d.accepted}
                    className={`rounded border px-3 py-1 text-sm ${
                      d.accepted
                        ? 'border-ok bg-ok text-white'
                        : 'border-line'
                    }`}
                    onClick={() => onDraft(q.id, { accepted: true, text: '' })}
                  >
                    Accept
                  </button>
                  <textarea
                    className="min-w-0 flex-1 rounded-lg border border-line bg-ground px-2 py-1 text-sm text-fg"
                    rows={2}
                    maxLength={MAX_ANSWER}
                    placeholder="…or write your own"
                    aria-label={`Your own answer to question ${i + 1}`}
                    value={d.text}
                    onChange={(e) => onDraft(q.id, { accepted: false, text: e.target.value })}
                  />
                </div>
              </div>
            );
          })}
          <div className="mt-3 flex flex-col items-end gap-1">
            {error && <span className="text-bad">{error}</span>}
            <div className="flex gap-2">
              <button
                type="button"
                disabled={!complete || busy}
                className="rounded-lg bg-accent px-3 py-1 text-sm text-white disabled:opacity-50"
                onClick={() => run(onSend)}
              >
                Send to BAU
              </button>
              <button
                type="button"
                disabled={busy}
                className="rounded-lg border border-line px-3 py-1 text-sm text-dim disabled:opacity-50"
                onClick={() => run(onNotBau)}
              >
                Not BAU
              </button>
            </div>
          </div>
        </fieldset>
      )}
    </div>
  );
}

export default function Questions({ rows, level, onSend, onNotBau }: Props) {
  const [drafts, setDrafts] = useState<Drafts>(loadDrafts);
  const [gone, setGone] = useState<Set<string>>(new Set());
  const [toggled, setToggled] = useState<Record<string, boolean>>({});

  useEffect(() => saveDrafts(drafts), [drafts]);

  const sorted = useMemo(
    () => [...rows].sort((a, b) => b.age_days - a.age_days).filter((r) => !gone.has(keyOf(r))),
    [rows, gone],
  );
  if (sorted.length === 0) return null;

  // Open state: explicit toggles win; otherwise up to three open, or only the first. A card the user
  // has focused or typed in is pinned open, so a refresh that changes the row count cannot fold it.
  const isOpen = (r: QuestionRow, i: number) =>
    toggled[keyOf(r)] ?? (sorted.length <= OPEN_ALL_UP_TO || i === 0);

  const remove = (row: QuestionRow) => {
    const k = keyOf(row);
    setGone((g) => new Set(g).add(k));
    setDrafts((d) => {
      const { [k]: _dropped, ...rest } = d;
      return rest;
    });
    // The next card opens (the removal shifts it into this row's slot).
    const next = sorted[sorted.findIndex((r) => keyOf(r) === k) + 1];
    if (next) setToggled((t) => ({ ...t, [keyOf(next)]: true }));
  };

  const oldest = sorted.reduce((m, r) => Math.max(m, r.age_days), 0);

  return (
    <section className="mt-8">
      <h2 className="mb-3 flex items-baseline gap-2 text-lg font-semibold">
        Questions ({sorted.length})
        {level !== 'ok' && sorted.length > 0 && (
          <span className={`rounded px-2 py-0.5 text-xs font-semibold ${badge[level]}`}>
            oldest {ageLabel(oldest)}
          </span>
        )}
      </h2>
      {sorted.map((row, i) => {
        const k = keyOf(row);
        return (
          <Card
            key={k}
            row={row}
            open={isOpen(row, i)}
            draft={drafts[k] ?? {}}
            onToggle={() => setToggled((t) => ({ ...t, [k]: !isOpen(row, i) }))}
            onEngage={() => {
              if (toggled[k] !== true) setToggled((t) => ({ ...t, [k]: true }));
            }}
            onDraft={(id, d) => {
              setToggled((t) => ({ ...t, [k]: true }));
              setDrafts((all) => ({ ...all, [k]: { ...all[k], [id]: d } }));
            }}
            onSend={async () => {
              const answers = row.questions.map((q): Answer => {
                const d = drafts[k]?.[q.id];
                return d?.accepted
                  ? { id: q.id, accepted: true, text: q.recommendation }
                  : { id: q.id, accepted: false, text: d?.text ?? '' };
              });
              await onSend(row.ref, row.round, row.asked_at, answers);
              remove(row);
            }}
            onNotBau={async () => {
              await onNotBau(row.ref);
              remove(row);
            }}
          />
        );
      })}
    </section>
  );
}
