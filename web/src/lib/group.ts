import type { OpenPr } from '../types';

export type RepoGroup = { repo: string; prs: OpenPr[] };

/** Lower is more urgent: failing CI, then unresolved comments, pending CI, drafts, green, no CI. */
export function stateRank(pr: OpenPr): number {
  if (pr.ci === 'red') return 0;
  if (pr.unresolved_comments > 0) return 1;
  if (pr.ci === 'pending') return 2;
  if (pr.draft) return 3;
  if (pr.ci === 'green') return 4;
  return 5;
}

const byUrgency = (a: OpenPr, b: OpenPr) =>
  stateRank(a) - stateRank(b) || b.age_days - a.age_days || a.ref.localeCompare(b.ref);

/** Groups by repository; the repository with the most urgent PR comes first, then by name. */
export function groupByRepo(prs: OpenPr[]): RepoGroup[] {
  const map = new Map<string, OpenPr[]>();
  for (const pr of prs) map.set(pr.repo, [...(map.get(pr.repo) ?? []), pr]);
  const groups = [...map.entries()].map(([repo, list]) => ({
    repo,
    prs: [...list].sort(byUrgency),
  }));
  return groups.sort(
    (a, b) => stateRank(a.prs[0]) - stateRank(b.prs[0]) || a.repo.localeCompare(b.repo),
  );
}
