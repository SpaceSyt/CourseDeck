import { FileText, Loader2, X } from 'lucide-react';
import type { ChatAttachment, DraftAttachment } from './chat-types';

export const attachmentUrl = (id: string) =>
  `/api/chat/attachments/${encodeURIComponent(id)}/download`;

function fileSize(size: number) {
  return size >= 1024 * 1024
    ? `${(size / (1024 * 1024)).toFixed(1)} MB`
    : `${Math.max(1, Math.ceil(size / 1024))} KB`;
}

export function ChatAttachments({
  attachments,
  drafts,
  remove,
}: {
  attachments?: ChatAttachment[];
  drafts?: DraftAttachment[];
  remove?: (key: string) => void;
}) {
  const entries =
    drafts ??
    attachments?.map((attachment) => ({
      key: attachment.id,
      name: attachment.name,
      size: attachment.size,
      status: 'ready' as const,
      attachment,
    })) ??
    [];
  if (!entries.length) return null;
  return (
    <ul className="chat-attachments" aria-label={drafts ? 'Attached files' : 'Message files'}>
      {entries.map((entry: DraftAttachment) => (
        <li key={entry.key}>
          <div className="chat-attachment-row">
            {entry.status === 'uploading' || entry.removing ? (
              <Loader2 size={16} className="spin" />
            ) : (
              <FileText size={16} />
            )}
            <div className="chat-attachment-info">
              {entry.attachment ? (
                <a
                  href={attachmentUrl(entry.attachment.id)}
                  target="_blank"
                  rel="noreferrer"
                  title={entry.name}
                >
                  {entry.name}
                </a>
              ) : (
                <span title={entry.name}>{entry.name}</span>
              )}
              <span className="chat-attachment-meta">
                {entry.status === 'uploading'
                  ? 'Uploading…'
                  : entry.removing
                    ? 'Removing…'
                    : fileSize(entry.size)}
                {entry.attachment?.pages != null &&
                  ` · ${entry.attachment.pages} ${entry.attachment.pages === 1 ? 'page' : 'pages'}`}
                {entry.attachment?.media_type === 'application/pdf' && ' · Text only'}
              </span>
            </div>
            {remove && (
              <button
                type="button"
                aria-label={`Remove ${entry.name}`}
                title="Remove file"
                disabled={entry.removing}
                onClick={() => remove(entry.key)}
              >
                <X size={15} />
              </button>
            )}
          </div>
          {entry.error && (
            <p className="warning" role="alert">
              {entry.error}
            </p>
          )}
          {entry.attachment?.warnings.map((warning) => (
            <p key={warning} className="warning">
              {warning}
            </p>
          ))}
          {entry.attachment?.complete === false && !entry.attachment.warnings.length && (
            <p className="warning">Some file content could not be read.</p>
          )}
        </li>
      ))}
    </ul>
  );
}
