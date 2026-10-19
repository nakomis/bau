import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import App from './App';
import type { Snapshot } from './types';

const NOW = Date.parse('2026-10-14T12:00:00Z');

const snap: Snapshot = {
  schema: 1,
  generated_at: '2026-10-14T11:48:00Z',
  paused: false,
  last_run: null,
  pace: null,
  open_prs: [
    {
      repo: 'nakomis/home-infra',
      ref: 'HOME-42',
      title: 'Rotate logs',
      url: 'https://github.com/nakomis/home-infra/pull/7',
      opened: '2026-10-13',
      age_days: 1,
      ci: 'green',
      draft: false,
      unresolved_comments: 0,
      plane_url: 'https://plane.home.nakomis.com/nakomis/browse/HOME-42/',
    },
  ],
  deploy_pending: [
    {
      repo: 'nakomis/home-infra',
      ref: 'HOME-40',
      title: 'Old',
      pr_url: 'https://github.com/nakomis/home-infra/pull/6',
      merged_at: 'x',
      action: 'make apply',
    },
  ],
};

function respond(body: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => body };
}

async function mount() {
  await act(async () => {
    render(<App />);
  });
}

describe('App', () => {
  beforeEach(() => {
    vi.useFakeTimers({
      now: NOW,
      toFake: ['Date', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'],
    });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('renders the three sections, the PR and a relative updated time', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(respond(snap)));
    await mount();
    expect(screen.getByRole('heading', { name: /awaiting review/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /to deploy/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /last run/i })).toBeInTheDocument();
    expect(screen.getByText(/Rotate logs/)).toBeInTheDocument();
    const updated = screen.getByText('updated 12 minutes ago');
    expect(updated).toHaveAttribute('title', '2026-10-14T11:48:00Z');
    expect(screen.queryByText('stale')).not.toBeInTheDocument();
    expect(screen.getByText(/usage figures unavailable/i)).toBeInTheDocument();
    expect(screen.getByText(/no run recorded yet/i)).toBeInTheDocument();
  });

  it('shows a stale badge beyond 26 hours', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(respond({ ...snap, generated_at: '2026-10-13T08:00:00Z' })),
    );
    await mount();
    expect(screen.getByText('stale')).toBeInTheDocument();
  });

  it('shows the overall tier and groups PRs by repository', async () => {
    const other = {
      ...snap.open_prs?.[0],
      repo: 'nakomis/bau',
      ref: 'BAU-1',
      title: 'Other',
      ci: 'red',
    };
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(respond({ ...snap, open_prs: [...(snap.open_prs ?? []), other] })),
    );
    await mount();
    expect(screen.getByText('Green')).toBeInTheDocument();
    expect(screen.getByText('All clear')).toBeInTheDocument();
    const repos = screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent);
    expect(repos[0]).toMatch(/^bau/);
    expect(repos[1]).toMatch(/^home-infra/);
  });

  it('is red and says why when the last run failed', async () => {
    const last_run = {
      started: '2026-10-14T01:00:00Z',
      ended: '2026-10-14T02:00:00Z',
      sessions: 1,
      prs_opened: 0,
      feedback_handled: 0,
      stop_reason: 'failed: SystemExit: 143',
      telemetry: 'ok',
    };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(respond({ ...snap, last_run })));
    await mount();
    expect(screen.getByText('Red')).toBeInTheDocument();
    expect(screen.getByText(/The last run failed \(failed: SystemExit: 143\)/)).toBeInTheDocument();
  });

  it('is red when the state cannot be loaded', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));
    await mount();
    expect(screen.getByText('Red')).toBeInTheDocument();
  });

  it('refetches when the tab becomes visible again', async () => {
    const f = vi.fn().mockResolvedValue(respond(snap));
    vi.stubGlobal('fetch', f);
    await mount();
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    expect(f).toHaveBeenCalledTimes(2);
  });

  it('says there is no snapshot yet', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(respond({ schema: 1, missing: true })));
    await mount();
    expect(screen.getByText(/no snapshot yet/i)).toBeInTheDocument();
  });

  it('renders a bare snapshot with absent arrays', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(respond({ schema: 1 })));
    await mount();
    expect(screen.getByText(/nothing awaiting review/i)).toBeInTheDocument();
    expect(screen.getByText('Nothing to deploy.')).toBeInTheDocument();
  });

  it('shows paused prominently', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(respond({ ...snap, paused: true })));
    await mount();
    expect(screen.getAllByText(/paused/i).length).toBeGreaterThan(0);
  });

  it('reports a fetch failure without crashing', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('offline')));
    await mount();
    expect(screen.getByText(/could not load state/i)).toBeInTheDocument();
  });

  it('refetches every five minutes and stops on unmount', async () => {
    const f = vi.fn().mockResolvedValue(respond(snap));
    vi.stubGlobal('fetch', f);
    let unmount = () => {};
    await act(async () => {
      unmount = render(<App />).unmount;
    });
    expect(f).toHaveBeenCalledTimes(1);
    await act(async () => {
      vi.advanceTimersByTime(300_000);
    });
    expect(f).toHaveBeenCalledTimes(2);
    unmount();
    await act(async () => {
      vi.advanceTimersByTime(600_000);
    });
    expect(f).toHaveBeenCalledTimes(2);
  });

  it('abandons in two steps, shows the result and refetches', async () => {
    const f = vi.fn((url: string, init?: RequestInit) => {
      if (url === '/state.json') return Promise.resolve(respond(snap));
      expect(url).toBe('/abandon/HOME-42');
      expect(init?.method).toBe('POST');
      return Promise.resolve(respond({ ref: 'HOME-42', pr: 7, restored_state: 'Todo' }));
    });
    vi.stubGlobal('fetch', f);
    await mount();
    fireEvent.click(screen.getByRole('button', { name: 'Abandon' }));
    expect(f).toHaveBeenCalledTimes(1);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Confirm abandon' }));
    });
    expect(f).toHaveBeenCalledTimes(3);
    expect(screen.getAllByText(/restored to Todo/).length).toBeGreaterThan(0);
  });

  it('shows the API error text when abandon is refused', async () => {
    const f = vi.fn((url: string) =>
      url === '/state.json'
        ? Promise.resolve(respond(snap))
        : Promise.resolve(respond({ error: 'a run is in progress' }, false, 409)),
    );
    vi.stubGlobal('fetch', f);
    await mount();
    fireEvent.click(screen.getByRole('button', { name: 'Abandon' }));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Confirm abandon' }));
    });
    expect(screen.getByText('a run is in progress')).toBeInTheDocument();
  });

  it('keeps only the latest five notices', async () => {
    const prs = Array.from({ length: 7 }, (_, i) => ({
      ...snap.open_prs?.[0],
      ref: `HOME-${i + 1}`,
      title: `T${i + 1}`,
    }));
    const f = vi.fn((url: string) =>
      url === '/state.json'
        ? Promise.resolve(respond({ ...snap, open_prs: prs }))
        : Promise.resolve(respond({ restored_state: 'Todo' })),
    );
    vi.stubGlobal('fetch', f);
    await mount();
    for (let i = 1; i <= 7; i++) {
      fireEvent.click(screen.getAllByRole('button', { name: 'Abandon' })[0]);
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Confirm abandon' }));
      });
    }
    expect(within(screen.getByRole('status')).getAllByRole('listitem')).toHaveLength(5);
    expect(screen.queryByText('HOME-1: restored to Todo')).not.toBeInTheDocument();
    expect(screen.getByText('HOME-7: restored to Todo')).toBeInTheDocument();
  });

  const qrow = {
    repo: 'nakomis/home-infra',
    ref: 'HOME-9',
    title: 'Ask me',
    plane_url: 'https://plane/HOME-9',
    repo_url: 'https://github.com/nakomis/home-infra',
    asked_at: '2026-10-13T01:00:00Z',
    age_days: 1,
    round: 1,
    summary: 'Need a decision',
    questions: [{ id: 'q1', text: 'Which?', recommendation: 'A', why: 'Because' }],
  };

  it('shows the questions section above open PRs and hides it when empty', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(respond({ ...snap, schema: 2, questions: [qrow] })),
    );
    await mount();
    const q = screen.getByRole('heading', { name: /Questions \(1\)/ });
    const prs = screen.getByRole('heading', { name: /awaiting review/i });
    expect(q.compareDocumentPosition(prs) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('has no questions heading when there are none', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(respond(snap)));
    await mount();
    expect(screen.queryByRole('heading', { name: /Questions/ })).not.toBeInTheDocument();
  });

  it('sends answers, shows a notice and refetches', async () => {
    const f = vi.fn((url: string) =>
      url === '/state.json'
        ? Promise.resolve(respond({ ...snap, schema: 2, questions: [qrow] }))
        : Promise.resolve(respond({ ref: 'HOME-9' })),
    );
    vi.stubGlobal('fetch', f);
    await mount();
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Send to BAU' }));
    });
    const post = f.mock.calls.find((c) => c[0] === '/answers/HOME-9');
    expect(post).toBeDefined();
    expect(screen.getByText('HOME-9: sent to BAU')).toBeInTheDocument();
    expect(f.mock.calls.filter((c) => c[0] === '/state.json')).toHaveLength(2);
  });

  it('marks not-bau with a notice', async () => {
    const f = vi.fn((url: string) =>
      url === '/state.json'
        ? Promise.resolve(respond({ ...snap, schema: 2, questions: [qrow] }))
        : Promise.resolve(respond({ ref: 'HOME-9' })),
    );
    vi.stubGlobal('fetch', f);
    await mount();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Not BAU' }));
    });
    expect(f.mock.calls.some((c) => c[0] === '/not-bau/HOME-9')).toBe(true);
    expect(screen.getByText('HOME-9: marked not-bau')).toBeInTheDocument();
  });

  it('lists repos waiting for the GitHub App even with no questions', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(respond({ ...snap, uninstalled: ['nakomis/a', 'nakomis/b'] })),
    );
    await mount();
    expect(screen.queryByRole('heading', { name: /Questions/ })).not.toBeInTheDocument();
    expect(
      screen.getByText('Waiting for the GitHub App: nakomis/a, nakomis/b'),
    ).toBeInTheDocument();
  });

  it('refetches after a refused send so a new round appears', async () => {
    const f = vi.fn((url: string) =>
      url === '/state.json'
        ? Promise.resolve(respond({ ...snap, schema: 2, questions: [qrow] }))
        : Promise.resolve(respond({ error: 'the questions have changed' }, false, 409)),
    );
    vi.stubGlobal('fetch', f);
    await mount();
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Send to BAU' }));
    });
    expect(screen.getByText('the questions have changed')).toBeInTheDocument();
    expect(f.mock.calls.filter((c) => c[0] === '/state.json')).toHaveLength(2);
  });
});
