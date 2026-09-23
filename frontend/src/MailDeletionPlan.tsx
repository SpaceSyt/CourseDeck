import { useEffect, useRef, useState } from 'react';
import { Loader2 } from 'lucide-react';

export interface MailDeletionPlanData {
  id: string;
  version: string;
  destination: 'local' | 'gmail';
  status: 'pending' | 'running' | 'done' | 'partial' | 'failed';
  matched: number;
  total_matched?: number;
  remaining_count?: number;
  description?: string;
  messages: { id: string; subject: string; sender: string; received_at: string | null }[];
  missing_date_count: number;
  result?: {
    changed: number;
    failed: number;
    unverified: number;
    skipped?: number;
    already_trashed?: number;
    results: { id: string; status: string; error?: string }[];
  };
  error?: string;
}

class PlanError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

async function requestPlan(id: string, version?: string): Promise<MailDeletionPlanData> {
  const response = await fetch(
    `/api/chat/inbox-plans/${encodeURIComponent(id)}${version ? '/apply' : ''}`,
    {
      method: version ? 'POST' : 'GET',
      headers: { 'Content-Type': 'application/json', 'X-CourseDeck': '1' },
      body: version ? JSON.stringify({ expected_version: version }) : undefined,
      signal: AbortSignal.timeout(version ? 300000 : 10000),
    },
  );
  const data = await response.json().catch(() => ({}));
  if (!response.ok)
    throw new PlanError(
      response.status === 404
        ? 'This deletion plan has expired. Search again in Chat.'
        : typeof data.detail === 'string'
          ? data.detail
          : `Could not read deletion status (${response.status}).`,
      response.status,
    );
  return data as MailDeletionPlanData;
}

export function MailDeletionPlan({ initial }: { initial: MailDeletionPlanData }) {
  const [plan, setPlan] = useState(initial);
  const [checking, setChecking] = useState(true);
  const [applying, setApplying] = useState(false);
  const [verified, setVerified] = useState(false);
  const [expired, setExpired] = useState(false);
  const [error, setError] = useState('');
  const [limit, setLimit] = useState(20);
  const mounted = useRef(true);
  const request = useRef(0);
  const writing = useRef(false);
  const read = async () => {
    const sequence = ++request.current;
    setChecking(true);
    setVerified(false);
    setError('');
    try {
      const latest = await requestPlan(initial.id);
      if (mounted.current && sequence === request.current) {
        setPlan(latest);
        setVerified(true);
        setExpired(false);
      }
      return true;
    } catch (failure) {
      if (mounted.current && sequence === request.current) {
        const missing = (failure as PlanError).status === 404;
        const retainedReceipt =
          missing && plan.result && ['done', 'partial', 'failed'].includes(plan.status);
        setExpired(missing);
        setError(retainedReceipt ? '' : (failure as Error).message);
      }
      return false;
    } finally {
      if (mounted.current && sequence === request.current) setChecking(false);
    }
  };
  useEffect(() => {
    mounted.current = true;
    void read();
    return () => {
      mounted.current = false;
      request.current += 1;
    };
  }, [initial.id]);
  const apply = async () => {
    if (writing.current || !verified || checking || plan.status !== 'pending') return;
    writing.current = true;
    setApplying(true);
    setError('');
    try {
      const latest = await requestPlan(plan.id, plan.version);
      if (mounted.current) {
        setPlan(latest);
        setVerified(true);
      }
    } catch (failure) {
      if (mounted.current) {
        const refreshed = await read();
        if (mounted.current)
          setError(
            (failure as PlanError).status === 404
              ? 'This deletion plan has expired. Search again in Chat.'
              : refreshed
                ? (failure as Error).message
                : `${(failure as Error).message} Current status could not be verified. Refresh status before trying again.`,
          );
      }
    } finally {
      writing.current = false;
      window.dispatchEvent(new Event('coursedeck:inbox-changed'));
      if (mounted.current) setApplying(false);
    }
  };
  const result = plan.result;
  const results = new Map(result?.results.map((item) => [item.id, item]));
  const resultLabel = (status: string) =>
    ({
      changed: plan.destination === 'gmail' ? 'Moved to Trash' : 'Deleted locally',
      deleted: 'Deleted locally',
      trashed: 'Moved to Trash',
      already_trashed: 'Already in Trash',
      failed: 'Failed',
      unverified: 'Unverified',
      skipped: 'Skipped',
    })[status] ?? status;
  const unresolved =
    (result?.failed ?? 0) + (result?.unverified ?? 0) + (result?.skipped ?? 0) > 0 ||
    ['partial', 'failed'].includes(plan.status);
  return (
    <section
      className="chat-mail-plan"
      aria-label={`${plan.destination === 'gmail' ? 'Gmail' : 'Local Inbox'} deletion plan`}
      aria-busy={checking || applying || plan.status === 'running'}
    >
      <strong>
        {plan.destination === 'gmail' ? 'Gmail' : 'Local Inbox'} · {plan.matched} selected messages
      </strong>
      {plan.description && <p>{plan.description}</p>}
      <p>
        {plan.destination === 'gmail'
          ? 'Moves each selected email’s entire Gmail conversation to Trash.'
          : 'Deletes the selected messages from local Inbox only.'}
      </p>
      {!!plan.remaining_count && (
        <p className="warning">
          {plan.total_matched ?? plan.matched + plan.remaining_count} matched;{' '}
          {plan.remaining_count} are outside this plan.
        </p>
      )}
      {!!plan.missing_date_count && (
        <p className="warning">
          {plan.missing_date_count} messages with unknown dates were excluded.
        </p>
      )}
      {error && (
        <p className="warning" role="alert">
          {error}
        </p>
      )}
      {plan.error && plan.error !== error && (
        <p className="warning" role="alert">
          {plan.error}
        </p>
      )}
      {result && (
        <p className={unresolved ? 'warning' : 'chat-mail-plan-result'} role="status">
          {plan.status === 'partial'
            ? 'Partially completed · '
            : plan.status === 'failed'
              ? 'Not completed · '
              : ''}
          {result.changed} {plan.destination === 'gmail' ? 'moved to Trash' : 'deleted locally'}
          {!!result.already_trashed && ` · ${result.already_trashed} already in Trash`}
          {!!result.failed && ` · ${result.failed} failed`}
          {!!result.unverified && ` · ${result.unverified} unverified`}
          {!!result.skipped && ` · ${result.skipped} skipped`}
        </p>
      )}
      {unresolved && !result && (
        <p className="warning">
          {plan.status === 'partial'
            ? 'Only part of this plan finished.'
            : 'This deletion plan failed.'}
        </p>
      )}
      <details className="chat-mail-plan-messages">
        <summary>Review messages{unresolved ? ' and results' : ''}</summary>
        {plan.messages.length < plan.matched && (
          <p>
            Showing {plan.messages.length} of {plan.matched} messages.
          </p>
        )}
        <ul>
          {plan.messages.slice(0, limit).map((message) => {
            const outcome = results.get(message.id);
            const date = message.received_at ? new Date(message.received_at) : null;
            return (
              <li key={message.id}>
                <span className="chat-mail-plan-subject">{message.subject || '(No subject)'}</span>
                <span className="chat-mail-plan-meta">
                  {message.sender || 'Unknown sender'} ·{' '}
                  {date && !Number.isNaN(date.getTime()) ? (
                    <time dateTime={date.toISOString()}>{date.toLocaleString()}</time>
                  ) : (
                    'Date unknown'
                  )}
                </span>
                {outcome && (
                  <span
                    className={['failed', 'unverified'].includes(outcome.status) ? 'warning' : ''}
                  >
                    {resultLabel(outcome.status)}
                    {outcome.error && `: ${outcome.error}`}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
        {limit < plan.messages.length && (
          <button onClick={() => setLimit((current) => current + 20)}>Show more</button>
        )}
      </details>
      <div className="chat-mail-plan-actions">
        {checking || applying || plan.status === 'running' ? (
          <span role="status">
            <Loader2 size={14} className="spin" /> {checking ? 'Checking status…' : 'Deleting…'}
          </span>
        ) : null}
        {!expired && plan.status === 'pending' && (
          <button
            disabled={!verified || checking || applying || !plan.messages.length}
            onClick={() => void apply()}
          >
            {plan.destination === 'gmail' ? 'Move to Gmail Trash' : 'Delete locally'}
          </button>
        )}
        {!expired && (!verified || plan.status === 'running') && (
          <button disabled={checking || applying} onClick={() => void read()}>
            Refresh status
          </button>
        )}
      </div>
    </section>
  );
}
