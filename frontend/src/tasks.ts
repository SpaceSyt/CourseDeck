import type { Task } from './types';

export const completed = (task: Task) =>
  task.local.dismissed || ['submitted', 'graded', 'returned'].includes(task.submission_status);
export function dayKey(date: Date, zone: string) {
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: zone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(date);
}
export function dayNumber(date: Date, zone: string) {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: zone,
    year: 'numeric',
    month: 'numeric',
    day: 'numeric',
  }).formatToParts(date);
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
