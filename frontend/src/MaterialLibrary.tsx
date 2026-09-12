import { useEffect, useRef, useState } from 'react';
import { ArrowUpRight, Loader2, Search } from 'lucide-react';
import { api } from './api';
import { citationUrl, type Citation } from './ChatMarkdown';
import type { Material, MaterialPage } from './chat-types';

export function MaterialLibrary({
  courseId,
  initialCitation,
}: {
  courseId: string;
  initialCitation: Citation | null;
}) {
  const [query, setQuery] = useState('');
  const [provider, setProvider] = useState('');
  const [kind, setKind] = useState('');
  const [freshness, setFreshness] = useState('all');
  const [completeness, setCompleteness] = useState('all');
  const [documents, setDocuments] = useState<Material[]>([]);
  const [total, setTotal] = useState(0);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [detail, setDetail] = useState<Material | null>(null);
  const [loading, setLoading] = useState(true);
  const [moreLoading, setMoreLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState('');
  const generation = useRef(0);
  const detailRequest = useRef(0);
  const documentWarnings = new Set([
    ...documents.flatMap((document) => document.warnings ?? []),
    ...(detail?.warnings ?? []),
  ]);
  const globalWarnings = warnings.filter((warning) => !documentWarnings.has(warning));
  const path = (offset = 0) =>
    `/chat/library?${new URLSearchParams({
      course_id: courseId,
      query,
      provider,
      kind,
      freshness,
      completeness,
      offset: String(offset),
      limit: '50',
    })}`;
  useEffect(() => {
    const sequence = ++generation.current;
    detailRequest.current += 1;
    setLoading(true);
    setMoreLoading(false);
    setDocuments([]);
    setTotal(0);
    setWarnings([]);
    setDetail(null);
    setDetailLoading(false);
    setError('');
    const timer = window.setTimeout(() => {
      void api<MaterialPage>(path())
        .then((result) => {
          if (generation.current === sequence) {
            setDocuments(result.documents);
            setTotal(result.total);
            setWarnings(result.warnings ?? []);
          }
        })
        .catch((e: Error) => {
          if (generation.current === sequence) setError(e.message);
        })
        .finally(() => {
          if (generation.current === sequence) setLoading(false);
        });
    }, 250);
    return () => {
      generation.current += 1;
      detailRequest.current += 1;
      window.clearTimeout(timer);
    };
  }, [courseId, query, provider, kind, freshness, completeness]);
  const loadMore = async () => {
    const sequence = generation.current;
    setMoreLoading(true);
    setError('');
    try {
      const result = await api<MaterialPage>(path(documents.length));
      if (generation.current === sequence) {
        setDocuments((current) => [...current, ...result.documents]);
        setTotal(result.total);
        setWarnings(result.warnings ?? []);
      }
    } catch (e) {
      if (generation.current === sequence) setError((e as Error).message);
    } finally {
      if (generation.current === sequence) setMoreLoading(false);
    }
  };
  const open = async (document: Material) => {
    const sequence = ++detailRequest.current;
    setDetail(document);
    setDetailLoading(true);
    setError('');
    try {
      const result = await api<Material>(`/chat/library/${encodeURIComponent(document.id)}`);
      if (detailRequest.current === sequence) setDetail(result);
    } catch (e) {
      if (detailRequest.current === sequence) setError((e as Error).message);
    } finally {
      if (detailRequest.current === sequence) setDetailLoading(false);
    }
  };
  useEffect(() => {
    if (initialCitation)
      void open({ ...initialCitation, body: '', course_id: initialCitation.course_id ?? null });
  }, [initialCitation]);
  return (
    <section className="material-library" aria-label="Course materials">
      <div className="material-search">
        <input
          aria-label="Search materials"
          placeholder="Search materials…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <span>
          {loading ? 'Loading…' : error && !documents.length ? 'Unavailable' : `${total} materials`}
        </span>
      </div>
      <div className="material-filters">
        <select
          aria-label="Material source"
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
        >
          <option value="">All sources</option>
          <option value="google_classroom">Classroom</option>
          <option value="brightspace">Brightspace</option>
          <option value="gradescope">Gradescope</option>
          <option value="webassign">WebAssign</option>
        </select>
        <select aria-label="Material type" value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">All types</option>
          <option value="material">Material</option>
          <option value="module">Module</option>
          <option value="announcement">Announcement</option>
        </select>
        <select
          aria-label="Material read time"
          value={freshness}
          onChange={(e) => setFreshness(e.target.value)}
        >
          <option value="all">Any read time</option>
          <option value="recent">Read in last 7 days</option>
          <option value="stale">Read over 7 days ago</option>
          <option value="unknown">Read time unknown</option>
        </select>
        <select
          aria-label="Material completeness"
          value={completeness}
          onChange={(e) => setCompleteness(e.target.value)}
        >
          <option value="all">Any completeness</option>
          <option value="complete">Complete</option>
          <option value="incomplete">Incomplete</option>
          <option value="unknown">Completeness unknown</option>
        </select>
      </div>
      {error && (
        <p className="warning" role="alert">
          {error}
        </p>
      )}
      {globalWarnings.map((warning) => (
        <p key={warning} className="warning">
          {warning}
        </p>
      ))}
      {detail ? (
        <article className="material-detail">
          <div className="material-detail-toolbar">
            <button
              onClick={() => {
                detailRequest.current += 1;
                setDetail(null);
                setDetailLoading(false);
              }}
            >
              Back to materials
            </button>
            {citationUrl(detail.url) && (
              <a href={citationUrl(detail.url)} target="_blank" rel="noreferrer">
                Open source <ArrowUpRight size={15} />
              </a>
            )}
          </div>
          <h2>{detail.title}</h2>
          <p className="material-meta">
            {detail.provider.replaceAll('_', ' ')} · {detail.kind.replaceAll('_', ' ')}
          </p>
          <MaterialTimes document={detail} />
          {[...new Set(detail.warnings ?? [])].map((warning) => (
            <p key={warning} className="warning">
              {warning}
            </p>
          ))}
          {detail.complete === false && !detail.warnings?.length && (
            <p className="warning">Material incomplete.</p>
          )}
          {detail.complete == null && <p className="warning">Completeness unknown.</p>}
          {detailLoading && (
            <p role="status" className="chat-pending">
              <Loader2 size={16} className="spin" /> Loading full text…
            </p>
          )}
          {detail.body ? (
            <div className="material-body">{detail.body}</div>
          ) : (
            <p className="chat-placeholder">No text extracted.</p>
          )}
          {detail.body_truncated && !detailLoading && (
            <p className="warning">Full text unavailable; showing cached excerpt.</p>
          )}
        </article>
      ) : !loading && !documents.length && !error ? (
        <p className="chat-placeholder">No materials found.</p>
      ) : (
        <>
          <div className="material-list" aria-busy={loading}>
            {documents.map((document) => (
              <button key={document.id} onClick={() => void open(document)} disabled={loading}>
                <strong>{document.title}</strong>
                <span className="material-meta">
                  {document.provider.replaceAll('_', ' ')} · {document.kind.replaceAll('_', ' ')}
                </span>
                <MaterialTimes document={document} compact />
                {document.complete === false && (
                  <span className="warning">
                    {document.body ? 'Incomplete' : 'Text unavailable'}
                  </span>
                )}
                {document.complete == null && <span className="warning">Completeness unknown</span>}
                {document.body && (
                  <span className="material-preview">{document.body.slice(0, 220)}</span>
                )}
              </button>
            ))}
          </div>
          {documents.length < total && !loading && (
            <button
              className="material-more"
              disabled={moreLoading}
              onClick={() => void loadMore()}
            >
              {moreLoading ? 'Loading…' : 'Load more'}
            </button>
          )}
        </>
      )}
    </section>
  );
}

function MaterialTimes({ document, compact = false }: { document: Material; compact?: boolean }) {
  const validDate = (value?: string | null) => {
    if (!value) return null;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? null : date;
  };
  const fetched = validDate(document.fetched_at);
  const checked = validDate(document.checked_at);
  const modified = validDate(document.source_modified_at);
  const times = [
    { label: 'Source updated', date: modified },
    { label: 'Read', date: fetched },
    { label: 'Checked', date: checked?.getTime() !== fetched?.getTime() ? checked : null },
  ].filter((item) => item.label !== 'Checked' || (!compact && item.date !== null));
  return (
    <span className="material-meta material-times" aria-label="Material retrieval times">
      {times.map(({ label, date }, index) => (
        <span key={label}>
          {index > 0 && ' · '}
          {label}{' '}
          {date ? (
            <time dateTime={date.toISOString()} title={date.toLocaleString()}>
              {date.toLocaleString(undefined, {
                dateStyle: 'medium',
                ...(compact ? {} : { timeStyle: 'short' as const }),
              })}
            </time>
          ) : (
            'unknown'
          )}
        </span>
      ))}
    </span>
  );
}
