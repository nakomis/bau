import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { OpenPr } from '../types';
import PrTable from './PrTable';

const row: OpenPr = {
  repo: 'nakomis/home-infra',
  ref: 'HOME-42',
  title: 'Rotate logs',
  url: 'https://gh/pull/7',
  opened: '2026-10-15',
  age_days: 0,
  ci: 'green',
  draft: false,
  unresolved_comments: 2,
  plane_url: 'https://plane/HOME-42',
};

describe('PrTable', () => {
  it('renders a row with age, CI and links', () => {
    render(<PrTable rows={[row]} onAbandon={vi.fn()} />);
    expect(screen.getByText('today')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /HOME-42/ })).toHaveAttribute(
      'href',
      'https://gh/pull/7',
    );
    expect(screen.getByRole('link', { name: /Plane/ })).toHaveAttribute(
      'href',
      'https://plane/HOME-42',
    );
    expect(screen.getByText('2 unresolved')).toBeInTheDocument();
    expect(screen.getByTitle('green')).toBeInTheDocument();
  });

  it('marks drafts and shows a dash when there are no comments', () => {
    render(
      <PrTable rows={[{ ...row, draft: true, unresolved_comments: 0 }]} onAbandon={vi.fn()} />,
    );
    expect(screen.getByText('draft')).toBeInTheDocument();
    expect(screen.queryByText(/unresolved/)).not.toBeInTheDocument();
  });

  it('says so when empty', () => {
    render(<PrTable rows={[]} onAbandon={vi.fn()} />);
    expect(screen.getByText(/nothing awaiting review/i)).toBeInTheDocument();
  });

  describe('abandon', () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    it('needs a second click, and reverts after five seconds', () => {
      const onAbandon = vi.fn();
      render(<PrTable rows={[row]} onAbandon={onAbandon} />);
      fireEvent.click(screen.getByRole('button', { name: 'Abandon' }));
      expect(screen.getByRole('button', { name: 'Confirm abandon' })).toBeInTheDocument();
      expect(screen.getByLabelText(/note/i)).toHaveAttribute('maxlength', '500');
      act(() => {
        vi.advanceTimersByTime(5000);
      });
      expect(screen.getByRole('button', { name: 'Abandon' })).toBeInTheDocument();
      expect(screen.queryByLabelText(/note/i)).not.toBeInTheDocument();
      expect(onAbandon).not.toHaveBeenCalled();
    });

    it('sends the note on confirm and shows the restored state', async () => {
      const onAbandon = vi.fn().mockResolvedValue('Todo');
      render(<PrTable rows={[row]} onAbandon={onAbandon} />);
      fireEvent.click(screen.getByRole('button', { name: 'Abandon' }));
      fireEvent.change(screen.getByLabelText(/note/i), { target: { value: 'wrong approach' } });
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Confirm abandon' }));
      });
      expect(onAbandon).toHaveBeenCalledWith('HOME-42', 'wrong approach');
      expect(screen.getByText('restored to Todo')).toBeInTheDocument();
    });

    it('shows the error text on failure and offers another go', async () => {
      const onAbandon = vi.fn().mockRejectedValue(new Error('run in progress'));
      render(<PrTable rows={[row]} onAbandon={onAbandon} />);
      fireEvent.click(screen.getByRole('button', { name: 'Abandon' }));
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Confirm abandon' }));
      });
      expect(screen.getByText('run in progress')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Abandon' })).toBeInTheDocument();
    });
  });
});
