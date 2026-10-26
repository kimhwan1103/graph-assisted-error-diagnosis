"""Validate a mounted Gemma 4 checkpoint and serve it without network downloads."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .design import HF_MODEL_ID, MODEL_ID


@contextmanager
def running_server(command, log_path, base_url, *, startup_seconds=1200):
    """Own the process through startup AND inference, including keyboard interrupts."""
    import httpx

    process = None
    with log_path.open("w", encoding="utf-8") as log:
        try:
            process = subprocess.Popen(
                command, env=os.environ.copy(), stdout=log, stderr=subprocess.STDOUT
            )
            deadline = time.monotonic() + startup_seconds
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"vLLM exited; inspect {log_path}")
                ready = False
                try:
                    response = httpx.get(base_url + "/models", timeout=2, trust_env=False)
                    ready = response.status_code == 200 and MODEL_ID in {
                        model.get("id") for model in response.json().get("data", [])
                    }
                except httpx.HTTPError:
                    pass
                if ready:
                    yield process
                    return
                time.sleep(2)
            raise RuntimeError(f"Server startup timed out; inspect {log_path}")
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=30)


def checkpoint_info(model_dir, *, hash_weights=False):
    model_dir = model_dir.resolve(strict=True)
    config_file = model_dir / "config.json"
    config = json.loads(config_file.read_text("utf-8"))
    text = config.get("text_config") or config
    quant = config.get("quantization_config") or {}
    groups = quant.get("config_groups") or {}
    if (
        config.get("model_type") != "gemma4"
        or text.get("hidden_size") != 5376
        or text.get("num_experts") is not None
        or quant.get("quant_method") != "compressed-tensors"
        or quant.get("format") != "pack-quantized"
        or not groups
        or any(g.get("weights", {}).get("num_bits") != 4 for g in groups.values())
    ):
        raise ValueError("Expected the official Gemma 4 31B W4A16 compressed-tensors checkpoint")
    weights = sorted(model_dir.glob("*.safetensors"))
    if not weights or any(p.stat().st_size == 0 for p in weights):
        raise ValueError("Missing or empty safetensors weights")
    files = {}
    for path in weights + [
        config_file,
        model_dir / "tokenizer_config.json",
        model_dir / "chat_template.jinja",
    ]:
        if not path.exists():
            continue
        sha = None
        if hash_weights or path.suffix != ".safetensors":
            h = hashlib.sha256()
            with path.open("rb") as file:
                for chunk in iter(lambda: file.read(8 * 1024 * 1024), b""):
                    h.update(chunk)
            sha = h.hexdigest()
        files[path.name] = {"bytes": path.stat().st_size, "sha256": sha}
    return {
        "intended_checkpoint": HF_MODEL_ID,
        "served_alias": MODEL_ID,
        "model_dir": str(model_dir),
        "files": files,
        "weights_hashed": hash_weights,
        "identity_limit": "architecture/quantization checked; record Kaggle Model version and compare "
        "weight hashes to the official checkpoint for exact identity",
    }


def server_command(model_dir, *, tp=4, max_model_len=16384, port=8000):
    return [
        sys.executable,
        "-m",
        "vllm.entrypoints.openai.api_server",
        "--model",
        str(model_dir),
        "--served-model-name",
        MODEL_ID,
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--tensor-parallel-size",
        str(tp),
        "--dtype",
        "bfloat16",
        "--max-model-len",
        str(max_model_len),
        "--max-num-seqs",
        "1",
        "--gpu-memory-utilization",
        "0.90",
        "--reasoning-parser",
        "gemma4",
        "--no-enable-prefix-caching",
        "--limit-mm-per-prompt",
        '{"image":0}',
    ]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--tensor-parallel-size", type=int, choices=(1, 2, 4, 8), default=4)
    parser.add_argument("--max-model-len", type=int, default=16384)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--hash-weights", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--provenance", type=Path, default=Path("results/gemma-server.json"))
    args = parser.parse_args(argv)
    if not 4096 <= args.max_model_len <= 262144 or not 1024 <= args.port <= 65535:
        parser.error("Invalid context length or port")
    try:
        info = checkpoint_info(args.model_dir, hash_weights=args.hash_weights)
    except (ValueError, FileNotFoundError) as exc:
        parser.exit(2, f"Checkpoint validation failed: {exc}\n")
    command = server_command(
        args.model_dir.resolve(),
        tp=args.tensor_parallel_size,
        max_model_len=args.max_model_len,
        port=args.port,
    )
    info["command"] = command
    args.provenance.parent.mkdir(parents=True, exist_ok=True)
    args.provenance.write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(info, ensure_ascii=False, indent=2), flush=True)
    if args.validate_only:
        return 0
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    return subprocess.call(command, env=env)


if __name__ == "__main__":
    raise SystemExit(main())
