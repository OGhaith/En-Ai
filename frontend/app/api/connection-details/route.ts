import { NextResponse } from 'next/server';
import { AccessToken, type AccessTokenOptions, type VideoGrant } from 'livekit-server-sdk';
import { RoomConfiguration } from '@livekit/protocol';

type ConnectionDetails = {
  serverUrl: string;
  roomName: string;
  participantName: string;
  participantToken: string;
};

type ConnectionRequestBody = {
  room_config?: {
    agents?: Array<{
      agent_name?: string;
      agentName?: string;
    }>;
  };
  metadata?: Record<string, unknown>;
};

const API_KEY = requiredEnv('LIVEKIT_API_KEY');
const API_SECRET = requiredEnv('LIVEKIT_API_SECRET');
const LIVEKIT_URL = normalizeLiveKitUrl(
  process.env.NEXT_PUBLIC_LIVEKIT_URL ?? process.env.LIVEKIT_URL,
  'NEXT_PUBLIC_LIVEKIT_URL or LIVEKIT_URL'
);
const DEFAULT_AGENT_NAME = optionalEnv(
  process.env.LIVEKIT_AGENT_NAME ??
    process.env.NEXT_PUBLIC_LIVEKIT_AGENT_NAME ??
    process.env.NEXT_PUBLIC_AGENT_NAME
);
const ROOM_PREFIX = safePrefix(process.env.LIVEKIT_ROOM_PREFIX, 'voice_assistant_room');
const PARTICIPANT_PREFIX = safePrefix(
  process.env.LIVEKIT_PARTICIPANT_PREFIX,
  'voice_assistant_user'
);
const PARTICIPANT_NAME = optionalEnv(process.env.LIVEKIT_PARTICIPANT_NAME) ?? 'user';
const TOKEN_TTL = optionalEnv(process.env.LIVEKIT_TOKEN_TTL) ?? '15m';

// Never cache token responses.
export const revalidate = 0;
export const dynamic = 'force-dynamic';

export async function GET() {
  // Small health/config endpoint so you can verify the frontend is reading .env.local
  // without leaking LIVEKIT_API_SECRET.
  return NextResponse.json(
    {
      ok: true,
      livekitUrl: LIVEKIT_URL,
      apiKeyConfigured: Boolean(API_KEY),
      apiSecretConfigured: Boolean(API_SECRET),
      agentName: DEFAULT_AGENT_NAME ?? null,
      roomPrefix: ROOM_PREFIX,
      participantPrefix: PARTICIPANT_PREFIX,
    },
    {
      headers: { 'Cache-Control': 'no-store' },
    }
  );
}

export async function POST(req: Request) {
  try {
    const body = await safeJson<ConnectionRequestBody>(req);
    const requestAgentName = firstNonEmpty(
      body?.room_config?.agents?.[0]?.agent_name,
      body?.room_config?.agents?.[0]?.agentName
    );
    const agentName = requestAgentName ?? DEFAULT_AGENT_NAME;

    const participantIdentity = `${PARTICIPANT_PREFIX}_${randomSuffix()}`;
    const roomName = `${ROOM_PREFIX}_${randomSuffix()}`;

    const participantToken = await createParticipantToken(
      {
        identity: participantIdentity,
        name: PARTICIPANT_NAME,
        metadata: body?.metadata ? JSON.stringify(body.metadata) : undefined,
      },
      roomName,
      agentName
    );

    const data: ConnectionDetails = {
      serverUrl: browserServerUrl(req),
      roomName,
      participantToken,
      participantName: PARTICIPANT_NAME,
    };

    return NextResponse.json(data, {
      headers: { 'Cache-Control': 'no-store' },
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Unknown token generation error';
    console.error('[connection-details]', message);
    return new NextResponse(message, { status: 500 });
  }
}

function createParticipantToken(
  userInfo: AccessTokenOptions,
  roomName: string,
  agentName?: string
): Promise<string> {
  const at = new AccessToken(API_KEY, API_SECRET, {
    ...userInfo,
    ttl: TOKEN_TTL,
  });

  const grant: VideoGrant = {
    room: roomName,
    roomJoin: true,
    canPublish: true,
    canPublishData: true,
    canSubscribe: true,
  };

  at.addGrant(grant);

  if (agentName) {
    at.roomConfig = new RoomConfiguration({
      agents: [{ agentName }],
    });
  }

  return at.toJwt();
}

async function safeJson<T>(req: Request): Promise<T | undefined> {
  try {
    return (await req.json()) as T;
  } catch {
    return undefined;
  }
}

function requiredEnv(name: string) {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`${name} is not defined in .env.local`);
  }
  return value;
}

function optionalEnv(value: string | undefined) {
  const trimmed = value?.trim();
  return trimmed && trimmed.length > 0 ? trimmed : undefined;
}

function normalizeLiveKitUrl(value: string | undefined, label: string) {
  const raw = value?.trim();
  if (!raw) {
    throw new Error(`${label} is not defined in .env.local`);
  }

  if (raw.startsWith('wss://') || raw.startsWith('ws://')) return raw;
  if (raw.startsWith('https://')) return `wss://${raw.slice('https://'.length)}`;
  if (raw.startsWith('http://')) return `ws://${raw.slice('http://'.length)}`;

  return `wss://${raw}`;
}

function safePrefix(value: string | undefined, fallback: string) {
  return (optionalEnv(value) ?? fallback).replace(/[^a-zA-Z0-9_-]/g, '_');
}

function firstNonEmpty(...values: Array<string | undefined>) {
  for (const value of values) {
    const trimmed = optionalEnv(value);
    if (trimmed) return trimmed;
  }
  return undefined;
}

function randomSuffix() {
  return Math.random().toString(36).slice(2, 10);
}

// Opt-in for this local installation, so DHCP/IP changes do not break calls.
function browserServerUrl(req: Request) {
  if (process.env.LIVEKIT_USE_REQUEST_HOST !== '1') return LIVEKIT_URL;
  const url = new URL(LIVEKIT_URL);
  // Next.js can construct req.url from the listening address (0.0.0.0).
  // Host is the address the browser actually used to reach this LAN server.
  const host = req.headers.get('host');
  const hostname = host ? new URL(`https://${host}`).hostname : new URL(req.url).hostname;
  if (hostname !== '0.0.0.0' && hostname !== '[::]') url.hostname = hostname;
  return url.toString();
}
