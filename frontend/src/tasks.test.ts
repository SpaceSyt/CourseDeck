import { describe, expect, it } from 'vitest';
import { bucket, dayNumber } from './tasks';
import type { Task } from './types';

const task = (due: string | null, dismissed = false) =>
  ({ due_at: due, submission_status: 'open', local: { dismissed } }) as Task;
describe('deadline grouping', () => {
  it('uses the chosen timezone across UTC midnight', () => {
    const now = new Date('2026-09-05T02:00:00Z');
    expect(bucket(task('2026-09-05T03:59:00Z'), 'America/New_York', now)).toBe('Today');
    expect(bucket(task('2026-09-05T05:00:00Z'), 'America/New_York', now)).toBe('Tomorrow');
  });
  it('uses calendar days across DST, not elapsed 24h intervals', () => {
    const now = new Date('2026-11-01T04:30:00Z');
    expect(
      dayNumber(new Date('2026-11-02T05:30:00Z'), 'America/New_York') -
        dayNumber(now, 'America/New_York'),
    ).toBe(1);
  });
  it('shows a passed deadline today as overdue and keeps undated work visible', () => {
    const now = new Date('2026-09-05T18:00:00Z');
    expect(bucket(task('2026-09-05T16:00:00Z'), 'America/New_York', now)).toBe('Overdue');
    expect(bucket(task('2026-09-05T16:00:00Z', true), 'America/New_York', now)).toBe('Today');
    expect(bucket(task(null), 'America/New_York', now)).toBe('No due date');
  });
});
