"""Prepare the local U2Net model once. Runtime inference performs no downloads."""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import urllib.request
from pathlib import Path

URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx"
SIZE = 175_997_641
SHA256 = "8d10d2f3bb75ae3b6d527c77944fc5e7dcd94b29809d47a739a7a728a912b491"


def verify(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size != SIZE:
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == SHA256


def download(destination: Path) -> None:
    if destination.exists():
        if verify(destination):
            print("Local U2Net model is ready and verified.")
            return
        raise RuntimeError(
            f"Existing model failed verification. Move it aside before retrying: {destination}"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(".onnx.part")
    if partial.exists() and partial.stat().st_size >= SIZE:
        if verify(partial):
            partial.rename(destination)
            print("Completed model verified.")
            return
        raise RuntimeError(f"Partial model is invalid. Move it aside before retrying: {partial}")

    offset = partial.stat().st_size if partial.exists() else 0
    headers = {"User-Agent": "offline-product-image-workbench/1.0"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(URL, headers=headers)
    print("Downloading U2Net from its upstream release. This step needs Internet access.")
    with urllib.request.urlopen(request, timeout=90) as response:
        if response.status == 206:
            prefix = f"bytes {offset}-"
            if not response.headers.get("Content-Range", "").startswith(prefix):
                raise RuntimeError("Download server returned an unexpected byte range.")
            mode = "ab"
        elif response.status == 200:
            mode = "wb"
            offset = 0
        else:
            raise RuntimeError(f"Unexpected download status: {response.status}")

        total = offset
        shown = -1
        with partial.open(mode) as target:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > SIZE:
                    raise RuntimeError("Received more bytes than the expected model size.")
                target.write(chunk)
                progress = total * 100 // SIZE
                if progress // 5 != shown:
                    print(f"{progress}% ({total}/{SIZE} bytes)", flush=True)
                    shown = progress // 5
            target.flush()
            os.fsync(target.fileno())

    if not verify(partial):
        raise RuntimeError(f"Model checksum failed. Partial file retained at {partial}")
    if destination.exists():
        if not verify(destination):
            raise RuntimeError("A different model appeared during download. Existing file preserved.")
        print("Another process prepared a verified model. Partial file retained.")
        return
    partial.rename(destination)
    print("U2Net model verified. The workbench can now use it offline.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model-dir", type=Path, default=Path(__file__).resolve().parents[1] / "models"
    )
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    destination = args.model_dir / "u2net.onnx"
    if args.verify_only:
        if not verify(destination):
            raise RuntimeError("Local U2Net model is missing or failed SHA-256 verification.")
        print("U2Net size and SHA-256 verified.")
    else:
        download(destination)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError) as error:
        print(f"Model preparation failed: {error}", file=sys.stderr)
        sys.exit(1)
