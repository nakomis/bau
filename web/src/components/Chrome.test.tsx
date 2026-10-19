import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import Footer from './Footer';
import Header from './Header';
import TierBanner from './TierBanner';

const gen = new Date(Date.now() - 12 * 60_000).toISOString();

describe('Header', () => {
  it('shows the tier, age, stale and paused badges', () => {
    render(<Header tier="red" generated={gen} stale paused />);
    expect(screen.getByText('Red')).toBeInTheDocument();
    expect(screen.getByText('updated 12 minutes ago')).toHaveAttribute('title', gen);
    expect(screen.getByText('stale')).toBeInTheDocument();
    expect(screen.getByText('PAUSED')).toBeInTheDocument();
  });

  it('shows no tier while loading', () => {
    render(<Header tier={null} generated={undefined} stale={false} paused={false} />);
    expect(screen.queryByText(/green|amber|red/i)).not.toBeInTheDocument();
    expect(screen.queryByText('stale')).not.toBeInTheDocument();
  });
});

describe('TierBanner', () => {
  it('says all clear with no reasons', () => {
    render(<TierBanner result={{ tier: 'green', reasons: [] }} openPrs={1} />);
    expect(screen.getByText('All clear')).toBeInTheDocument();
    expect(screen.getByText('1 open PR awaiting review')).toBeInTheDocument();
    expect(screen.queryByRole('listitem')).not.toBeInTheDocument();
  });

  it('lists every reason', () => {
    render(
      <TierBanner
        result={{
          tier: 'red',
          reasons: [
            { tier: 'red', text: 'The last run failed (rejected)' },
            { tier: 'amber', text: 'Usage telemetry is missing' },
          ],
        }}
        openPrs={12}
      />,
    );
    expect(screen.getByText('Needs attention')).toBeInTheDocument();
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
  });
});

describe('Footer', () => {
  it('renders', () => {
    render(<Footer />);
    expect(screen.getByText('Nakomis BAU')).toBeInTheDocument();
  });
});
