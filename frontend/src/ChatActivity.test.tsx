import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { expect, it } from 'vitest';
import { Activity } from './ChatActivity';

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
