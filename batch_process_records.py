#!/usr/bin/env python3
import argparse
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Tuple


ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE = Path("/Users/vishalsharma/Downloads/anthropod-recordings")
DEFAULT_DEST = ROOT / "reports and recordings"
MEDIA_EXTS = {".mp3", ".wav", ".m4a", ".mp4", ".webm", ".ogg", ".flac", ".mov"}


def stage_records(source: Path, dest: Path, limit: Optional[int] = None) -> List[str]:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    records = [p for p in sorted(source.iterdir(), key=lambda x: x.name) if p.is_dir()]
    if limit:
        records = records[:limit]

    staged: List[str] = []
    for rec in records:
        rid = rec.name
        out = dest / rid
        out.mkdir(parents=True, exist_ok=True)

        mer = rec / "mer.pdf"
        if mer.exists():
            shutil.copy2(mer, out / f"{rid}_MER.pdf")

        media = []
        for sub in ("audio", "video"):
            d = rec / sub
            if d.exists():
                media.extend(
                    p for p in sorted(d.iterdir(), key=lambda x: x.name)
                    if p.is_file() and p.suffix.lower() in MEDIA_EXTS
                )
        for idx, src in enumerate(media, start=1):
            shutil.copy2(src, out / f"{rid}_call{idx}{src.suffix.lower()}")

        if mer.exists() and media:
            staged.append(rid)
        else:
            print(f"SKIP staging incomplete record {rid}: mer={mer.exists()} media={len(media)}", flush=True)

    return staged


def process_one(rid: str, api_key: str, dest: Path) -> Tuple[str, int]:
    rec_dir = dest / rid
    cmd = [sys.executable, "medb.py", "--record-dir", str(rec_dir), "--api-key", api_key]
    log_path = rec_dir / "_processed" / "batch_process.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.run(cmd, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, text=True)
    return rid, proc.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage and process MediBuddy records in parallel.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--dest", type=Path, default=DEFAULT_DEST)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--parallel", type=int, default=10)
    parser.add_argument("--skip-stage", action="store_true")
    parser.add_argument("--upload-s3", action="store_true", help="Run s3_uploader.py after successful processing.")
    args = parser.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY is required in the environment.", file=sys.stderr)
        return 2

    if args.skip_stage:
        rids = [p.name for p in sorted(args.dest.iterdir(), key=lambda x: x.name) if p.is_dir()]
    else:
        rids = stage_records(args.source, args.dest, args.limit)

    print(f"Processing {len(rids)} records with parallel={args.parallel}", flush=True)
    failed: List[str] = []
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        futures = [pool.submit(process_one, rid, api_key, args.dest) for rid in rids]
        for fut in as_completed(futures):
            rid, rc = fut.result()
            status = "OK" if rc == 0 else f"FAILED rc={rc}"
            print(f"{rid}: {status}", flush=True)
            if rc != 0:
                failed.append(rid)

    subprocess.run([sys.executable, "decision_builder.py"], cwd=ROOT, check=False)

    if failed:
        print("Failed records:", ", ".join(failed), file=sys.stderr)
        return 1

    if args.upload_s3:
        subprocess.run([sys.executable, "s3_uploader.py"], cwd=ROOT, check=True)

    print("Batch complete.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
