import { describe, expect, it } from 'vitest';
import { bucket, completed, dayNumber, sourceWarning } from './tasks';
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

describe('source completion and uncertainty', () => {
  it('keeps local open/done overrides distinct from source uncertainty', () => {
    const unknown = { ...task(null), source_status_known: false, submission_status: 'unknown' };
    expect(
      completed({ ...unknown, local: { ...unknown.local, completion_override: 'done' } }),
    ).toBe(true);
    expect(sourceWarning(unknown)).toBe('Status unknown');
    const submitted = { ...task(null), source_status_known: true, submission_status: 'submitted' };
    expect(
      completed({ ...submitted, local: { ...submitted.local, completion_override: 'open' } }),
    ).toBe(false);
  });
  it('moves confirmed source submissions into done', () => {
    for (const submission_status of ['submitted', 'graded', 'returned', 'completed']) {
      expect(completed({ ...task(null), submission_status, source_status_known: true })).toBe(true);
    }
  });
  it('keeps missing and stale submissions visible without claiming completion', () => {
    const submitted = { ...task(null), submission_status: 'submitted' };
    const missing: Task = { ...submitted, source_availability: 'missing' };
    expect(completed(missing)).toBe(false);
    expect(sourceWarning(missing)).toBe('Not found in source');
    const unconfirmed: Task = { ...submitted, source_availability: 'unconfirmed' };
    expect(completed(unconfirmed)).toBe(false);
    expect(sourceWarning(unconfirmed)).toBe('Not refreshed');
    expect(sourceWarning({ ...unconfirmed, missing_count: 2 })).toBe('Not refreshed');
    const stale = { ...submitted, source_status_known: false };
    expect(completed(stale)).toBe(false);
    expect(sourceWarning(stale)).toBe('Status unknown');
    expect(completed({ ...submitted, missing_count: 1 })).toBe(false);
    expect(
      completed({ ...submitted, raw_data: { unavailable_fields: ['submission_status'] } }),
    ).toBe(false);
  });
  it('preserves explicit local completion and does not flag custom tasks', () => {
    const unknown = { ...task(null, true), submission_status: 'unknown' };
    expect(completed(unknown)).toBe(true);
    expect(sourceWarning(unknown)).toBe('Status unknown');
    expect(sourceWarning({ ...unknown, provider: 'custom' })).toBe('');
  });
});
