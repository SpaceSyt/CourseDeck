import type { Course } from './types';

export const sourceColors: Record<string, string> = {
  google_classroom: '#67c69b',
  gradescope: '#83aef5',
  brightspace: '#b699ee',
  webassign: '#e2b66b',
};

export function courseColor(course?: Course) {
  if (!course) return '#b7bdc8';
  // Stable across renaming and linking a local course to its source.
  const key = course.workspace_id || course.id;
  let hash = 0;
  for (const character of key) hash = ((hash << 5) - hash + character.charCodeAt(0)) | 0;
  const hue =
    { google_classroom: 150, gradescope: 215, brightspace: 270, webassign: 39 }[course.provider] ??
    190;
  return `hsl(${hue + (Math.abs(hash) % 41) - 20} 58% ${64 + (Math.abs(hash) % 9)}%)`;
}
