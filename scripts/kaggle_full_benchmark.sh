#!/usr/bin/env bash
set -Eeuo pipefail

# Complete, idempotent Kaggle setup and full-path voice benchmark.
# Run from the repository root:
#   bash scripts/kaggle_full_benchmark.sh
# Optional overrides:
#   LEVELS="1 10" CLINIC_NUM_CTX=32768 bash scripts/kaggle_full_benchmark.sh

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_ROOT="${KAGGLE_WORKING_DIR:-/kaggle/working}"
PIPER_DIR="${WORK_ROOT}/piper-linux"
PIPER_ARCHIVE="${WORK_ROOT}/piper.tar.gz"
PIPER_MODEL_DIR="${ROOT}/models/piper"
WHISPER_DIR="${ROOT}/models/hub/large-v3-turbo"
QUESTION_WAV="${WORK_ROOT}/benchmark-question.wav"
RESULT_DIR="${ROOT}/logs/kaggle-benchmark"
OLLAMA_MODEL_FILE="${WORK_ROOT}/ollama-model.txt"
WSVOICE_PID_FILE="${WORK_ROOT}/wsvoice.pid"
OLLAMA_LOG="${WORK_ROOT}/ollama.log"

LEVELS="${LEVELS:-1 10 20 30 40}"
CLINIC_NUM_CTX="${CLINIC_NUM_CTX:-49152}"
PIPER_THREADS="${PIPER_THREADS:-1}"
COOLDOWN_SECONDS="${COOLDOWN_SECONDS:-20}"
BENCHMARK_TIMEOUT="${BENCHMARK_TIMEOUT:-600}"

mkdir -p "${RESULT_DIR}" "${PIPER_MODEL_DIR}" "${WHISPER_DIR}"
cd "${ROOT}"

echo "[1/10] Installing Linux/Python dependencies"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq zstd curl wget ca-certificates
python -m pip install -q --no-cache-dir -r requirements-kaggle.txt

echo "[2/10] Installing Linux Piper"
if [[ ! -x "${PIPER_DIR}/piper" ]]; then
  rm -rf "${PIPER_DIR}"
  mkdir -p "${PIPER_DIR}"
  wget -q --show-progress \
    "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz" \
    -O "${PIPER_ARCHIVE}"
  tar -xzf "${PIPER_ARCHIVE}" -C "${PIPER_DIR}" --strip-components=1
  chmod +x "${PIPER_DIR}/piper"
fi

echo "[3/10] Downloading the low-CPU Danny voice"
VOICE_BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/danny/low"
for suffix in onnx onnx.json; do
  target="${PIPER_MODEL_DIR}/en_US-danny-low.${suffix}"
  if [[ ! -s "${target}" ]]; then
    wget -q --show-progress "${VOICE_BASE}/en_US-danny-low.${suffix}" -O "${target}"
  fi
done

echo "[4/10] Downloading faster-whisper large-v3-turbo"
if [[ ! -f "${WHISPER_DIR}/model.bin" ]]; then
  WHISPER_DIR="${WHISPER_DIR}" python - <<'PY'
import os
from huggingface_hub import snapshot_download

snapshot_download(
    repo_id="mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    local_dir=os.environ["WHISPER_DIR"],
)
PY
fi

echo "[5/10] Installing and starting Ollama"
if ! command -v ollama >/dev/null 2>&1; then
  curl -fsSL https://ollama.com/install.sh | sh
fi
if ! curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
  nohup env OLLAMA_HOST=127.0.0.1:11434 ollama serve >"${OLLAMA_LOG}" 2>&1 &
fi
for _ in $(seq 1 120); do
  curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1 && break
  sleep 1
done
curl -sf http://127.0.0.1:11434/api/tags >/dev/null

if [[ -s "${OLLAMA_MODEL_FILE}" ]] && ollama list | grep -q "^$(cat "${OLLAMA_MODEL_FILE}")"; then
  MODEL="$(cat "${OLLAMA_MODEL_FILE}")"
elif ollama pull gemma4:4b; then
  MODEL="gemma4:4b"
else
  ollama pull gemma3:4b
  MODEL="gemma3:4b"
fi
printf '%s' "${MODEL}" >"${OLLAMA_MODEL_FILE}"
echo "Using Ollama model: ${MODEL}"

echo "[6/10] Warming Ollama with the real clinic prompt (num_ctx=${CLINIC_NUM_CTX})"
ROOT="${ROOT}" MODEL="${MODEL}" CLINIC_NUM_CTX="${CLINIC_NUM_CTX}" python - <<'PY'
import os
import sys
import time
import requests

root = os.environ["ROOT"]
sys.path.insert(0, root)
from src.clinic_prompt import get_system_prompt

payload = {
    "model": os.environ["MODEL"],
    "messages": [
        {"role": "system", "content": get_system_prompt()},
        {"role": "user", "content": "How long does my report take?"},
    ],
    "stream": False,
    "keep_alive": -1,
    "options": {
        "num_ctx": int(os.environ["CLINIC_NUM_CTX"]),
        "num_predict": 80,
        "temperature": 0,
    },
}
started = time.perf_counter()
response = requests.post(
    "http://127.0.0.1:11434/api/chat",
    json=payload,
    timeout=1200,
)
print(f"Ollama warm-up HTTP={response.status_code} wall={time.perf_counter()-started:.3f}s")
print(response.text[:1200])
response.raise_for_status()
PY

echo "[7/10] Starting the WebSocket voice server"
if [[ -s "${WSVOICE_PID_FILE}" ]]; then
  kill "$(cat "${WSVOICE_PID_FILE}")" 2>/dev/null || true
  sleep 2
fi

# Kaggle has weak CPU capacity. One OpenMP thread per Piper process prevents
# 10-40 simultaneous callers from multiplying CPU worker threads.
PRESERVED_LD_LIBRARY_PATH="${PIPER_DIR}:${LD_LIBRARY_PATH:-}"
nohup env \
  LD_LIBRARY_PATH="${PRESERVED_LD_LIBRARY_PATH}" \
  OMP_NUM_THREADS="${PIPER_THREADS}" \
  OMP_DYNAMIC=FALSE \
  OMP_WAIT_POLICY=PASSIVE \
  OPENBLAS_NUM_THREADS=1 \
  MKL_NUM_THREADS=1 \
  NUMEXPR_NUM_THREADS=1 \
  OLLAMA_HOST=http://127.0.0.1:11434 \
  OLLAMA_MODEL="${MODEL}" \
  OLLAMA_KEEP_ALIVE=-1 \
  CHAT_NUM_CTX="${CLINIC_NUM_CTX}" \
  CLINIC_NUM_CTX="${CLINIC_NUM_CTX}" \
  CHAT_NUM_PREDICT=80 \
  WHISPER_DEVICE=cuda \
  WHISPER_COMPUTE_TYPE=float16 \
  WSVOICE_WHISPER_MODEL="${WHISPER_DIR}" \
  PIPER_EXE="${PIPER_DIR}/piper" \
  PIPER_MODEL="${PIPER_MODEL_DIR}/en_US-danny-low.onnx" \
  PIPER_CONFIG="${PIPER_MODEL_DIR}/en_US-danny-low.onnx.json" \
  PIPER_THREADS="${PIPER_THREADS}" \
  PIPER_NOISE_SCALE=0.7 \
  PIPER_LENGTH_SCALE=1.0 \
  WSVOICE_HOST=0.0.0.0 \
  WSVOICE_PORT=7444 \
  WSVOICE_TLS=0 \
  WSVOICE_CERT=/tmp/nonexistent-cert.pem \
  WSVOICE_KEY=/tmp/nonexistent-key.pem \
  python -u -m src.ws_voice_server >"${ROOT}/logs/kaggle-wsvoice.log" 2>&1 &
echo $! >"${WSVOICE_PID_FILE}"

for _ in $(seq 1 120); do
  curl -sf http://127.0.0.1:7444/ >/dev/null 2>&1 && break
  sleep 1
done
curl -sf http://127.0.0.1:7444/ >/dev/null
echo "Voice server PID=$(cat "${WSVOICE_PID_FILE}")"

echo "[8/10] Creating the benchmark WAV"
export LD_LIBRARY_PATH="${PRESERVED_LD_LIBRARY_PATH}"
printf '%s\n' "How long does my report take?" | \
  "${PIPER_DIR}/piper" \
  --model "${PIPER_MODEL_DIR}/en_US-danny-low.onnx" \
  --config "${PIPER_MODEL_DIR}/en_US-danny-low.onnx.json" \
  --output_file "${QUESTION_WAV}"

echo "[9/10] Running synchronized full-path levels: ${LEVELS}"
for USERS in ${LEVELS}; do
  PADDED="$(printf '%02d' "${USERS}")"
  echo "===== ${USERS} concurrent users ====="
  python scripts/benchmark_wsvoice_concurrency.py \
    --uri ws://127.0.0.1:7444/ \
    --audio "${QUESTION_WAV}" \
    --question "How long does my report take?" \
    --voice danny-low \
    --levels "${USERS}" \
    --timeout "${BENCHMARK_TIMEOUT}" \
    --warmup \
    --output "${RESULT_DIR}/level-${PADDED}.json"
  sleep "${COOLDOWN_SECONDS}"
done

echo "[10/10] Writing the summary"
RESULT_DIR="${RESULT_DIR}" python - <<'PY'
import glob
import json
import os
import statistics
from pathlib import Path

def percentile(values, percentage):
    values = sorted(values)
    if not values:
        return 0.0
    position = (len(values) - 1) * percentage / 100
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight

result_dir = Path(os.environ["RESULT_DIR"])
rows = []
for filename in sorted(glob.glob(str(result_dir / "level-*.json"))):
    data = json.loads(Path(filename).read_text(encoding="utf-8"))
    level = data["levels"][0]
    clients = [item for item in level["clients"] if item["success"]]

    def median(key):
        values = [item[key] for item in clients if item.get(key) is not None]
        return statistics.median(values) if values else 0.0

    response = [item["end_to_audio_seconds"] for item in clients if item.get("end_to_audio_seconds") is not None]
    rows.append({
        "users": level["concurrency"],
        "success": f'{level["successes"]}/{level["concurrency"]}',
        "correct_stt": level["correct_transcripts"],
        "correct_answers": level["correct_answers"],
        "stt": median("end_to_transcript_seconds"),
        "llm": median("transcript_to_reply_seconds"),
        "tts": median("reply_to_audio_seconds"),
        "median": statistics.median(response) if response else 0.0,
        "p95": percentile(response, 95),
        "max": max(response) if response else 0.0,
        "vram": level.get("max_gpu_memory_mib") or 0,
    })

header = "| Users | Success | Correct STT | Correct answers | STT s | LLM s | TTS s | Median s | P95 s | Max s | Peak VRAM MiB |"
separator = "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
lines = [header, separator]
for row in rows:
    lines.append(
        f"| {row['users']} | {row['success']} | {row['correct_stt']} | {row['correct_answers']} | "
        f"{row['stt']:.3f} | {row['llm']:.3f} | {row['tts']:.3f} | "
        f"{row['median']:.3f} | {row['p95']:.3f} | {row['max']:.3f} | {row['vram']:.0f} |"
    )

report = "\n".join(lines) + "\n"
(result_dir / "SUMMARY.md").write_text(report, encoding="utf-8")
print(report)
print(f"Saved: {result_dir / 'SUMMARY.md'}")
PY

echo "Benchmark complete. Results: ${RESULT_DIR}"
