"""
WebSocket voice server — the "old", LiveKit-free voice path.

Browser streams raw Int16 PCM (16 kHz mono) over a WebSocket; this server:
  - buffers an utterance (silence-based auto-end, push-to-talk "end" or timeout)
  - transcribes it with the local Whisper model (models/hub)
  - runs the user turn through the existing Brain (Ollama gemma4:4b)
  - synthesizes the reply with the local Piper TTS
  - sends back the reply text (JSON) and the reply audio (binary WAV)

No LiveKit, no WebRTC — plain plain WebSocket, same LAN machine.

Run:
    python -m src.ws_voice_server
    (or start via scripts/start-wsvoice.cmd)
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import threading
import time
import wave
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import websockets

from .piper_process import PiperProcess

ROOT = Path(__file__).resolve().parents[1]
from dotenv import load_dotenv
load_dotenv(ROOT / ".env.local")

# ---------------------------------------------------------------------------
# Env / config (matches voice_server.py + .env.local)
# ---------------------------------------------------------------------------
DEFAULT_HF_CACHE = ROOT / "models" / "hub"
DEFAULT_HF_CACHE.mkdir(parents=True, exist_ok=True)
if not os.getenv("HUGGINGFACE_HUB_CACHE"):
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(DEFAULT_HF_CACHE)

os.environ.setdefault("WSVOICE_HOST", "0.0.0.0")
os.environ.setdefault("WSVOICE_PORT", "7444")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def env_str(name: str, default: str) -> str:
    return os.getenv(name, default) or default


# VAD (same tuned defaults as whisper_stt._record_until_silence)
VAD_START = env_float("VAD_START", 0.0035)
VAD_STOP = env_float("VAD_STOP", 0.0020)
VAD_CHUNK_MS = env_int("VAD_CHUNK_MS", 30)
VAD_SILENCE_S = env_float("VAD_SILENCE_S", 1.15)
VAD_PRE_ROLL_MS = env_int("VAD_PRE_ROLL_MS", 450)
VAD_MIN_SPEECH_MS = env_int("VAD_MIN_SPEECH_MS", 550)
VAD_MAX_S = env_float("VAD_MAX_S", 10.0)
VAD_GAIN = env_float("VAD_GAIN", 3.0)  # adaptive pre-gain for quiet mics

TARGET_SR = 16000


# Voice choices shown on the 7444 page. Benchmark values were measured on this
# machine with Piper kept warm, OMP_NUM_THREADS=6, noise_scale=0.7 and
# length_scale=1.0. Model paths stay server-side and are never sent to clients.
DEFAULT_VOICE_ID = "danny-low"
VOICE_CATALOG: Dict[str, Dict] = {
    "danny-low": {
        "id": "danny-low",
        "name": "Danny Low",
        "accent": "American English",
        "gender": "Male",
        "quality": "Low",
        "model": ROOT / "models" / "piper" / "en_US-danny-low.onnx",
        "model_size_mb": 60.2,
        "load_seconds": 0.30,
        "generation_seconds": 0.24,
        "sample_audio_seconds": 10.74,
        "realtime_multiple": 45.0,
        "sample_rate_hz": 16000,
        "description": "Current voice. Fastest and lightest option.",
    },
    "lessac-high": {
        "id": "lessac-high",
        "name": "Lessac High",
        "accent": "American English",
        "gender": "Female",
        "quality": "High",
        "model": ROOT / "models" / "piper" / "candidates" / "en_US-lessac-high.onnx",
        "model_size_mb": 108.6,
        "load_seconds": 0.42,
        "generation_seconds": 1.17,
        "sample_audio_seconds": 9.66,
        "realtime_multiple": 8.3,
        "sample_rate_hz": 22050,
        "description": "Clear, natural American voice. Recommended for the clinic agent.",
    },
    "ryan-high": {
        "id": "ryan-high",
        "name": "Ryan High",
        "accent": "American English",
        "gender": "Male",
        "quality": "High",
        "model": ROOT / "models" / "piper" / "candidates" / "en_US-ryan-high.onnx",
        "model_size_mb": 115.2,
        "load_seconds": 0.40,
        "generation_seconds": 1.16,
        "sample_audio_seconds": 9.86,
        "realtime_multiple": 8.5,
        "sample_rate_hz": 22050,
        "description": "Detailed American male voice with high-quality pronunciation.",
    },
    "jenny-dioco-medium": {
        "id": "jenny-dioco-medium",
        "name": "Jenny Dioco Medium",
        "accent": "British English",
        "gender": "Female",
        "quality": "Medium",
        "model": ROOT / "models" / "piper" / "candidates" / "en_GB-jenny_dioco-medium.onnx",
        "model_size_mb": 60.3,
        "load_seconds": 0.27,
        "generation_seconds": 0.32,
        "sample_audio_seconds": 10.65,
        "realtime_multiple": 33.2,
        "sample_rate_hz": 22050,
        "description": "Calm British female voice with an excellent speed-to-quality balance.",
    },
    "northern-english-male-medium": {
        "id": "northern-english-male-medium",
        "name": "Northern English Male Medium",
        "accent": "Northern British English",
        "gender": "Male",
        "quality": "Medium",
        "model": ROOT / "models" / "piper" / "candidates" / "en_GB-northern_english_male-medium.onnx",
        "model_size_mb": 60.3,
        "load_seconds": 0.28,
        "generation_seconds": 0.28,
        "sample_audio_seconds": 9.66,
        "realtime_multiple": 35.1,
        "sample_rate_hz": 22050,
        "description": "Fast Northern English male voice with low memory use.",
    },
}


def public_voice_catalog() -> List[Dict]:
    """Return UI-safe voice metadata without local filesystem paths."""
    return [
        {key: value for key, value in spec.items() if key != "model"}
        for spec in VOICE_CATALOG.values()
    ]


def new_voice_process(voice_id: str) -> PiperProcess:
    """Create a lazy persistent Piper process for one call's selected voice."""
    spec = VOICE_CATALOG.get(voice_id)
    if spec is None:
        raise ValueError(f"Unknown TTS voice: {voice_id}")

    model = Path(spec["model"])
    config = Path(str(model) + ".json")
    if not model.exists() or not config.exists():
        raise FileNotFoundError(f"Voice files are missing for {spec['name']}")

    threads = env_int("PIPER_THREADS", 0)
    if threads > 0:
        # The bundled Piper build uses the OpenMP variable and doesn't expose a
        # --threads CLI flag. Child processes inherit this setting.
        os.environ["OMP_NUM_THREADS"] = str(threads)

    if os.getenv("PIPER_PYTHON_WORKER", "0").lower() in {"1", "true", "yes"}:
        command = [sys.executable, str(ROOT / "scripts" / "piper_cuda_worker.py")]
    else:
        command = [env_str("PIPER_EXE", str(ROOT / "bin" / "piper.exe"))]

    command.extend([
        "--model", str(model),
        "--config", str(config),
        "--noise_scale", str(env_float("PIPER_NOISE_SCALE", 0.7)),
        "--length_scale", str(env_float("PIPER_LENGTH_SCALE", 1.0)),
    ])
    if os.getenv("PIPER_USE_CUDA", "0").lower() in {"1", "true", "yes"}:
        command.append("--cuda")
    return PiperProcess(command)


_SHARED_PIPER_LOCK = threading.Lock()
_SHARED_PIPER_PROCESSES: Dict[str, PiperProcess] = {}


def shared_piper_enabled() -> bool:
    return os.getenv("PIPER_SHARED", "0").lower() in {"1", "true", "yes"}


def load_voice_process(voice_id: str) -> tuple[PiperProcess, bool]:
    """Load and warm a voice, sharing one persistent worker when requested."""
    if not shared_piper_enabled():
        process = new_voice_process(voice_id)
        process.synthesize("Voice system ready.")
        return process, False

    with _SHARED_PIPER_LOCK:
        process = _SHARED_PIPER_PROCESSES.get(voice_id)
        if process is not None:
            return process, True

        process = new_voice_process(voice_id)
        process.synthesize("Voice system ready.")
        _SHARED_PIPER_PROCESSES[voice_id] = process
        return process, False


# ---------------------------------------------------------------------------
# Lazily-loaded runtime pieces
# ---------------------------------------------------------------------------
class Runtime:
    """Singletons: WhisperModel + OllamaClient(+Brain-ish convo) + Piper synth."""

    _lock = threading.Lock()
    _whisper = None
    _brain = None
    _synth = None

    @classmethod
    def whisper(cls):
        with cls._lock:
            if cls._whisper is None:
                from faster_whisper import WhisperModel

                hub_root = Path(env_str("WSVOICE_WHISPER_MODEL", str(ROOT / "models" / "hub")))
                # The HF cache layout differs: model.bin may sit directly in the
                # repo dir or inside a snapshots/<hash>/ subdir. Find whichever
                # directory actually contains model.bin.
                model_path = None
                if hub_root and (hub_root / "model.bin").exists():
                    model_path = hub_root
                else:
                    for cand in sorted(hub_root.rglob("model.bin")):
                        model_path = cand.parent
                        break
                if model_path is None:
                    raise FileNotFoundError(f"whisper model.bin not found under {hub_root}")

                device = env_str("WHISPER_DEVICE", "cuda")
                compute_type = env_str("WHISPER_COMPUTE_TYPE", "float16")
                cls._whisper = WhisperModel(str(model_path), device=device, compute_type=compute_type)
                print(f"[WSVOICE] whisper ready device={device} compute={compute_type} path={model_path}", flush=True)
            return cls._whisper

    @classmethod
    def brain(cls):
        with cls._lock:
            if cls._brain is None:
                from .ollama_client import OllamaClient
                from .clinic_prompt import get_system_prompt, recommended_num_ctx

                model = env_str("OLLAMA_MODEL", "gemma4:4b")
                role_prompt = get_system_prompt()
                cls._brain = {
                    "client": OllamaClient(),
                    "model": model,
                    "role": role_prompt.strip(),
                    "num_ctx": recommended_num_ctx(base_default=env_int("CHAT_NUM_CTX", 1024)),
                }
                print(f"[WSVOICE] brain ready model={model} num_ctx={cls._brain['num_ctx']}", flush=True)
            return cls._brain

    @classmethod
    def synth(cls):
        with cls._lock:
            if cls._synth is None:
                exe = Path(env_str("PIPER_EXE", str(ROOT / "bin" / "piper.exe")))
                model_p = Path(env_str("PIPER_MODEL", str(ROOT / "models" / "piper" / "en_US-danny-low.onnx")))
                config = Path(env_str("PIPER_CONFIG", str(ROOT / "models" / "piper" / "en_US-danny-low.onnx.json")))
                cls._synth = {
                    "exe": exe,
                    "model": model_p,
                    "config": config,
                    "noise_scale": env_float("PIPER_NOISE_SCALE", 0.6),
                    "length_scale": env_float("PIPER_LENGTH_SCALE", 1.05),
                    "use_cuda": os.getenv("PIPER_USE_CUDA", "0").lower() in {"1", "true", "yes"},
                    "threads": env_int("PIPER_THREADS", 0),
                }
                print(f"[WSVOICE] piper ready {model_p.name}", flush=True)
            return cls._synth


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------
def _resample_linear(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if x.size == 0 or src_sr == dst_sr:
        return x.astype(np.float32, copy=False)
    duration = x.shape[0] / float(src_sr)
    n_out = int(round(duration * dst_sr))
    if n_out <= 1:
        return np.array([], dtype=np.float32)
    xp = np.linspace(0.0, duration, num=x.shape[0], endpoint=False)
    fp = x.astype(np.float32, copy=False)
    x_new = np.linspace(0.0, duration, num=n_out, endpoint=False)
    return np.interp(x_new, xp, fp).astype(np.float32)


def wav_bytes_to_mono_f32(data: bytes, target_sr: int) -> Optional[np.ndarray]:
    """Decode a WAV byte blob to mono float32 at target_sr (16 kHz)."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wf:
            rate = wf.getframerate()
            channels = wf.getnchannels()
            raw = wf.readframes(wf.getnframes())
        if not raw:
            return None
        if wf.getsampwidth() == 2:
            x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        elif wf.getsampwidth() == 4:
            x = (np.frombuffer(raw, dtype=np.int32) >> 16).astype(np.float32) / 32768.0
        else:
            return None
        if channels > 1:
            x = x.reshape(-1, channels).mean(axis=1)
        if rate != target_sr:
            x = _resample_linear(x, rate, target_sr)
        return x
    except Exception:
        return None


def int16_to_f32(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0


def f32_to_int16(x: np.ndarray) -> np.ndarray:
    return np.clip(x * 32768.0, -32768, 32767).astype(np.int16)


# ---------------------------------------------------------------------------
# Piper synth (mirror of PiperSynth.synthesize_wav, no livekit imports)
# ---------------------------------------------------------------------------
def _synthesize_wav(text: str, process: Optional[PiperProcess] = None) -> bytes:
    import subprocess
    import tempfile

    txt = (text or "").strip()
    if not txt:
        return b""
    if process is not None:
        return process.synthesize(txt)

    s = Runtime.synth()
    fd, wav_path = tempfile.mkstemp(prefix="piper_", suffix=".wav")
    os.close(fd)
    cmd = [
        str(s["exe"]),
        "--model", str(s["model"]),
        "--output_file", wav_path,
        "--noise_scale", str(s["noise_scale"]),
        "--length_scale", str(s["length_scale"]),
    ]
    if s["use_cuda"]:
        cmd.append("--cuda")
    if s["config"] is not None and Path(s["config"]).exists():
        cmd += ["--config", str(s["config"])]
    if s["threads"] and s["threads"] > 0:
        cmd += ["--threads", str(int(s["threads"]))]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
    )
    try:
        assert proc.stdin is not None
        proc.stdin.write(txt + "\n")
        proc.stdin.flush()
        proc.stdin.close()
        proc.wait(timeout=30)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        data = Path(wav_path).read_bytes()
    except Exception:
        data = b""
    else:
        wav_path = Path(wav_path)
        try:
            wav_path.unlink(missing_ok=True)
        except Exception:
            pass
    return data


# ---------------------------------------------------------------------------
# Brain (chat-mode wrapper mirroring voice_server.Brain chat path)
# ---------------------------------------------------------------------------
class ChatSession:
    """Holds per-connection conversation context + logging."""

    def __init__(self) -> None:
        self.history: List[Dict[str, str]] = []
        self.max_history = env_int("HISTORY_MAX", 10)

    def run(self, user_text: str) -> str:
        from .logging.chat_logger import ChatLogConfig, log_turn, new_session_id

        b = Runtime.brain()
        client = b["client"]
        model = b["model"]
        role = b["role"]

        self.history.append({"role": "user", "content": user_text})
        if self.max_history > 0:
            self.history = self.history[-self.max_history:]

        sys_prompt = role
        messages: List[Dict[str, str]] = []
        if sys_prompt:
            messages.append({"role": "system", "content": sys_prompt})
        messages.extend(self.history)

        answer = client.chat(
            model,
            messages,
            options={
                "temperature": env_float("CHAT_TEMPERATURE", 0.2),
                "num_predict": env_int("CHAT_NUM_PREDICT", 80),
                "num_ctx": b.get("num_ctx", env_int("CHAT_NUM_CTX", 1024)),
            },
            keep_alive=env_int("OLLAMA_KEEP_ALIVE", -1),
        )
        answer = (answer or "").strip()

        if answer:
            self.history.append({"role": "assistant", "content": answer})
            if self.max_history > 0:
                self.history = self.history[-self.max_history:]

        try:
            log_cfg = ChatLogConfig(csv_path=ROOT / "data" / "ws_chat_log.csv")
            sid = new_session_id()
            log_turn(log_cfg, session_id=sid, role="user", text=user_text, meta={"client": "wsvoice"})
            if answer:
                log_turn(log_cfg, session_id=sid, role="assistant", text=answer, meta={"client": "wsvoice", "model": model})
        except Exception:
            pass
        return answer


# ---------------------------------------------------------------------------
# STT: local faster-whisper
# ---------------------------------------------------------------------------
def _transcribe(audio_f32: np.ndarray) -> str:
    if audio_f32 is None or audio_f32.size == 0:
        return ""
    audio = np.asarray(audio_f32, dtype=np.float32).reshape(-1)
    if audio.size < TARGET_SR * 0.2:
        return ""

    # pre-roll pad + peak/RMS normalization (mirrors the local pipeline)
    pad_ms = env_int("WHISPER_PRE_ROLL_MS", 180)
    pad = np.zeros(int(TARGET_SR * pad_ms / 1000.0), dtype=np.float32)
    audio = np.concatenate([pad, audio])

    peak = float(np.max(np.abs(audio)) + 1e-9)
    if peak > 0:
        audio = audio / peak
    rms = float(np.sqrt(np.mean(audio * audio) + 1e-12))
    if rms >= 0.015:
        gain = min(0.08 / rms, 6.0)
        audio = np.clip(audio * gain, -1.0, 1.0)

    model = Runtime.whisper()
    segments, _info = model.transcribe(
        audio,
        language=env_str("WHISPER_LANGUAGE", "en") or None,
        beam_size=env_int("WHISPER_BEAM_SIZE", 1),
        temperature=env_float("WHISPER_TEMPERATURE", 0.0),
        vad_filter=True,
        condition_on_previous_text=False,
    )
    text = " ".join(seg.text.strip() for seg in segments if seg.text).strip()
    return text


# ---------------------------------------------------------------------------
# Utterance VAD state machine (fed one int16 chunk at a time)
# ---------------------------------------------------------------------------
class UtteranceVAD:
    def __init__(self, on_utterance) -> None:
        self.on_utterance = on_utterance
        self.sr = TARGET_SR
        self.chunk = env_int("VAD_CHUNK_MS", 30) * self.sr // 1000
        self.silence_needed = int(env_float("VAD_SILENCE_S", 1.15) * self.sr / self.chunk)
        self.pre_roll = env_int("VAD_PRE_ROLL_MS", 450) * self.sr // 1000 // self.chunk
        self.min_speech = env_int("VAD_MIN_SPEECH_MS", 550) * self.sr // 1000 // self.chunk
        self.max_chunks = int(env_float("VAD_MAX_S", 10.0) * self.sr / self.chunk)

        self.frames: List[np.ndarray] = []
        self.pre: List[np.ndarray] = []
        self.speaking = False
        self.silence = 0
        self.speech = 0
        self.total = 0
        self.finished = False

    def _finalize(self) -> None:
        if self.finished:
            return
        self.finished = True
        if not self.frames:
            self.on_utterance(None)
            return
        audio = np.concatenate(self.frames).astype(np.float32)
        peak0 = float(np.max(np.abs(audio)) + 1e-9)
        rms0 = float(np.sqrt(np.mean(audio * audio) + 1e-12))
        if rms0 < 0.01 and peak0 < 0.25:
            self.on_utterance(None)
            return
        self.on_utterance(audio)

    def feed(self, chunk_f32: np.ndarray) -> None:
        if self.finished or chunk_f32.size == 0:
            return
        for i in range(0, chunk_f32.size, self.chunk):
            x = chunk_f32[i:i + self.chunk]
            if x.size < self.chunk:
                x = np.pad(x, (0, self.chunk - x.size))
            self.total += 1

            rms = float(np.sqrt(np.mean(x * x) + 1e-12))
            gain = 1.0
            if not self.speaking and rms > 1e-6:
                if rms < VAD_START and rms >= (VAD_START * 0.35):
                    gain = min(VAD_GAIN, VAD_START / (rms + 1e-9))
            if gain != 1.0:
                x = x * gain
                rms = float(np.sqrt(np.mean(x * x) + 1e-12))

            self.pre.append(x.copy())
            if len(self.pre) > self.pre_roll:
                self.pre.pop(0)

            if not self.speaking:
                if rms >= VAD_START:
                    self.speaking = True
                    self.frames.extend(self.pre)
                    self.frames.append(x)
                    self.silence = 0
                    self.speech = 0
            else:
                self.frames.append(x)
                self.speech += 1
                if self.speech < self.min_speech:
                    self.silence = 0
                else:
                    self.silence = self.silence + 1 if rms < VAD_STOP else 0
                    if self.silence >= self.silence_needed or self.total >= self.max_chunks:
                        self._finalize()
                        return

    def force_end(self) -> None:
        self._finalize()


# ---------------------------------------------------------------------------
# Per-connection handler
# ---------------------------------------------------------------------------
class VoiceConnection:
    def __init__(self, ws):
        self.ws = ws
        self.session = ChatSession()
        self.busy = False
        self.process_task = None
        self.utterance: List[np.ndarray] = []
        self.vad: Optional[UtteranceVAD] = None
        self.buf = b""
        self.voice_id = DEFAULT_VOICE_ID
        self.tts: Optional[PiperProcess] = None

    async def send_json(self, obj: dict) -> None:
        try:
            await self.ws.send(json.dumps(obj, ensure_ascii=False))
        except Exception:
            pass

    async def send_wav(self, data: bytes) -> None:
        try:
            await self.ws.send(data)
        except Exception:
            pass

    def close(self) -> None:
        process, self.tts = self.tts, None
        if process is not None and not shared_piper_enabled():
            process.close()

    async def select_voice(self, voice_id: str) -> None:
        spec = VOICE_CATALOG.get(voice_id)
        if spec is None:
            await self.send_json({
                "type": "voice_error",
                "text": "That TTS voice is not available.",
            })
            return
        if self.busy:
            await self.send_json({
                "type": "voice_error",
                "text": "The voice cannot be changed while a reply is being prepared.",
            })
            return
        if self.tts is not None and self.voice_id == voice_id:
            await self.send_json({
                "type": "voice_ready",
                "voice": {key: value for key, value in spec.items() if key != "model"},
                "actual_load_seconds": 0.0,
                "cached": True,
            })
            return

        self.busy = True
        await self.send_json({
            "type": "voice_loading",
            "voice_id": voice_id,
            "text": f"Loading {spec['name']}...",
        })
        started = time.perf_counter()
        process: Optional[PiperProcess] = None
        try:
            process, cached = await asyncio.to_thread(load_voice_process, voice_id)
            actual_load = time.perf_counter() - started

            previous, self.tts = self.tts, process
            self.voice_id = voice_id
            if previous is not None and previous is not process and not shared_piper_enabled():
                previous.close()

            await self.send_json({
                "type": "voice_ready",
                "voice": {key: value for key, value in spec.items() if key != "model"},
                "actual_load_seconds": round(actual_load, 3),
                "cached": cached,
            })
            print(
                f"[WSVOICE] selected voice={voice_id} load_ms={actual_load * 1000:.0f}",
                flush=True,
            )
        except Exception as exc:
            if process is not None:
                process.close()
            print(f"[WSVOICE] voice load failed voice={voice_id}: {exc}", flush=True)
            await self.send_json({
                "type": "voice_error",
                "text": f"Could not load {spec['name']}.",
            })
        finally:
            self.busy = False

    def _start_utterance(self) -> None:
        self.utterance = []
        self.vad = UtteranceVAD(self._on_utterance)

    def _on_utterance(self, audio: Optional[np.ndarray]) -> None:
        # The browser streams continuously during a call. Release the finished
        # detector immediately so the next audio packet starts a fresh turn.
        self.vad = None
        self.utterance.append(audio)
        if self.process_task is None or self.process_task.done():
            self.process_task = asyncio.create_task(self._drain_utterances())

    async def _drain_utterances(self) -> None:
        while self.utterance:
            await self._process()

    async def _process(self) -> None:
        if self.busy:
            return
        audio = self.utterance.pop(0) if self.utterance else None
        if audio is None or audio.size == 0:
            await self.send_json({"type": "status", "text": "I didn't catch that."})
            return

        self.busy = True
        try:
            await self.send_json({"type": "status", "text": "listening_to_reply"})
            text = await asyncio.to_thread(_transcribe, audio)
            if not text:
                await self.send_json({"type": "transcript", "text": ""})
                await self.send_json({"type": "reply", "text": "I didn't catch that. Could you repeat?"})
                return

            await self.send_json({"type": "transcript", "text": text})
            answer = await asyncio.to_thread(self.session.run, text)
            if not answer:
                answer = "I'm not sure how to answer that."
            await self.send_json({"type": "reply", "text": answer})

            if self.tts is None:
                self.tts, _ = await asyncio.to_thread(load_voice_process, self.voice_id)
            wav = await asyncio.to_thread(_synthesize_wav, answer, self.tts)
            if wav:
                await self.send_wav(wav)
        except Exception as exc:
            print(f"[WSVOICE] processing error: {exc}", flush=True)
            await self.send_json({"type": "status", "text": "Sorry, something went wrong."})
        finally:
            self.busy = False

    async def handle(self) -> None:
        async for message in self.ws:
            if isinstance(message, bytes):
                if not self.vad:
                    self._start_utterance()
                self.vad.feed(int16_to_f32(message)) if self.vad else None
                continue

            try:
                obj = json.loads(message)
            except Exception:
                continue
            mtype = obj.get("type")
            if mtype == "set_voice":
                await self.select_voice(str(obj.get("voice_id") or ""))
            elif mtype == "start":
                self._start_utterance()
            elif mtype == "end":
                if self.vad:
                    self.vad.force_end()
            elif mtype == "reset":
                self.session = ChatSession()
                await self.send_json({"type": "status", "text": "reset"})


async def handler(ws, path=None):
    conn = VoiceConnection(ws)
    print(f"[WSVOICE] client connected {ws.remote_address}", flush=True)
    await conn.send_json({
        "type": "voice_catalog",
        "voices": public_voice_catalog(),
        "default_voice_id": DEFAULT_VOICE_ID,
    })
    await conn.send_json({"type": "status", "text": "connected"})
    try:
        await conn.handle()
    except websockets.ConnectionClosed:
        pass
    except Exception as exc:
        print(f"[WSVOICE] handler error: {exc}", flush=True)
    finally:
        print(f"[WSVOICE] client disconnected {ws.remote_address}", flush=True)
        if conn.process_task:
            conn.process_task.cancel()
            await asyncio.gather(conn.process_task, return_exceptions=True)
        conn.close()


# ---------------------------------------------------------------------------
# Served voice page (self-contained HTML, streamed audio via WebSocket only)
# ---------------------------------------------------------------------------
VOICE_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Local Voice AI</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font-family:system-ui,Segoe UI,Roboto,sans-serif; background:#0f1115; color:#e8eaed; display:flex; flex-direction:column; align-items:center; justify-content:center; min-height:100vh; padding:16px; }
  h1 { font-size:20px; font-weight:600; margin:0 0 4px; }
  .sub { color:#9aa0a6; font-size:13px; margin-bottom:28px; }
  .pill { font-size:12px; padding:3px 10px; border-radius:999px; background:#1e2733; color:#8ab4f8; border:1px solid #2d3a4d; margin-bottom:28px; }
  .status-row { display:flex; align-items:center; gap:10px; margin-bottom:26px; }
  .dot { width:10px; height:10px; border-radius:50%; background:#5f6368; transition:background .15s; }
  .dot.idle { background:#5f6368; }
  .dot.listening { background:#f28b82; box-shadow:0 0 10px #f28b82; }
  .dot.busy { background:#fbbc04; box-shadow:0 0 10px #fbbc04; }
  .dot.speaking { background:#81c995; box-shadow:0 0 10px #81c995; }
  button { font:inherit; }
  .talk { width:150px; height:150px; border-radius:50%; border:none; background:#1e2733; color:#e8eaed; font-size:16px; font-weight:600; cursor:pointer; transition:transform .06s, background .15s, box-shadow .15s; user-select:none; -webkit-user-select:none; }
  .talk:hover { background:#26384f; }
  .talk:active, .talk.on { background:#8ab4f8; color:#0f1115; transform:scale(.97); box-shadow:0 0 26px rgba(138,180,248,.35); }
  .transcript { margin-top:30px; min-height:52px; max-width:560px; width:100%; text-align:center; font-size:15px; line-height:1.5; color:#e8eaed; }
  .reply { color:#9aa0a6; font-size:15px; line-height:1.5; }
  .err { color:#f28b82; }
</style>
</head>
<body>
  <h1>Local Voice AI</h1>
  <div class="sub">WebSocket voice — no WebRTC. Hold to talk.</div>
  <div class="pill">LAN self-signed · audio stays on this machine</div>

  <div class="status-row">
    <span class="dot idle" id="dot"></span>
    <span id="statusText" style="font-size:14px;color:#9aa0a6;">tap the mic and speak</span>
  </div>

  <button id="talk" class="talk" disabled>setup…</button>

  <div id="banner" class="err" style="margin-top:22px;max-width:560px;text-align:center;font-size:13px;"></div>

  <div class="transcript" id="transcript"></div>
  <div class="reply" id="reply"></div>

<script>
(function () {
  const talk = document.getElementById('talk');
  const dot = document.getElementById('dot');
  const statusText = document.getElementById('statusText');
  const banner = document.getElementById('banner');
  const transcriptEl = document.getElementById('transcript');
  const replyEl = document.getElementById('reply');

  function setStatus(label, cls) {
    dot.className = 'dot ' + cls;
    statusText.textContent = label;
  }

  const wsUrl = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host;
  let ws = null;
  let ctx = null;
  let source = null;
  let processor = null;
  let stream = null;
  let talking = false;
  let ready = false;

  async function ensureAudio() {
    if (ctx) return;
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    ctx = new AudioContext({ sampleRate: 16000 });
    source = ctx.createMediaStreamSource(stream);
    processor = ctx.createScriptProcessor(2048, 1, 1);
    processor.onaudioprocess = (e) => {
      if (!talking || !ws || ws.readyState !== 1) return;
      const f32 = e.inputBuffer.getChannelData(0);
      const i16 = new Int16Array(f32.length);
      for (let i = 0; i < f32.length; i++) {
        let s = f32[i] * 32768;
        s = s < -32768 ? -32768 : s > 32767 ? 32767 : s;
        i16[i] = s | 0;
      }
      ws.send(i16.buffer);
    };
    source.connect(processor);
    processor.connect(ctx.destination);
  }

  function startTalking() {
    if (!ready || talking) return;
    talking = true;
    talk.classList.add('on');
    setStatus('listening… (release to send)', 'listening');
    try {
      if (ws.readyState === 1) ws.send(JSON.stringify({ type: 'start' }));
    } catch (e) {}
  }

  function endTalking() {
    if (!talking) return;
    talking = false;
    talk.classList.remove('on');
    setStatus('thinking…', 'busy');
    try {
      if (ws.readyState === 1) ws.send(JSON.stringify({ type: 'end' }));
    } catch (e) {}
  }

  talk.addEventListener('pointerdown', startTalking);
  window.addEventListener('pointerup', endTalking);
  window.addEventListener('pointercancel', endTalking);

  function connect() {
    setStatus('connecting…', 'busy');
    ws = new WebSocket(wsUrl);
    ws.binaryType = 'arraybuffer';

    ws.onopen = () => { ready = true; talk.disabled = false; talk.textContent = 'TALK'; setStatus('tap the mic and speak', 'idle'); };
    ws.onclose = () => { ready = false; talk.disabled = true; talk.textContent = 'reconnecting…'; setStatus('disconnected — reconnecting…', 'busy'); setTimeout(connect, 1500); };
    ws.onerror = () => { setStatus('connection error', 'idle'); };

    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        let msg;
        try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (msg.type === 'transcript') {
          transcriptEl.textContent = 'You: ' + (msg.text || '…');
        } else if (msg.type === 'reply') {
          replyEl.textContent = 'AI: ' + msg.text;
        } else if (msg.type === 'status') {
          if (msg.text === 'connected' || msg.text === 'reset') {
            setStatus('tap the mic and speak', 'idle');
          } else if (msg.text === 'listening_to_reply') {
            setStatus('thinking…', 'busy');
          } else {
            replyEl.textContent = 'AI: ' + msg.text;
            setStatus('tap the mic and speak', 'idle');
          }
        }
      } else {
        // binary WAV reply
        setStatus('speaking…', 'speaking');
        ctx.decodeAudioData(ev.data).then((buf) => {
          const srcNode = ctx.createBufferSource();
          srcNode.buffer = buf;
          srcNode.connect(ctx.destination);
          srcNode.onended = () => setStatus('tap the mic and speak', 'idle');
          srcNode.start();
        }).catch(() => setStatus('tap the mic and speak', 'idle'));
      }
    };
  }

  ensureAudio().catch((e) => {
    banner.textContent = 'Microphone not available: ' + (e && e.message ? e.message : e);
    setStatus('mic error', 'idle');
  });
  connect();
})();
</script>
</body>
</html>
"""


HTTP_PAGE_BODY = VOICE_PAGE.encode("utf-8")


_VOICE_PAGE_FILE = ROOT / "src" / "ws_voice_page.html"
if _VOICE_PAGE_FILE.exists():
    VOICE_PAGE = _VOICE_PAGE_FILE.read_text(encoding="utf-8")
HTTP_PAGE_BODY = VOICE_PAGE.encode("utf-8")
print(
    f"[WSVOICE] page={_VOICE_PAGE_FILE} exists={_VOICE_PAGE_FILE.exists()} bytes={len(VOICE_PAGE)}",
    flush=True,
)


def _process_request(connection, request):
    """Serve the voice page over plain HTTP(S) on the same port as the WS.

    websockets 16.0 API: called as process_request(connection, request) and must
    return a Response to short-circuit the handshake, or None to keep going.
    """
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    upgrade = ""
    try:
        upgrade = request.headers.get("Upgrade", "") if request.headers is not None else ""
    except Exception:
        upgrade = ""
    # WebSocket upgrade requests must pass through (return None).
    if upgrade.lower() == "websocket":
        return None

    path = (getattr(request, "path", None) or "/").split("?")[0]
    if path in ("/tts_voices", "/tts_voices.json"):
        body = json.dumps({
            "voices": public_voice_catalog(),
            "default_voice_id": DEFAULT_VOICE_ID,
        }).encode("utf-8")
        headers = Headers({
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-store",
        })
        return Response(200, "OK", headers, body)
    if path in ("/voice_config", "/voice_config.json"):
        try:
            from .clinic_prompt import voice_config

            body = json.dumps(voice_config()).encode("utf-8")
        except Exception as e:
            body = json.dumps({"enabled": False, "error": str(e)}).encode("utf-8")
        headers = Headers({
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": str(len(body)),
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-store",
        })
        return Response(200, "OK", headers, body)
    if path == "/":
        headers = Headers({
            "Content-Type": "text/html; charset=utf-8",
            "Content-Length": str(len(HTTP_PAGE_BODY)),
            "Cache-Control": "no-store",
        })
        return Response(200, "OK", headers, HTTP_PAGE_BODY)
    if path == "/favicon.ico":
        return Response(404, "Not Found", Headers({}), b"")
    return None


def main() -> None:
    import ssl

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env.local")
    load_dotenv(ROOT / ".env")

    host = env_str("WSVOICE_HOST", "0.0.0.0")
    port = env_int("WSVOICE_PORT", 7444)

    ssl_context = None
    scheme = "ws"
    cert = Path(env_str("WSVOICE_CERT", str(ROOT / "certs" / "livekit.pem")))
    key = Path(env_str("WSVOICE_KEY", str(ROOT / "certs" / "livekit-key.pem")))
    if os.getenv("WSVOICE_TLS", env_str("WSVOICE_TLS", "0")).lower() in {"1", "true", "yes"} or (
        cert.exists() and key.exists()
    ):
        try:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(cert, key)
            scheme = "wss"
        except Exception as e:
            print(f"[WSVOICE] TLS disabled: {e}", flush=True)
            ssl_context = None

    print(f"[WSVOICE] listening {scheme}://{host}:{port}  (16k mono Int16 PCM in, JSON + WAV out)", flush=True)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _serve():
        # websockets 16.x needs a running event loop when constructing serve().
        async with websockets.serve(
            handler,
            host,
            port,
            ping_interval=None,
            ssl=ssl_context,
            process_request=_process_request,
        ):
            await asyncio.Future()

    try:
        loop.run_until_complete(_serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
