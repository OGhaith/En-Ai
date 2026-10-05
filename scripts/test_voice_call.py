"""Exercise real local RTC audio, endpointing, reply audio and reconnects.

No microphone is recorded: the test publishes Piper-generated questions.
Run: .venv/Scripts/python.exe scripts/test_voice_call.py
"""
from __future__ import annotations
import asyncio
import io
import json
import os
from pathlib import Path
import sys
import time
import uuid
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / '.env.local')
import numpy as np
from livekit import api, rtc
from src.voice_server import _build_tts


def pcm(wav):
    with wave.open(io.BytesIO(wav), 'rb') as w:
        rate = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), np.int16)
    return rate, data


async def wait_until(predicate, timeout=45):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('Timed out waiting for call state')
        await asyncio.sleep(.05)


async def call(questions, call_number):
    name = 'call-test-' + uuid.uuid4().hex[:10]
    token = (api.AccessToken(os.environ['LIVEKIT_API_KEY'], os.environ['LIVEKIT_API_SECRET'])
             .with_identity('test-speaker-' + name)
             .with_grants(api.VideoGrants(room_join=True, room=name)).to_jwt())
    room = rtc.Room()
    messages, audio_times, frames, readers = [], [], [], []

    @room.on('data_received')
    def data_received(packet):
        if packet.topic != 'lk.chat':
            return
        msg = json.loads(packet.data)
        messages.append(msg)
        print(f"CHAT {msg.get('role')}: {msg.get('message')}", flush=True)

    async def read_audio(track):
        stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
        try:
            async for event in stream:
                samples = np.frombuffer(event.frame.data, np.int16)
                frames.append(samples.copy())
                if samples.size and np.max(np.abs(samples.astype(np.int32))) > 250:
                    audio_times.append(time.monotonic())
        finally:
            await stream.aclose()

    @room.on('track_subscribed')
    def subscribed(track, publication, participant):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            readers.append(asyncio.create_task(read_audio(track)))

    started = time.monotonic()
    try:
        await room.connect('ws://127.0.0.1:7880', token)
        await wait_until(lambda: any(p.attributes.get('voice.state') for p in room.remote_participants.values()))
        print(f'CALL {call_number}: agent ready in {time.monotonic()-started:.2f}s', flush=True)
        await wait_until(lambda: audio_times and time.monotonic()-audio_times[-1] > .7)
        source = rtc.AudioSource(16000, 1, queue_size_ms=40)
        track = rtc.LocalAudioTrack.create_audio_track('test-microphone', source)
        await room.local_participant.publish_track(track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        for rate, samples in questions:
            assert rate == 16000
            before_users = sum(m.get('role') == 'user' for m in messages)
            before_replies = sum(m.get('role') == 'assistant' for m in messages)
            audio_start = len(audio_times)
            last_voice = time.monotonic()
            # Include silence to let Silero detect the end without a push-to-talk signal.
            samples = np.concatenate([samples, np.zeros(16000, np.int16)])
            for pos in range(0, len(samples), 320):
                chunk = samples[pos:pos+320]
                if len(chunk) < 320:
                    chunk = np.pad(chunk, (0, 320-len(chunk)))
                frame = rtc.AudioFrame(chunk.tobytes(), 16000, 1, 320)
                await source.capture_frame(frame)
                if np.max(np.abs(chunk.astype(np.int32))) > 250:
                    last_voice = time.monotonic() + source.queued_duration
            await source.wait_for_playout()
            await wait_until(lambda: sum(m.get('role') == 'user' for m in messages) > before_users)
            await wait_until(lambda: len(audio_times) > audio_start)
            latency = audio_times[audio_start]-last_voice
            print(f'CALL {call_number}: silence_to_reply_audio={latency:.2f}s', flush=True)
            assert 0 <= latency < 15, f'Reply too slow: {latency:.2f}s'
            await wait_until(lambda: sum(m.get('role') == 'assistant' for m in messages) > before_replies)
            await wait_until(lambda: time.monotonic()-audio_times[-1] > .7)
        await source.aclose()
        if frames:
            out = ROOT/'logs'/f'call-test-{call_number}-reply.wav'
            with wave.open(str(out), 'wb') as w:
                w.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                w.writeframes(np.concatenate(frames).tobytes())
        print(f'PASS call {call_number}: transcripts and audible replies received', flush=True)
    finally:
        await room.disconnect()
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


async def main():
    tts = _build_tts()
    questions = [pcm(tts.synthesize_wav(text)) for text in
                 ['What is the name of your clinic?', 'Do you offer MRI scans?']]
    await call(questions, 1)
    await call(questions[:1], 2)


if __name__ == '__main__':
    asyncio.run(main())
