import { describe, expect, it } from 'vitest';
import type { OpenPr } from '../types';
import { groupByRepo, stateRank } from './group';

const base: OpenPr = {
  repo: 'nakomis/a',
  ref: 'A-1',
  title: 't',
  url: 'https://github.com/nakomis/home-infra/pull/1',
  opened: '2026-10-13',
  age_days: 1,
  ci: 'green',
  draft: false,
  unresolved_comments: 0,
  plane_url: 'https://plane.home.nakomis.com/nakomis/browse/A-1/',
};
const pr = (o: Partial<OpenPr>): OpenPr => ({ ...base, ...o });

describe('stateRank', () => {
  it('orders red CI, comments, pending, draft, green, none', () => {
    const ranks = [
      pr({ ci: 'red', unresolved_comments: 3 }),
      pr({ unresolved_comments: 1 }),
      pr({ ci: 'pending' }),
      pr({ draft: true }),
      pr({}),
      pr({ ci: 'none' }),
    ].map(stateRank);
    expect(ranks).toEqual([0, 1, 2, 3, 4, 5]);
  });
});

describe('groupByRepo', () => {
  it('returns nothing for nothing', () => {
    expect(groupByRepo([])).toEqual([]);
  });

  it('groups by repo, urgent repos first, then by name', () => {
    const groups = groupByRepo([
      pr({ repo: 'nakomis/b', ref: 'B-1' }),
      pr({ repo: 'nakomis/a', ref: 'A-1' }),
      pr({ repo: 'nakomis/c', ref: 'C-1', ci: 'red' }),
    ]);
    expect(groups.map((g) => g.repo)).toEqual(['nakomis/c', 'nakomis/a', 'nakomis/b']);
  });

  it('orders PRs by state, then age (oldest first), then ref', () => {
    const g = groupByRepo([
      pr({ ref: 'A-3', age_days: 9 }),
      pr({ ref: 'A-2', age_days: 2, ci: 'red' }),
      pr({ ref: 'A-1', age_days: 9 }),
      pr({ ref: 'A-4', age_days: 1, draft: true }),
    ])[0];
    expect(g.prs.map((p) => p.ref)).toEqual(['A-2', 'A-4', 'A-1', 'A-3']);
  });

  it('handles a repo with only drafts', () => {
    const g = groupByRepo([pr({ draft: true }), pr({ ref: 'A-2', draft: true })]);
    expect(g).toHaveLength(1);
    expect(g[0].prs).toHaveLength(2);
  });
});
