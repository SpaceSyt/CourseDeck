import type { Course, Source } from './types';

export function sourceSyncStatus(
  source: Pick<Source, 'status' | 'syncing' | 'last_outcome' | 'warnings'>,
) {
  const yellow = (label: string) => ({ color: '#d8b370', label });
  if (source.syncing) return yellow('Syncing…');
  if (source.status === 'login_pending') return yellow('Finish login');
  if (source.status === 'not_configured') return yellow('Not configured');
  if (source.last_outcome && !['success', 'partial'].includes(source.last_outcome))
    return { color: '#ee897e', label: `Sync failed: ${source.last_outcome.replaceAll('_', ' ')}` };
  if (source.status === 'not_connected') return yellow('Not connected');
  if (source.status !== 'connected')
    return { color: '#ee897e', label: source.status.replaceAll('_', ' ') };
  if (source.last_outcome === 'partial') return yellow('Partial sync');
  if (source.warnings?.length) return yellow('Sync needs attention');
  if (source.last_outcome === 'success') return { color: '#72c89b', label: 'Last sync succeeded' };
  return yellow('Not synced yet');
}

export function courseColor(course?: Course) {
  if (!course) return '#b7bdc8';
  if (course.color && /^#[0-9a-f]{6}$/i.test(course.color)) return course.color;
  // Source rows inherit their local group key, so its default color is shared too.
  const key = course.workspace_id || course.id;
  let hash = 0;
  for (const character of key) hash = ((hash << 5) - hash + character.charCodeAt(0)) | 0;
  const hue = Math.abs(hash) % 360;
  const lightness = (64 + (Math.abs(hash) % 9)) / 100;
  const amplitude = 0.58 * Math.min(lightness, 1 - lightness);
  const channel = (offset: number) => {
    const part = (offset + hue / 30) % 12;
    const value = lightness - amplitude * Math.max(-1, Math.min(part - 3, 9 - part, 1));
    return Math.round(255 * value)
      .toString(16)
      .padStart(2, '0');
  };
  return `#${channel(0)}${channel(8)}${channel(4)}`;
}
