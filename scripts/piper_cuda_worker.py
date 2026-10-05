#!/usr/bin/env python3
"""Persistent Piper worker compatible with src.piper_process.PiperProcess."""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--noise-scale", "--noise_scale", type=float, default=0.667)
    parser.add_argument("--length-scale", "--length_scale", type=float, default=1.0)
    parser.add_argument("--cuda", action="store_true")
    parser.add_argument("--json-input", "--json_input", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    import onnxruntime as ort
    from piper import PiperVoice, SynthesisConfig

    providers = ort.get_available_providers()
    if args.cuda and "CUDAExecutionProvider" not in providers:
        raise RuntimeError(
            "Piper CUDA was requested but onnxruntime does not expose "
            f"CUDAExecutionProvider. Available providers: {providers}"
        )

    voice = PiperVoice.load(
        args.model,
        config_path=args.config,
        use_cuda=args.cuda,
    )
    synthesis_config = SynthesisConfig(
        noise_scale=args.noise_scale,
        length_scale=args.length_scale,
    )

    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue

        request = json.loads(raw_line)
        output_file = Path(request["output_file"])
        with wave.open(str(output_file), "wb") as wav_file:
            voice.synthesize_wav(
                str(request.get("text", "")),
                wav_file,
                syn_config=synthesis_config,
            )

        print(str(output_file), flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
