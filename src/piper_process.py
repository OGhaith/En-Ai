"""Keep the existing Piper binary/model loaded across synthesis requests."""
import atexit
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time


class PiperProcess:
    def __init__(self, command):
        self.command = command
        self.lock = threading.Lock()
        self.process = None
        self.lines = None
        atexit.register(self.close)

    def close(self):
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            for pipe in (process.stdin, process.stdout):
                if pipe:
                    pipe.close()

    def start(self):
        self.close()
        self.lines = queue.Queue()
        self.process = subprocess.Popen(
            self.command + ['--json-input'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding='utf-8', bufsize=1,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        process, lines = self.process, self.lines

        def read():
            try:
                for line in process.stdout:
                    lines.put(line.strip())
            finally:
                lines.put(None)

        threading.Thread(target=read, daemon=True).start()

    def synthesize(self, text):
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self.start()
            fd, name = tempfile.mkstemp(prefix='voice_piper_', suffix='.wav')
            os.close(fd)
            try:
                self.process.stdin.write(json.dumps({'text': text, 'output_file': name}) + '\n')
                self.process.stdin.flush()
                result = self.lines.get(timeout=30)
                if result != name:
                    raise RuntimeError('Piper stopped before completing synthesis')
                # Piper prints the completed path just before Windows always
                # releases the WAV handle. Under high concurrency this can
                # briefly raise sharing violation 32, so retry that hand-off
                # race instead of failing an otherwise successful call.
                data = None
                for attempt in range(20):
                    try:
                        data = Path(name).read_bytes()
                        break
                    except PermissionError:
                        if attempt == 19:
                            raise
                        time.sleep(0.025)
                if data is None:
                    raise RuntimeError("Piper WAV was not readable")
                if len(data) <= 44:
                    raise RuntimeError('Piper returned empty audio')
                return data
            except Exception:
                self.close()
                raise
            finally:
                Path(name).unlink(missing_ok=True)
