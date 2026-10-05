import asyncio
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import numpy as np
from src.ollama_client import OllamaClient


class StreamingTests(unittest.TestCase):
    def test_cancel_before_headers_cannot_cancel_replacement(self):
        old_started, release_old, new_started, release_new = [threading.Event() for _ in range(4)]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                name = body['messages'][0]['content']
                if name == 'old':
                    old_started.set()
                    release_old.wait(3)
                else:
                    new_started.set()
                    release_new.wait(3)
                result = (json.dumps({'message': {'content': name}, 'done': True}) + '\n').encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(result)))
                self.end_headers()
                try:
                    self.wfile.write(result)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        serving = threading.Thread(target=server.serve_forever, daemon=True)
        serving.start()
        client = OllamaClient(f'http://127.0.0.1:{server.server_port}')
        old_tokens, new_tokens = [], []
        stop_old, stop_new = threading.Event(), threading.Event()
        old = threading.Thread(target=lambda: old_tokens.extend(client.chat_stream('test', [{'role': 'user', 'content': 'old'}], stop_event=stop_old)))
        new = threading.Thread(target=lambda: new_tokens.extend(client.chat_stream('test', [{'role': 'user', 'content': 'new'}], stop_event=stop_new)))
        try:
            old.start()
            self.assertTrue(old_started.wait(2))
            started = time.monotonic()
            client.cancel_active_stream()
            self.assertLess(time.monotonic()-started, .1)
            new.start()
            self.assertTrue(new_started.wait(2))
            release_old.set()
            old.join(2)
            self.assertFalse(old.is_alive())
            self.assertEqual(old_tokens, [])
            self.assertFalse(stop_new.is_set())
            release_new.set()
            new.join(2)
            self.assertEqual(new_tokens, ['new'])
        finally:
            release_old.set()
            release_new.set()
            old.join(3)
            if new.ident:
                new.join(3)
            server.shutdown()
            server.server_close()


class WebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_silence_processes_audio_without_end_message(self):
        from src import ws_voice_server as voice
        packets = []

        class Socket:
            async def send(self, data):
                packets.append(data)

        with patch.object(voice, 'ChatSession') as chat, patch.object(voice, '_transcribe', return_value='hello'), patch.object(voice, '_synthesize_wav', return_value=b'RIFFtest'):
            chat.return_value.run.return_value = 'Welcome'
            connection = voice.VoiceConnection(Socket())
            connection._start_utterance()
            samples = .04*np.sin(2*np.pi*220*np.arange(16000)/16000)
            connection.vad.feed(samples.astype(np.float32))
            connection.vad.feed(np.zeros(24000, np.float32))
            self.assertIsNotNone(connection.process_task)
            await asyncio.wait_for(connection.process_task, 2)
            self.assertIn(b'RIFFtest', packets)
            self.assertTrue(any(isinstance(p, str) and 'Welcome' in p for p in packets))

    async def test_pcm_scaling_keeps_quiet_speech(self):
        from src.ws_voice_server import f32_to_int16
        self.assertEqual(f32_to_int16(np.array([-.5, 0, .5])).tolist(), [-16384, 0, 16384])


if __name__ == '__main__':
    unittest.main()
