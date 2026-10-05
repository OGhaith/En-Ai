"""Real-time concurrent benchmark for the live WebSocket voice endpoint."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import ssl
import statistics
import subprocess
import time
import wave
from dataclasses import asdict, dataclass
from io import BytesIO
from pathlib import Path

import numpy as np
import websockets
from faster_whisper.audio import decode_audio


SAMPLE_RATE = 16000
CHUNK_SAMPLES = 2048  # Same ScriptProcessor block size as the browser client.


@dataclass
class ClientResult:
    client: int
    success: bool
    error: str
    transcript: str
    reply: str
    transcript_similarity: float
    answer_correct: bool
    audio_bytes: int
    audio_seconds: float
    end_to_transcript_seconds: float | None
    transcript_to_reply_seconds: float | None
    reply_to_audio_seconds: float | None
    end_to_audio_seconds: float | None
    total_seconds: float


def normalize_text(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", value.lower()).split())


def similarity(reference: str, hypothesis: str) -> float:
    from difflib import SequenceMatcher

    return SequenceMatcher(None, normalize_text(reference), normalize_text(hypothesis)).ratio()


def wav_duration(data: bytes) -> float:
    with wave.open(BytesIO(data), "rb") as wav_file:
        return wav_file.getnframes() / float(wav_file.getframerate())


def gpu_snapshot() -> tuple[float, float] | None:
    try:
        output = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).strip().splitlines()[0]
        util, memory = [float(part.strip()) for part in output.split(",")[:2]]
        return util, memory
    except Exception:
        return None


async def sample_gpu(stop: asyncio.Event, samples: list[tuple[float, float]]) -> None:
    while not stop.is_set():
        snapshot = await asyncio.to_thread(gpu_snapshot)
        if snapshot is not None:
            samples.append(snapshot)
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            pass


async def run_client(
    client_id: int,
    uri: str,
    pcm: np.ndarray,
    expected_question: str,
    voice_id: str,
    start_event: asyncio.Event,
    ready: list[int],
    timeout_seconds: float,
) -> ClientResult:
    started = time.perf_counter()
    transcript = ""
    reply = ""
    end_at = transcript_at = reply_at = audio_at = None
    audio = b""
    try:
        context = None
        if uri.startswith("wss://"):
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        async with websockets.connect(
            uri,
            ssl=context,
            open_timeout=10,
            close_timeout=5,
            max_size=16 * 1024 * 1024,
        ) as websocket:
            # Fully load this call's selected Piper model before releasing the
            # synchronized start gate. Model startup is therefore excluded
            # from the measured turn latency.
            await websocket.send(json.dumps({"type": "set_voice", "voice_id": voice_id}))
            voice_deadline = time.perf_counter() + 120
            while time.perf_counter() < voice_deadline:
                initial = await asyncio.wait_for(
                    websocket.recv(), timeout=max(0.1, voice_deadline - time.perf_counter())
                )
                if not isinstance(initial, str):
                    continue
                payload = json.loads(initial)
                if payload.get("type") == "voice_ready":
                    break
                if payload.get("type") == "voice_error":
                    raise RuntimeError(str(payload.get("text") or "TTS voice load failed"))
            else:
                raise TimeoutError("TTS voice did not finish loading")
            ready.append(client_id)
            await asyncio.wait_for(start_event.wait(), timeout=240)

            await websocket.send(json.dumps({"type": "start"}))
            for offset in range(0, pcm.size, CHUNK_SAMPLES):
                chunk = pcm[offset : offset + CHUNK_SAMPLES]
                await websocket.send(chunk.tobytes())
                await asyncio.sleep(chunk.size / SAMPLE_RATE)
            await websocket.send(json.dumps({"type": "end"}))
            end_at = time.perf_counter()

            deadline = end_at + timeout_seconds
            while time.perf_counter() < deadline:
                message = await asyncio.wait_for(
                    websocket.recv(), timeout=max(0.1, deadline - time.perf_counter())
                )
                now = time.perf_counter()
                if isinstance(message, bytes):
                    audio = message
                    audio_at = now
                    break
                payload = json.loads(message)
                if payload.get("type") == "transcript":
                    transcript = str(payload.get("text") or "")
                    transcript_at = now
                elif payload.get("type") == "reply":
                    reply = str(payload.get("text") or "")
                    reply_at = now
                elif payload.get("type") == "status" and "wrong" in str(payload.get("text", "")).lower():
                    raise RuntimeError(str(payload.get("text")))

        if not audio:
            raise RuntimeError("no WAV reply received")
        normalized_reply = normalize_text(reply)
        answer_correct = (
            ("24" in normalized_reply and "48" in normalized_reply)
            or ("twenty four" in normalized_reply and "forty eight" in normalized_reply)
        )
        return ClientResult(
            client=client_id,
            success=True,
            error="",
            transcript=transcript,
            reply=reply,
            transcript_similarity=similarity(expected_question, transcript),
            answer_correct=answer_correct,
            audio_bytes=len(audio),
            audio_seconds=wav_duration(audio),
            end_to_transcript_seconds=(transcript_at - end_at) if transcript_at else None,
            transcript_to_reply_seconds=(reply_at - transcript_at) if reply_at and transcript_at else None,
            reply_to_audio_seconds=(audio_at - reply_at) if audio_at and reply_at else None,
            end_to_audio_seconds=(audio_at - end_at) if audio_at else None,
            total_seconds=time.perf_counter() - started,
        )
    except Exception as exc:
        return ClientResult(
            client=client_id,
            success=False,
            error=f"{type(exc).__name__}: {exc}",
            transcript=transcript,
            reply=reply,
            transcript_similarity=similarity(expected_question, transcript),
            answer_correct=False,
            audio_bytes=len(audio),
            audio_seconds=0.0,
            end_to_transcript_seconds=None,
            transcript_to_reply_seconds=None,
            reply_to_audio_seconds=None,
            end_to_audio_seconds=None,
            total_seconds=time.perf_counter() - started,
        )


async def run_level(
    concurrency: int,
    uri: str,
    pcm: np.ndarray,
    expected_question: str,
    voice_id: str,
    timeout_seconds: float,
) -> dict[str, object]:
    start_event = asyncio.Event()
    ready: list[int] = []
    gpu_samples: list[tuple[float, float]] = []
    gpu_stop = asyncio.Event()
    gpu_task = asyncio.create_task(sample_gpu(gpu_stop, gpu_samples))
    tasks = [
        asyncio.create_task(
            run_client(
                index,
                uri,
                pcm,
                expected_question,
                voice_id,
                start_event,
                ready,
                timeout_seconds,
            )
        )
        for index in range(1, concurrency + 1)
    ]
    # Higher levels may need time to create and warm dozens of CPU Piper
    # processes. Do not release the synchronized start until all clients are
    # actually ready (or have failed), otherwise this isn't steady-state load.
    ready_deadline = time.perf_counter() + 180
    while len(ready) < concurrency and time.perf_counter() < ready_deadline:
        # Before the gate opens, a completed task represents a setup failure.
        # Start all successfully prepared clients once every client is either
        # ready or has already failed setup.
        if len(ready) + sum(task.done() for task in tasks) >= concurrency:
            break
        await asyncio.sleep(0.05)
    start_event.set()
    results = await asyncio.gather(*tasks)
    gpu_stop.set()
    await gpu_task

    successful = [result for result in results if result.success]
    latencies = [result.end_to_audio_seconds for result in successful if result.end_to_audio_seconds is not None]
    summary: dict[str, object] = {
        "concurrency": concurrency,
        "connected_before_start": len(ready),
        "successes": len(successful),
        "failures": concurrency - len(successful),
        "correct_transcripts": sum(result.transcript_similarity >= 0.9 for result in successful),
        "correct_answers": sum(result.answer_correct for result in successful),
        "median_end_to_audio_seconds": round(statistics.median(latencies), 3) if latencies else None,
        "max_end_to_audio_seconds": round(max(latencies), 3) if latencies else None,
        "max_gpu_utilization_percent": max((sample[0] for sample in gpu_samples), default=None),
        "max_gpu_memory_mib": max((sample[1] for sample in gpu_samples), default=None),
        "clients": [asdict(result) for result in results],
    }
    print("LEVEL=" + json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


async def async_main(args: argparse.Namespace) -> int:
    audio = decode_audio(str(args.audio), sampling_rate=SAMPLE_RATE)
    pcm = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
    baseline = gpu_snapshot()
    output: dict[str, object] = {
        "uri": args.uri,
        "audio": str(args.audio.resolve()),
        "audio_seconds": len(pcm) / SAMPLE_RATE,
        "question": args.question,
        "voice_id": args.voice,
        "baseline_gpu": {
            "utilization_percent": baseline[0],
            "memory_mib": baseline[1],
        }
        if baseline
        else None,
        "levels": [],
    }

    if args.warmup:
        print("Starting warm-up request", flush=True)
        await run_level(1, args.uri, pcm, args.question, args.voice, args.timeout)

    for concurrency in args.levels:
        print(f"Starting concurrency={concurrency}", flush=True)
        level = await run_level(concurrency, args.uri, pcm, args.question, args.voice, args.timeout)
        output["levels"].append(level)
        if int(level["failures"]) > 0:
            print("Stopping ramp after first failing level", flush=True)
            break

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OUTPUT={args.output.resolve()}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", default="wss://192.168.197.45:7444/")
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--question", default="How long does my report take?")
    parser.add_argument("--voice", default="danny-low")
    parser.add_argument("--levels", nargs="+", type=int, default=[1, 2, 4, 6, 8])
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--warmup", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("logs/wsvoice-concurrency-benchmark.json"),
    )
    args = parser.parse_args()
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
