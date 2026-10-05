'use client';

import React, { useEffect, useMemo, useRef, useState } from 'react';
import { RoomEvent, Track } from 'livekit-client';
import { type HTMLMotionProps, motion } from 'motion/react';
import {
  BarVisualizer,
  useLocalParticipant,
  useRemoteParticipants,
  useSessionContext,
  useSessionMessages,
  useVoiceAssistant,
} from '@livekit/components-react';
import { MicrophoneIcon } from '@phosphor-icons/react/dist/ssr';
import type { AppConfig } from '@/app-config';
import { PreConnectMessage } from '@/components/app/preconnect-message';
import { TileLayout } from '@/components/app/tile-layout';
import {
  AgentControlBar,
  type ControlBarControls,
} from '@/components/livekit/agent-control-bar/agent-control-bar';
import { cn } from '@/lib/utils';
import { ScrollArea } from '../livekit/scroll-area/scroll-area';

const MotionBottom = motion.create('div');

const BOTTOM_VIEW_MOTION_PROPS: HTMLMotionProps<'div'> = {
  variants: {
    visible: {
      opacity: 1,
      translateY: '0%',
    },
    hidden: {
      opacity: 0,
      translateY: '100%',
    },
  },
  initial: 'hidden',
  animate: 'visible',
  exit: 'hidden',
  transition: {
    duration: 0.3,
    delay: 0.5,
    ease: 'easeOut',
  },
};

interface FadeProps {
  top?: boolean;
  bottom?: boolean;
  className?: string;
}

type NormalizedMessage = {
  id: string;
  timestamp: number;
  message: string;
  role: 'user' | 'assistant';
};

type RawDataMessage = {
  id: string;
  timestamp: number;
  message: string;
  role?: string;
  topic?: string;
  participant?: string;
};

type DiagnosticEvent = {
  time: number;
  label: string;
  detail?: string;
};

function StatusPill({ ok, label, value }: { ok: boolean; label: string; value: string }) {
  return (
    <div
      className={cn(
        'rounded-full border px-3 py-1 text-[11px] font-medium',
        ok
          ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-300'
          : 'border-destructive/30 bg-destructive/10 text-destructive'
      )}
    >
      <span className="opacity-70">{label}: </span>
      <span>{value}</span>
    </div>
  );
}

function safeJsonText(payload: Uint8Array) {
  try {
    return new TextDecoder().decode(payload);
  } catch {
    return '';
  }
}

function pickMessageText(obj: unknown) {
  if (!obj || typeof obj !== 'object') return '';

  const record = obj as Record<string, unknown>;
  const direct = record.message ?? record.text ?? record.transcript ?? record.content;

  if (typeof direct === 'string') return direct;

  if (Array.isArray(record.segments)) {
    return record.segments
      .map((segment) => {
        if (!segment || typeof segment !== 'object') return '';
        const segmentRecord = segment as Record<string, unknown>;
        const segmentText = segmentRecord.text ?? segmentRecord.message ?? segmentRecord.transcript;
        return typeof segmentText === 'string' ? segmentText : '';
      })
      .join(' ')
      .trim();
  }

  return '';
}

function pickMessageRole(obj: unknown, participantIdentity?: string) {
  if (!obj || typeof obj !== 'object') {
    return participantIdentity ? 'assistant' : undefined;
  }

  const record = obj as Record<string, unknown>;
  const role = record.role ?? record.type ?? record.speaker;

  if (role === 'user' || role === 'human') return 'user';
  if (role === 'assistant' || role === 'agent' || role === 'ai') return 'assistant';

  return participantIdentity ? 'assistant' : undefined;
}

function formatTime(ts: number) {
  return new Date(ts).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

function formatDebugTime(ts: number) {
  return new Date(ts).toLocaleTimeString([], {
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
  });
}

export function Fade({ top = false, bottom = false, className }: FadeProps) {
  return (
    <div
      className={cn(
        'from-background pointer-events-none h-4 bg-linear-to-b to-transparent',
        top && 'bg-linear-to-b',
        bottom && 'bg-linear-to-t',
        className
      )}
    />
  );
}

interface SessionViewProps {
  appConfig: AppConfig;
}

export const SessionView = ({
  appConfig,
  ...props
}: React.ComponentProps<'section'> & SessionViewProps) => {
  const session = useSessionContext();
  const { messages } = useSessionMessages(session);
  const { localParticipant, microphoneTrack, isMicrophoneEnabled, lastMicrophoneError } =
    useLocalParticipant();
  const remoteParticipants = useRemoteParticipants();
  const { state: agentState, audioTrack } = useVoiceAssistant();
  const [voiceState, setVoiceState] = useState('listening');

  // Always show transcript (ChatGPT-like log), no text input.
  const chatOpen = true;
  const scrollAreaRef = useRef<HTMLDivElement>(null);
  const [dataMessages, setDataMessages] = useState<RawDataMessage[]>([]);
  const [roomState, setRoomState] = useState<string>(session.room?.state ?? 'unknown');
  const [diagnosticEvents, setDiagnosticEvents] = useState<DiagnosticEvent[]>([]);
  const [lastRawPacket, setLastRawPacket] = useState<RawDataMessage | null>(null);

  const pushDiagnosticEvent = (label: string, detail?: string) => {
    setDiagnosticEvents((prev) => [{ time: Date.now(), label, detail }, ...prev].slice(0, 6));
  };

  // Listen to raw data packets (lk.chat / lk-chat-topic) as a fallback for chat log,
  // and keep all received data packets visible in diagnostics so we know whether
  // the frontend is receiving anything from LiveKit.
  useEffect(() => {
    const room = session.room;
    if (!room) return;

    setRoomState(room.state ?? 'unknown');

    const handleAttributes = (
      attributes: Record<string, string>,
      participant: { isAgent?: boolean }
    ) => {
      if (participant.isAgent && attributes['voice.state'])
        setVoiceState(attributes['voice.state']);
    };
    room.on(RoomEvent.ParticipantAttributesChanged, handleAttributes);
    const handleState = () => {
      setRoomState(room.state ?? 'unknown');
      pushDiagnosticEvent('room_state', String(room.state ?? 'unknown'));
    };

    const handleDataReceived = (
      payload: Uint8Array,
      participant: { identity?: string } | undefined,
      _kind?: unknown,
      topic?: string
    ) => {
      const txt = safeJsonText(payload);
      const now = Date.now();
      let parsed: unknown = null;

      try {
        parsed = JSON.parse(txt);
      } catch {
        parsed = null;
      }

      const message = pickMessageText(parsed) || txt;
      const role = pickMessageRole(parsed, participant?.identity);
      const idFromPayload =
        parsed && typeof parsed === 'object' && 'id' in parsed
          ? String((parsed as Record<string, unknown>).id)
          : undefined;
      const timestampFromPayload =
        parsed && typeof parsed === 'object' && 'timestamp' in parsed
          ? Number((parsed as Record<string, unknown>).timestamp)
          : undefined;

      const packet: RawDataMessage = {
        id: idFromPayload || `data-${now}-${Math.random().toString(16).slice(2)}`,
        timestamp: Number.isFinite(timestampFromPayload) ? Number(timestampFromPayload) : now,
        message,
        role,
        topic,
        participant: participant?.identity,
      };

      setLastRawPacket(packet);
      pushDiagnosticEvent(
        'data_packet',
        `topic=${topic || 'none'} from=${participant?.identity || 'unknown'}`
      );

      if (!message.trim()) return;

      if (topic === 'lk.chat' || topic === 'lk-chat-topic' || topic === undefined) {
        setDataMessages((prev) => [...prev, packet]);
      }
    };

    const handleTranscriptionReceived = (...args: unknown[]) => {
      pushDiagnosticEvent('transcription_event', `${args.length} args received`);
      console.log('[voice-debug] transcriptionReceived', args);
    };

    const handleParticipantConnected = (participant: { identity?: string; isAgent?: boolean }) => {
      pushDiagnosticEvent(
        participant.isAgent ? 'agent_joined' : 'participant_joined',
        participant.identity || 'unknown'
      );
    };

    const handleParticipantDisconnected = (participant: {
      identity?: string;
      isAgent?: boolean;
    }) => {
      pushDiagnosticEvent(
        participant.isAgent ? 'agent_left' : 'participant_left',
        participant.identity || 'unknown'
      );
    };

    const handleLocalTrackPublished = () => {
      pushDiagnosticEvent('local_track_published', 'microphone/camera changed');
    };

    const handleLocalTrackUnpublished = () => {
      pushDiagnosticEvent('local_track_unpublished', 'microphone/camera changed');
    };

    const handleLocalAudioSilence = () => {
      pushDiagnosticEvent('local_audio_silence', 'browser detected silence');
    };

    room.on(RoomEvent.ConnectionStateChanged, handleState);
    room.on(RoomEvent.Connected, handleState);
    room.on(RoomEvent.Reconnecting, handleState);
    room.on(RoomEvent.Reconnected, handleState);
    room.on(RoomEvent.Disconnected, handleState);
    room.on(RoomEvent.DataReceived, handleDataReceived);
    room.on(RoomEvent.TranscriptionReceived, handleTranscriptionReceived);
    room.on(RoomEvent.ParticipantConnected, handleParticipantConnected);
    room.on(RoomEvent.ParticipantDisconnected, handleParticipantDisconnected);
    room.on(RoomEvent.LocalTrackPublished, handleLocalTrackPublished);
    room.on(RoomEvent.LocalTrackUnpublished, handleLocalTrackUnpublished);
    room.on(RoomEvent.LocalAudioSilenceDetected, handleLocalAudioSilence);

    return () => {
      room.off(RoomEvent.ParticipantAttributesChanged, handleAttributes);
      room.off(RoomEvent.ConnectionStateChanged, handleState);
      room.off(RoomEvent.Connected, handleState);
      room.off(RoomEvent.Reconnecting, handleState);
      room.off(RoomEvent.Reconnected, handleState);
      room.off(RoomEvent.Disconnected, handleState);
      room.off(RoomEvent.DataReceived, handleDataReceived);
      room.off(RoomEvent.TranscriptionReceived, handleTranscriptionReceived);
      room.off(RoomEvent.ParticipantConnected, handleParticipantConnected);
      room.off(RoomEvent.ParticipantDisconnected, handleParticipantDisconnected);
      room.off(RoomEvent.LocalTrackPublished, handleLocalTrackPublished);
      room.off(RoomEvent.LocalTrackUnpublished, handleLocalTrackUnpublished);
      room.off(RoomEvent.LocalAudioSilenceDetected, handleLocalAudioSilence);
    };
  }, [session.room]);

  const displayMessages = useMemo(() => {
    const normalizeLivekit = messages.map<NormalizedMessage>((m, idx) => ({
      id: m.id ?? `${m.timestamp}-${idx}`,
      timestamp: m.timestamp ?? Date.now(),
      message: m.message ?? '',
      role: m.from?.isLocal ? 'user' : 'assistant',
    }));
    const normalizeData = dataMessages.map<NormalizedMessage>((m, idx) => ({
      id: m.id ?? `data-${m.timestamp}-${idx}`,
      timestamp: m.timestamp ?? Date.now(),
      message: m.message ?? '',
      role: m.role === 'user' ? 'user' : 'assistant',
    }));

    const combined = (normalizeData.length ? normalizeData : normalizeLivekit).filter(
      (m) => m.message.trim().length > 0
    );

    // collapse updates that reuse the same id (keep newest payload)
    const indexById = new Map<string, number>();
    const ordered: NormalizedMessage[] = [];
    combined.forEach((m) => {
      if (indexById.has(m.id)) {
        const pos = indexById.get(m.id)!;
        ordered[pos] = m;
        return;
      }
      indexById.set(m.id, ordered.length);
      ordered.push(m);
    });

    // drop consecutive duplicates (same role + identical text)
    const deduped = ordered.filter((m, i) => {
      const prev = ordered[i - 1];
      if (!prev) return true;
      return !(prev.role === m.role && prev.message.trim() === m.message.trim());
    });

    // Preserve chronological history from both transcription transports.
    return deduped.sort((a, b) => a.timestamp - b.timestamp);
  }, [messages, dataMessages]);

  const controls: ControlBarControls = {
    leave: true,
    microphone: true,
    chat: false, // voice-only UX
    camera: false,
    screenShare: false,
  };

  useEffect(() => {
    if (scrollAreaRef.current) {
      scrollAreaRef.current.scrollTop = scrollAreaRef.current.scrollHeight;
    }
  }, [displayMessages]);

  const microphonePublication = useMemo(() => {
    return Array.from(localParticipant.trackPublications.values()).find(
      (publication) => publication.source === Track.Source.Microphone
    );
  }, [localParticipant.trackPublications, microphoneTrack]);

  const agentParticipant = useMemo(() => {
    return remoteParticipants.find((participant) => participant.isAgent);
  }, [remoteParticipants]);

  const lastUserMessage = useMemo(() => {
    return [...displayMessages].reverse().find((message) => message.role === 'user');
  }, [displayMessages]);

  const lastAssistantMessage = useMemo(() => {
    return [...displayMessages].reverse().find((message) => message.role === 'assistant');
  }, [displayMessages]);

  const microphoneStatus = microphonePublication
    ? microphonePublication.isMuted
      ? 'muted'
      : 'published'
    : isMicrophoneEnabled
      ? 'enabled, not published'
      : 'off';

  const isRoomConnected = session.isConnected && roomState === 'connected';
  const isMicrophonePublished = Boolean(microphonePublication && !microphonePublication.isMuted);
  const isAgentAvailable = Boolean(agentParticipant);

  return (
    <section className="bg-background relative z-10 h-full w-full overflow-hidden" {...props}>
      {/* Voice Chat Log */}
      <div className="fixed inset-x-0 top-8 bottom-32 z-30 flex justify-center px-3 md:px-8">
        <div className="bg-background/90 border-input/40 w-full max-w-5xl overflow-hidden rounded-3xl border shadow-2xl shadow-black/30 backdrop-blur-md">
          <div className="text-muted-foreground flex flex-col gap-3 px-4 py-3 text-sm md:flex-row md:items-center md:justify-between">
            <div className="flex items-center gap-2">
              <MicrophoneIcon weight="bold" /> Voice call
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <StatusPill ok={isRoomConnected} label="Room" value={roomState} />
              <StatusPill ok={isMicrophonePublished} label="Mic" value={microphoneStatus} />
              <StatusPill
                ok={isAgentAvailable}
                label="Agent"
                value={agentParticipant?.identity || 'not joined'}
              />
              <StatusPill ok={agentState !== 'disconnected'} label="State" value={agentState} />
            </div>
          </div>

          <div
            className="border-input/40 flex items-center justify-center gap-4 border-t px-5 py-4"
            aria-live="polite"
          >
            <BarVisualizer
              state={agentState}
              trackRef={audioTrack}
              barCount={5}
              className="h-12 w-24"
            />
            <span className="text-sm font-medium">
              {!isRoomConnected
                ? 'Reconnecting…'
                : !isAgentAvailable || agentState === 'initializing' || agentState === 'connecting'
                  ? 'Loading your voice agent…'
                  : agentState === 'speaking' || voiceState === 'speaking'
                    ? 'Speaking — you can interrupt'
                    : voiceState === 'thinking'
                      ? 'Thinking…'
                      : 'Listening — speak naturally'}
            </span>
          </div>
          <details className="text-xs">
            <summary className="text-muted-foreground cursor-pointer px-4 py-2">
              Connection details
            </summary>
            <div className="border-input/40 bg-muted/30 text-muted-foreground border-y px-4 py-3 text-xs">
              <div className="grid gap-2 md:grid-cols-2">
                <div>
                  <span className="text-foreground font-semibold">Last user transcript: </span>
                  <span>{lastUserMessage?.message || 'no user transcript received yet'}</span>
                </div>
                <div>
                  <span className="text-foreground font-semibold">Last assistant reply: </span>
                  <span>{lastAssistantMessage?.message || 'no assistant reply received yet'}</span>
                </div>
                <div>
                  <span className="text-foreground font-semibold">Last data packet: </span>
                  <span>
                    {lastRawPacket
                      ? `${lastRawPacket.topic || 'no-topic'} / ${lastRawPacket.participant || 'unknown'}`
                      : 'none'}
                  </span>
                </div>
                <div>
                  <span className="text-foreground font-semibold">Mic error: </span>
                  <span>{lastMicrophoneError?.message || 'none'}</span>
                </div>
              </div>
              {diagnosticEvents.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-2">
                  {diagnosticEvents.map((event, index) => (
                    <span
                      key={`${event.time}-${index}`}
                      className="bg-background/70 rounded-md px-2 py-1"
                    >
                      {formatDebugTime(event.time)} {event.label}
                      {event.detail ? `: ${event.detail}` : ''}
                    </span>
                  ))}
                </div>
              )}
            </div>
          </details>
          <ScrollArea ref={scrollAreaRef} className="h-[calc(70vh-160px)] px-5 py-4">
            {displayMessages.length === 0 ? (
              <p className="text-muted-foreground text-sm">
                {isAgentAvailable
                  ? 'Speak naturally. Your conversation will appear here.'
                  : 'Preparing your session. Wait for the agent to join…'}
              </p>
            ) : (
              <div className="space-y-4">
                {displayMessages.map((m) =>
                  m.role === 'user' ? (
                    <div key={m.id} className="flex justify-end">
                      <div className="max-w-[65%] space-y-1 text-right">
                        <div className="bg-primary text-primary-foreground inline-flex rounded-full px-4 py-2 text-sm break-words whitespace-pre-wrap shadow-md">
                          {m.message || 'Listening...'}
                        </div>
                        <div className="text-muted-foreground flex items-center justify-end gap-1 text-[11px]">
                          <MicrophoneIcon className="h-4 w-4" weight="fill" />
                          {formatTime(m.timestamp)}
                        </div>
                      </div>
                    </div>
                  ) : (
                    <div key={m.id} className="flex justify-start">
                      <div className="bg-muted/80 text-foreground max-w-[78%] rounded-2xl px-4 py-3 text-base leading-6 shadow-inner">
                        <div className="break-words whitespace-pre-wrap">{m.message}</div>
                        <div className="text-muted-foreground mt-1 text-[11px]">
                          {formatTime(m.timestamp)}
                        </div>
                      </div>
                    </div>
                  )
                )}
              </div>
            )}
          </ScrollArea>
        </div>
      </div>

      {/* Tile Layout */}
      <TileLayout chatOpen={chatOpen} />

      {/* Bottom */}
      <MotionBottom
        {...BOTTOM_VIEW_MOTION_PROPS}
        className="fixed inset-x-3 bottom-0 z-50 md:inset-x-12"
      >
        {appConfig.isPreConnectBufferEnabled && (
          <PreConnectMessage messages={messages} className="pb-4" />
        )}
        <div className="bg-background relative mx-auto max-w-2xl pb-3 md:pb-12">
          <Fade bottom className="absolute inset-x-0 top-0 h-4 -translate-y-full" />
          <AgentControlBar
            controls={controls}
            isConnected={session.isConnected}
            onDisconnect={session.end}
          />
        </div>
      </MotionBottom>
    </section>
  );
};
