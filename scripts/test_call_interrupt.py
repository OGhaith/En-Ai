"""Verify a real RTC reply stops during speech and a follow-up receives audio."""
import asyncio
import time
import uuid
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'scripts'))
from test_voice_call import pcm, wait_until
from livekit import api, rtc
import numpy as np
from src.voice_server import _build_tts


async def main():
    synth = _build_tts()
    question = pcm(synth.synthesize_wav('Please tell me about the imaging services you offer.'))[1]
    interruption = pcm(synth.synthesize_wav('Excuse me, please stop. What is your address?'))[1]
    room = rtc.Room()
    name = 'barge-test-' + uuid.uuid4().hex[:8]
    token = (api.AccessToken(os.environ['LIVEKIT_API_KEY'], os.environ['LIVEKIT_API_SECRET'])
             .with_identity('tester').with_grants(api.VideoGrants(room_join=True, room=name)).to_jwt())
    audio_times, tasks = [], []
    async def read(track):
        stream = rtc.AudioStream(track, sample_rate=16000, num_channels=1)
        try:
            async for event in stream:
                data = np.frombuffer(event.frame.data, np.int16).astype(np.int32)
                if data.size and np.max(np.abs(data)) > 250:
                    audio_times.append(time.monotonic())
        finally:
            await stream.aclose()
    @room.on('track_subscribed')
    def subscribed(track, *_):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            tasks.append(asyncio.create_task(read(track)))
    source = rtc.AudioSource(16000, 1, queue_size_ms=40)
    async def send(samples):
        for pos in range(0, len(samples), 320):
            part = samples[pos:pos+320]
            part = np.pad(part, (0, 320-len(part)))
            await source.capture_frame(rtc.AudioFrame(part.tobytes(), 16000, 1, 320))
        await source.wait_for_playout()
    try:
        await room.connect('ws://127.0.0.1:7880', token)
        await wait_until(lambda: audio_times and time.monotonic()-audio_times[-1] > .7)
        await room.local_participant.publish_track(rtc.LocalAudioTrack.create_audio_track('mic', source), rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        old_count = len(audio_times)
        await send(np.concatenate([question, np.zeros(16000, np.int16)]))
        await wait_until(lambda: len(audio_times) > old_count)
        # Interrupt an answer that is actually playing, without sending an end signal.
        await asyncio.sleep(.25)
        onset = time.monotonic()
        sending = asyncio.create_task(send(np.concatenate([interruption, np.zeros(16000, np.int16)])))
        await asyncio.sleep(1.2)
        during = [t for t in audio_times if t >= onset]
        cutoff = (during[-1] - onset) if during else 0
        print(f'barge_in_audio_cutoff={cutoff:.2f}s', flush=True)
        assert cutoff < .9, f'Old reply continued during interruption: {cutoff:.2f}s'
        await sending
        finished = time.monotonic()
        await wait_until(lambda: audio_times and audio_times[-1] > finished)
        print('PASS interruption: old audio stopped; follow-up audio received', flush=True)
    finally:
        await source.aclose()
        await room.disconnect()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == '__main__':
    asyncio.run(main())
