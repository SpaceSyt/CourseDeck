import type { Task } from './types';
import taskContract from './task-contract.json';

export const completed = (task: Task) =>
  task.local.completion_override
    ? task.local.completion_override === 'done'
    : task.local.dismissed ||
      (!sourceWarning(task) &&
        (task.source_state !== undefined
          ? task.source_state === 'done'
          : taskContract.done_statuses.includes(task.submission_status)));
export function sourceWarning(task: Task) {
  if (task.provider === 'custom') return '';
  if (
    task.source_availability === 'missing' ||
    (!task.source_availability && task.missing_count > 0)
  )
    return 'Not found in source';
  if (task.source_availability === 'unconfirmed') return 'Not refreshed';
  if (
    task.source_status_known === false ||
    !task.submission_status ||
    task.submission_status === 'unknown' ||
    task.raw_data?.unavailable_fields?.includes('submission_status')
  )
    return 'Status unknown';
  return '';
}
let calendarFormats:
  | { zone: string; key: Intl.DateTimeFormat; number: Intl.DateTimeFormat }
  | undefined;

function formats(zone: string) {
  // The workspace uses one timezone; retain only its formatters, not formatted dates.
  if (calendarFormats?.zone !== zone) {
    calendarFormats = {
      zone,
      key: new Intl.DateTimeFormat('en-CA', {
        timeZone: zone,
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
      }),
      number: new Intl.DateTimeFormat('en-US', {
        timeZone: zone,
        year: 'numeric',
        month: 'numeric',
        day: 'numeric',
      }),
    };
  }
  return calendarFormats;
}

export function dayKey(date: Date, zone: string) {
  return formats(zone).key.format(date);
}
export function dayNumber(date: Date, zone: string) {
  const parts = formats(zone).number.formatToParts(date);
  const value = (key: string) => Number(parts.find((p) => p.type === key)?.value);
  return Date.UTC(value('year'), value('month') - 1, value('day')) / 86400000;
}
export function bucket(task: Task, zone: string, clock: Date) {
  if (!task.due_at) return 'No due date';
  const due = new Date(task.due_at),
    diff = dayNumber(due, zone) - dayNumber(clock, zone);
  if (due < clock && !completed(task)) return 'Overdue';
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Tomorrow';
  if (diff >= 2 && diff < 7) return 'Next 7 days';
  return 'Later';
}
