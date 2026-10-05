# Local Voice AI — Project Specification

**Status:** Living specification, derived from source inspection.
**Scope:** What the system *is* — architecture, components, models, configuration surface, interfaces, and known defects. It deliberately contains **no setup or operating procedures**.
**Audience:** Operators and developers who need to understand, extend, or debug the system.
**For how to run it:** see the root [`README.md`](../README.md).

> **Secret handling:** every credential in this document is written as a placeholder
> (`<LIVEKIT_API_KEY>`, `<LIVEKIT_API_SECRET>`, …). No live secret value is reproduced here.

---

## Table of Contents

1. [Project Summary](#1-project-summary)
2. [Architecture](#2-architecture)
3. [Technology Stack](#3-technology-stack)
4. [Hardware and Runtime Requirements](#4-hardware-and-runtime-requirements)
5. [Model Specifications](#5-model-specifications)
6. [Module Map](#6-module-map)
7. [Configuration Surface](#7-configuration-surface)
8. [Network Interfaces and Ports](#8-network-interfaces-and-ports)
9. [Data Formats and Persisted Artifacts](#9-data-formats-and-persisted-artifacts)
10. [Prompt Assembly Pipeline](#10-prompt-assembly-pipeline)
11. [Audio Signal Processing](#11-audio-signal-processing)
12. [Dependencies and Licensing](#12-dependencies-and-licensing)
13. [Known Issues and Inconsistencies](#13-known-issues-and-inconsistencies)
14. [Security Notes](#14-security-notes)

---

## 1. Project Summary

A fully on-premises, real-time voice assistant. Audio is captured in a browser, transcribed
locally, answered by a local LLM, and spoken back by a local neural TTS engine. No audio,
transcript, or conversation content leaves the machine through the inference stack.

The codebase originated as a general-purpose "JARVIS" voice agent and has since been
retargeted at a radiology clinic. Two identities coexist in the repository and are not fully
reconciled — see [§13](#13-known-issues-and-inconsistencies).

**Current configured deployment identity** (from `configs/clinic_runtime.json`):

| Field | Value |
|---|---|
| Agent name | `Tower Radiology at National Harbor` |
| Language | `English` |
| Prompt template | `Prompt_tower_R_Restructured.docx` |
| Knowledge base | `Clinic ai q&a - National.docx` |

The project is **not purely offline**: WebRTC connectivity uses public STUN/TURN relays, so
metadata about the call is exposed to third parties even though all inference is local
([§14](#14-security-notes)).

---

## 2. Architecture

### 2.1 Component overview

The system is a set of cooperating local processes. There is no central orchestrator; each
service is independent and communicates over the network.

```
┌───────────────────────────────────────────────────────────────────────────┐
│                              Browser (Next.js)                            │
│                    frontend/app/(app)/voice/page.tsx                      │
└───────────────┬───────────────────────────────────┬───────────────────────┘
                │ WebRTC (audio)                     │ WSS (audio + JSON)
                ▼                                   ▼
┌───────────────────────────────┐   ┌──────────────────────────────────────┐
│  LiveKit Server (local SFU)   │   │   WebSocket Voice Server             │
│  livekit_server/*.exe --dev   │   │   src/ws_voice_server.py  :7444      │
│  ws://…:7880  wss://…:7443    │   │   no WebRTC, no SFU                  │
└───────────────┬───────────────┘   └──────────────────┬───────────────────┘
                │ agent dispatch (auto-join)            │
                ▼                                       │
┌───────────────────────────────────────────┐           │
│  Python Agent Glue                        │           │
│  livekit_agent/src/agent.py  (CLI entry)  │           │
│    └─ src/voice_server.py   (AgentServer) │           │
└───────────────┬───────────────────────────┘           │
                │                                       │
                └───────────────┬───────────────────────┘
                                ▼
              ┌─────────────────────────────────────┐
              │        Shared Conversation Core      │
              │  src/agent_core.py  ·  Brain        │
              │  src/router/  ·  src/tools.py        │
              └──────┬───────────┬───────────┬───────┘
                     │           │           │
        ┌────────────▼──┐  ┌─────▼──────┐  ┌─▼──────────────┐
        │ faster-whisper│  │   Ollama   │  │     Piper      │
        │      STT      │  │    LLM     │  │      TTS       │
        │  CUDA float16 │  │  local GGUF│  │  ONNX, 16 kHz  │
        └───────────────┘  └────────────┘  └────────────────┘
```

### 2.2 Voice Path A — LiveKit / WebRTC (primary)

The production path. Chosen for NAT traversal, echo cancellation, and device handling.

| Stage | Component | Detail |
|---|---|---|
| Capture | Browser `getUserMedia` | Requires a secure context; the frontend is served over self-signed HTTPS |
| Transport | LiveKit SFU | `livekit-server.exe --dev`, plaintext `ws://` on 7880, TLS-terminated `wss://` on 7443 via a local proxy |
| Join | `frontend/app/api/connection-details/route.ts` | Server-side mints a short-lived participant JWT |
| Dispatch | `livekit_agent/src/agent.py` | LiveKit auto-joins the agent to the room |
| VAD | Silero (`livekit-plugins-silero`) | Two instances: one inside the STT stream adapter, one on the `AgentSession` for barge-in |
| STT | `src/whisper_stt.py` via `LocalWhisperSTT` in `src/voice_server.py` | A `livekit.agents.stt.STT` subclass wrapping `faster-whisper` |
| LLM | `src/ollama_client.py` | Streaming token generator against the local Ollama daemon |
| TTS | `src/piper_tts.py` via `PiperSynth` in `src/voice_server.py` | `piper.exe` subprocess; audio played through a `sounddevice` worker |
| Playback | LiveKit audio track | |

`src/voice_server.py` (1375 lines) is the centre of this path. It contains the DSP
preprocessors, the `Brain` conversation class, the `PiperSynth` wrapper, the `LocalWhisperSTT`
plugin, model prewarming, and the `voice_session` job entry point.

### 2.3 Voice Path B — WebSocket (secondary, no WebRTC)

`src/ws_voice_server.py` (803 lines) is a self-contained alternative described in its own
docstring as *"the old, LiveKit-free voice path"*. It exists to remove the SFU from the
critical path.

| Aspect | Specification |
|---|---|
| Protocol | Plain WebSocket, `ws://0.0.0.0:7444` |
| Wire format | Browser → server: raw Int16 PCM, 16 kHz mono. Server → browser: JSON reply text **and** a separate binary WAV frame |
| Turn end | Three triggers: silence-based auto-end, an explicit push-to-talk `"end"` message, and a `VAD_MAX_S` timeout |
| Endpointing | Threshold-based, not Silero — independent `VAD_START` / `VAD_STOP` levels plus an adaptive `VAD_GAIN` pre-gain for quiet microphones |
| Model loading | Locates `model.bin` under `models/hub`, walking into `snapshots/<hash>/` when the cache is laid out that way |
| Shared core | Reuses the same `OllamaClient`, `clinic_prompt`, and Piper synthesis as Path A |
| Frontend | No LiveKit client; a dedicated page drives raw PCM over the socket |

Both paths log to separate CSVs (`data/chat_log.csv` for LiveKit, `data/ws_chat_log.csv` for
the WebSocket path).

### 2.4 Conversation core

`src/agent_core.py` orchestrates routing and tool use, and is shared by both paths.

```
user turn
   │
   ├─► intent classification     src/router/intent.py
   │      yes / no / affirm / deny / booking-request regex
   │      + optional Ollama classifier (model: qwen2.5:7b-instruct)
   │
   ├─► role routing             src/router/router.py
   │      role inferred from the keys of configs/role_tools.json
   │      + optional Ollama inference (model: qwen2.5:7b-instruct)
   │      + string-match fallback
   │      ▼
   │      "general"  ← current effective result, see §13.4
   │
   ├─► tool gate                 src/router/session.py
   │      active_tool · pending_action · tool_allowed
   │
   ├─► tool execution            src/tools.py
   │      sandboxed math expression evaluator
   │
   └─► LLM turn                  src/ollama_client.py
          clinic system prompt + rolling history
```

**Session state** (`src/router/session.py`) is a dataclass carrying `active_tool`,
`pending_action`, `tool_allowed`, and a bounded message history (8 messages by default in the
LiveKit path, 10 in the `Brain` constructor default).

**Slot filling.** `src/agent_core.py` extracts phone numbers, names, and ISO dates from user
text with pattern matching before dispatching to tools — the classic radiology-appointment
slot-filling flow (name, phone, date, department, extension).

### 2.5 Prompt selection

At request time, `Brain._chat_system_prompt()` resolves the system prompt in this order:

1. `src/clinic_prompt.get_system_prompt()` — the rendered clinic DOCX prompt, **if any DOCX is usable**.
2. `configs/agents/role.txt` — the JARVIS persona, used as fallback.
3. Empty string.

`configs/agents/base.txt` is loaded into `Brain.base_prompt` at construction but is *not*
part of the chat path; it is consulted only in tool mode. See [§10](#10-prompt-assembly-pipeline).

---

## 3. Technology Stack

### 3.1 Runtime components

| Layer | Technology | Version | Role |
|---|---|---|---|
| SFU | LiveKit Server (`livekit-server.exe`) | `--dev` mode | WebRTC media routing |
| Agent framework | `livekit-agents` | `1.3.12` | Session, job lifecycle, VAD plugins |
| LiveKit client lib | `livekit` | `1.0.23` | RTC room participation |
| VAD | `livekit-plugins-silero` | `1.3.12` | Speech onset, endpointing, barge-in |
| Turn detection | `livekit-plugins-turn-detector` | `1.3.12` | Semantic turn taking |
| STT | `faster-whisper` | `1.2.1` | Speech-to-text |
| STT runtime | `ctranslate2` | `4.6.3` | Whisper inference backend |
| LLM server | Ollama | external daemon | Local GGUF inference |
| LLM client | `httpx` | `0.28.1` | `/api/chat` streaming |
| TTS | Piper (`bin/piper.exe`) | external binary | Neural TTS |
| TTS runtime | `onnxruntime` | `1.23.1` | ONNX model execution |
| Audio I/O | `sounddevice` | `0.5.5` | TTS playback worker |
| DSP | `numpy` / `scipy` | `2.2.6` / `1.15.3` | Filtering, resampling, framing |
| Endpointing (Path B) | `webrtcvad-wheels` | `2.0.14` | Silence detection |
| WebSocket | `websockets` | `16.0` | Path B transport |
| Frontend | Next.js + React | `15.5.12` / `19` | Browser client |
| LiveKit UI | `@livekit/components-react` | `^2.9.15` | Prebuilt call UI primitives |
| Token minting | `livekit-server-sdk` | `^2.13.2` | Participant JWT |
| Auth (JWT) | `jose` | `^6.0.12` | Token helpers |
| Styling | Tailwind CSS | `^4` | UI |
| Package manager | pnpm | `9.15.9` | Frontend |
| Python | CPython | `>=3.10` (agent pkg declares `>=3.9`) | Runtime |

### 3.2 Notable version constraint

`ctranslate2 4.6.3` on CUDA supports **only** `float16`, `bfloat16`, and `float32` compute
types. The `int8*` family is unsupported. This is recorded inline at `.env.local:6` and is
the reason the deployment pins `WHISPER_COMPUTE_TYPE=float16` on an RTX 5060 (`sm_120`).

### 3.3 Legacy dependencies

`torch`, `torchaudio`, and `torchvision` (`2.5.1+cu121`) are pinned in `requirements.txt` but
are **not** on the inference path — `faster-whisper` runs on CTranslate2 and Piper on
ONNX Runtime. They are almost certainly transitive or historical. They substantially inflate
install size and CUDA driver requirements.

---

## 4. Hardware and Runtime Requirements

### 4.1 Development/deployment machine (as currently configured)

| Component | Specification |
|---|---|
| CPU | AMD Ryzen 5 9600X (6 cores / 12 threads) |
| GPU | NVIDIA GeForce RTX 5060, 8151 MiB VRAM, `sm_120` |
| System RAM | 15.2 GB |
| OS | Windows, PowerShell shell |
| GPU driver | Must support `sm_120` and CUDA 12.1+ |

`PIPER_THREADS=6` in `.env.local` matches the physical core count.

### 4.2 VRAM budget

Whisper (`large-v3-turbo`, float16) and Ollama (`gemma4:4b`, Q4_K_M) share the 8 GB card.
Silero VAD is deliberately pinned to `cpu` (`.env.local:36`) to leave VRAM for the two large
models and to avoid a per-chunk GPU round-trip.

### 4.3 Minimum viable hardware

The repository's own `voice/README.md` documents an earlier 4 GB RTX 3050 dev box on which
XTTS v2 and Ollama could not co-reside. That constraint motivated the decision to keep Piper
as the live TTS backend. Whisper large-v3-turbo plus a 4B-class LLM realistically require
≥8 GB VRAM.

### 4.4 External requirements

- Ollama installed and running, listening on `http://127.0.0.1:11434`. The client defaults to
  the literal IP rather than `localhost` to avoid IPv6 resolution failures on Windows
  (`src/ollama_client.py`). Overridable with `OLLAMA_HOST`.
- Node.js 18+ for the frontend.
- A self-signed TLS certificate pair in `certs/` (`livekit.pem`, `livekit-key.pem`) for
  browser microphone access on non-`localhost` origins.

---

## 5. Model Specifications

| Role | Model | Quantization | Runtime | Notes |
|---|---|---|---|---|
| STT | `large-v3-turbo` | float16 | `faster-whisper` / CTranslate2 on CUDA | Cached under `models/hub/`; `beam_size=1`, `temperature=0.0`, language `en` |
| LLM (primary) | `gemma4:4b` | Q4_K_M (`gemma-4-E4B-it-Q4_K_M.gguf`) | Ollama | A **thinking** model — requires `OLLAMA_THINK=false` |
| LLM (classifier) | `qwen2.5:7b-instruct` | as pulled | Ollama | Hard-coded for role routing and intent classification |
| TTS | `en_US-danny-low` | ONNX | Piper via `onnxruntime` | `low` quality tier, 16 kHz output |
| VAD | Silero VAD | packaged | `livekit-plugins-silero` | CPU inference |

### 5.1 The `OLLAMA_THINK` requirement

`gemma4:4b` is a reasoning model. With thinking enabled it consumes the entire token budget
in the `thinking` field and returns an **empty `content`**, so the agent never speaks. The
client therefore maps `OLLAMA_THINK` to a tri-state flag:

- `false` → send `think: false`
- `true` → send `think: true`
- `auto` → omit the flag entirely

This is a hard operational requirement, not a tuning knob. Any substitution of a
non-thinking model can relax it.

### 5.2 Second-model requirement

Role routing and intent classification issue a **separate** Ollama request pinned to
`qwen2.5:7b-instruct` (`src/router/router.py:43`, `src/router/intent.py:54`). A deployment
that pulls only `gemma4:4b` will hit a missing-model error on those paths. Today the failure
is masked: both call sites have string-matching fallbacks. See [§13.5](#135-role-routing-is-inert).

### 5.3 Model path conventions

| Asset | Path |
|---|---|
| Whisper cache | `models/hub/` (override: `HUGGINGFACE_HUB_CACHE`) |
| Piper voice | `models/piper/en_US-danny-low.onnx` + `.onnx.json` |
| Piper binary | `bin/piper.exe` |
| LLM weights | Managed by Ollama, not in this repository |

Model binaries are not committed. The repository ships `.onnx`/`.gguf`/`.bin` files under
`models/` and executables under `bin/` and `livekit_server/` in the working copy; these are
excluded from source control.

---

## 6. Module Map

### 6.1 `src/` — application core

| File | Responsibility |
|---|---|
| `voice_server.py` | **Primary LiveKit agent.** 1375 lines. `AgentServer`, `voice_session` job, `Brain`, `PiperSynth`, `LocalWhisperSTT`, DSP filters, `split_into_tts_chunks`, model prewarm, DSP + logging config |
| `ws_voice_server.py` | **WebSocket voice path.** 803 lines. `Runtime` singleton container (lazy Whisper/Brain/Piper), session state, PCM framing, turn-end logic |
| `agent_core.py` | Conversation orchestration: routing, tool dispatch, phone/name/date extraction, session bookkeeping |
| `ollama_client.py` | Ollama transport. `/api/chat`, non-streaming and streaming generators, `keep_alive` handling, cancellation support, `OLLAMA_THINK` → `think` flag mapping |
| `groq_client.py` | Optional cloud LLM backend. Inactive by default — the deployment is fully local |
| `whisper_stt.py` | `faster-whisper` wrapper. Sample-rate negotiation (48k → 44.1k → 32k → 16k probe order), linear resampling, silence-bounded recording |
| `piper_tts.py` | `PiperConfig`, temp-WAV subprocess invocation, `sounddevice` playback worker, streaming chunk API, prefetch pipeline |
| `clinic_prompt.py` | DOCX → system prompt renderer. OOXML paragraph walk, placeholder substitution, caching, `voice_config()` safe-metadata accessor, `recommended_num_ctx()` |
| `tools.py` | Tool registry. Math evaluation via a restricted AST walker — whitelisted node types and operators only, no `eval` of arbitrary source |
| `router/router.py` | Role inference from `role_tools.json` keys; Ollama-assisted with string-match fallback |
| `router/intent.py` | Intent classification: yes/no/affirm/deny/booking, regex-first, optional LLM disambiguation |
| `router/session.py` | `SessionState` dataclass: `active_tool`, `pending_action`, `tool_allowed`, bounded history |
| `logging/chat_logger.py` | Append-only CSV conversation logger with a stable column contract |

### 6.2 `livekit_agent/` — agent entry point

| File | Responsibility |
|---|---|
| `src/agent.py` | Thin launcher. Puts the repo root on `sys.path`, loads root `.env.local`, imports `server` from `src.voice_server`, then delegates to the LiveKit `cli.run_app()`. Wraps the run in a **crash-restart loop** with 2→10 s linear backoff and a `data/crash_flag.txt` sentinel. Also intends to expose `GET /healthz`. |
| `pyproject.toml` | Package metadata; `livekit-agents[silero,turn-detector,openai]~=1.3`; pytest + ruff config |
| `tests/test_agent.py` | Agent-level tests |
| `.env.example` | Standalone agent env template (`LIVEKIT_URL=http://localhost:7880`, placeholder credentials) |

### 6.3 `frontend/` — browser client

| Path | Responsibility |
|---|---|
| `app/(app)/voice/page.tsx` | The call screen |
| `app/api/connection-details/route.ts` | **Token endpoint.** `GET` → safe config/health JSON (never the secret). `POST` → mints a participant JWT with a `VideoGrant`. Room and participant names are `<prefix>_<random8>`. Token TTL from `LIVEKIT_TOKEN_TTL`, default `15m`. Response is `no-store`, route is `force-dynamic`. |
| `app/(app)/page.tsx` | Landing page |
| `app/debug/page.tsx` | Debug view |
| `app-config.ts` | Typed reads of `NEXT_PUBLIC_*` variables |
| `components/app/*` | Session view, chat transcript, welcome, layout, theme |
| `components/livekit/*` | Agent control bar, track selectors, chat entry, toasts, alert |
| `hooks/useAgentErrors.tsx` | Agent error surface |
| `lib/ice.ts` | Hard-coded `RTCConfiguration` — see [§14](#14-security-notes) |
| `lib/utils.ts` | `cn()` class-name helper |
| `app/(app)/opengraph-image.tsx` | Social card |

### 6.4 `scripts/` — process and validation tooling

| File | Responsibility |
|---|---|
| `start-all.ps1` | Launches all five services detached via `Win32_Process.Create`, waits 20 s, probes ports 3000/7443/7444/7880/7881, prints LAN URLs |
| `stop-all.ps1` | Terminates services. Notably kills the agent **process tree** with `taskkill /T`, then sweeps orphaned `multiprocessing-fork` children whose command lines no longer contain `livekit_agent` — otherwise they retain GPU context and lock the log file, blocking the next start |
| `start-livekit.cmd` | LiveKit SFU with `--dev --node-ip` and inline `--keys` |
| `start-wssproxy.cmd` | TLS proxy |
| `start-agent.cmd` | Python agent, `PYTHONUNBUFFERED=1` |
| `start-wsvoice.cmd` | WebSocket voice server |
| `start-frontend.cmd` | Next.js dev server, turbopack, self-signed HTTPS |
| `start-frontend-copy.cmd` | Same, for the `- Copy` checkout |
| `wss-proxy.py` | 74-line threaded TCP proxy. TLS-terminates `0.0.0.0:7443` and forwards plaintext to `127.0.0.1:7880`. Two `pump` threads per connection, `SO_REUSEADDR`, listen backlog 128 |
| `allow-firewall.ps1` | Windows firewall rule helper |
| `validate_clinic_prompt.py` | **Static validator for the prompt pipeline** — see [§10](#10-prompt-assembly-pipeline) |
| `e2e_audio_test.py` | End-to-end audio test harness |

### 6.5 `auto_test/` — text-only regression harness

`auto_test/auto_test.py` replicates the exact `Brain` / `OllamaClient` / `SessionState`
wiring from `voice_server.py` but bypasses STT and TTS, giving a fast text-in/text-out loop.

- Input: `auto_test/questions.txt`, one question per line
- Output: `auto_test/results.csv` — `question`, `answer`, `latency_ms`, `timestamp`
- Loads both `.env.local` and `.env`

### 6.6 `voice/` — isolated XTTS experiment

A deliberately quarantined sandbox for Coqui XTTS v2 voice cloning, with its own
`requirements.txt` and an explicit instruction to use a **separate** virtualenv
(`voice\.venv-xtts`) so it can never contend with the agent's `.venv` for VRAM. It is
evaluation-only and is not wired into the live pipeline. `voice/refs/` holds reference audio.

### 6.7 `configs/`

| File | Purpose |
|---|---|
| `clinic_runtime.json` | Runtime clinic identity, knowledge DOCX paths, and dynamic placeholder data |
| `role_tools.json` | Maps role name → permitted tool names. **Currently `{}`** |
| `agents/base.txt` | System-prompt preamble with tool-mode formatting rules |
| `agents/role.txt` | Two-line JARVIS persona; the prompt fallback |

### 6.8 `data/`

Runtime output directory. Contains `chat_log.csv`, `ws_chat_log.csv`,
`clinic_bookings.csv`, `hotel_bookings.csv`, `last_session.txt`, `last_final.json`, and
five `final_<uuid>.json` session snapshots. See [§14](#14-security-notes) before sharing.

---

## 7. Configuration Surface

### 7.1 Root `.env.local` — inference and audio

Values shown are the **live deployment configuration**.

| Variable | Value | Effect |
|---|---|---|
| `OLLAMA_MODEL` | `gemma4:4b` | Primary conversation model |
| `OLLAMA_THINK` | `false` | Required for gemma4 — see [§5.1](#51-the-ollama_think-requirement) |
| `OLLAMA_KEEP_ALIVE` | `-1` | Unload never; model stays resident in VRAM |
| `OLLAMA_HOST` | *(unset)* | Defaults to `http://127.0.0.1:11434` |
| `STT_BACKEND` | `whisper` | STT selection |
| `WHISPER_MODEL` | `large-v3-turbo` | Whisper checkpoint |
| `WHISPER_DEVICE` | `cuda` | Inference device |
| `WHISPER_COMPUTE_TYPE` | `float16` | Mandatory on CUDA — see [§3.2](#32-notable-version-constraint) |
| `WHISPER_LANGUAGE` | `en` | Forced language |
| `WHISPER_BEAM_SIZE` | `1` | Greedy decoding |
| `WHISPER_TEMPERATURE` | `0.0` | Deterministic |
| `WHISPER_PRE_ROLL_MS` | `240` | Audio kept before speech onset |
| `WHISPER_HPF_HZ` | `110` | High-pass corner — removes rumble/handling noise |
| `WHISPER_TARGET_RMS_DB` | `-18` | RMS normalisation target for STT input |
| `USER_FRAGMENT_MERGE_SEC` | `0.6` | Gap under which two STT fragments merge into one turn |
| `PREEMPTIVE_STT` | `1` | Transcribe while the user is still speaking |
| `SILERO_ACTIVATION` | `0.26` | Speech-onset threshold; lower = more sensitive |
| `SILERO_DEACTIVATION` | `0.16` | Speech-end threshold |
| `SILERO_MIN_SPEECH_MS` | `50` | Minimum utterance length — permits single letters |
| `SILERO_MIN_SILENCE_MS` | `650` | Silence required to close a turn |
| `SILERO_PREFIX_PAD_MS` | `900` | Pre-roll prepended so consonant onsets are not clipped |
| `SILERO_DEVICE` | `cpu` | Deliberate — frees VRAM, avoids GPU round-trip latency |
| `CHAT_NUM_PREDICT` | `200` | Max tokens per reply |
| `CHAT_NUM_CTX` | `2048` | Base context window |
| `CHAT_TEMPERATURE` | `0.1` | Sampling temperature |
| `CLINIC_NUM_CTX` | *(unset)* | Safety-valve override of the auto-computed context window |
| `PIPER_EXE` | `bin/piper.exe` | Piper binary |
| `PIPER_MODEL` | `models/piper/en_US-danny-low.onnx` | Voice |
| `PIPER_CONFIG` | `….onnx.json` | Voice metadata |
| `PIPER_USE_CUDA` | `0` | Piper stays on CPU |
| `PIPER_THREADS` | `6` | Equals physical core count |
| `PIPER_NOISE_SCALE` | `0.7` | Higher = flatter, more robotic |
| `PIPER_LENGTH_SCALE` | `1.0` | Speech rate; `<1.0` is faster |
| `HUGGINGFACE_HUB_CACHE` | *(unset)* | Defaults to `models/hub` |
| `AGENT_HEALTH_PORT` | `9090` | Intended `/healthz` port — currently non-functional, see [§13.2](#132-health-endpoint-never-starts) |

### 7.2 LiveKit connection

| Variable | Value | Scope |
|---|---|---|
| `LIVEKIT_URL` | `ws://192.168.197.45:7880` | Python agent → SFU, plaintext |
| `LIVEKIT_API_KEY` | `<LIVEKIT_API_KEY>` | Python agent |
| `LIVEKIT_API_SECRET` | `<LIVEKIT_API_SECRET>` | Python agent |
| `LIVEKIT_AGENT_NAME` | *(unset)* | Pins explicit agent dispatch when set |

### 7.3 WebSocket voice path (`src/ws_voice_server.py`)

Independent endpointing parameters, defaulting from the module-level config block. They
do **not** read the `SILERO_*` variables.

| Variable | Default | Effect |
|---|---|---|
| `WSVOICE_HOST` | `0.0.0.0` | Bind address |
| `WSVOICE_PORT` | `7444` | Bind port |
| `WSVOICE_WHISPER_MODEL` | `models/hub` | Explicit model directory override |
| `VAD_START` | `0.0035` | Speech-onset level |
| `VAD_STOP` | `0.0020` | Silence level |
| `VAD_CHUNK_MS` | `30` | Analysis frame size |
| `VAD_SILENCE_S` | `1.15` | Silence that ends a turn |
| `VAD_PRE_ROLL_MS` | `450` | Pre-roll |
| `VAD_MIN_SPEECH_MS` | `550` | Minimum utterance length |
| `VAD_MAX_S` | `10.0` | Hard turn timeout |
| `VAD_GAIN` | `3.0` | Adaptive pre-gain for quiet microphones |

### 7.4 Frontend environment

Read by `app-config.ts` and `app/api/connection-details/route.ts`. All `NEXT_PUBLIC_*` values
are embedded in the client bundle and are therefore public.

| Variable | Live value | Effect |
|---|---|---|
| `NEXT_PUBLIC_LIVEKIT_URL` | `wss://192.168.197.45:7443` | Browser → SFU over TLS |
| `LIVEKIT_URL` | `wss://192.168.197.45:7443` | Server-side fallback |
| `LIVEKIT_API_KEY` | `<LIVEKIT_API_KEY>` | Token minting |
| `LIVEKIT_API_SECRET` | `<LIVEKIT_API_SECRET>` | Token minting — **server-side only** |
| `LIVEKIT_ROOM_PREFIX` | `voice_assistant_room` | Room name prefix |
| `LIVEKIT_PARTICIPANT_PREFIX` | `voice_assistant_user` | Identity prefix |
| `LIVEKIT_PARTICIPANT_NAME` | `user` | Display name |
| `LIVEKIT_TOKEN_TTL` | `15m` | JWT lifetime |
| `NEXT_PUBLIC_AGENT_NAME` / `LIVEKIT_AGENT_NAME` | *(unset)* | Explicit agent dispatch |
| `NEXT_PUBLIC_COMPANY_NAME` | `Voice AI` | UI branding |
| `NEXT_PUBLIC_PAGE_TITLE` | `Voice AI Assistant` | Document title |
| `NEXT_PUBLIC_START_BUTTON_TEXT` | `Start call` | CTA label |
| `NEXT_PUBLIC_SUPPORTS_CHAT_INPUT` | `true` | Text input surface |
| `NEXT_PUBLIC_SUPPORTS_VIDEO_INPUT` | `false` | Video disabled |
| `NEXT_PUBLIC_SUPPORTS_SCREEN_SHARE` | `false` | Screen share disabled |
| `NEXT_PUBLIC_PRECONNECT_BUFFER_ENABLED` | `true` | Preconnect buffering |

`route.ts` normalises the URL scheme: `https://` → `wss://`, `http://` → `ws://`, and a bare
host is assumed `wss://`. `safePrefix()` strips anything outside `[A-Za-z0-9_-]` from the
room and participant prefixes.

### 7.5 `configs/clinic_runtime.json` schema

| Key | Type | Notes |
|---|---|---|
| `agent_name` | string | Identity in the system prompt |
| `language` | string | `English` |
| `prompt_docx` | string | Prompt template filename, repo root |
| `qa_docx` | string | Knowledge-base filename, repo root |
| `working_hours` | string | **Empty** — placeholder substitution leaves it unset |
| `ai_trigger_words` | string | **Empty** |
| `ex[]` | array of `{department, number, message}` | 7 extension slots, **all fields empty** |
| `screens[]` | array of `{department, sip_uri, message, data_to_collect}` | 4 interactive-display slots, **all fields empty** |

The renderer deliberately does **not** invent values for empty fields. It reports them
through `voice_config()["unset_vars"]` instead, so a partially configured deployment produces
an honest prompt rather than a hallucinated one.

### 7.6 `configs/role_tools.json` schema

Maps a role name to the list of tools it may invoke. Consumed by `infer_role_from_prompt()`,
which uses the keys as the candidate role set. **Currently `{}`**, which makes role inference
unconditionally return `"general"`.

---

## 8. Network Interfaces and Ports

| Port | Protocol | Service | Bind | TLS |
|---|---|---|---|---|
| 3000 | HTTPS | Next.js frontend | LAN interface | Self-signed cert |
| 7443 | WSS | TLS proxy → LiveKit | `0.0.0.0` | `certs/livekit.pem` + `livekit-key.pem` |
| 7444 | WS | WebSocket voice server | `0.0.0.0` | None |
| 7880 | WS | LiveKit SFU (dev) | `--node-ip` | None |
| 7881 | — | LiveKit SIP (dev) | — | Probed by `start-all.ps1` but not started by any script |
| 9090 | HTTP | Intended `/healthz` | `0.0.0.0` | None — non-functional, see [§13.2](#132-health-endpoint-never-starts) |
| 11434 | HTTP | Ollama | `127.0.0.1` | None |

`start-all.ps1` reports `https://192.168.197.45:3000` and `https://192.168.197.45:7444` as the
LAN access points, and instructs the operator to accept the self-signed certificate once on
port 3000 and once on 7443.

### 8.1 Token grant shape

`route.ts` issues a `VideoGrant` with `roomJoin`, `canPublish`, `canPublishData`, and
`canSubscribe` all `true`. There is no `canPublishSources` restriction, so the grant is
capable of publishing video even though the UI disables the camera.

---

## 9. Data Formats and Persisted Artifacts

### 9.1 Chat log CSV

Both voice paths append to a CSV with a deliberately stable five-column contract
(`src/logging/chat_logger.py`):

| Column | Type | Content |
|---|---|---|
| `ts_utc` | ISO-8601 string | UTC timestamp, second precision — stable sort key |
| `session_id` | string | `uuid4().hex`; written to `data/last_session.txt` on start |
| `role` | string | `user` \| `assistant` \| `tool` \| `system` |
| `text` | string | Message content |
| `meta_json` | JSON string | Free-form; carries `model`, `tool_name`, `client` |

The header is written lazily on first append, and the file is opened in append mode — logging
never truncates history. Metadata is serialised with `ensure_ascii=False`, so non-Latin
transcripts survive intact.

### 9.2 Auto-test results CSV

`auto_test/results.csv`: `question`, `answer`, `latency_ms`, `timestamp`.

### 9.3 Session snapshots

`data/final_<uuid>.json` and `data/last_final.json` capture end-of-session state. Five
snapshots are present in the working copy.

### 9.4 Bookings

Two coexisting schemas: `data/clinic_bookings.csv` (radiology) and `data/hotel_bookings.csv`
(a prior hospitality tenant). See [§13.11](#1311-stale-and-duplicated-artifacts).

### 9.5 WebSocket wire format

| Direction | Payload |
|---|---|
| Client → server | Binary: Int16 PCM, 16 kHz mono. Text: control messages including `"end"` |
| Server → client | Text: JSON containing the reply string. Binary: WAV audio |

---

## 10. Prompt Assembly Pipeline

`src/clinic_prompt.py` turns two Word documents plus a JSON config into a single system
prompt string. It is shared by both voice paths and by the auto-test harness.

```
configs/clinic_runtime.json
        │
        ├── prompt_docx ──► extract_docx_text()  ─┐
        │                                          │  raw OOXML paragraph walk
        └── qa_docx     ──► extract_docx_text()  ─┘
                                                   │
        ┌──────────────────────────────────────────┘
        ▼
   build_placeholder_map()   {{agent_name}}, {{working_hours}},
        │                     {{ai_trigger_words}}, {{ex_N_*}}, {{screen_N_*}},
        │                     plus the Q&A block
        ▼
   _render()  ──► text + meta
        │            meta: ok · prompt_source · qa_injected · prompt_chars
        │                  unset_vars · leftover_placeholders
        ▼
   _cache_key(cfg)  ──► skip re-render when config is unchanged
        │
        ▼
   get_system_prompt()  ──► configs/agents/role.txt if no DOCX is usable
```

### 10.1 DOCX extraction

`extract_docx_text()` reads the `.docx` container directly as a zip and walks
`word/document.xml` with the WordprocessingML namespace (`W_NS`). It is a paragraph-level
extractor, not a general DOCX renderer — it reads text runs and discards all other content.

### 10.2 Precedence note

`PRECEDENCE_NOTE` (`clinic_prompt.py:69`) is appended to the rendered prompt to establish
document authority over conversational drift. The validator asserts both the note and the
`clinic_faq` marker survive rendering.

### 10.3 Safe metadata surface

`voice_config()` returns agent name, enablement, injection flags, unset-variable names,
leftover placeholders, prompt character count, and `model_env`. It deliberately **excludes**
prompt text, file paths, and any config secrets, and is safe to expose to a front-end
endpoint.

### 10.4 Context-window sizing

`recommended_num_ctx(base_default=2048)` returns `max(base_default, est_prompt_tokens + 1024)`
where tokens are estimated as `prompt_chars / 4`. `CLINIC_NUM_CTX` overrides the result
entirely, floored at 1024, and exists as an explicit escape hatch for the 8 GB card.

### 10.5 Static validator

`scripts/validate_clinic_prompt.py` is a runnable, exit-code-bearing check over the whole
pipeline. It asserts:

1. The prompt source is the DOCX, not a file path.
2. The Q&A block is present and injected exactly once.
3. No `{{...}}` placeholder literals survive.
4. **No filesystem, drive-letter, or `.docx` path strings leak into the prompt** — the point
   of this check is that the model must never learn it is reading a file.
5. The `DOCUMENT PRIORITY` precedence note is present.
6. The recommended `num_ctx` is at least the prompt-size estimate.
7. `voice_config()` exposes no prompt text, role text, or content keys.

It exits `0` on success and `1` on any failure, making it CI-injectable.

---

## 11. Audio Signal Processing

### 11.1 Input conditioning (`src/voice_server.py`)

Applied before transcription, in order:

| Stage | Function | Purpose |
|---|---|---|
| High-pass | `_highpass_filter(signal, WHISPER_HPF_HZ, fs)` | `scipy.signal.lfilter`; removes low rumble and handling noise at 110 Hz |
| RMS normalise | `_rms_normalize(signal, WHISPER_TARGET_RMS_DB)` | Scales to −18 dBFS so the level does not depend on microphone gain |
| Speech boost | `_speech_boost(signal, fs, f_low, f_high, gain_db)` | Band-limited emphasis on the speech formants |
| Resample | `whisper_stt` | Linear interpolation to a Whisper-supported rate |
| Frame | `merge_frames` / `_wav_bytes_to_frames` | 20 ms `rtc.AudioFrame` units |

### 11.2 Output conditioning

`_lowpass_filter` removes high-frequency artefacts before playback, and
`_frames_stream` yields frames to LiveKit's `AudioSource` with backpressure rather than
manual pacing.

### 11.3 Text chunking

`split_into_tts_chunks(text)` segments LLM output for synthesis. The documented strategy is
an early first-clause flush — the opening clause is emitted at the first comma or colon past
roughly 50 characters, with sentence boundaries (`.!?`) as the general case — so playback
starts as soon as the first thought is formed instead of waiting for a full sentence.

### 11.4 Prefetch pipeline

`PiperSynth` keeps exactly one synthesis task in flight while the current chunk plays. Each
chunk costs a Piper subprocess spawn and ONNX model load (historically 150–400 ms); a deeper
queue would require cancelling multiple buffered chunks on barge-in, which complicates
interruption for no measurable gain. One chunk of lookahead fully hides synthesis latency.

### 11.5 Barge-in

Two Silero VAD instances are configured deliberately:

- Inside the STT `StreamAdapter` — endpointing for transcription segmentation.
- On the `AgentSession` with `min_interruption_duration≈0.2 s` — fast speech detection that
  truncates TTS roughly one word after the user starts speaking.

A single VAD on the stream adapter is insufficient, because the session would otherwise wait
for the *final* transcript (≥650 ms of silence plus decode) before cutting playback.

---

## 12. Dependencies and Licensing

### 12.1 Project license

**MIT License with Attribution Requirement** — © 2026 Shivansh Thakur
(`LICENSE`, `README.md` header).

The attribution requirement is stricter than stock MIT: redistribution must retain the
attribution notice.

### 12.2 Model licenses

Each model carries its own upstream license, independent of the project license:

| Model | Upstream |
|---|---|
| `en_US-danny-low` (Piper) | Piper VOICES catalogue |
| `large-v3-turbo` | faster-whisper / Whisper |
| `gemma4:4b` | Google Gemma terms |
| `qwen2.5:7b-instruct` | Alibaba Qwen terms |
| Silero VAD | MIT |
| Coqui XTTS v2 (experiment only) | Coqui CPML — **non-commercial** |

XTTS's CPML is why it is quarantined in `voice/` and not shipped in the live path.

### 12.3 Frontend package manager ambiguity

The repository contains **both** `frontend/package-lock.json` (npm) and
`frontend/pnpm-lock.yaml` (pnpm), while `package.json` declares
`"packageManager": "pnpm@9.15.9"`. Resolutions can differ between the two.

### 12.4 Stray archives in the repository root

`.env.zip`, `frontend.zip`, and `livekit_cloud_env_patch.zip` are committed. `.env.zip` is
potentially a packaged copy of real environment files — verify before any distribution.

---

## 13. Known Issues and Inconsistencies

Findings from source inspection. Ordered by operational impact.

### 13.1 All startup scripts target a non-existent directory

**Severity: critical — the system cannot be started from its current location.**

`scripts/start-all.ps1:3` sets `$root` to:

```
C:\Users\Global PC\Desktop\EN-AI\local-voice-ai
```

but the working copy is:

```
C:\Users\Global PC\Desktop\EN-AI\local-voice-ai - Copy
```

The same stale root is hard-coded in every launcher:

| File | Line | Stale reference |
|---|---|---|
| `scripts/start-all.ps1` | 3 | `$root` |
| `scripts/start-livekit.cmd` | 2 | `cd /d` |
| `scripts/start-wssproxy.cmd` | 2 | `cd /d` |
| `scripts/start-agent.cmd` | 2 | `cd /d` |
| `scripts/start-wsvoice.cmd` | 2 | `cd /d` + absolute `.venv` path |
| `scripts/start-frontend.cmd` | 2 | `cd /d` + absolute cert paths |
| `scripts/wss-proxy.py` | 5–6 | `CERT`, `KEY` absolute paths |
| `frontend/LIVEKIT_CLOUD_PATCH_README.md` | — | references the same path |

Only `scripts/start-frontend-copy.cmd` points at the `- Copy` checkout. Because
`start-all.ps1` shells out to the other six, **none** of the services launch from this
folder, and the health probe at line 22 will report all five ports `DOWN`.

Secondary consequence: `start-all.ps1:30` advertises `https://192.168.197.45:7444` as the
WebSocket endpoint, but that port serves **plaintext `ws://`**. Only ports 3000 and 7443 are
TLS-terminated.

*Not fixed in this document, per the "document only" directive.*

### 13.2 Health endpoint never starts

**Severity: high — silent failure.**

`livekit_agent/src/agent.py:56` calls `os.getenv("AGENT_HEALTH_PORT", "9090")`, but the
module imports only `sys`, `time`, `threading`, `Path`, and `dotenv` — **`os` is never
imported**. The resulting `NameError` is raised inside a bare
`except Exception: pass` (lines 57–59), so it is swallowed with no log line.

Net effect: the `/healthz` endpoint intended for external monitoring is never bound, and
nothing reports the failure. The import is missing; a monitoring integration depending on
port 9090 will see a connection refusal and cannot distinguish it from a crashed agent.

### 13.3 Language conflict between prompt layers

`configs/agents/base.txt` mandates:

> All user-facing text must be in Hindi (Devanagari).

`configs/clinic_runtime.json` sets `"language": "English"`, and the rendered clinic prompt is
English. `base.txt` is loaded unconditionally into `Brain.base_prompt`
(`src/voice_server.py:232`) but excluded from the chat path by the fast path at
`voice_server.py:263–280`, which prefers the clinic prompt and otherwise falls back to
`role.txt`.

So the Hindi instruction is **latent**: it does not affect ordinary conversation, but it
governs tool-mode formatting, and any refactor that routes tool output through `base.txt`
will silently switch replies to Hindi. Two of the three prompt layers disagree about the
output language.

### 13.4 Role routing is inert

`configs/role_tools.json` is `{}`. `infer_role_from_prompt()` derives its candidate set from
the keys of that file, so with no keys it returns `"general"` for every input. The Ollama
classifier at `router.py:43` is therefore never reached in practice, and `qwen2.5:7b-instruct`
is not required at runtime despite being referenced — which incidentally hides the
missing-model problem described in [§5.2](#52-second-model-requirement).

The tool-permission surface that `SessionState.tool_allowed` and `role_tools.json` are
designed to enforce is currently inert.

### 13.5 Persona mismatch in the fallback prompt

`configs/agents/role.txt` is a two-line **JARVIS** persona — a "refined British AI assistant"
who addresses the user as "sir". This is the prompt used whenever the clinic DOCX pipeline is
unusable (`clinic_prompt.py:310`, `voice_server.py:280`).

The failure mode is therefore not a degraded clinic assistant but a **completely different
assistant with the wrong identity, wrong tone, and the wrong domain**, and it will speak
confidently. A radiology clinic whose knowledge base fails to load becomes a British butler.
`voice_config()["enabled"]` is the signal that distinguishes the two states.

### 13.6 Unconfigured runtime placeholders

`configs/clinic_runtime.json` ships with `working_hours`, `ai_trigger_words`, all seven
`ex[]` entries, and all four `screens[]` entries set to empty strings. Roughly 30 placeholder
slots are unset.

The renderer behaves correctly — it does not fabricate values and reports them via
`unset_vars` — but the resulting prompt is missing the extension list, the interactive-display
list, and the operating hours. Any "call this extension" or "what are your hours" query will
be unanswerable, and the model is instructed not to invent them.

### 13.7 Credential duplication

The same LiveKit development credential pair is committed in **six** places:

| File | Line |
|---|---|
| `README.md` | 72 |
| `.env.example` | — |
| `.env.local` | 14–15 |
| `frontend/.env.example` | — |
| `frontend/.env.local` | 3–4 |
| `scripts/start-livekit.cmd` | 3 |

The value is LiveKit's well-known `--dev` key, so the immediate risk is low, but the pattern
is unsafe: the first real key rotation will miss copies, and `.env.example` is a tracked file
whose entire purpose is to be a safe template.

### 13.8 `.gitignore` contradicts itself

`.gitignore:14` ignores `.env.local`. `.gitignore:31` then states that everything else —
*"including .env.local"* — is committed on purpose. The second comment describes the opposite
intent. A reader cannot tell whether `.env.local` is protected, and `data/*.csv` (which
contains real transcripts and phone numbers) is not excluded at all.

### 13.9 "Fully local" is not accurate

`README.md:7` claims *"no cloud APIs, no external services"*. `frontend/lib/ice.ts`
hard-codes four public Google/OpenRelay STUN and TURN servers. WebRTC contacts them for
connectivity checks and, on restrictive networks, relays media. Audio stays local, but
call metadata — client IP, timing, and potentially media on a TURN path — reaches third
parties. A truly air-gapped deployment needs a local TURN server and an empty
`iceServers` array.

### 13.10 Stale `auto_test` fixtures

`auto_test/questions.txt` asks *"What is Harij Softech"* and *"What is Harij Softech
Solutions"* — questions for a previous hospitality tenant. `data/hotel_bookings.csv` is the
matching artifact. The regression harness is no longer testing the deployed domain, and
`data/last_final.json` carries the same legacy framing.

### 13.11 Stale and duplicated artifacts

| Artifact | Problem |
|---|---|
| `data/hotel_bookings.csv` | Prior tenant's booking data |
| `data/clinic_bookings.csv` | Current tenant, different schema |
| `data/final_*.json` (×5) + `last_final.json` | Accumulated session snapshots in the working copy |
| `data/chat_log.csv`, `ws_chat_log.csv` | Real transcripts and phone numbers |
| `voice/README.md` | Documents a 4 GB RTX 3050; the machine now has an 8 GB RTX 5060 |
| `src/voice_server.py:222` | `Brain` default model is still `llama3.1:8b` |
| `.env.zip`, `frontend.zip`, `livekit_cloud_env_patch.zip` | Stray committed archives |

### 13.12 Model drift across the repository

Five different model identities are asserted in committed files:

| Source | Model |
|---|---|
| `README.md:13, 28, 92` | `qwen2.5:0.5b`, `llama3.2:3b` |
| `.env.example` | `qwen2.5:0.5b` |
| `.env.local:18` (live) | `gemma4:4b` |
| `src/voice_server.py:222` (default) | `llama3.1:8b` |
| `src/router/*.py` (hard-coded) | `qwen2.5:7b-instruct` |

Only `.env.local` reflects the running system. The `Brain` constructor default is dead code
that would silently select `llama3.1:8b` for any caller that does not pass a model.

### 13.13 `start-all.ps1` probes a port nothing binds

The readiness loop checks `3000, 7443, 7444, 7880, 7881`. Port 7881 is LiveKit's dev-mode SIP
port; no script in `scripts/` starts SIP, so the probe can never succeed. `$ok` is computed
and then never used — the script prints `port 7881 DOWN` and exits `0` regardless.

### 13.14 Unused `base_prompt` and double-bootstrapped imports

`Brain` reads `base.txt` at construction and stores it in `self.base_prompt`, but
`_chat_system_prompt()` never consults it — the chat path returns the clinic prompt or
`role.txt`. The attribute is dead except in tool mode, and it is an unconditional file read
that will crash `Brain` construction if `base.txt` is ever removed.

`agent.py` and `auto_test.py` also perform redundant `sys.path` and `load_dotenv` work that
`voice_server.py` already handles, and `clinic_prompt` is imported through both a relative
and a top-level fallback in `_chat_system_prompt` and `_chat_options`.

---

## 14. Security Notes

### 14.1 Threat model

The design intent is that **no conversation content leaves the machine**: audio, transcripts,
LLM inference, and TTS are all local. The actual exposure surface is the network perimeter
and the filesystem.

### 14.2 Network exposure

- **Five services bind to the LAN interface**, not loopback. Any host on the same network can
  reach the SFU, the plaintext WebSocket voice server, and the frontend.
- The WebSocket path on 7444 is **unauthenticated and unencrypted**. Anyone who can reach it
  can hold a full voice conversation with the clinic agent, and its audio is in cleartext.
- The frontend and the 7443 proxy use a **self-signed certificate**. Browser trust is
  user-accepted, so a network-positioned attacker can present a different self-signed cert
  without a visible warning. This defeats transport confidentiality against an active MITM
  while appearing secure.
- STUN/TURN relays are third-party — see [§13.9](#139-fully-local-is-not-accurate).

### 14.3 PII in the working tree

`data/` contains append-only conversation logs with the schema in
[§9.1](#91-chat-log-csv). `text` holds verbatim patient speech and
[§2.4](#24-conversation-core) records that phone numbers are extracted from user turns, so
`data/chat_log.csv` and `data/ws_chat_log.csv` plausibly contain patient identifiers,
appointment details, and names. `.gitignore` does not exclude `data/`.

Before sharing a copy of this repository, scrub `data/`, `.env.local`, `frontend/.env.local`,
`certs/`, `.env.zip`, and `logs/`.

### 14.4 Credential hygiene

- Rotate the LiveKit key pair before any non-local deployment, and update all six locations
  listed in [§13.7](#137-credential-duplication).
- `LIVEKIT_API_SECRET` is required server-side only. It must never appear in a
  `NEXT_PUBLIC_*` variable, which would embed it in the client bundle.
- `route.ts` is written defensively: `GET` returns booleans (`apiKeyConfigured`,
  `apiSecretConfigured`) rather than values, responses are `no-store`, and the route is
  `force-dynamic`. This pattern should be preserved.
- Token TTL is 15 minutes with `roomJoin`/`canPublish`/`canSubscribe` all granted and **no
  `canPublishSources` restriction**, so a leaked token permits publishing to the room for its
  full lifetime. Tighten the grant and shorten the TTL for any shared deployment.

### 14.5 Tool execution

`src/tools.py` evaluates math through a restricted AST walker that whitelists node types and
operators rather than calling `eval` on arbitrary source. This is the correct pattern and
should be preserved: any future tool that accepts model-generated text must keep the same
allow-list discipline. Tool permissions are currently unenforced
([§13.4](#134-role-routing-is-inert)), so the AST sandbox is the only boundary in place.

### 14.6 Medical-domain caveat

The agent answers from an operator-supplied DOCX knowledge base and is explicitly instructed
not to fabricate bookings. The knowledge base is a static Word document with no version
control, no review workflow, and no effective date — there is no mechanism guaranteeing that
a given answer reflects current clinic policy. Any clinical or scheduling use requires
human confirmation; the tool layer performs the actual writes, not the model.

---

*End of specification. Operating procedures remain in the root `README.md`.*
