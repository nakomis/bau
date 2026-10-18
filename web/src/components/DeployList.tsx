import type { DeployRow } from '../types';
import ExtLink from './ExtLink';

export default function DeployList({ rows }: { rows: DeployRow[] }) {
  if (rows.length === 0) return <p className="text-dim">Nothing to deploy.</p>;
  const groups = new Map<string, DeployRow[]>();
  for (const r of rows) groups.set(r.repo, [...(groups.get(r.repo) ?? []), r]);
  return (
    <div className="space-y-4">
      {[...groups.entries()].map(([repo, items]) => (
        <div key={repo} className="rounded-[14px] border border-line bg-card p-4">
          <h3 className="font-semibold">{repo.replace('nakomis/', '')}</h3>
          <ul className="list-disc pl-5 text-sm">
            {items.map((r) => (
              <li key={r.ref}>
                <ExtLink className="underline" href={r.pr_url}>
                  {r.ref}: {r.title}
                </ExtLink>
              </li>
            ))}
          </ul>
          <code className="mt-1 block overflow-x-auto rounded-lg bg-ground px-2 py-1 text-xs">
            {items[0].action}
          </code>
        </div>
      ))}
    </div>
  );
}
