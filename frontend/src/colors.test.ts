import { describe, expect, it } from 'vitest';
import { sourceSyncStatus } from './colors';

describe('source sync indicators', () => {
  it('does not show a connected source as healthy after a failed or partial sync', () => {
    const source = { status: 'connected', syncing: false };
    expect(sourceSyncStatus({ ...source, last_outcome: 'auth_required' }).color).toBe('#ee897e');
    expect(sourceSyncStatus({ ...source, last_outcome: 'partial' }).color).toBe('#d8b370');
    expect(
      sourceSyncStatus({ ...source, last_outcome: 'success', warnings: ['Missing materials'] })
        .color,
    ).toBe('#d8b370');
    expect(sourceSyncStatus({ ...source, last_outcome: 'success' }).color).toBe('#72c89b');
  });

  it('does not reuse an old success after disconnecting or assume login means synced', () => {
    expect(
      sourceSyncStatus({ status: 'not_connected', syncing: false, last_outcome: 'success' }).color,
    ).toBe('#d8b370');
    expect(sourceSyncStatus({ status: 'connected', syncing: false }).label).toBe('Not synced yet');
  });

  it('shows the current sync while retrying an earlier failure', () => {
    expect(
      sourceSyncStatus({ status: 'connected', syncing: true, last_outcome: 'network_error' }),
    ).toEqual({ color: '#d8b370', label: 'Syncing…' });
  });
});
