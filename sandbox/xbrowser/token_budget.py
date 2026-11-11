"""Actual Qwen token counting when configured; explicitly labelled fallback."""

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=2)
def load_tokenizer(path):
    from tokenizers import Tokenizer
    expected = json.loads(Path(__file__).with_name("tokenizer_config.json").read_text())["sha256"]
    if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
        raise ValueError("Configured Qwen tokenizer checksum does not match tokenizer_config.json")
    return Tokenizer.from_file(path)


def tokenizer():
    path = os.environ.get("XBROWSER_TOKENIZER_PATH")
    if path:
        return load_tokenizer(str(Path(path).resolve()))
    cached = Path(__file__).resolve().parents[2] / ".cache" / "xbrowser" / "tokenizer.json"
    return load_tokenizer(str(cached)) if cached.exists() else None


def token_count(text):
    encoder = tokenizer()
    return len(encoder.encode(text, add_special_tokens=False).ids) if encoder else None


def main():
    import argparse
    import urllib.request
    parser = argparse.ArgumentParser(description="Cache the checksum-pinned official Qwen tokenizer for local development")
    parser.add_argument("--download", action="store_true", required=True)
    parser.parse_args()
    config = json.loads(Path(__file__).with_name("tokenizer_config.json").read_text())
    data = urllib.request.urlopen(config["url"], timeout=120).read()
    if hashlib.sha256(data).hexdigest() != config["sha256"]:
        raise ValueError("Downloaded tokenizer checksum mismatch")
    path = Path(__file__).resolve().parents[2] / ".cache" / "xbrowser" / "tokenizer.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    print(f"Cached verified tokenizer at {path}")


if __name__ == "__main__":
    main()
