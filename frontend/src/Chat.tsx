import { useEffect, useRef, useState } from 'react';
import { ArrowUp, ArrowUpRight, Loader2, MessageSquare, Plus, Search } from 'lucide-react';
import type { Course } from './types';
import { api } from './api';
import { readChatStream, mergeActivity, type ChatActivity } from './chat-stream';
import { ChatMarkdown, citationUrl, type Citation } from './ChatMarkdown';
import './chat.css';

import type {
  ChatMessage,
  ChatConfig,
  ChatResponse,
  Conversation,
  Material,
  TaskContext,
} from './chat-types';
import { MaterialLibrary } from './MaterialLibrary';
import { Activity } from './ChatActivity';
import { ConversationTitleEditor } from './ConversationTitleEditor';
export { AISettings } from './AISettings';

class ChatError extends Error {
  conversationId?: string;
  constructor(message: string, conversationId?: string) {
    super(message);
    this.conversationId = conversationId;
  }
}

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
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [view, setView] = useState<'conversation' | 'materials'>('conversation');
  const [libraryCitation, setLibraryCitation] = useState<Citation | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [courseId, setCourseId] = useState(initialCourseId ?? '');
  const [taskId, setTaskId] = useState<string | null>(initialTaskId ?? null);
  const [taskContext, setTaskContext] = useState<TaskContext | null>(null);
  const [taskLoading, setTaskLoading] = useState(!!initialTaskId);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState('');
  const [fetchMaterials, setFetchMaterials] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [credentialError, setCredentialError] = useState('');
  const [pendingMessage, setPendingMessage] = useState('');
  const [streamText, setStreamText] = useState('');
  const [activity, setActivity] = useState<ChatActivity[]>([]);
  const stopped = useRef(false);
  const bottom = useRef<HTMLDivElement>(null);
  const mounted = useRef(true);
  const selection = useRef(0);
  const request = useRef<AbortController | null>(null);
  const sending = useRef(false);

  const refreshConversations = async () => {
    const response = await api<{ conversations: Conversation[] }>('/chat/conversations');
    if (mounted.current) setConversations(response.conversations);
  };
  const loadConversation = async (id: string) => {
    const sequence = ++selection.current;
    setLoading(true);
    setError('');
    try {
      const response = await api<Conversation & { messages: ChatMessage[] }>(
        `/chat/conversations/${encodeURIComponent(id)}`,
      );
      if (mounted.current && selection.current === sequence) {
        setConversationId(response.id);
        setCourseId(response.course_id ?? '');
        setTaskId(response.task_id ?? null);
        setMessages(response.messages);
        setStreamText('');
        setActivity([]);
        return true;
      }
    } catch (e) {
      if (mounted.current && selection.current === sequence) setError((e as Error).message);
    } finally {
      if (mounted.current && selection.current === sequence) setLoading(false);
    }
    return false;
  };
  useEffect(() => {
    mounted.current = true;
    void refreshConversations().catch((e: Error) => {
      if (mounted.current) setError(e.message);
    });
    void api<ChatConfig>('/chat/config')
      .then((config) => {
        if (mounted.current) setCredentialError(config.credential_error ?? '');
      })
      .catch((e: Error) => {
        if (mounted.current) setError((current) => current || e.message);
      });
    return () => {
      mounted.current = false;
      selection.current += 1;
      request.current?.abort();
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
            setCourseId(context.task.course_id ?? '');
          }
        })
        .catch((e: Error) => {
          if (active) setError(e.message);
        })
        .finally(() => {
          if (active) setTaskLoading(false);
        });
    return () => {
      active = false;
    };
  }, [taskId]);
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
          if (active) setError(e.message);
        });
    return () => {
      active = false;
    };
  }, [initialDocumentId]);
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: 'nearest' });
  }, [messages, pendingMessage, streamText]);

  const newConversation = (course = courseId) => {
    selection.current += 1;
    setLoading(false);
    setConversationId(null);
    setCourseId(course);
    setTaskId(null);
    setMessages([]);
    setActivity([]);
    setStreamText('');
    setError('');
  };
  const openCitation = (citation: Citation) => {
    if (citation.kind === 'linked_document' && citationUrl(citation.url)) {
      window.open(citationUrl(citation.url), '_blank', 'noopener,noreferrer');
      return;
    }
    setLibraryCitation(citation);
    setView('materials');
  };
  const send = async () => {
    const content = draft.trim();
    if (!content || sending.current || loading || taskLoading) return;
    sending.current = true;
    setBusy(true);
    setError('');
    setPendingMessage(content);
    setStreamText('');
    setActivity([]);
    stopped.current = false;
    const controller = new AbortController();
    request.current = controller;
    const timeout = window.setTimeout(() => controller.abort(), 300000);
    let savedConversation: string | undefined;
    let receivedText = '';
    let receivedActivity: ChatActivity[] = [];
    let completedReply = false;
    try {
      const response = await fetch('/api/chat/messages/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CourseDeck': '1' },
        body: JSON.stringify({
          message: content,
          conversation_id: conversationId ?? undefined,
          course_id: courseId || undefined,
          task_id: taskId ?? undefined,
          fetch_materials: fetchMaterials,
        }),
        signal: controller.signal,
      });
      if (!response.ok) {
        const result = await response.json().catch(() => ({}));
        const detail = result.detail;
        throw new ChatError(
          typeof detail === 'string'
            ? detail
            : detail?.message || `Request failed (${response.status}).`,
          detail?.conversation_id,
        );
      }
      await readChatStream(response, (event) => {
        if (!mounted.current) return;
        if (event.type === 'session') {
          savedConversation = event.conversation_id as string;
          setConversationId(savedConversation);
          setMessages((current) => [...current, event.message as ChatMessage]);
          setPendingMessage('');
          setDraft('');
        } else if (event.type === 'answer_start') {
          receivedText = '';
          setStreamText('');
        } else if (event.type === 'delta') {
          receivedText += String(event.text);
          setStreamText((current) => current + String(event.text));
        } else if (event.type === 'activity') {
          receivedActivity = mergeActivity(receivedActivity, event as unknown as ChatActivity);
          setActivity((current) => mergeActivity(current, event as unknown as ChatActivity));
        } else if (event.type === 'done') {
          completedReply = true;
          const answer = event as unknown as ChatResponse;
          setMessages((current) => [...current, answer.message]);
          setStreamText('');
          setActivity([]);
        } else if (event.type === 'error') {
          throw new ChatError(String(event.message), event.conversation_id as string | undefined);
        }
      });
    } catch (e) {
      if (!mounted.current) return;
      const failure = e as ChatError;
      savedConversation = failure.conversationId ?? savedConversation;
      if (
        controller.signal.aborted &&
        !completedReply &&
        (receivedText || receivedActivity.some((step) => step.id))
      ) {
        setMessages((current) => [
          ...current,
          {
            id: `interrupted-${Date.now()}`,
            role: 'assistant',
            content: receivedText,
            activity: receivedActivity,
            warnings: ['Reply interrupted.'],
          },
        ]);
        setStreamText('');
        setActivity([]);
      }
      if (savedConversation && !controller.signal.aborted) {
        setConversationId(savedConversation);
        const restored = await loadConversation(savedConversation);
        if (mounted.current && restored) {
          setDraft('');
          setStreamText('');
          setActivity([]);
        }
      }
      if (mounted.current)
        setError(
          controller.signal.aborted
            ? stopped.current
              ? 'Stopped.'
              : 'Response timed out. Check this conversation before sending again.'
            : failure.message,
        );
    } finally {
      window.clearTimeout(timeout);
      request.current = null;
      sending.current = false;
      if (mounted.current) {
        setBusy(false);
        setPendingMessage('');
        void refreshConversations().catch(() => {
          if (mounted.current) setError((current) => current || 'Could not refresh chat history.');
        });
      }
    }
  };

  const courseSelector = (
    <select
      aria-label="Chat course"
      value={courseId}
      disabled={busy || loading || taskLoading}
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
          disabled={busy}
          onClick={() => {
            setView('conversation');
            newConversation();
          }}
        >
          <Plus size={16} /> New chat
        </button>
        <div className="chat-history-list">
          {conversations.map((conversation) =>
            renaming === conversation.id ? (
              <ConversationTitleEditor
                key={conversation.id}
                conversation={conversation}
                cancel={() => setRenaming(null)}
                saved={(title) => {
                  setConversations((current) =>
                    current.map((item) =>
                      item.id === conversation.id ? { ...item, title } : item,
                    ),
                  );
                  setRenaming(null);
                }}
              />
            ) : (
              <button
                key={conversation.id}
                className={conversationId === conversation.id ? 'selected' : ''}
                aria-pressed={conversationId === conversation.id}
                disabled={busy}
                title={`${conversation.title} · Double-click to rename`}
                onDoubleClick={() => setRenaming(conversation.id)}
                onKeyDown={(event) => {
                  if (event.key === 'F2') {
                    event.preventDefault();
                    setRenaming(conversation.id);
                  }
                }}
                onClick={() => {
                  setView('conversation');
                  void loadConversation(conversation.id);
                }}
              >
                <MessageSquare size={14} />
                <span>{conversation.title}</span>
              </button>
            ),
          )}
        </div>
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
          </div>
          {courseSelector}
        </div>
        {taskId && (
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
          <MaterialLibrary courseId={courseId} initialCitation={libraryCitation} />
        ) : (
          <section className="chat-panel" aria-label="Conversation">
            <div
              className="chat-messages"
              role="log"
              aria-label="Messages"
              aria-busy={busy || loading}
            >
              {loading ? (
                <p className="chat-placeholder">
                  <Loader2 size={18} className="spin" /> Loading…
                </p>
              ) : (
                <>
                  {messages.map((message) => (
                    <article className={`chat-message ${message.role}`} key={message.id}>
                      <span className="chat-role">
                        {message.role === 'user' ? 'You' : 'Assistant'}
                      </span>
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
              <div ref={bottom} />
            </div>
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
                disabled={busy || loading}
                onChange={(e) => setDraft(e.target.value)}
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
                  disabled={busy || loading}
                  onClick={() => setFetchMaterials(!fetchMaterials)}
                >
                  <Search size={15} />
                  Find materials
                </button>
                {busy ? (
                  <button
                    type="button"
                    className="chat-stop"
                    onClick={() => {
                      stopped.current = true;
                      request.current?.abort();
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
