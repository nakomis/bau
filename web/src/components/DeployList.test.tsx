import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { DeployRow } from '../types';
import DeployList from './DeployList';

const mk = (repo: string, ref: string): DeployRow => ({
  repo,
  ref,
  title: `Title ${ref}`,
  pr_url: `https://gh/${ref}`,
  merged_at: '2026-10-14',
  action: `deploy ${repo}`,
});

describe('DeployList', () => {
  it('groups by repo with the action once per group', () => {
    render(
      <DeployList
        rows={[mk('nakomis/a', 'A-1'), mk('nakomis/a', 'A-2'), mk('nakomis/b', 'B-1')]}
      />,
    );
    expect(screen.getAllByRole('heading', { level: 3 })).toHaveLength(2);
    expect(screen.getAllByText('deploy nakomis/a')).toHaveLength(1);
    expect(screen.getByText(/A-2: Title A-2/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /B-1/ })).toHaveAttribute('href', 'https://gh/B-1');
  });
  it('renders unsafe URLs as text and opens good ones safely', () => {
    const { unmount } = render(
      <DeployList rows={[{ ...mk('nakomis/a', 'A-1'), pr_url: 'javascript:alert(1)' }]} />,
    );
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.getByText(/A-1: Title A-1/)).toBeInTheDocument();
    unmount();
    render(<DeployList rows={[mk('nakomis/a', 'A-1')]} />);
    expect(screen.getByRole('link')).toHaveAttribute('rel', 'noopener noreferrer');
  });

  it('says so when empty', () => {
    render(<DeployList rows={[]} />);
    expect(screen.getByText('Nothing to deploy.')).toBeInTheDocument();
  });
});
