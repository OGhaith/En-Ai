'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Room, RoomEvent, TokenSource } from 'livekit-client';
import {
  RoomAudioRenderer,
  SessionProvider,
  StartAudio,
  useSession,
} from '@livekit/components-react';
import type { AppConfig } from '@/app-config';
import { ViewController } from '@/components/app/view-controller';
import { Toaster } from '@/components/livekit/toaster';
import { useAgentErrors } from '@/hooks/useAgentErrors';
import { useDebugMode } from '@/hooks/useDebug';
import { getSandboxTokenSource } from '@/lib/utils';

const IN_DEVELOPMENT = process.env.NODE_ENV !== 'production';

function AppSetup() {
  useDebugMode({ enabled: IN_DEVELOPMENT });
  useAgentErrors();

  return null;
}

interface AppProps {
  appConfig: AppConfig;
}

export function App({ appConfig }: AppProps) {
  const [attempt, setAttempt] = useState(0);
  const [connectionError, setConnectionError] = useState('');
  const resetSession = useCallback((message = '') => {
    setConnectionError(message);
    setAttempt((value) => value + 1);
  }, []);

  return (
    <CallSession
      key={attempt}
      appConfig={appConfig}
      connectionError={connectionError}
      onReset={resetSession}
    />
  );
}

function CallSession({
  appConfig,
  connectionError,
  onReset,
}: AppProps & {
  connectionError: string;
  onReset: (message?: string) => void;
}) {
  // A single bundled peer connection needs one ICE negotiation instead of two.
  // Splitting publish/subscribe into separate connections doubled the candidate
  // checks, and on this cross-subnet LAN the extra subscriber PC was the one
  // that never reached `connected`. A failed or ended call unmounts this
  // component, so retries never reuse a failed ICE engine.
  const room = useMemo(() => new Room({ singlePeerConnection: true }), []);
  const resetSent = useRef(false);
  const resetSession = useCallback(
    (message = '') => {
      if (resetSent.current) return;
      resetSent.current = true;
      onReset(message);
    },
    [onReset]
  );
  useEffect(() => {
    let connected = false;
    const onConnected = () => {
      connected = true;
    };
    const onDisconnected = () => {
      if (connected) resetSession();
    };
    room.on(RoomEvent.Connected, onConnected);
    room.on(RoomEvent.Disconnected, onDisconnected);
    return () => {
      room.off(RoomEvent.Connected, onConnected);
      room.off(RoomEvent.Disconnected, onDisconnected);
      void room.disconnect();
    };
  }, [room, resetSession]);

  const tokenSource = useMemo(() => {
    return typeof process.env.NEXT_PUBLIC_CONN_DETAILS_ENDPOINT === 'string'
      ? getSandboxTokenSource(appConfig)
      : TokenSource.endpoint('/api/connection-details');
  }, [appConfig]);

  const session = useSession(tokenSource, {
    room,
    ...(appConfig.agentName ? { agentName: appConfig.agentName } : {}),
  });

  return (
    <SessionProvider session={session}>
      <AppSetup />
      <main className="grid h-svh grid-cols-1 place-content-center">
        <ViewController
          appConfig={appConfig}
          connectionError={connectionError}
          onConnectionFailed={resetSession}
        />
      </main>
      <StartAudio label="Start Audio" />
      <RoomAudioRenderer />
      <Toaster />
    </SessionProvider>
  );
}
