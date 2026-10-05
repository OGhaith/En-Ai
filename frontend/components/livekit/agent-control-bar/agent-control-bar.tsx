'use client';

import { type HTMLAttributes, useCallback, useMemo, useState } from 'react';
import { Track } from 'livekit-client';
import {
  BarVisualizer,
  useChat,
  useRemoteParticipants,
  useVoiceAssistant,
} from '@livekit/components-react';
import { ChatTextIcon, PhoneDisconnectIcon } from '@phosphor-icons/react/dist/ssr';
import { TrackToggle } from '@/components/livekit/agent-control-bar/track-toggle';
import { Button } from '@/components/livekit/button';
import { Toggle } from '@/components/livekit/toggle';
import { cn } from '@/lib/utils';
import { ChatInput } from './chat-input';
import { UseInputControlsProps, useInputControls } from './hooks/use-input-controls';
import { usePublishPermissions } from './hooks/use-publish-permissions';
import { TrackSelector } from './track-selector';

export interface ControlBarControls {
  leave?: boolean;
  camera?: boolean;
  microphone?: boolean;
  screenShare?: boolean;
  chat?: boolean;
}

export interface AgentControlBarProps extends UseInputControlsProps {
  controls?: ControlBarControls;
  isConnected?: boolean;
  onChatOpenChange?: (open: boolean) => void;
  onDeviceError?: (error: { source: Track.Source; error: Error }) => void;
}

/**
 * A control bar specifically designed for voice assistant interfaces.
 *
 * This version intentionally shows connection diagnostics because the most common
 * voice bugs are not LLM bugs. Usually one of these is false:
 * 1. browser connected to LiveKit
 * 2. local microphone track is published
 * 3. backend agent joined the same room
 */
export function AgentControlBar({
  controls,
  saveUserChoices = true,
  className,
  isConnected = false,
  onDisconnect,
  onDeviceError,
  onChatOpenChange,
  ...props
}: AgentControlBarProps & HTMLAttributes<HTMLDivElement>) {
  const { send } = useChat();
  const { state: agentState, audioTrack: agentAudioTrack } = useVoiceAssistant();
  const participants = useRemoteParticipants();
  const [chatOpen, setChatOpen] = useState(false);
  const publishPermissions = usePublishPermissions();
  const {
    micTrackRef,
    cameraToggle,
    microphoneToggle,
    screenShareToggle,
    handleAudioDeviceChange,
    handleVideoDeviceChange,
    handleMicrophoneDeviceSelectError,
    handleCameraDeviceSelectError,
  } = useInputControls({ onDeviceError, saveUserChoices });

  const handleSendMessage = async (message: string) => {
    await send(message);
  };

  const handleToggleTranscript = useCallback(
    (open: boolean) => {
      setChatOpen(open);
      onChatOpenChange?.(open);
    },
    [onChatOpenChange, setChatOpen]
  );

  const visibleControls = {
    leave: controls?.leave ?? true,
    microphone: controls?.microphone ?? publishPermissions.microphone,
    screenShare: controls?.screenShare ?? publishPermissions.screenShare,
    camera: controls?.camera ?? publishPermissions.camera,
    chat: controls?.chat ?? publishPermissions.data,
  };

  const agentParticipant = useMemo(() => {
    return participants.find((participant) => participant.isAgent);
  }, [participants]);

  const isAgentAvailable = Boolean(agentParticipant);

  const micStatus = microphoneToggle.pending
    ? 'pending'
    : microphoneToggle.enabled
      ? 'mic on'
      : 'mic off';

  return (
    <div
      aria-label="Voice assistant controls"
      className={cn(
        'bg-background border-input/50 dark:border-muted flex flex-col rounded-[31px] border p-3 drop-shadow-md/3',
        className
      )}
      {...props}
    >
      {/* Chat input hidden for voice-first UI */}
      {visibleControls.chat && chatOpen && (
        <ChatInput
          chatOpen={chatOpen}
          isAgentAvailable={isAgentAvailable}
          onSend={handleSendMessage}
        />
      )}

      <div className="text-muted-foreground mb-2 flex flex-wrap items-center gap-2 px-1 text-[11px]">
        <span
          className={cn(
            'rounded-full border px-2 py-0.5',
            isConnected
              ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-300'
              : 'border-destructive/30 bg-destructive/10 text-destructive'
          )}
        >
          Room: {isConnected ? 'connected' : 'disconnected'}
        </span>
        <span
          className={cn(
            'rounded-full border px-2 py-0.5',
            microphoneToggle.enabled
              ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-300'
              : 'border-destructive/30 bg-destructive/10 text-destructive'
          )}
        >
          Mic: {micStatus}
        </span>
        <span
          className={cn(
            'rounded-full border px-2 py-0.5',
            isAgentAvailable
              ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-300'
              : 'border-destructive/30 bg-destructive/10 text-destructive'
          )}
        >
          Agent: {agentParticipant?.identity || 'waiting'}
        </span>
        <span className="border-input/50 bg-muted/40 rounded-full border px-2 py-0.5">
          State: {agentState}
        </span>
      </div>

      <div className="flex items-center gap-1">
        <div className="flex gap-1">
          {/* Toggle Microphone */}
          {visibleControls.microphone && (
            <TrackSelector
              kind="audioinput"
              aria-label="Toggle microphone"
              source={Track.Source.Microphone}
              pressed={microphoneToggle.enabled}
              disabled={microphoneToggle.pending}
              audioTrackRef={micTrackRef}
              onPressedChange={microphoneToggle.toggle}
              onMediaDeviceError={handleMicrophoneDeviceSelectError}
              onActiveDeviceChange={handleAudioDeviceChange}
            />
          )}

          {/* Toggle Camera */}
          {visibleControls.camera && (
            <TrackSelector
              kind="videoinput"
              aria-label="Toggle camera"
              source={Track.Source.Camera}
              pressed={cameraToggle.enabled}
              pending={cameraToggle.pending}
              disabled={cameraToggle.pending}
              onPressedChange={cameraToggle.toggle}
              onMediaDeviceError={handleCameraDeviceSelectError}
              onActiveDeviceChange={handleVideoDeviceChange}
            />
          )}

          {/* Toggle Screen Share */}
          {visibleControls.screenShare && (
            <TrackToggle
              size="icon"
              variant="secondary"
              aria-label="Toggle screen share"
              source={Track.Source.ScreenShare}
              pressed={screenShareToggle.enabled}
              disabled={screenShareToggle.pending}
              onPressedChange={screenShareToggle.toggle}
            />
          )}

          {/* Transcript toggle hidden in voice-only mode unless controls.chat=true */}
          {visibleControls.chat && (
            <Toggle
              size="icon"
              variant="secondary"
              aria-label="Toggle transcript"
              pressed={chatOpen}
              onPressedChange={handleToggleTranscript}
            >
              <ChatTextIcon weight="bold" />
            </Toggle>
          )}
        </div>

        {/* Compact bar visualizer in the center */}
        <div className="flex flex-1 items-center justify-center px-3">
          <BarVisualizer
            barCount={5}
            state={agentState}
            options={{ minHeight: 4 }}
            trackRef={agentAudioTrack}
            className="flex h-6 w-24 items-end justify-center gap-1"
          >
            <span className="bg-primary/80 data-[lk-highlighted=true]:bg-primary h-full w-1.5 rounded-full transition-[height,background-color] duration-150 ease-linear" />
          </BarVisualizer>
        </div>

        {/* Disconnect */}
        {visibleControls.leave && (
          <Button
            variant="destructive"
            onClick={onDisconnect}
            disabled={!isConnected}
            className="font-mono"
          >
            <PhoneDisconnectIcon weight="bold" />
            <span className="hidden md:inline">END CALL</span>
            <span className="inline md:hidden">END</span>
          </Button>
        )}
      </div>
    </div>
  );
}
