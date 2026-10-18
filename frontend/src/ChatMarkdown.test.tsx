import { renderToStaticMarkup } from 'react-dom/server';
import { expect, test } from 'vitest';
import { ChatMarkdown } from './ChatMarkdown';

const citation = {
  id: 'task:math:hw',
  title: 'Homework',
  url: 'https://example.com/hw',
  provider: 'test',
  kind: 'assignment',
};
const render = (content: string, pending = false) =>
  renderToStaticMarkup(
    <ChatMarkdown
      content={content}
      citations={[citation]}
      openCitation={() => {}}
      pending={pending}
    />,
  );

test('renders GFM tables, headings, lists and citations together', () => {
  const html = render(
    '## 近期任务\n\n| 作业 | 来源 |\n| --- | --- |\n| **Homework** | [[task:math:hw]] |\n\n1. First\n2. Second\n\n> Note\n\n~~Old~~',
  );
  expect(html).toContain('<h2>近期任务</h2>');
  expect(html).toContain('<table>');
  expect(html).toContain('<strong>Homework</strong>');
  expect(html).toContain('class="chat-inline-citation"');
  expect(html).toContain('<ol>');
  expect(html).toContain('<blockquote>');
  expect(html).toContain('<del>Old</del>');
});

test('keeps fenced and inline code literal without converting citation examples', () => {
  const html = render('`[[task:math:hw]]`\n\n```python\nprint("**text**")\n[[task:math:hw]]\n```');
  expect(html).toContain('<pre><code class="language-python">');
  expect(html).toContain('**text**');
  expect(html).not.toContain('chat-inline-citation');
});

test('rejects executable markup and unsafe URLs without loading remote images', () => {
  const html = render(
    '<script>alert(1)</script>\n\n[unsafe](javascript:alert%281%29) [safe](https://example.com)\n\n![diagram](https://example.com/tracker.png)',
  );
  expect(html).not.toContain('<script');
  expect(html).not.toContain('href="javascript:');
  expect(html).not.toContain('<img');
  expect(html).toContain('href="https://example.com/"');
});

test('streaming partial Markdown stays renderable, unknown citations warn only after completion', () => {
  expect(render('## Heading\n\n**unfinished', true)).toContain('<h2>Heading</h2>');
  expect(render('[[unknown]]', true)).not.toContain('source unavailable');
  expect(render('[[unknown]]')).toContain('source unavailable');
});
