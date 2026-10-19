import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { QuestionRow } from '../types';
import Questions from './Questions';

const mk = (ref: string, age: number, extra: Partial<QuestionRow> = {}): QuestionRow => ({
  repo: 'nakomis/home-infra',
  ref,
  title: `Title ${ref}`,
  plane_url: `https://plane.home.nakomis.com/nakomis/browse/${ref}/`,
  repo_url: 'https://github.com/nakomis/home-infra',
  asked_at: '2026-10-14T01:00:00Z',
  age_days: age,
  round: 1,
  summary: `Summary ${ref}`,
  questions: [
    { id: 'q1', text: `First ${ref}?`, recommendation: `Rec1 ${ref}`, why: `Why1 ${ref}` },
    { id: 'q2', text: `Second ${ref}?`, recommendation: `Rec2 ${ref}`, why: `Why2 ${ref}` },
  ],
  ...extra,
});

function setup(rows: QuestionRow[], level: 'ok' | 'warn' | 'down' = 'ok') {
  const onSend = vi.fn().mockResolvedValue(undefined);
  const onNotBau = vi.fn().mockResolvedValue(undefined);
  const view = render(<Questions rows={rows} level={level} onSend={onSend} onNotBau={onNotBau} />);
  const update = (next: QuestionRow[]) =>
    view.rerender(<Questions rows={next} level={level} onSend={onSend} onNotBau={onNotBau} />);
  return { onSend, onNotBau, update, ...view };
}

const accepts = () => screen.getAllByRole('button', { name: 'Accept' });
const send = () => screen.getByRole('button', { name: 'Send to BAU' });

describe('Questions', () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.restoreAllMocks());

  it('renders recommendation and why', () => {
    setup([mk('HOME-1', 4)]);
    expect(screen.getByRole('heading', { name: /Questions \(1\)/ })).toBeInTheDocument();
    expect(screen.getByText('Summary HOME-1')).toBeInTheDocument();
    expect(screen.getByText('First HOME-1?')).toBeInTheDocument();
    expect(screen.getByText(/Rec1 HOME-1/)).toBeInTheDocument();
    expect(screen.getByText(/Why1 HOME-1/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Plane/ })).toHaveAttribute(
      'href',
      'https://plane.home.nakomis.com/nakomis/browse/HOME-1/',
    );
    expect(screen.getByRole('link', { name: /Repo/ })).toHaveAttribute(
      'href',
      'https://github.com/nakomis/home-infra',
    );
  });

  it('renders nothing for no rows', () => {
    const { container } = setup([]);
    expect(container).toBeEmptyDOMElement();
  });

  it('shows an age badge only when warn or down', () => {
    const { unmount } = setup([mk('HOME-1', 4), mk('HOME-2', 9)], 'warn');
    expect(screen.getByText('oldest 9 days')).toBeInTheDocument();
    unmount();
    setup([mk('HOME-1', 4)], 'ok');
    expect(screen.queryByText(/oldest/)).not.toBeInTheDocument();
  });

  it('send disabled until every question answered', () => {
    setup([mk('HOME-1', 1)]);
    expect(send()).toBeDisabled();
    fireEvent.click(accepts()[0]);
    expect(send()).toBeDisabled();
    fireEvent.change(screen.getAllByPlaceholderText('…or write your own')[1], {
      target: { value: '   ' },
    });
    expect(send()).toBeDisabled();
    fireEvent.change(screen.getAllByPlaceholderText('…or write your own')[1], {
      target: { value: 'mine' },
    });
    expect(send()).toBeEnabled();
  });

  it('accept then send posts accepted answers', async () => {
    const { onSend } = setup([mk('HOME-1', 1)]);
    for (const b of accepts()) fireEvent.click(b);
    await act(async () => {
      fireEvent.click(send());
    });
    expect(onSend).toHaveBeenCalledWith('HOME-1', 1, '2026-10-14T01:00:00Z', [
      { id: 'q1', accepted: true, text: 'Rec1 HOME-1' },
      { id: 'q2', accepted: true, text: 'Rec2 HOME-1' },
    ]);
  });

  it('typed answer overrides recommendation, keeps line breaks, and accepting clears it', async () => {
    const { onSend } = setup([mk('HOME-1', 1)]);
    const boxes = screen.getAllByPlaceholderText('…or write your own');
    fireEvent.click(accepts()[0]);
    fireEvent.change(boxes[0], { target: { value: 'line one\nline two' } });
    fireEvent.click(accepts()[1]);
    fireEvent.change(boxes[1], { target: { value: 'typed' } });
    fireEvent.click(accepts()[1]);
    expect(boxes[1]).toHaveValue('');
    await act(async () => {
      fireEvent.click(send());
    });
    expect(onSend.mock.calls[0][3]).toEqual([
      { id: 'q1', accepted: false, text: 'line one\nline two' },
      { id: 'q2', accepted: true, text: 'Rec2 HOME-1' },
    ]);
  });

  it('send removes the card and opens the next', async () => {
    const rows = [
      mk('HOME-1', 5),
      mk('HOME-2', 4),
      mk('HOME-3', 3),
      mk('HOME-4', 2),
      mk('HOME-5', 1),
    ];
    setup(rows);
    expect(screen.queryByText('First HOME-2?')).not.toBeInTheDocument();
    for (const b of accepts()) fireEvent.click(b);
    await act(async () => {
      fireEvent.click(send());
    });
    expect(screen.queryByText('Title HOME-1', { exact: false })).not.toBeInTheDocument();
    expect(screen.getByText('First HOME-2?')).toBeInTheDocument();
    expect(screen.queryByText('First HOME-3?')).not.toBeInTheDocument();
  });

  it('shows the error and keeps the card when send fails', async () => {
    const { onSend } = setup([mk('HOME-1', 1)]);
    onSend.mockRejectedValue(new Error('a run is in progress'));
    for (const b of accepts()) fireEvent.click(b);
    await act(async () => {
      fireEvent.click(send());
    });
    expect(screen.getByText('a run is in progress')).toBeInTheDocument();
    expect(screen.getByText('First HOME-1?')).toBeInTheDocument();
  });

  it('not BAU removes the card and opens the next', async () => {
    const rows = [mk('HOME-1', 5), mk('HOME-2', 4), mk('HOME-3', 3), mk('HOME-4', 2)];
    const { onNotBau } = setup(rows);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Not BAU' }));
    });
    expect(onNotBau).toHaveBeenCalledWith('HOME-1');
    expect(screen.queryByText('Title HOME-1', { exact: false })).not.toBeInTheDocument();
    expect(screen.getByText('First HOME-2?')).toBeInTheDocument();
  });

  it('shows the error when not BAU fails', async () => {
    const { onNotBau } = setup([mk('HOME-1', 1)]);
    onNotBau.mockRejectedValue(new Error('nope'));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Not BAU' }));
    });
    expect(screen.getByText('nope')).toBeInTheDocument();
  });

  it('three rows start all open', () => {
    setup([mk('HOME-1', 3), mk('HOME-2', 2), mk('HOME-3', 1)]);
    expect(screen.getByText('First HOME-1?')).toBeInTheDocument();
    expect(screen.getByText('First HOME-2?')).toBeInTheDocument();
    expect(screen.getByText('First HOME-3?')).toBeInTheDocument();
  });

  it('more than three rows start with only the first (oldest) open', () => {
    setup([mk('HOME-1', 1), mk('HOME-2', 9), mk('HOME-3', 3), mk('HOME-4', 2)]);
    expect(screen.getByText('First HOME-2?')).toBeInTheDocument();
    expect(screen.queryByText('First HOME-1?')).not.toBeInTheDocument();
    expect(screen.queryByText('First HOME-3?')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /HOME-3/ }));
    expect(screen.getByText('First HOME-3?')).toBeInTheDocument();
  });

  it('drafts survive a remount and clear on send', async () => {
    const first = setup([mk('HOME-1', 1)]);
    fireEvent.click(accepts()[0]);
    fireEvent.change(screen.getAllByPlaceholderText('…or write your own')[1], {
      target: { value: 'half done' },
    });
    first.unmount();
    const second = setup([mk('HOME-1', 1)]);
    expect(screen.getAllByPlaceholderText('…or write your own')[1]).toHaveValue('half done');
    expect(send()).toBeEnabled();
    await act(async () => {
      fireEvent.click(send());
    });
    expect(localStorage.getItem('bau.drafts') ?? '{}').not.toContain('half done');
    second.unmount();
    setup([mk('HOME-1', 1)]);
    expect(screen.getAllByPlaceholderText('…or write your own')[1]).toHaveValue('');
  });

  it('a new round of the same ref does not inherit the old draft', () => {
    const first = setup([mk('HOME-1', 1)]);
    fireEvent.change(screen.getAllByPlaceholderText('…or write your own')[0], {
      target: { value: 'old' },
    });
    first.unmount();
    setup([mk('HOME-1', 1, { round: 2 })]);
    expect(screen.getAllByPlaceholderText('…or write your own')[0]).toHaveValue('');
  });

  it('localStorage throwing does not break the page', async () => {
    vi.spyOn(localStorage, 'getItem').mockImplementation(() => {
      throw new Error('denied');
    });
    vi.spyOn(localStorage, 'setItem').mockImplementation(() => {
      throw new Error('denied');
    });
    const { onSend } = setup([mk('HOME-1', 1)]);
    for (const b of accepts()) fireEvent.click(b);
    await act(async () => {
      fireEvent.click(send());
    });
    expect(onSend).toHaveBeenCalled();
  });

  it('ignores corrupt stored drafts', () => {
    localStorage.setItem('bau.drafts', '{not json');
    setup([mk('HOME-1', 1)]);
    expect(screen.getByText('First HOME-1?')).toBeInTheDocument();
  });

  it('a card typed in stays open when a refresh adds rows', () => {
    const three = [mk('HOME-1', 3), mk('HOME-2', 2), mk('HOME-3', 1)];
    const { update } = setup(three);
    fireEvent.change(screen.getAllByPlaceholderText('…or write your own')[4], {
      target: { value: 'typing' },
    });
    update([mk('HOME-0', 9), ...three]);
    expect(screen.getByText('First HOME-3?')).toBeInTheDocument();
    expect(screen.queryByText('First HOME-2?')).not.toBeInTheDocument();
  });

  it('a card focused stays open when a refresh adds rows', () => {
    const three = [mk('HOME-1', 3), mk('HOME-2', 2), mk('HOME-3', 1)];
    const { update } = setup(three);
    fireEvent.focus(screen.getAllByPlaceholderText('…or write your own')[2]);
    update([mk('HOME-0', 9), ...three]);
    expect(screen.getByText('First HOME-2?')).toBeInTheDocument();
  });

  it('clicking a closed card opens it (focus does not fight the toggle)', () => {
    setup([mk('HOME-1', 4), mk('HOME-2', 3), mk('HOME-3', 2), mk('HOME-4', 1)]);
    const header = screen.getByRole('button', { name: /HOME-2/ });
    fireEvent.focus(header);
    fireEvent.click(header);
    expect(screen.getByText('First HOME-2?')).toBeInTheDocument();
  });
});
