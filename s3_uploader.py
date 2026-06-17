import os
import sys
import time
import json
import mimetypes
import argparse
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
import urllib.request
import email.utils

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
    aws_access_key = os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY")
    aws_secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_KEY")
    aws_region = os.environ.get("AWS_REGION", "ap-south-1")

    # If explicit env vars are provided, use them; otherwise rely on default provider chain (profile/SSO/IMDS)
    if aws_access_key and aws_secret_key:
        return boto3.client(
            "s3",
            aws_access_key_id=aws_access_key,
            aws_secret_access_key=aws_secret_key,
            region_name=aws_region,
            config=Config(signature_version="s3v4"),
        )
    return boto3.client(
        "s3",
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
        # Prune excluded directories and generated artifacts. Upload only original MER PDFs
        # and original call/video recordings; processed audio is derived locally.
        dirs[:] = [d for d in dirs if d not in exclude_dirs and d != "_processed"]

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


def refresh_presigned_urls(
    s3_client: boto3.client,
    manifest_path: Path,
    presign_expires: int = 604800,
):
    """Refresh presigned URLs in an existing manifest and update local pointer files.

    This does NOT upload any files again. It regenerates URLs based on each item's bucket/key
    and overwrites the local file (if present) with the fresh URL pointer.
    """
    if not manifest_path.exists():
        print(f"Manifest not found at {manifest_path}")
        return 0

    try:
        with open(manifest_path, "r", encoding="utf-8") as mf:
            manifest = json.load(mf)
    except Exception as e:
        print(f"ERROR: Failed to read manifest: {e}")
        return 0

    # Use S3 server time for presigning to avoid any local clock skew causing immediate expiry
    server_dt = _fetch_s3_server_time(manifest.get("bucket") or os.environ.get("S3_BUCKET_NAME", ""))
    if not server_dt:
        server_dt = datetime.now(timezone.utc)
    ymd = server_dt.strftime("%Y%m%d")
    amz_date = server_dt.strftime("%Y%m%dT%H%M%SZ")
    region = os.environ.get("AWS_REGION", "ap-south-1")
    access_key = os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_ACCESS_KEY")
    secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("AWS_SECRET_KEY")

    items = manifest.get("items", [])
    refreshed = 0
    for item in items:
        bucket = item.get("bucket") or manifest.get("bucket")
        key = item.get("key")
        local_path = item.get("local_path")
        if not bucket or not key:
            continue
        # Prefer manual SigV4 with server time to prevent expired links on hosts with skewed clocks
        url = None
        if access_key and secret_key:
            try:
                url = _manual_presign_s3_get(
                    bucket=bucket,
                    key=key,
                    region=region,
                    access_key=access_key,
                    secret_key=secret_key,
                    expires=presign_expires,
                    amz_date=amz_date,
                    yyyymmdd=ymd,
                )
            except Exception as e:
                print(f"ERROR: Manual presign failed for s3://{bucket}/{key}: {e}")
                url = None
        if not url:
            try:
                url = s3_client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": bucket, "Key": key},
                    ExpiresIn=presign_expires,
                )
            except (BotoCoreError, ClientError) as e:
                print(f"ERROR: Failed to generate URL for s3://{bucket}/{key}: {e}")
                url = None
        if not url:
            continue
        item["presigned_url"] = url
        refreshed += 1

        # Update local pointer file if it still exists
        try:
            if local_path:
                lp = WORKSPACE_ROOT / local_path
                if lp.exists():
                    with open(lp, "w", encoding="utf-8") as wf:
                        wf.write(url)
                        wf.write("\n")
        except Exception as e:
            print(f"WARN: Failed to update local pointer {local_path}: {e}")

    manifest["refreshed_at"] = datetime.now(timezone.utc).isoformat()
    try:
        with open(manifest_path, "w", encoding="utf-8") as mf:
            json.dump(manifest, mf, indent=2)
    except Exception as e:
        print(f"ERROR: Failed to write updated manifest: {e}")
        return refreshed

    print(f"Refreshed {refreshed} presigned URLs. Manifest updated: {manifest_path}")
    return refreshed


def _fetch_s3_server_time(bucket: str) -> datetime:
    """HEAD the bucket to get S3 Date header as a reliable server time source."""
    host = f"{bucket}.s3.amazonaws.com" if bucket else "s3.amazonaws.com"
    req = urllib.request.Request(f"https://{host}/", method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            date_hdr = resp.headers.get("Date")
    except Exception as e:
        # Sometimes S3 returns error but with headers
        date_hdr = getattr(e, "headers", None).get("Date") if getattr(e, "headers", None) else None
    if not date_hdr:
        return None
    try:
        return email.utils.parsedate_to_datetime(date_hdr).astimezone(timezone.utc)
    except Exception:
        return None


def _manual_presign_s3_get(*, bucket: str, key: str, region: str, access_key: str, secret_key: str, expires: int, amz_date: str, yyyymmdd: str) -> str:
    """Minimal SigV4 presign for S3 GET using server time to avoid clock skew issues."""
    service = "s3"
    credential_scope = f"{yyyymmdd}/{region}/{service}/aws4_request"
    host = f"{bucket}.s3.amazonaws.com"
    canonical_uri = "/" + quote(key, safe="/")
    params = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{access_key}/{credential_scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    canonical_query = "&".join([f"{quote(k, safe='~')}={quote(str(params[k]), safe='~')}" for k in sorted(params)])
    canonical_headers = f"host:{host}\n"
    signed_headers = "host"
    payload_hash = "UNSIGNED-PAYLOAD"
    canonical_request = "\n".join([
        "GET",
        canonical_uri,
        canonical_query,
        canonical_headers,
        signed_headers,
        payload_hash,
    ])
    import hashlib, hmac as _hmac
    cr_hash = hashlib.sha256(canonical_request.encode()).hexdigest()
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amz_date,
        credential_scope,
        cr_hash,
    ])
    def _sign(key_bytes, msg):
        return _hmac.new(key_bytes, msg.encode("utf-8"), hashlib.sha256).digest()
    k_secret = ("AWS4" + secret_key).encode("utf-8")
    k_date = _sign(k_secret, yyyymmdd)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    k_signing = _sign(k_service, "aws4_request")
    signature = _hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    final_query = canonical_query + "&X-Amz-Signature=" + signature
    return f"https://{host}{canonical_uri}?{final_query}"


def main():
    bucket = os.environ.get("S3_BUCKET_NAME", "anthropod")
    key_prefix = os.environ.get("S3_KEY_PREFIX") or os.environ.get("S3_FOLDER") or "temp/medibuddy"

    # Scope to current record inputs. Avoid uploading root-level old sample files.
    rr_dir = WORKSPACE_ROOT / "reports and recordings"
    scan_dirs = [rr_dir] if rr_dir.exists() else [WORKSPACE_ROOT]

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


def cli_main():
    parser = argparse.ArgumentParser(description="Upload media to S3 and manage presigned URLs")
    sub = parser.add_subparsers(dest="command")

    # Default upload command (no subcommand still supported)
    refresh = sub.add_parser("refresh", help="Refresh presigned URLs from existing s3_manifest.json")
    refresh.add_argument("--expires", type=int, default=604800, help="Expiry in seconds for presigned URLs (default 7 days)")

    # Back-compat flags (ignored by refresh)
    parser.add_argument("--expires", type=int, default=604800, help="Expiry in seconds for presigned URLs during upload (default 7 days)")

    args = parser.parse_args()

    if args.command == "refresh":
        s3 = build_s3_client()
        manifest_path = WORKSPACE_ROOT / "s3_manifest.json"
        refresh_presigned_urls(s3, manifest_path, presign_expires=args.expires)
        return 0

    # Default behavior: perform upload-and-replace and write manifest
    return main()


if __name__ == "__main__":
    sys.exit(cli_main())
