# Running the voice call

From PowerShell, run `& ".\scripts\start-all.ps1"` in this project. The launcher resolves its own checkout, starts services in the background, and waits for agent readiness. Running it again keeps this checkout's existing services. Use `& ".\scripts\stop-all.ps1"` to stop only the processes it started.

Open **https://localhost:3000**, allow microphone access, and press **Start call**. Wait for the agent to join. The microphone stays open; silence ends a turn automatically, and speaking during a reply interrupts it. A working microphone must be available to the browser. For LAN clients use this PC's current hostname/IP; the local HTTPS certificate must be trusted on both 3000 and 7443. A certificate for an old LAN address must be renewed if the address changes.

## Ports

| Port | Service |
| --- | --- |
| 3000 | HTTPS frontend |
| 7443 | TLS proxy for LiveKit signaling |
| 7880 | Local LiveKit signaling |
| 7881 TCP / 7882 UDP | LiveKit development media transport |
| 7444 | Preserved alternate WebSocket voice page |
| 9090 | Agent `/healthz`: 200 when runtime is ready, 503 while loading |
| 8081 | LiveKit agent worker health |
| 11434 | Existing Ollama daemon |

## Repairs

- Launchers resolve this checkout instead of an old absolute directory; stopping does not sweep unrelated Python/Node processes.
- The TLS proxy no longer applies its 10-second connection timeout to the entire call.
- Whisper/Silero/Piper are shared across warm worker threads, while conversation history and Ollama clients remain per-call.
- Piper keeps the existing binary and model loaded, with the original one-shot synthesis retained as fallback.
- Silence endpoint is 350 ms; Whisper honors the configured language. Actual transcription timing now includes lazy segment decoding.
- Generation cancellation covers pending HTTP headers, old streaming requests, queued synthesis, and call disconnects. Incomplete speech hypotheses no longer launch duplicate replies.
- `CLINIC_NUM_CTX=49152` preserves the full clinic prompt on this Gemma/Ollama build, which was truncating the input with the previous context setting. Cold startup still takes longer than an already warmed call.
- The frontend shows loading/listening/thinking/speaking states, exposes connection errors, and allows retry after a missing microphone or failed connection.
- The alternate WebSocket backend now processes silence completion without requiring an explicit end message; PCM scaling is corrected.

## Verification

- `python -m unittest discover -s tests -v`: three transport/VAD/PCM regressions passed.
- `npm run build`: passed, including TypeScript and ESLint checks (existing unused-code warnings remain).
- `python scripts/test_voice_call.py`: real LiveKit audio with generated questions, two calls and reconnect. Warm measurements: agent ready in 0.36–0.38 s; end of speech to received reply audio in 1.55–1.82 s. These are measured results on this machine, not a guaranteed latency.
- `python scripts/test_call_interrupt.py`: reply audio stopped about 0.52 s after interruption, and follow-up audio was received.
- TLS connection survived 12 seconds of complete silence and subsequently returned HTTP 200.
- Browser start/retry was checked. The test browser reported no available microphone, so acoustic microphone/echo behavior still requires a test with a physical microphone.

Changes were backed up under `logs/before-call-fix-20260928-183125` before editing. Existing models, clinic documents, routing components, and both voice transports were retained.

## LAN access

Open **https://192.168.197.45:3000** from a device on the same LAN. LiveKit advertises the Ethernet address via `LIVEKIT_NODE_IP`. The token endpoint uses the browser Host header, since Next.js may use `0.0.0.0` in its internal request URL. The certificate includes `192.168.197.45`, `127.0.0.1`, and `localhost`. Existing firewall rules allow TCP 3000/7443/7881 and UDP 7882. No router port forwarding is required.
