export interface AppConfig {
  pageTitle: string;
  pageDescription: string;
  companyName: string;

  supportsChatInput: boolean;
  supportsVideoInput: boolean;
  supportsScreenShare: boolean;
  isPreConnectBufferEnabled: boolean;

  logo: string;
  startButtonText: string;
  accent?: string;
  logoDark?: string;
  accentDark?: string;

  // for LiveKit Cloud Sandbox / LiveKit Agents dispatch
  sandboxId?: string;
  agentName?: string;
}

function envString(value: string | undefined, fallback: string) {
  const trimmed = value?.trim();
  return trimmed && trimmed.length > 0 ? trimmed : fallback;
}

function envOptionalString(value: string | undefined) {
  const trimmed = value?.trim();
  return trimmed && trimmed.length > 0 ? trimmed : undefined;
}

function envBoolean(value: string | undefined, fallback: boolean) {
  const normalized = value?.trim().toLowerCase();

  if (!normalized) return fallback;
  if (['1', 'true', 'yes', 'y', 'on'].includes(normalized)) return true;
  if (['0', 'false', 'no', 'n', 'off'].includes(normalized)) return false;

  return fallback;
}

export const APP_CONFIG_DEFAULTS: AppConfig = {
  companyName: envString(process.env.NEXT_PUBLIC_COMPANY_NAME, 'Voice AI'),
  pageTitle: envString(process.env.NEXT_PUBLIC_PAGE_TITLE, 'Voice AI Assistant'),
  pageDescription: envString(
    process.env.NEXT_PUBLIC_PAGE_DESCRIPTION,
    'Chat live with your voice AI assistant'
  ),

  // Voice-first defaults. Override them from .env.local if needed.
  supportsChatInput: envBoolean(process.env.NEXT_PUBLIC_SUPPORTS_CHAT_INPUT, true),
  supportsVideoInput: envBoolean(process.env.NEXT_PUBLIC_SUPPORTS_VIDEO_INPUT, false),
  supportsScreenShare: envBoolean(process.env.NEXT_PUBLIC_SUPPORTS_SCREEN_SHARE, false),
  isPreConnectBufferEnabled: envBoolean(
    process.env.NEXT_PUBLIC_PRECONNECT_BUFFER_ENABLED,
    true
  ),

  logo: envString(process.env.NEXT_PUBLIC_LOGO, '/lk-logo.svg'),
  accent: envString(process.env.NEXT_PUBLIC_ACCENT, '#00c2ff'),
  logoDark: envString(process.env.NEXT_PUBLIC_LOGO_DARK, '/lk-logo-dark.svg'),
  accentDark: envString(process.env.NEXT_PUBLIC_ACCENT_DARK, '#00c2ff'),
  startButtonText: envString(process.env.NEXT_PUBLIC_START_BUTTON_TEXT, 'Start call'),

  // If your Python agent is registered with an agent_name, put it in .env.local:
  // NEXT_PUBLIC_LIVEKIT_AGENT_NAME=your-agent-name
  // If your agent works without a fixed name, leave it empty.
  sandboxId: envOptionalString(process.env.NEXT_PUBLIC_SANDBOX_ID),
  agentName: envOptionalString(
    process.env.NEXT_PUBLIC_LIVEKIT_AGENT_NAME ?? process.env.NEXT_PUBLIC_AGENT_NAME
  ),
};
