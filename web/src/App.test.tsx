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
      url: 'https://gh/pull/7',
      opened: '2026-10-13',
      age_days: 1,
      ci: 'green',
      draft: false,
      unresolved_comments: 0,
      plane_url: 'https://plane/HOME-42',
    },
  ],
  deploy_pending: [
    {
      repo: 'nakomis/home-infra',
      ref: 'HOME-40',
      title: 'Old',
      pr_url: 'https://gh/6',
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
});
