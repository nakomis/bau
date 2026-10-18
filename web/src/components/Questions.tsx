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
  uninstalled?: string[];
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
  warn: 'bg-amber-500 text-black',
  down: 'bg-red-600 text-white',
};

function Card({
  row,
  open,
  draft,
  onToggle,
  onDraft,
  onSend,
  onNotBau,
}: {
  row: QuestionRow;
  open: boolean;
  draft: Record<string, Draft>;
  onToggle: () => void;
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
    <div className="mb-3 rounded border border-neutral-200 p-3 dark:border-neutral-800">
      <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <button
          type="button"
          aria-expanded={open}
          className="text-left font-medium underline-offset-2 hover:underline"
          onClick={onToggle}
        >
          {row.ref}: {row.title}
        </button>
        <span className="text-xs text-neutral-500">
          {row.repo.replace('nakomis/', '')} · {ageLabel(row.age_days)}
        </span>
        <span className="ml-auto flex gap-3 text-sm">
          <ExtLink className="text-neutral-500 underline" href={row.plane_url}>
            Plane ↗
          </ExtLink>
          <ExtLink className="text-neutral-500 underline" href={row.repo_url}>
            Repo ↗
          </ExtLink>
        </span>
      </div>
      {open && (
        <>
          <p className="mt-2 text-sm text-neutral-600 italic dark:text-neutral-400">
            {row.summary}
          </p>
          {row.questions.map((q, i) => {
            const d = draft[q.id] ?? { accepted: false, text: '' };
            return (
              <div key={q.id} className="mt-3">
                <p>
                  <b>{i + 1}.</b> {q.text}
                </p>
                <div className="mt-1 rounded bg-sky-50 p-2 text-sm dark:bg-sky-950">
                  <div>
                    <b>Recommended:</b> {q.recommendation}
                  </div>
                  <div className="text-neutral-600 dark:text-neutral-400">Why: {q.why}</div>
                </div>
                <div className="mt-2 flex flex-wrap items-start gap-2">
                  <button
                    type="button"
                    aria-pressed={d.accepted}
                    className={`rounded border px-3 py-1 text-sm ${
                      d.accepted
                        ? 'border-green-700 bg-green-700 text-white'
                        : 'border-neutral-300 dark:border-neutral-700'
                    }`}
                    onClick={() => onDraft(q.id, { accepted: true, text: '' })}
                  >
                    Accept
                  </button>
                  <textarea
                    className="min-w-0 flex-1 rounded border border-neutral-300 bg-white px-2 py-1 text-sm text-neutral-900 dark:border-neutral-700 dark:bg-neutral-900 dark:text-neutral-100"
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
            {error && <span className="text-red-700 dark:text-red-400">{error}</span>}
            <div className="flex gap-2">
              <button
                type="button"
                disabled={!complete || busy}
                className="rounded bg-neutral-900 px-3 py-1 text-sm text-white disabled:opacity-50 dark:bg-neutral-100 dark:text-neutral-900"
                onClick={() => run(onSend)}
              >
                Send to BAU
              </button>
              <button
                type="button"
                disabled={busy}
                className="rounded border border-neutral-300 px-3 py-1 text-sm text-neutral-600 disabled:opacity-50 dark:border-neutral-700 dark:text-neutral-400"
                onClick={() => run(onNotBau)}
              >
                Not BAU
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

export default function Questions({ rows, level, uninstalled = [], onSend, onNotBau }: Props) {
  const [drafts, setDrafts] = useState<Drafts>(loadDrafts);
  const [gone, setGone] = useState<Set<string>>(new Set());
  const [toggled, setToggled] = useState<Record<string, boolean>>({});

  useEffect(() => saveDrafts(drafts), [drafts]);

  const sorted = useMemo(
    () => [...rows].sort((a, b) => b.age_days - a.age_days).filter((r) => !gone.has(keyOf(r))),
    [rows, gone],
  );
  if (sorted.length === 0) return null;

  // Open state: explicit toggles win; otherwise up to three open, or only the first.
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
            onDraft={(id, d) => setDrafts((all) => ({ ...all, [k]: { ...all[k], [id]: d } }))}
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
      {uninstalled.length > 0 && (
        <p className="text-sm text-neutral-500">
          Waiting for the GitHub App: {uninstalled.join(', ')}
        </p>
      )}
    </section>
  );
}
