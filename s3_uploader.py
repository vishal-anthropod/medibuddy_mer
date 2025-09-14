import os
import sys
import time
import json
import mimetypes
from pathlib import Path

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError


WORKSPACE_ROOT = Path(__file__).resolve().parent


def human_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for unit in units:
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"


def build_s3_client() -> boto3.client:
    aws_access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    aws_secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    aws_region = os.environ.get("AWS_REGION", "ap-south-1")

    if not aws_access_key or not aws_secret_key:
        raise RuntimeError("AWS credentials not found in environment variables.")

    return boto3.client(
        "s3",
        aws_access_key_id=aws_access_key,
        aws_secret_access_key=aws_secret_key,
        region_name=aws_region,
        config=Config(signature_version="s3v4"),
    )


def iter_media_files(base_dir: Path):
    allowed_ext = {
        ".pdf",
        ".mp3",
        ".wav",
        ".m4a",
        ".mp4",
        ".mov",
        ".avi",
        ".mkv",
    }
    exclude_dirs = {"node_modules", ".git", "__pycache__"}

    for root, dirs, files in os.walk(base_dir):
        # Prune excluded directories and heavy processed artifacts
        dirs[:] = [d for d in dirs if d not in exclude_dirs]

        # Prefer to skip deep processed chunks to avoid noise
        if "_processed" in root and "/chunks" in root:
            continue

        for fname in files:
            fpath = Path(root) / fname
            if fpath.suffix.lower() in allowed_ext:
                yield fpath


def guess_content_type(path: Path) -> str:
    ctype, _ = mimetypes.guess_type(str(path))
    return ctype or "application/octet-stream"


def upload_and_replace(
    s3_client: boto3.client,
    bucket: str,
    key_prefix: str,
    files: list[Path],
    presign_expires: int = 604800,  # 7 days
):
    manifest = []
    total = len(files)
    uploaded_bytes = 0

    for idx, path in enumerate(files, start=1):
        rel_path = path.relative_to(WORKSPACE_ROOT)
        key = f"{key_prefix}/{rel_path.as_posix()}"
        content_type = guess_content_type(path)

        size = path.stat().st_size
        print(f"[{idx}/{total}] Uploading {rel_path} ({human_size(size)}) → s3://{bucket}/{key}")
        try:
            s3_client.upload_file(
                Filename=str(path),
                Bucket=bucket,
                Key=key,
                ExtraArgs={"ContentType": content_type},
            )
            uploaded_bytes += size
        except (BotoCoreError, ClientError) as e:
            print(f"ERROR: Failed to upload {rel_path}: {e}")
            continue

        try:
            url = s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=presign_expires,
            )
        except (BotoCoreError, ClientError) as e:
            print(f"ERROR: Failed to generate URL for {rel_path}: {e}")
            url = None

        # Overwrite local file with pointer to S3 URL (text). This intentionally replaces content.
        try:
            with open(path, "w", encoding="utf-8") as wf:
                wf.write(url or f"s3://{bucket}/{key}")
                wf.write("\n")
        except Exception as e:
            print(f"ERROR: Failed to replace local file {rel_path} with URL: {e}")

        manifest.append(
            {
                "local_path": str(rel_path),
                "bucket": bucket,
                "key": key,
                "content_type": content_type,
                "size_bytes": size,
                "presigned_url": url,
            }
        )

    return manifest, uploaded_bytes


def main():
    bucket = os.environ.get("S3_BUCKET_NAME", "anthropod")
    key_prefix = os.environ.get("S3_KEY_PREFIX", "temp/medibuddy")

    # Scope: root and "reports and recordings" if it exists
    scan_dirs = [WORKSPACE_ROOT]
    rr_dir = WORKSPACE_ROOT / "reports and recordings"
    if rr_dir.exists():
        scan_dirs.append(rr_dir)

    # Collect files (unique paths)
    files = []
    seen = set()
    for base in scan_dirs:
        for f in iter_media_files(base):
            # Ensure within workspace
            try:
                rel = f.relative_to(WORKSPACE_ROOT)
            except ValueError:
                continue
            if rel in seen:
                continue
            seen.add(rel)
            files.append(f)

    if not files:
        print("No media/PDF files found to upload.")
        return 0

    print(f"Found {len(files)} files to upload.")

    s3 = build_s3_client()
    t0 = time.time()
    manifest, total_bytes = upload_and_replace(s3, bucket, key_prefix, files)
    dt = time.time() - t0

    manifest_path = WORKSPACE_ROOT / "s3_manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as mf:
        json.dump(
            {
                "bucket": bucket,
                "key_prefix": key_prefix,
                "total_files": len(manifest),
                "total_bytes": total_bytes,
                "elapsed_seconds": round(dt, 2),
                "items": manifest,
            },
            mf,
            indent=2,
        )

    print(
        f"Uploaded {len(manifest)} files ({human_size(total_bytes)}) in {dt:.1f}s. Manifest: {manifest_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())


