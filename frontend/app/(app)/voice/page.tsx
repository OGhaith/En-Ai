'use client';

import { useEffect, useState } from 'react';

interface VoiceConfig {
  agent?: string;
  enabled?: boolean;
  prompt_active?: boolean;
  qa_injected?: boolean;
  unset_vars?: string[];
  model_env?: string;
}

export default function VoiceRoute() {
  const [url, setUrl] = useState('');
  const [cfg, setCfg] = useState<VoiceConfig | null>(null);
  const [cfgErr, setCfgErr] = useState('');

  useEffect(() => {
    const host = window.location.hostname || 'localhost';
    const base = `https://${host}:7444`;
    setUrl(`${base}/`);

    // Read the SAME runtime config the audio server uses (via /voice_config).
    fetch(`${base}/voice_config`, { cache: 'no-store' })
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data: VoiceConfig) => setCfg(data))
      .catch((e) => setCfgErr(e?.message || 'unreachable'));
  }, []);

  return (
    <main
      style={{
        padding: 24,
        font: '14px/1.5 system-ui, sans-serif',
        background: '#111',
        color: '#eee',
        minHeight: '100vh',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        textAlign: 'center',
      }}
    >
      <h1 style={{ marginTop: 0 }}>
        {cfg?.agent || 'Local Voice AI'} — websocket path (no WebRTC)
      </h1>

      {cfg && (
        <div
          style={{
            marginBottom: 8,
            padding: '10px 16px',
            borderRadius: 8,
            background: cfg.enabled ? '#102a17' : '#2a1616',
            color: cfg.enabled ? '#7fe0a0' : '#f0a0a0',
            fontSize: 12,
          }}
        >
          clinic prompt {cfg.enabled ? 'active' : 'inactive'}
          {cfg.prompt_active ? ` · Q&A ${cfg.qa_injected ? 'injected' : 'missing'}` : ''}
          {cfg.model_env ? ` · model ${cfg.model_env}` : ''}
          {cfg.unset_vars && cfg.unset_vars.length > 0
            ? ` · unset vars: ${cfg.unset_vars.join(', ')}`
            : ''}
        </div>
      )}
      {cfgErr && (
        <p style={{ color: '#999', marginBottom: 8, fontSize: 12 }}>
          /voice_config unavailable ({cfgErr}) — showing defaults.
        </p>
      )}

      <p style={{ maxWidth: 520 }}>
        The standalone voice page serves from the same HTTPS origin as the audio websocket, so there
        is no mixed-content or cross-origin problem.
      </p>
      <a
        href={url || '/voice'}
        style={{
          marginTop: 16,
          display: 'inline-block',
          padding: '12px 22px',
          background: '#1a5fb4',
          color: '#fff',
          borderRadius: 8,
          textDecoration: 'none',
          fontWeight: 600,
        }}
      >
        Open voice page {url ? `(${url})` : ''}
      </a>
      <p style={{ color: '#999', marginTop: 20, fontSize: 12 }}>
        If it does not open, type the URL above directly. Accept the self-signed cert the first
        time.
      </p>
    </main>
  );
}
