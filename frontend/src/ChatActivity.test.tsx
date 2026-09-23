import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { expect, it } from 'vitest';
import { Activity } from './ChatActivity';

it('distinguishes saved memories from proposals that still need approval', () => {
  const pending = renderToStaticMarkup(
    <Activity
      items={[
        {
          id: 'memory',
          label: 'Remembering a preference',
          status: 'done',
          memory_change: { id: 'memory-1', text: 'Prefer examples', status: 'pending' },
        },
      ]}
    />,
  );
  expect(pending).toContain('Needs confirmation in Memories');
  expect(pending).toContain('Prefer examples');
  expect(pending).not.toContain('Saved to memory');
  expect(pending).not.toContain('class="warning"');
  const active = renderToStaticMarkup(
    <Activity
      items={[
        {
          id: 'memory',
          label: 'Remembering a preference',
          status: 'done',
          memory_change: { id: 'memory-1', text: 'Prefer examples', status: 'active' },
        },
      ]}
    />,
  );
  expect(active).toContain('Saved to memory');
  expect(active).not.toContain('Needs confirmation');
});

it('distinguishes Inbox previews from local changes and includes the affected email', () => {
  const preview = renderToStaticMarkup(
    <Activity
      items={[
        {
          id: 'preview',
          label: 'Previewing Inbox changes',
          status: 'done',
          inbox_preview: { action: 'delete', matched: 2, changed: 2 },
        },
      ]}
    />,
  );
  expect(preview).toContain('2 matched');
  expect(preview).not.toContain('Deleted locally');
  const applied = renderToStaticMarkup(
    <Activity
      items={[
        {
          id: 'apply',
          label: 'Updating local Inbox',
          status: 'done',
          inbox_change: {
            action: 'unread',
            matched: 1,
            changed: 1,
            has_more: false,
            messages: [{ id: 'mail-1', subject: 'Survey' }],
          },
        },
      ]}
    />,
  );
  expect(applied).toContain('Marked unread locally');
  expect(applied).toContain('Survey');
});

it('keeps visited sources and incomplete coverage visible in browser activity', () => {
  const html = renderToStaticMarkup(
    <Activity
      items={[
        {
          id: 'visit',
          label: 'Browsing course material',
          status: 'done',
          browser_visit: {
            title: 'Rubric',
            url: 'https://school.example/d2l/home/123',
            checked_at: '2026-09-11T12:00:00Z',
            warnings: ['An embedded frame could not be read'],
          },
        },
      ]}
    />,
  );
  expect(html).toContain('https://school.example/d2l/home/123');
  expect(html).toContain('Rubric');
  expect(html).toContain('2026-09-11T12:00:00Z');
  expect(html).toContain('An embedded frame could not be read');
});

it('keeps rule preview distinct from a committed rule and displays the affected fields', () => {
  const preview = renderToStaticMarkup(
    <Activity
      items={[
        {
          id: 'preview',
          label: 'Previewing mail rule changes',
          status: 'done',
          mail_rule_preview: { changed: 3, newly_ignored: 2, new_conflicts: 1 },
        },
      ]}
    />,
  );
  expect(preview).toContain('3 affected');
  expect(preview).not.toContain('Mail rule saved');
  const saved = renderToStaticMarkup(
    <Activity
      items={[
        {
          id: 'write',
          label: 'Updating mail rule',
          status: 'done',
          mail_rule_change: {
            changed: true,
            rule_id: 'r',
            before: null,
            after: {
              field: 'subject',
              contains: 'Intro',
              action: 'course',
              value: 'private:course-id',
              course_name: 'Programming',
              enabled: true,
            },
          },
        },
      ]}
    />,
  );
  expect(saved).toContain('Mail rule saved');
  expect(saved).toContain('Programming');
  expect(saved).not.toContain('private:course-id');
});
