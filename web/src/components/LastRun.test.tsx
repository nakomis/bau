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
  const base = {
    started: '2026-10-15T01:00:00Z',
    ended: '2026-10-15T02:00:00Z',
    sessions: 1,
    prs_opened: 0,
    feedback_handled: 0,
    stop_reason: 'cutoff',
    telemetry: 'ok',
  };
  it('warns when telemetry is missing', () => {
    render(<LastRun run={{ ...base, telemetry: 'missing' }} pace={null} paused={false} />);
    expect(
      screen.getByText('Telemetry missing: fail-closed, at most two sessions'),
    ).toBeInTheDocument();
  });
  it('does not warn about telemetry when it is present', () => {
    render(<LastRun run={base} pace={null} paused={false} />);
    expect(screen.queryByText(/fail-closed/i)).not.toBeInTheDocument();
    expect(screen.queryByText('Last run failed')).not.toBeInTheDocument();
  });
  it.each(['failed: RuntimeError: boom', 'preflight: docker down', 'rejected'])(
    'shows a failure badge for stop_reason %s',
    (stop_reason) => {
      render(<LastRun run={{ ...base, stop_reason }} pace={null} paused={false} />);
      expect(screen.getByText('Last run failed')).toBeInTheDocument();
    },
  );
  it('renders relative times with the absolute value in the title', () => {
    const iso = new Date(Date.now() - 3 * 3_600_000).toISOString();
    render(<LastRun run={{ ...base, started: iso, ended: iso }} pace={null} paused={false} />);
    const els = screen.getAllByText('3 hours ago');
    expect(els).toHaveLength(2);
    expect(els[0]).toHaveAttribute('title', iso);
  });
});
