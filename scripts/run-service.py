"""Launch one service from this checkout, independent of the current directory."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
load_dotenv(ROOT / '.env.local')
os.environ['PYTHONUNBUFFERED'] = '1'
service = sys.argv[1]
python = sys.executable
if service == 'livekit':
    cmd = [str(ROOT / 'livekit_server/livekit-server.exe'), '--dev',
           '--bind', '0.0.0.0', '--keys',
           f"{os.environ['LIVEKIT_API_KEY']}: {os.environ['LIVEKIT_API_SECRET']}",
           # Enables the built-in TURN server over TLS/TCP. The browser and this
           # host are on separate OpenVPN-bridged networks where UDP is dropped,
           # so audio has to be relayed over the TCP path that does work.
           '--config', str(ROOT / 'livekit.yaml')]
    # Prefer the Ethernet address over VPN/virtual adapters when configured.
    if os.getenv('LIVEKIT_NODE_IP'):
        cmd += ['--node-ip', os.environ['LIVEKIT_NODE_IP']]
elif service == 'agent':
    os.environ['LIVEKIT_URL'] = os.getenv('LIVEKIT_INTERNAL_URL', 'ws://127.0.0.1:7880')
    cmd = [python, '-m', 'livekit_agent.src.agent', 'start']
elif service == 'wsvoice':
    cmd = [python, '-m', 'src.ws_voice_server']
elif service == 'wssproxy':
    cmd = [python, str(ROOT / 'scripts/wss-proxy.py')]
elif service == 'frontend':
    os.chdir(ROOT / 'frontend')
    # The frontend has its own URL for browsers; do not inherit the agent URL.
    load_dotenv(ROOT / 'frontend/.env.local', override=True)
    cmd = [shutil.which('node') or 'node', 'node_modules/next/dist/bin/next',
           'dev', '--turbopack', '--hostname', '0.0.0.0', '--port', '3000',
           '--experimental-https', '--experimental-https-cert', str(ROOT / 'certs/livekit.pem'),
           '--experimental-https-key', str(ROOT / 'certs/livekit-key.pem')]
else:
    raise SystemExit(f'Unknown service: {service}')
raise SystemExit(subprocess.call(cmd))
