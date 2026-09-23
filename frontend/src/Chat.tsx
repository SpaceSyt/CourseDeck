import { useEffect, useRef, useState } from 'react';
import {
  ArrowDown,
  ArrowUp,
  ArrowUpRight,
  Check,
  Copy,
  Loader2,
  MessageSquare,
  Pencil,
  Plus,
  Search,
  Trash2,
} from 'lucide-react';
import type { Course } from './types';
import { api } from './api';
import { chatSessions, useChatSessions } from './chat-session-store';
import { ChatMarkdown, citationUrl, type Citation } from './ChatMarkdown';
import './chat.css';

import type { ChatConfig, Material, TaskContext } from './chat-types';
import { MaterialLibrary } from './MaterialLibrary';
import { ChatMemories } from './ChatMemories';
import { Activity } from './ChatActivity';
import { ConversationTitleEditor } from './ConversationTitleEditor';
export { AISettings } from './AISettings';

export function Chat({
  courses,
  initialCourseId,
  initialTaskId,
  initialDocumentId,
}: {
  courses: Course[];
  initialCourseId?: string;
  initialTaskId?: string;
  initialDocumentId?: string;
}) {
  const state = useChatSessions();
  const session = state.sessions[state.selected];
  const {
    id: conversationId,
    courseId,
    taskId,
    messages,
    draft,
    fetchMaterials,
    busy,
    loading,
    error,
    pendingMessage,
    streamText,
    activity,
  } = session;
  const patch = (values: Partial<typeof session>) => chatSessions.patch(session.key, values);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [view, setView] = useState<'conversation' | 'materials' | 'memories'>('conversation');
  const [libraryCitation, setLibraryCitation] = useState<Citation | null>(null);
  const [taskContext, setTaskContext] = useState<TaskContext | null>(null);
  const [taskLoading, setTaskLoading] = useState(!!initialTaskId);
  const [credentialError, setCredentialError] = useState('');
  const [copied, setCopied] = useState<string | null>(null);
  const [following, setFollowing] = useState(true);
  const scroll = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const initialized = useRef(false);
  const history = [
    ...Object.values(state.sessions)
      .filter(
        (item) =>
          (item.busy || item.draft || item.messages.length || item.error) &&
          !state.conversations.some((conversation) => conversation.id === item.id),
      )
      .map((item) => ({ id: item.id ?? item.key, title: item.title, course_id: item.courseId })),
    ...state.conversations,
  ];
  useEffect(() => {
    if (!initialized.current) {
      initialized.current = true;
      if (initialCourseId || initialTaskId)
        chatSessions.newConversation(initialCourseId ?? '', initialTaskId ?? null);
    }
    let active = true;
    void chatSessions.refresh();
    void chatSessions.select(chatSessions.state.selected);
    void api<ChatConfig>('/chat/config')
      .then((config) => {
        if (active) setCredentialError(config.credential_error ?? '');
      })
      .catch((e: Error) => {
        if (active) setCredentialError(e.message);
      });
    return () => {
      active = false;
    };
  }, []);
  useEffect(() => {
    let active = true;
    setTaskContext(null);
    setTaskLoading(!!taskId);
    if (taskId)
      void api<TaskContext>(`/chat/task-context/${encodeURIComponent(taskId)}`)
        .then((context) => {
          if (active) {
            setTaskContext(context);
            chatSessions.patch(session.key, { courseId: context.task.course_id ?? '' });
          }
        })
        .catch((e: Error) => {
          if (active) chatSessions.patch(session.key, { error: e.message });
        })
        .finally(() => {
          if (active) setTaskLoading(false);
        });
    return () => {
      active = false;
    };
  }, [taskId, session.key]);
  useEffect(() => {
    let active = true;
    if (initialDocumentId)
      void api<Material>(`/chat/library/${encodeURIComponent(initialDocumentId)}`)
        .then((document) => {
          if (active) {
            setLibraryCitation(document);
            setView('materials');
          }
        })
        .catch((e: Error) => {
          if (active) chatSessions.patch(session.key, { error: e.message });
        });
    return () => {
      active = false;
    };
  }, [initialDocumentId]);
  useEffect(() => {
    follow.current = true;
    setFollowing(true);
    if (scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [session.key, view]);
  useEffect(() => {
    if (follow.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [messages, pendingMessage, streamText, activity, loading]);

  const newConversation = (course = courseId) => chatSessions.newConversation(course);
  const openCitation = (citation: Citation) => {
    if (['linked_document', 'browser_page'].includes(citation.kind) && citationUrl(citation.url)) {
      window.open(citationUrl(citation.url), '_blank', 'noopener,noreferrer');
      return;
    }
    setLibraryCitation(citation);
    setView('materials');
  };
  const send = () => {
    if (taskLoading || !draft.trim() || busy) return;
    follow.current = true;
    setFollowing(true);
    void chatSessions.send(session.key);
  };
  const copy = async (messageId: string, content: string) => {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(messageId);
    } catch {
      patch({ error: 'Could not copy the answer.' });
    }
  };

  const courseSelector = (
    <select
      aria-label="Chat course"
      value={courseId}
      disabled={loading || taskLoading}
      onChange={(e) => newConversation(e.target.value)}
    >
      <option value="">All courses</option>
      {courseId && !courses.some((course) => course.id === courseId) && (
        <option value={courseId}>Unavailable course</option>
      )}
      {courses.map((course) => (
        <option key={course.id} value={course.id}>
          {course.name}
        </option>
      ))}
    </select>
  );
  return (
    <div className="chat-workspace">
      <aside className="chat-history" aria-label="Chat history">
        <button
          onClick={() => {
            setView('conversation');
            newConversation();
          }}
        >
          <Plus size={16} /> New chat
        </button>
        <div className="chat-history-list">
          {history.map((conversation) => {
            const item = Object.values(state.sessions).find(
              (entry) => entry.id === conversation.id || entry.key === conversation.id,
            );
            const selected = item?.key === session.key;
            const saved = item ? !!item.id : true;
            return renaming === conversation.id ? (
              <ConversationTitleEditor
                key={conversation.id}
                conversation={conversation}
                cancel={() => setRenaming(null)}
                saved={(title) => {
                  chatSessions.rename(conversation.id, title);
                  setRenaming(null);
                }}
              />
            ) : (
              <div
                key={conversation.id}
                className={`chat-history-row${selected ? ' selected' : ''}`}
              >
                <button
                  className="chat-history-select"
                  aria-pressed={selected}
                  title={conversation.title}
                  onDoubleClick={() => {
                    if (saved) setRenaming(conversation.id);
                  }}
                  onKeyDown={(event) => {
                    if (event.key === 'F2' && saved) {
                      event.preventDefault();
                      setRenaming(conversation.id);
                    }
                  }}
                  onClick={() => {
                    setView('conversation');
                    void chatSessions.select(conversation.id);
                  }}
                >
                  {item?.busy ? (
                    <Loader2 size={14} className="spin" aria-label="Generating" />
                  ) : (
                    <MessageSquare size={14} />
                  )}
                  <span>{conversation.title}</span>
                  {item?.error && (
                    <span className="chat-history-status" aria-label="Conversation needs attention">
                      !
                    </span>
                  )}
                </button>
                <button
                  className="chat-history-action"
                  aria-label={`Rename ${conversation.title}`}
                  title="Rename"
                  disabled={!saved || item?.deleting}
                  onClick={() => setRenaming(conversation.id)}
                >
                  <Pencil size={13} />
                </button>
                <button
                  className="chat-history-action"
                  aria-label={`Delete ${conversation.title}`}
                  title={item?.busy ? 'Stop the reply before deleting' : 'Delete chat'}
                  disabled={item?.busy || item?.deleting}
                  onClick={() => {
                    if (window.confirm('Delete this chat and its messages?'))
                      void chatSessions.remove(conversation.id);
                  }}
                >
                  <Trash2 size={13} />
                </button>
              </div>
            );
          })}
        </div>
        {state.historyError && (
          <p className="warning" role="alert">
            {state.historyError}
          </p>
        )}
      </aside>

      <div className="chat-main">
        <div className="chat-topline">
          <div className="chat-tabs" role="group" aria-label="Chat view">
            <button aria-pressed={view === 'conversation'} onClick={() => setView('conversation')}>
              Conversation
            </button>
            <button
              aria-pressed={view === 'materials'}
              onClick={() => {
                setLibraryCitation(null);
                setView('materials');
              }}
            >
              Materials
            </button>
            <button aria-pressed={view === 'memories'} onClick={() => setView('memories')}>
              Memories
            </button>
          </div>
          {view !== 'memories' && courseSelector}
        </div>
        {taskId && view !== 'memories' && (
          <div className="chat-task-context" aria-label="Task context">
            <strong>
              {taskContext?.task.title ?? (taskLoading ? 'Loading task…' : 'Task unavailable')}
            </strong>
            {!!taskContext?.documents.length && (
              <details>
                <summary>Related materials ({taskContext.documents.length})</summary>
                {taskContext.documents.map((document) => (
                  <button key={document.id} onClick={() => openCitation(document)}>
                    {document.title}
                    <span>
                      {document.related_via === 'keyword_match' ? 'Keyword match' : 'Linked'}
                    </span>
                  </button>
                ))}
              </details>
            )}
            {taskContext?.warnings.map((warning) => (
              <p className="warning" key={warning}>
                {warning}
              </p>
            ))}
          </div>
        )}
        {view === 'materials' ? (
          <MaterialLibrary
            courseId={courseId}
            initialCitation={libraryCitation}
            conversationId={conversationId}
          />
        ) : view === 'memories' ? (
          <ChatMemories courses={courses} initialCourseId={courseId} />
        ) : (
          <section className="chat-panel" aria-label="Conversation">
            <div
              ref={scroll}
              className="chat-messages"
              onScroll={() => {
                const element = scroll.current;
                if (element) {
                  follow.current =
                    element.scrollHeight - element.scrollTop - element.clientHeight < 64;
                  setFollowing(follow.current);
                }
              }}
              role="log"
              aria-label="Messages"
              aria-busy={busy || loading}
            >
              {loading && !messages.length ? (
                <p className="chat-placeholder">
                  <Loader2 size={18} className="spin" /> Loading…
                </p>
              ) : (
                <>
                  {messages.map((message) => (
                    <article className={`chat-message ${message.role}`} key={message.id}>
                      <div className="chat-message-header">
                        <span className="chat-role">
                          {message.role === 'user' ? 'You' : 'Assistant'}
                        </span>
                        {message.role === 'assistant' && message.content && (
                          <button
                            className="chat-copy"
                            aria-label={copied === message.id ? 'Copied' : 'Copy answer'}
                            title={copied === message.id ? 'Copied' : 'Copy answer'}
                            onClick={() => void copy(message.id, message.content)}
                          >
                            {copied === message.id ? <Check size={14} /> : <Copy size={14} />}
                          </button>
                        )}
                      </div>
                      {!!message.activity?.length && <Activity items={message.activity} />}
                      <div className="chat-message-content">
                        {message.role === 'assistant' ? (
                          <ChatMarkdown
                            content={message.content}
                            citations={message.citations}
                            openCitation={openCitation}
                          />
                        ) : (
                          message.content
                        )}
                      </div>
                      {message.warnings?.map((warning) => (
                        <p key={warning} className="warning">
                          {warning}
                        </p>
                      ))}
                      {!!message.citations?.length && (
                        <ul className="chat-citations" aria-label="Sources">
                          {message.citations.map((citation, index) => (
                            <li key={citation.id}>
                              <button onClick={() => openCitation(citation)}>
                                [{index + 1}] {citation.title}
                              </button>
                              {citationUrl(citation.url) && (
                                <a
                                  href={citationUrl(citation.url)}
                                  target="_blank"
                                  rel="noreferrer"
                                  aria-label={`Open source: ${citation.title}`}
                                >
                                  <ArrowUpRight size={14} />
                                </a>
                              )}
                            </li>
                          ))}
                        </ul>
                      )}
                    </article>
                  ))}
                  {pendingMessage && (
                    <article className="chat-message user">
                      <span className="chat-role">You</span>
                      <div className="chat-message-content">{pendingMessage}</div>
                    </article>
                  )}
                  {(busy || streamText || activity.length > 0) && (
                    <article className="chat-message assistant" aria-busy={busy}>
                      <span className="chat-role">Assistant</span>
                      <Activity items={activity} busy={busy} />
                      <div className="chat-message-content chat-stream-text">
                        <ChatMarkdown content={streamText} openCitation={openCitation} pending />
                      </div>
                    </article>
                  )}
                </>
              )}
            </div>
            {!following && (
              <button
                className="chat-latest"
                onClick={() => {
                  follow.current = true;
                  setFollowing(true);
                  if (scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
                }}
              >
                <ArrowDown size={14} /> Latest
              </button>
            )}
            <div className="chat-feedback">
              {credentialError && (
                <p className="warning" role="alert">
                  {credentialError}
                </p>
              )}
              {error && (
                <p className="warning" role="alert">
                  {error}
                </p>
              )}
            </div>
            <form
              className="chat-composer"
              onSubmit={(e) => {
                e.preventDefault();
                void send();
              }}
            >
              <textarea
                aria-label="Message"
                placeholder="Ask a question…"
                rows={3}
                maxLength={12000}
                value={draft}
                disabled={loading || session.deleting}
                onChange={(e) => patch({ draft: e.target.value })}
                onKeyDown={(e) => {
                  if (
                    e.key === 'Enter' &&
                    !e.shiftKey &&
                    !e.nativeEvent.isComposing &&
                    e.nativeEvent.keyCode !== 229
                  ) {
                    e.preventDefault();
                    void send();
                  }
                }}
              />
              <div className="chat-composer-actions">
                <button
                  type="button"
                  className="chat-material-toggle"
                  aria-pressed={fetchMaterials}
                  title={
                    courseId
                      ? 'Refresh course materials before answering'
                      : 'Select a course to refresh materials'
                  }
                  disabled={!courseId || loading || session.deleting}
                  onClick={() => patch({ fetchMaterials: !fetchMaterials })}
                >
                  <Search size={15} />
                  Refresh materials
                </button>
                {busy ? (
                  <button
                    type="button"
                    className="chat-stop"
                    onClick={() => {
                      chatSessions.stop(session.key);
                    }}
                  >
                    Stop
                  </button>
                ) : (
                  <button
                    type="submit"
                    className="chat-send"
                    aria-label="Send"
                    title="Send (Enter)"
                    disabled={busy || loading || taskLoading || !draft.trim()}
                  >
                    <ArrowUp size={20} />
                  </button>
                )}
              </div>
            </form>
          </section>
        )}
      </div>
    </div>
  );
}
