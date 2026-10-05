'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { type LocalAudioTrack, Track, createLocalAudioTrack } from 'livekit-client';
import { AnimatePresence, motion } from 'motion/react';
import { useSessionContext } from '@livekit/components-react';
import type { AppConfig } from '@/app-config';
import { SessionView } from '@/components/app/session-view';
import { WelcomeView } from '@/components/app/welcome-view';
import { APP_RTC_CONFIG } from '@/lib/ice';

const MotionWelcomeView = motion.create(WelcomeView);
const MotionSessionView = motion.create(SessionView);

const VIEW_MOTION_PROPS = {
  variants: {
    visible: {
      opacity: 1,
    },
    hidden: {
      opacity: 0,
    },
  },
  initial: 'hidden',
  animate: 'visible',
  exit: 'hidden',
  transition: {
    duration: 0.5,
    ease: 'linear' as const,
  },
};

interface ViewControllerProps {
  appConfig: AppConfig;
  connectionError: string;
  onConnectionFailed: (message: string) => void;
}

export function ViewController({
  appConfig,
  connectionError,
  onConnectionFailed,
}: ViewControllerProps) {
  const { isConnected, start, end, room } = useSessionContext();
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState(connectionError);
  const starting = useRef(false);
  const capture = useRef<LocalAudioTrack | null>(null);
  const connectionAttempt = useRef<AbortController | null>(null);
  useEffect(
    () => () => {
      connectionAttempt.current?.abort();
      capture.current?.stop();
    },
    []
  );
  const handleStartCall = useCallback(async () => {
    if (starting.current) return;

    // Across routed LAN/VPN subnets, WebRTC media can be blocked even though
    // HTTPS and WebSocket signalling work. The project already includes a
    // TLS WebSocket voice transport; use it directly for LAN callers so Start
    // Call opens a continuous low-latency voice session instead of timing out.
    const hostname = window.location.hostname;
    if (hostname !== 'localhost' && hostname !== '127.0.0.1' && hostname !== '::1') {
      window.location.assign(`https://${hostname}:7444/`);
      return;
    }

    starting.current = true;
    setConnecting(true);
    setError('');
    const controller = new AbortController();
    connectionAttempt.current = controller;
    let microphone: LocalAudioTrack | undefined;
    try {
      // Obtain capture permission before ICE gathering (particularly on Firefox).
      // useSession's default starts getUserMedia and room.connect concurrently.
      microphone = await createLocalAudioTrack({
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      });
      if (controller.signal.aborted) {
        microphone.stop();
        return;
      }
      capture.current = microphone;
      await start({
        signal: controller.signal,
        tracks: { microphone: { enabled: false } },
        roomConnectOptions: { rtcConfig: APP_RTC_CONFIG, peerConnectionTimeout: 20000 },
      });
      await room.localParticipant.publishTrack(microphone, { source: Track.Source.Microphone });
      capture.current = null; // Room.disconnect now owns track cleanup.
    } catch (err) {
      microphone?.stop();
      capture.current = null;
      try {
        await end();
      } catch {
        /* Cleanup must not hide the original error. */
      }
      onConnectionFailed(
        err instanceof Error && err.name === 'NotFoundError'
          ? 'No microphone was found. Connect a microphone and try again.'
          : err instanceof Error
            ? err.message
            : 'Could not connect. Please try again.'
      );
    } finally {
      connectionAttempt.current = null;
      starting.current = false;
      setConnecting(false);
    }
  }, [start, end, room, onConnectionFailed]);

  return (
    <AnimatePresence mode="wait">
      {/* Welcome view */}
      {!isConnected && (
        <MotionWelcomeView
          key="welcome"
          {...VIEW_MOTION_PROPS}
          startButtonText={appConfig.startButtonText}
          onStartCall={handleStartCall}
          connecting={connecting}
          error={error}
        />
      )}
      {/* Session view */}
      {isConnected && (
        <MotionSessionView key="session-view" {...VIEW_MOTION_PROPS} appConfig={appConfig} />
      )}
    </AnimatePresence>
  );
}
