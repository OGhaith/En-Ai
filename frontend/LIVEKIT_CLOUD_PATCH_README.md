# LiveKit Cloud patch

## What this patch changes

This patch keeps your frontend/backend structure as-is and only makes the frontend token/config layer easier to use with `.env.local`.

Changed files:

- `app-config.ts`
- `app/api/connection-details/route.ts`
- `.env.example`

It does **not** include `.env.local`, so your LiveKit API key and secret will not be overwritten.

## Required `.env.local`

Keep your existing `frontend/.env.local`, but make sure it has these values:

```env
NEXT_PUBLIC_LIVEKIT_URL=wss://YOUR_PROJECT.livekit.cloud
LIVEKIT_URL=wss://YOUR_PROJECT.livekit.cloud
LIVEKIT_API_KEY=YOUR_LIVEKIT_API_KEY
LIVEKIT_API_SECRET=YOUR_LIVEKIT_API_SECRET
```

Optional agent name:

```env
NEXT_PUBLIC_LIVEKIT_AGENT_NAME=your-agent-name
LIVEKIT_AGENT_NAME=your-agent-name
```

Leave agent name empty if your Python agent does not use a fixed `agent_name`.

## Quick check

Start the frontend:

```powershell
pnpm dev
```

Open:

```text
http://localhost:3000/api/connection-details
```

You should see JSON like:

```json
{
  "ok": true,
  "livekitUrl": "wss://YOUR_PROJECT.livekit.cloud",
  "apiKeyConfigured": true,
  "apiSecretConfigured": true
}
```

## Important

After changing `.env.local`, stop and restart `pnpm dev`.
