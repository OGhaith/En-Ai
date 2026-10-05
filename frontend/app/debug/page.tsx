'use client';

import { useCallback, useState } from 'react';
import { Room, RoomEvent } from 'livekit-client';
import { APP_RTC_CONFIG } from '@/lib/ice';

type Log = { t: number; m: string };

export default function DebugPage() {
  const [logs, setLogs] = useState<Log[]>([]);
  const [busy, setBusy] = useState(false);

  const add = useCallback((m: string) => {
    setLogs((p) => [...p, { t: Date.now(), m }]);
  }, []);

  const run = useCallback(
    async (policy: 'all' | 'relay') => {
      setBusy(true);
      setLogs([]);
      let room: Room | undefined;
      try {
        const res = await fetch('/api/connection-details', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ participantName: 'dbg' + Math.floor(Math.random() * 10000) }),
        });
        const j = await res.json();
        add('serverUrl=' + j.serverUrl);

        room = new Room({
          adaptiveStream: false,
          dynacast: false,
          singlePeerConnection: true,
        });
        room.on(RoomEvent.SignalConnected, () => add('signal connected'));
        room.on(RoomEvent.ConnectionStateChanged, (s) => add('conn state: ' + s));
        room.on(RoomEvent.Disconnected, (reason) => add('disconnected: ' + String(reason ?? '')));

        const t0 = performance.now();
        const opts = {
          autoSubscribe: false,
          peerConnectionTimeout: 20000,
          rtcConfig: {
            ...APP_RTC_CONFIG,
            iceTransportPolicy: policy,
          } as RTCConfiguration,
        };
        await room.connect(j.serverUrl, j.participantToken, opts);
        add(
          'CONNECT OK in ' + ((performance.now() - t0) / 1000).toFixed(2) + 's, state=' + room.state
        );
        await new Promise((r) => setTimeout(r, 1500));
        await room.disconnect();
        add('disconnected ok');
      } catch (e) {
        add('FAILED: ' + ((e as Error).message ?? String(e)));
      } finally {
        await room?.disconnect();
        setBusy(false);
      }
    },
    [add]
  );

  return (
    <main
      style={{
        padding: 24,
        font: '14px/1.5 monospace',
        background: '#111',
        color: '#eee',
        minHeight: '100vh',
      }}
    >
      <h1 style={{ marginTop: 0 }}>WebRTC debug</h1>
      <button disabled={busy} onClick={() => run('all')}>
        connect (UDP+TCP)
      </button>
      <button disabled={busy} onClick={() => run('relay')} style={{ marginLeft: 8 }}>
        connect (relay only)
      </button>
      <button disabled={busy} onClick={() => run('relay')} style={{ marginLeft: 8 }} />
      <pre style={{ whiteSpace: 'pre-wrap', marginTop: 16 }}>
        {logs.map((l) => `[${((l.t - (logs[0]?.t ?? 0)) / 1000).toFixed(2)}s] ${l.m}`).join('\n')}
      </pre>
    </main>
  );
}
