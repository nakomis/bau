import type { ReactNode } from 'react';

export type Tone = 'ok' | 'warn' | 'bad' | 'neutral';

const tones: Record<Tone, string> = {
  ok: 'bg-ok/15 text-ok',
  warn: 'bg-warn/20 text-warn',
  bad: 'bg-bad/15 text-bad',
  neutral: 'bg-white/10 text-fg',
};

/** The portal's soft badge: a tinted background with solid text. */
export default function Badge({
  tone,
  title,
  children,
}: {
  tone: Tone;
  title?: string;
  children: ReactNode;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${tones[tone]}`}
    >
      {children}
    </span>
  );
}
