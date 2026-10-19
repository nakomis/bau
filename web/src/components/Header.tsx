import logo from '../assets/logo.png';
import { relativeTime } from '../lib/format';
import type { Tier } from '../lib/tier';
import Badge from './Badge';

const tierBadge: Record<Tier, { tone: 'ok' | 'warn' | 'bad'; label: string }> = {
  green: { tone: 'ok', label: 'Green' },
  amber: { tone: 'warn', label: 'Amber' },
  red: { tone: 'bad', label: 'Red' },
};

export default function Header({
  tier,
  generated,
  stale,
  paused,
}: {
  tier: Tier | null;
  generated: string | undefined;
  stale: boolean;
  paused: boolean;
}) {
  return (
    <header className="border-b border-line bg-bar">
      <div className="mx-auto flex max-w-5xl flex-wrap items-center gap-x-3 gap-y-1 px-4 py-3">
        <img src={logo} alt="" className="h-8 w-8" />
        <h1 className="text-lg font-semibold">BAU</h1>
        {tier && <Badge tone={tierBadge[tier].tone}>{tierBadge[tier].label}</Badge>}
        {generated && (
          <span className="text-sm text-dim" title={generated}>
            updated {relativeTime(generated)}
          </span>
        )}
        {stale && <Badge tone="bad">stale</Badge>}
        {paused && <Badge tone="warn">PAUSED</Badge>}
      </div>
    </header>
  );
}
