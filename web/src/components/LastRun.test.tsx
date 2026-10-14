import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import LastRun from './LastRun';

describe('LastRun', () => {
  it('handles a missing pace block', () => {
    render(
      <LastRun
        run={{
          started: 's',
          ended: 'e',
          sessions: 1,
          prs_opened: 1,
          feedback_handled: 0,
          stop_reason: 'cutoff',
          telemetry: 'missing',
        }}
        pace={null}
        paused={false}
      />,
    );
    expect(
      screen.getByText(/usage figures unavailable \(telemetry missing\)/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/stopped: cutoff/i)).toBeInTheDocument();
    expect(screen.queryByText(/paused/i)).not.toBeInTheDocument();
  });
  it('shows pace and paused when present, and copes with no run', () => {
    render(
      <LastRun
        run={null}
        pace={{
          five_hour_used: 0.2,
          five_hour_resets: 0,
          seven_day_used: 0.3,
          seven_day_resets: 0,
          elapsed_pct: 0.5,
        }}
        paused={true}
      />,
    );
    expect(screen.getByText(/weekly 30% used, 50% of the week elapsed/i)).toBeInTheDocument();
    expect(screen.getByText(/paused/i)).toBeInTheDocument();
    expect(screen.getByText(/no run recorded yet/i)).toBeInTheDocument();
  });
  it('treats an undefined run and pace as absent', () => {
    render(<LastRun run={undefined} pace={undefined} paused={false} />);
    expect(screen.getByText(/no run recorded yet/i)).toBeInTheDocument();
    expect(screen.getByText(/usage figures unavailable/i)).toBeInTheDocument();
  });
});
