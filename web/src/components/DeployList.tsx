import type { DeployRow } from '../types';

export default function DeployList({ rows }: { rows: DeployRow[] }) {
  if (rows.length === 0) return <p className="text-neutral-500">Nothing to deploy.</p>;
  const groups = new Map<string, DeployRow[]>();
  for (const r of rows) groups.set(r.repo, [...(groups.get(r.repo) ?? []), r]);
  return (
    <div className="space-y-4">
      {[...groups.entries()].map(([repo, items]) => (
        <div key={repo}>
          <h3 className="font-semibold">{repo.replace('nakomis/', '')}</h3>
          <ul className="list-disc pl-5 text-sm">
            {items.map((r) => (
              <li key={r.ref}>
                <a className="underline" href={r.pr_url}>
                  {r.ref}: {r.title}
                </a>
              </li>
            ))}
          </ul>
          <code className="mt-1 block overflow-x-auto rounded bg-neutral-200 px-2 py-1 text-xs dark:bg-neutral-800">
            {items[0].action}
          </code>
        </div>
      ))}
    </div>
  );
}
