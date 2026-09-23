import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { expect, test } from 'vitest';
import { MailDeletionPlan, type MailDeletionPlanData } from './MailDeletionPlan';
import { Activity } from './ChatActivity';

const plan: MailDeletionPlanData = {
  id: 'plan',
  version: 'v1',
  destination: 'gmail',
  status: 'pending',
  matched: 1,
  messages: [{ id: 'message', subject: 'Course email', sender: 'Teacher', received_at: null }],
  missing_date_count: 2,
  total_matched: 4,
  remaining_count: 3,
};

test('a shortened activity preview keeps the full confirmed match count', () => {
  const html = renderToStaticMarkup(<MailDeletionPlan initial={{ ...plan, matched: 655 }} />);
  expect(html).toContain('655 selected messages');
  expect(html).toContain('Showing 1 of 655 messages.');
  expect(html).not.toContain('1 selected messages');
});

test('Gmail confirmation identifies whole conversations, excluded scope, and waits for fresh status', () => {
  const html = renderToStaticMarkup(<MailDeletionPlan initial={plan} />);
  expect(html).toContain('entire Gmail conversation to Trash');
  expect(html).toContain('3 are outside this plan');
  expect(html).toContain('2 messages with unknown dates were excluded');
  expect(html).toContain('Date unknown');
  expect(html).toContain('<button disabled="">Move to Gmail Trash</button>');
});

test('partial results distinguish failed, unverified and unattempted messages', () => {
  const html = renderToStaticMarkup(
    <MailDeletionPlan
      initial={{
        ...plan,
        status: 'partial',
        result: {
          changed: 1,
          already_trashed: 2,
          failed: 2,
          unverified: 3,
          skipped: 4,
          results: [{ id: 'message', status: 'unverified', error: 'Could not verify Trash' }],
        },
      }}
    />,
  );
  expect(html).toContain('Partially completed');
  expect(html).toContain('1 moved to Trash');
  expect(html).toContain('2 already in Trash');
  expect(html).toContain('2 failed');
  expect(html).toContain('3 unverified');
  expect(html).toContain('4 skipped');
  expect(html).toContain('Could not verify Trash');
  expect(html).not.toContain('>Move to Gmail Trash</button>');
});

test('confirmation remains outside collapsed activity and duplicate plan receipts collapse', () => {
  const html = renderToStaticMarkup(
    <Activity
      items={[
        { id: 'first', label: 'Prepared deletion', status: 'done', mail_deletion_plan: plan },
        { id: 'second', label: 'Prepared deletion', status: 'done', mail_deletion_plan: plan },
      ]}
    />,
  );
  expect(html.split('aria-label="Gmail deletion plan"')).toHaveLength(2);
  expect(html.indexOf('class="chat-mail-plan"')).toBeGreaterThan(html.indexOf('</details>'));
});
