import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { Element, Root, Text } from 'hast';

export interface Citation {
  id: string;
  title: string;
  url: string | null;
  provider: string;
  course_id?: string | null;
  kind: string;
}

export function citationUrl(value: string | null | undefined) {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password
      ? url.href
      : undefined;
  } catch {
    return undefined;
  }
}

// Add citation controls to prose after Markdown parsing, preserving tables and inline markup.
// Code and links remain literal; raw HTML never supplies these internal properties.
function citationNodes() {
  return (tree: Root) => {
    const walk = (parent: Root | Element) => {
      for (let index = parent.children.length - 1; index >= 0; index--) {
        const child = parent.children[index];
        if (child.type === 'element') {
          if (!['pre', 'code', 'a'].includes(child.tagName)) walk(child);
          continue;
        }
        if (child.type !== 'text') continue;
        const nodes: (Text | Element)[] = [];
        let cursor = 0;
        for (const match of child.value.matchAll(/\[\[([^\]\n]+)\]\]/g)) {
          nodes.push({ type: 'text', value: child.value.slice(cursor, match.index) });
          nodes.push({
            type: 'element',
            tagName: 'span',
            properties: { dataCitationId: match[1] },
            children: [{ type: 'text', value: match[0] }],
          });
          cursor = match.index! + match[0].length;
        }
        nodes.push({ type: 'text', value: child.value.slice(cursor) });
        parent.children.splice(index, 1, ...nodes);
      }
    };
    walk(tree);
  };
}

export function ChatMarkdown({
  content,
  citations = [],
  openCitation,
  pending = false,
}: {
  content: string;
  citations?: Citation[];
  openCitation: (citation: Citation) => void;
  pending?: boolean;
}) {
  return (
    <div className="chat-markdown">
      <Markdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[citationNodes]}
        skipHtml
        urlTransform={(url) => citationUrl(url) ?? ''}
        components={{
          span: ({ node, children }) => {
            const id = node?.properties.dataCitationId;
            if (typeof id !== 'string') return <span>{children}</span>;
            const index = citations.findIndex((citation) => citation.id === id);
            if (index >= 0)
              return (
                <button
                  type="button"
                  className="chat-inline-citation"
                  title={citations[index].title}
                  onClick={() => openCitation(citations[index])}
                >
                  [{index + 1}]
                </button>
              );
            return (
              <span className={pending ? 'chat-pending-citation' : 'chat-missing-citation'}>
                {children}
                {!pending && ' (source unavailable)'}
              </span>
            );
          },
          a: ({ href, children }) =>
            citationUrl(href) ? (
              <a href={citationUrl(href)} target="_blank" rel="noopener noreferrer">
                {children}
              </a>
            ) : (
              <span>{children}</span>
            ),
          img: ({ src, alt }) =>
            citationUrl(src) ? (
              <a href={citationUrl(src)} target="_blank" rel="noopener noreferrer">
                {alt || 'Open image'}
              </a>
            ) : (
              <span>{alt}</span>
            ),
          table: ({ children }) => (
            <div className="chat-table-scroll" role="region" aria-label="Table" tabIndex={0}>
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {content}
      </Markdown>
    </div>
  );
}
