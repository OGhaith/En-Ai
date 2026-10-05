"""End-to-end audio test: publishes a WAV as a microphone track into a LiveKit
room and prints any lk.chat data messages the agent publishes back.

Usage: python scripts/e2e_audio_test.py <wav_path> [room_name]
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import wave

import numpy as np
from livekit import api, rtc

KEY = os.getenv("LIVEKIT_API_KEY", "devkey")
SECRET = os.getenv("LIVEKIT_API_SECRET", "6f1d0c9b7a34e6c2d8f501c9a4b3e2975c4d8f6a1b2c3d4e5f60718293a0b1c0")
URL = os.getenv("LIVEKIT_URL", "ws://localhost:7880")
ROOM = "voice_assistant_room_e2e"
IDENTITY = "e2e_speaker"
SR_OUT = 48000


def load_wav_mono(path: str):
    with wave.open(path, "rb") as wf:
        sr, nch, sw = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if sw == 2:
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        a = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise SystemExit(f"unsupported sample width: {sw}")
    if nch > 1:
        a = a.reshape(-1, nch).mean(axis=1)
    return a, sr


def resample(a: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    if sr_in == sr_out or a.size == 0:
        return a.astype(np.float32)
    n = int(round(a.size * sr_out / sr_in))
    return np.interp(
        np.linspace(0.0, a.size - 1, n), np.arange(a.size), a
    ).astype(np.float32)


async def main() -> None:
    wav_path = sys.argv[1]
    room_name = sys.argv[2] if len(sys.argv) > 2 else ROOM

    audio, sr = load_wav_mono(wav_path)
    audio = resample(audio, sr, SR_OUT)
    audio = np.concatenate([
        np.zeros(int(SR_OUT * 0.4), dtype=np.float32),
        audio,
        np.zeros(int(SR_OUT * 2.0), dtype=np.float32),
    ])
    print(f"[AUDIO] {audio.size / SR_OUT:.2f}s @ {SR_OUT}Hz from {wav_path}", flush=True)

    token = (
        api.AccessToken(KEY, SECRET)
        .with_identity(IDENTITY)
        .with_name("E2E Speaker")
        .with_grants(api.VideoGrants(room_join=True, room=room_name))
        .to_jwt()
    )

    room = rtc.Room()
    received: list[dict] = []

    @room.on("data_received")
    def _on_data(dp) -> None:
        try:
            topic = getattr(dp, "topic", None)
            data = getattr(dp, "data", b"")
            if topic in ("lk.chat", "lk-chat-topic"):
                obj = json.loads(bytes(data).decode("utf-8", "replace"))
                received.append(obj)
                print(f"[CHAT] {obj.get('role')}: {obj.get('message')}", flush=True)
        except Exception as exc:  # pragma: no cover
            print(f"[CHAT] decode error: {exc!r}", flush=True)

    @room.on("participant_connected")
    def _on_join(p) -> None:
        print(f"[ROOM] participant joined: {p.identity}", flush=True)

    await room.connect(URL, token)
    print(f"[ROOM] connected as {IDENTITY} -> {room_name}", flush=True)

    source = rtc.AudioSource(SR_OUT, 1)
    track = rtc.LocalAudioTrack.create_audio_track("e2e-mic", source)
    opts = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    await room.local_participant.publish_track(track, opts)
    print("[ROOM] published microphone track", flush=True)

    for _ in range(150):
        if any(p.identity.startswith("agent-") for p in room.remote_participants.values()):
            break
        await asyncio.sleep(0.1)
    print(f"[ROOM] remotes: {[p.identity for p in room.remote_participants.values()]}", flush=True)
    await asyncio.sleep(3.0)

    spc = int(SR_OUT * 0.02)
    for i in range(0, audio.size, spc):
        chunk = audio[i : i + spc]
        if chunk.size < spc:
            chunk = np.pad(chunk, (0, spc - chunk.size))
        frame = rtc.AudioFrame.create(SR_OUT, 1, spc)
        np.frombuffer(frame.data, dtype=np.int16)[:] = np.clip(
            chunk * 32767.0, -32768.0, 32767.0
        ).astype(np.int16)
        await source.capture_frame(frame)
        await asyncio.sleep(0.02)

    print("[AUDIO] streaming finished; waiting 30s for agent reply...", flush=True)
    await asyncio.sleep(30.0)
    print(f"[RESULT] chat messages received: {len(received)}", flush=True)
    for m in received:
        print(f"[RESULT] {m.get('role')}: {m.get('message')}", flush=True)

    await room.disconnect()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python scripts/e2e_audio_test.py <wav_path> [room]")
    asyncio.run(main())
