import os
import json
import math
from datetime import timedelta
from typing import Dict, Any, List, Optional, Tuple
from flask import Flask, jsonify, send_file, request, Response, render_template, render_template_string, redirect
import threading

import wave
import re
from pathlib import Path
import subprocess
import shlex
from datetime import datetime

# Remove complex processing imports - we'll use medb.py as subprocess


# -------- Configuration --------
TRANSCRIPT_PATH = os.environ.get(
    "TRANSCRIPT_PATH",
    "/Users/vishalsharma/Downloads/medibuddy/transcript.json",
)
QA_REPORT_PATH = os.environ.get(
    "QA_REPORT_PATH",
    "/Users/vishalsharma/Downloads/medibuddy/qa_report.json",
)
QA_REPORT_PART2_PATH = os.environ.get(
    "QA_REPORT_PART2_PATH",
    "/Users/vishalsharma/Downloads/medibuddy/qa_report_part2.json",
)
AUDIO_PATH = os.environ.get(
    "AUDIO_PATH",
    "/Users/vishalsharma/Downloads/medibuddy/medibuddy_sample_call_1 (2) (1).wav",
)
RECORDS_DIR = os.environ.get(
    "RECORDS_DIR",
    "/Users/vishalsharma/Downloads/medibuddy/reports and recordings",
)


def get_audio_duration_seconds(wav_path: str) -> Optional[float]:
    try:
        with wave.open(wav_path, 'rb') as w:
            frames = w.getnframes()
            rate = w.getframerate()
            if rate == 0:
                return None
            return frames / float(rate)
    except Exception:
        return None


def effective_duration_seconds(audio_path: str, transcript: Dict[str, Any]) -> Optional[float]:
    """Return audio duration in seconds, with transcript fallback when audio is unreadable.

    - Try reading container duration via wave
    - If unavailable, derive from the maximum end_timestamp in transcript segments
    """
    dur = get_audio_duration_seconds(audio_path)
    if dur and dur > 0:
        return dur
    max_end = 0.0
    try:
        for seg in (transcript.get('segments') or []):
            end = parse_mmss_to_seconds(seg.get('end_timestamp', ''))
            if end is not None:
                if end > max_end:
                    max_end = end
    except Exception:
        max_end = 0.0
    return max_end if max_end > 0 else None


# -------- Multi-record support --------

def _record_id_from_mer(path: Path) -> Optional[str]:
    name = path.name
    m = re.match(r"(.+?)_MER\.pdf$", name, flags=re.IGNORECASE)
    return m.group(1) if m else None


def _is_audio(p: Path) -> bool:
    return p.suffix.lower() in {".mp3", ".wav", ".m4a", ".webm", ".ogg", ".mp4"}


def scan_records() -> Dict[str, Dict[str, Any]]:
    base = Path(RECORDS_DIR)
    records: Dict[str, Dict[str, Any]] = {}
    if not base.exists():
        return records
    files = list(base.iterdir())
    # Index MERs
    for f in files:
        if f.is_file() and f.suffix.lower() == ".pdf" and f.name.lower().endswith("_mer.pdf"):
            rid = _record_id_from_mer(f)
            if rid:
                records.setdefault(rid, {"mer_pdf": str(f), "calls": []})
    # Attach audio/video
    for f in files:
        if f.is_file() and _is_audio(f):
            # match by prefix until first underscore or entire stem up to non-alnum
            for rid in list(records.keys()):
                if f.name.startswith(rid):
                    records[rid]["calls"].append({"path": str(f), "name": f.name})
    # Sort calls consistently
    for rid, rec in records.items():
        rec["calls"].sort(key=lambda x: x["name"])  # deterministic
        # Assign indices
        for i, c in enumerate(rec["calls"], start=1):
            c["index"] = i
    return records


def _processed_dir(record_id: str) -> Path:
    p = Path(RECORDS_DIR) / record_id / "_processed"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _log_step(record_id: str, message: str):
    try:
        ts = datetime.now().strftime('%H:%M:%S')
        line = f"[{ts}] {message}"
        print(line, flush=True)
        log_path = _processed_dir(record_id) / 'process.log'
        with open(log_path, 'a', encoding='utf-8') as lf:
            lf.write(line + "\n")
    except Exception:
        try:
            print(message, flush=True)
        except Exception:
            pass


def process_record(record_id: str, api_key: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
    """Process record by calling medb.py as subprocess"""
    recs = scan_records()
    rec = recs.get(record_id)
    if not rec:
        return {"error": "record_not_found", "id": record_id}

    _log_step(record_id, f"Starting processing via medb.py (force={force}) api_key_provided={'yes' if api_key else 'no'}")
    print(f"[proc] start record={record_id} force={force}", flush=True)

    # Create a temporary directory structure for the record
    record_dir = Path(RECORDS_DIR) / record_id
    record_dir.mkdir(parents=True, exist_ok=True)
    
    # Find the MER file for this record
    mer_file = None
    for f in Path(RECORDS_DIR).glob(f"{record_id}_MER.pdf"):
        mer_file = f
        break
    
    if not mer_file:
        return {"error": "mer_not_found", "id": record_id}
    
    # Copy MER to record directory if not already there
    record_mer = record_dir / f"{record_id}_MER.pdf"
    if not record_mer.exists():
        import shutil
        shutil.copy2(mer_file, record_mer)
    
    # Copy media files to record directory if not already there
    for call in rec.get("calls", []):
        media_path = Path(call["path"])
        record_media = record_dir / media_path.name
        if not record_media.exists():
            import shutil
            shutil.copy2(media_path, record_media)
    
    # Call medb.py as subprocess
    try:
        cmd = [
            "python3", "medb.py",
            "--record-dir", str(record_dir),
            "--api-key", api_key or os.environ.get("GEMINI_API_KEY", "")
        ]
        
        _log_step(record_id, f"Calling medb.py: {' '.join(cmd[:-2])} --api-key [HIDDEN]")
        
        # Run medb.py and capture output
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=Path(__file__).parent,
            bufsize=1,
            universal_newlines=True
        )
        
        # Stream output to log
        while True:
            output = process.stdout.readline()
            if output == '' and process.poll() is not None:
                break
            if output:
                # Forward medb.py output to our log
                _log_step(record_id, f"medb.py: {output.strip()}")
        
        return_code = process.poll()
        
        if return_code == 0:
            _log_step(record_id, "medb.py completed successfully")
            
            # Read the processing summary
            summary_path = record_dir / "_processed" / "processing_summary.json"
            if summary_path.exists():
                with open(summary_path) as f:
                    results = json.load(f)
                    results["status"] = "completed"
                    return results
            else:
                return {"id": record_id, "status": "completed", "message": "Processing completed but summary not found"}
        else:
            _log_step(record_id, f"medb.py failed with return code {return_code}")
            return {"error": "processing_failed", "id": record_id, "return_code": return_code}
            
    except Exception as e:
        _log_step(record_id, f"Failed to run medb.py: {e}")
        return {"error": "subprocess_failed", "id": record_id, "message": str(e)}

    mer_pdf = rec.get("mer_pdf")
    mer_md = ""
    if extract_pdf_to_markdown and mer_pdf and os.path.exists(mer_pdf):
        try:
            mer_md = extract_pdf_to_markdown(mer_pdf)
            _log_step(record_id, f"MER extracted: {len(mer_md)} chars from {mer_pdf}")
            log_progress("MER extraction completed")
        except Exception:
            mer_md = ""
            _log_step(record_id, f"MER extraction failed for {mer_pdf}")
            log_progress("MER extraction failed")

    out_root = _processed_dir(record_id)
    results = {"id": record_id, "calls": []}
    call_meta: List[Dict[str, Any]] = []
    _log_step(record_id, f"Found {len(rec.get('calls', []))} call(s)")
    print(f"[proc] calls={len(rec.get('calls', []))}", flush=True)

    # Pre-pass: transcribe all calls in parallel with 5-min chunking
    pre_transcribed: set = set()
    def _pre_worker(call_item: Dict[str, Any]):
        try:
            idx = call_item["index"]
            audio_path = call_item["path"]
            call_dir = out_root / f"call{idx}"
            call_dir.mkdir(parents=True, exist_ok=True)
            _log_step(record_id, f"Call {idx}: source={audio_path}")
            print(f"[pre] call={idx} src={audio_path}", flush=True)
            log_progress(f"Starting Call {idx} processing")
            src_for_transcription = audio_path
            if str(audio_path).lower().endswith('.mp4'):
                try:
                    _log_step(record_id, f"Call {idx}: MP4 detected, extracting audio...")
                    print(f"[pre] call={idx} MP4 detected, extracting audio...", flush=True)
                    mp3_out = call_dir / 'audio.mp3'
                    if force or (not mp3_out.exists()):
                        _log_step(record_id, f"Call {idx}: Running ffmpeg extraction...")
                        print(f"[pre] call={idx} Running ffmpeg extraction...", flush=True)
                        cmd = f"ffmpeg -y -i {shlex.quote(audio_path)} -vn -acodec libmp3lame -q:a 2 {shlex.quote(str(mp3_out))}"
                        subprocess.run(cmd, shell=True, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
                        _log_step(record_id, f"Call {idx}: ffmpeg extraction completed")
                        print(f"[pre] call={idx} ffmpeg extraction completed", flush=True)
                    if mp3_out.exists():
                        src_for_transcription = str(mp3_out)
                        _log_step(record_id, f"Call {idx}: extracted audio -> {mp3_out}")
                        print(f"[pre] call={idx} extracted audio -> {mp3_out}", flush=True)
                except Exception as e:
                    _log_step(record_id, f"Call {idx}: audio extraction failed (prepass): {e}")
                    print(f"[pre] call={idx}: audio extraction failed: {e}", flush=True)
            transcript_path = call_dir / "transcript.json"
            _log_step(record_id, f"Call {idx}: Checking duration of {src_for_transcription}")
            print(f"[pre] call={idx} Checking duration of {src_for_transcription}", flush=True)
            dur = media_duration_seconds(src_for_transcription) or 0.0
            _log_step(record_id, f"Call {idx}: Duration detected: {dur}s")
            print(f"[pre] call={idx} Duration detected: {dur}s", flush=True)
            if not dur:
                _log_step(record_id, f"Call {idx}: duration unknown (ffprobe failed); defaulting to chunking")
                print(f"[pre] call={idx} duration=unknown -> chunking", flush=True)
            treat_as_long = (dur == 0.0) or (dur > 300)
            if treat_as_long:
                _log_step(record_id, f"Call {idx}: duration {int(dur)}s > 300s, splitting into 5-min chunks")
                chunks_dir = call_dir / 'chunks'
                chunk_paths = split_audio_into_chunks(src_for_transcription, chunks_dir, chunk_seconds=300)
                print(f"[pre] call={idx} chunks={len(chunk_paths)}", flush=True)
                for ci, cp in enumerate(chunk_paths, start=1):
                    try:
                        cdur = media_duration_seconds(cp)
                    except Exception:
                        cdur = None
                    _log_step(record_id, f"Call {idx}: chunk {ci}/{len(chunk_paths)} ready -> {cp} dur={int(cdur or 0)}s")
                _log_step(record_id, f"Call {idx}: {len(chunk_paths)} chunk(s) ready")
                pieces = transcribe_in_parallel(chunk_paths, api_key, record_id, idx)
                merged_t = {"segments": []}
                offset = 0.0
                for cp, segs_obj in zip(chunk_paths, pieces):
                    segs = (segs_obj or {}).get('segments') or []
                    cd = media_duration_seconds(cp) or 0.0
                    for s in segs:
                        st = parse_mmss_to_seconds(s.get('start_timestamp','') or '') or 0.0
                        en = parse_mmss_to_seconds(s.get('end_timestamp','') or '') or st
                        merged_t['segments'].append({
                            'segment_id': s.get('segment_id',''),
                            'text': s.get('text',''),
                            'speaker': s.get('speaker',''),
                            'start_timestamp': seconds_to_mmss(st + offset),
                            'end_timestamp': seconds_to_mmss(en + offset),
                        })
                    offset += cd
                with open(transcript_path, 'w') as f:
                    json.dump(merged_t, f)
                _log_step(record_id, f"Call {idx}: transcript saved -> {transcript_path}")
                print(f"[pre] call={idx} transcript saved segs={len(merged_t['segments'])}", flush=True)
            else:
                _log_step(record_id, f"Call {idx}: transcribing (no split)")
                tdict = _transcribe_one(src_for_transcription, api_key)
                with open(transcript_path, 'w') as f:
                    json.dump(tdict, f)
                _log_step(record_id, f"Call {idx}: transcript saved -> {transcript_path}")
                print(f"[pre] call={idx} transcript saved (no split) segs={len((tdict or {}).get('segments', []) or [])}", flush=True)
            pre_transcribed.add(idx)
        except Exception as e:
            _log_step(record_id, f"Call {call_item.get('index','?')}: pre-transcription error: {e}")
            print(f"[pre] call={call_item.get('index','?')} ERROR {e}", flush=True)

    _threads: List[threading.Thread] = []
    for _call in rec.get("calls", []):
        t = threading.Thread(target=_pre_worker, args=(_call,), daemon=True)
        _threads.append(t)
        t.start()
    for t in _threads:
        t.join()
    print("[pre] all pre-transcriptions finished", flush=True)
    log_progress("Pre-transcription completed")

    # Continue per-call analysis
    for call in rec.get("calls", []):
        idx = call["index"]
        audio_path = call["path"]
        call_dir = out_root / f"call{idx}"
        call_dir.mkdir(parents=True, exist_ok=True)
        _log_step(record_id, f"Call {idx}: source={audio_path}")
        # If source is a video, extract audio track to MP3 for faster/more reliable transcription
        src_for_transcription = audio_path
        if str(audio_path).lower().endswith('.mp4'):
            try:
                mp3_out = call_dir / 'audio.mp3'
                if force or (not mp3_out.exists()):
                    cmd = f"ffmpeg -y -i {shlex.quote(audio_path)} -vn -acodec libmp3lame -q:a 2 {shlex.quote(str(mp3_out))}"
                    subprocess.run(cmd, shell=True, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=600)
                if mp3_out.exists():
                    src_for_transcription = str(mp3_out)
                    _log_step(record_id, f"Call {idx}: extracted audio -> {mp3_out}")
            except Exception:
                _log_step(record_id, f"Call {idx}: audio extraction failed")

        # Transcribe
        transcript_path = call_dir / "transcript.json"
        if transcribe_audio and (((not transcript_path.exists())) or (force and (idx not in pre_transcribed))):
            try:
                # Decide chunking if duration unknown or > 5 minutes
                dur = media_duration_seconds(src_for_transcription) or 0.0
                if not dur:
                    _log_step(record_id, f"Call {idx}: duration unknown (ffprobe failed); defaulting to chunking")
                treat_as_long = (dur == 0.0) or (dur > 300)
                if treat_as_long:
                    _log_step(record_id, f"Call {idx}: duration {int(dur)}s > 300s, splitting into 5-min chunks")
                    chunks_dir = call_dir / 'chunks'
                    chunk_paths = split_audio_into_chunks(src_for_transcription, chunks_dir, chunk_seconds=300)
                    for ci, cp in enumerate(chunk_paths, start=1):
                        try:
                            cdur = media_duration_seconds(cp)
                        except Exception:
                            cdur = None
                        _log_step(record_id, f"Call {idx}: chunk {ci}/{len(chunk_paths)} ready -> {cp} dur={int(cdur or 0)}s")
                    _log_step(record_id, f"Call {idx}: {len(chunk_paths)} chunk(s) ready")
                    pieces = transcribe_in_parallel(chunk_paths, api_key, record_id, idx)
                    # Merge chunk transcripts and adjust timestamps with offsets
                    merged_t = {"segments": []}
                    offset = 0.0
                    for pi, (cp, segs_obj) in enumerate(zip(chunk_paths, pieces)):
                        segs = (segs_obj or {}).get('segments') or []
                        # Probe each chunk duration for offset
                        cd = media_duration_seconds(cp) or 0.0
                        for s in segs:
                            st = parse_mmss_to_seconds(s.get('start_timestamp','') or '') or 0.0
                            en = parse_mmss_to_seconds(s.get('end_timestamp','') or '') or st
                            merged_t['segments'].append({
                                'segment_id': s.get('segment_id',''),
                                'text': s.get('text',''),
                                'speaker': s.get('speaker',''),
                                'start_timestamp': seconds_to_mmss(st + offset),
                                'end_timestamp': seconds_to_mmss(en + offset),
                            })
                        offset += cd
                    tdict = merged_t
                else:
                    _log_step(record_id, f"Call {idx}: transcribing (no split)")
                    tdict = transcribe_audio(src_for_transcription, api_key or os.environ.get("GEMINI_API_KEY", ""))

                if save_transcript:
                    save_transcript(tdict, str(transcript_path))
                else:
                    with open(transcript_path, 'w') as f:
                        json.dump(tdict, f)
                _log_step(record_id, f"Call {idx}: transcript saved -> {transcript_path}")
                log_progress(f"Call {idx} transcription completed")
            except Exception as e:
                _log_step(record_id, f"Call {idx}: transcription failed: {e}")
                log_progress(f"Call {idx} transcription failed")

        # Load transcript (robust: supports code-fenced JSON inside raw_text)
        try:
            tdict = load_transcript_from_path(transcript_path)
        except Exception:
            tdict = {"segments": []}
        _log_step(record_id, f"Call {idx}: transcript segments={len(tdict.get('segments') or [])}")

        # Optional audio technical analysis (audibility)
        try:
            audibility = analyze_audibility(audio_path)
            _log_step(record_id, f"Call {idx}: audibility level={audibility.get('audibility_level')} dBFS={audibility.get('avg_dbfs')}")
        except Exception:
            audibility = {"recording_exists": os.path.exists(audio_path), "audibility_level": "unknown", "avg_dbfs": None}
            _log_step(record_id, f"Call {idx}: audibility analysis failed")

        # Optional video frame extraction and simple tagging (if video file)
        video_info = {}
        if str(audio_path).lower().endswith('.mp4'):
            try:
                shots = extract_video_screenshots(audio_path, call_dir)
            except Exception:
                shots = []
            video_info = {"screenshots": [str(p) for p in shots]}
            _log_step(record_id, f"Call {idx}: screenshots={len(shots)}")

        # QA report
        qa_path = call_dir / "qa_report.json"
        if analyze_qa and (force or (not qa_path.exists())):
            try:
                _log_step(record_id, f"Call {idx}: QA analysis")
                qdict = analyze_qa(tdict, mer_md, api_key or os.environ.get("GEMINI_API_KEY", ""))
                # Merge technical_status and video_analysis scaffolds
                qdict.setdefault('technical_status', {})
                qdict['technical_status'].update({
                    'recording_exists': bool(audibility.get('recording_exists')),
                    'audibility_level': audibility.get('audibility_level') or 'unknown',
                    'avg_dbfs': audibility.get('avg_dbfs')
                })
                if video_info:
                    qdict.setdefault('video_analysis', {})
                    qdict['video_analysis'].update({
                        'screenshots': video_info.get('screenshots', [])
                    })
                if save_qa_report:
                    save_qa_report(qdict, str(qa_path))
                else:
                    with open(qa_path, 'w') as f:
                        json.dump(qdict, f)
                _log_step(record_id, f"Call {idx}: qa_report.json saved")
                log_progress(f"Call {idx} QA completed")
            except Exception:
                _log_step(record_id, f"Call {idx}: QA analysis failed")
                log_progress(f"Call {idx} QA failed")
        # Always upsert technical_status and video_analysis into qa_report.json, and ensure placeholder sections exist
        try:
            existing = {}
            if qa_path.exists():
                with open(qa_path) as f:
                    existing = json.load(f)
            existing.setdefault('technical_status', {})
            existing['technical_status'].update({
                'recording_exists': bool(audibility.get('recording_exists')),
                'audibility_level': audibility.get('audibility_level') or 'unknown',
                'avg_dbfs': audibility.get('avg_dbfs')
            })
            if video_info:
                va = existing.setdefault('video_analysis', {})
                if isinstance(va, dict):
                    va.setdefault('screenshots', [])
                    # merge unique
                    old = set(va.get('screenshots') or [])
                    for pth in video_info.get('screenshots', []):
                        if pth not in old:
                            va['screenshots'].append(pth)
            # Ensure placeholder keys for new sections to satisfy output structure
            va = existing.setdefault('video_analysis', {})
            if isinstance(va, dict):
                va.setdefault('attire_check', 'unknown')
                va.setdefault('visibility_status', 'unknown')
                va.setdefault('privacy_maintained', None)
                va.setdefault('screenshots', [])
            bf = existing.setdefault('behavioral_flags', {})
            if isinstance(bf, dict):
                bf.setdefault('prompting_detected', {"value": None, "timestamps": [], "examples": []})
                bf.setdefault('customer_hesitation', {"value": None, "timestamps": [], "examples": []})
            dq = existing.setdefault('documentation_quality', {})
            if isinstance(dq, dict):
                dq.setdefault('spelling_errors_count', None)
                dq.setdefault('typos_found', [])
                dq.setdefault('notes', 'not evaluated')
            with open(qa_path, 'w') as f:
                json.dump(existing, f, indent=2)
            _log_step(record_id, f"Call {idx}: qa_report.json enriched (tech/video placeholders)")
        except Exception:
            _log_step(record_id, f"Call {idx}: enrichment failed")
        # QC part 2
        qc2_path = call_dir / "qa_report_part2.json"
        if analyze_qc_part2 and (force or (not qc2_path.exists())):
            try:
                _log_step(record_id, f"Call {idx}: QC Part2 analysis")
                qc2 = analyze_qc_part2(tdict, api_key or os.environ.get("GEMINI_API_KEY", ""))
                if save_qc_part2:
                    save_qc_part2(qc2, str(qc2_path))
                else:
                    with open(qc2_path, 'w') as f:
                        json.dump(qc2, f)
                _log_step(record_id, f"Call {idx}: qa_report_part2.json saved")
                log_progress(f"Call {idx} QC completed")
            except Exception:
                _log_step(record_id, f"Call {idx}: QC Part2 failed")
                log_progress(f"Call {idx} QC failed")

        # Compute duration via transcript max end for merged offsets
        try:
            max_end = 0.0
            for seg in (tdict.get('segments') or []):
                e = parse_mmss_to_seconds(seg.get('end_timestamp','') or '')
                if e is not None and e > max_end:
                    max_end = e
        except Exception:
            max_end = 0.0

        call_meta.append({
            "index": idx,
            "audio_path": audio_path,
            "call_dir": str(call_dir),
            "transcript_path": str(transcript_path),
            "transcript": tdict,
            "duration_sec": max_end,
            "qa_path": str(qa_path),
            "qc_path": str(qc2_path),
            "audibility": audibility,
            "video_info": video_info,
        })

        results["calls"].append({"index": idx, "transcript": str(transcript_path), "qa_report": str(qa_path), "qc_report": str(qc2_path)})

    # Merged LLM runs across all calls
    try:
        _log_step(record_id, "Building merged transcript")
        merged = {"segments": []}
        offset = 0.0
        for c in sorted(call_meta, key=lambda x: x["index"]):
            segs = (c.get("transcript") or {}).get("segments") or []
            for s in segs:
                try:
                    st = parse_mmss_to_seconds(s.get('start_timestamp','') or '') or 0.0
                    en = parse_mmss_to_seconds(s.get('end_timestamp','') or '') or st
                except Exception:
                    st, en = 0.0, 0.0
                st2 = max(0.0, st + offset)
                en2 = max(st2, en + offset)
                merged["segments"].append({
                    "segment_id": s.get("segment_id", ""),
                    "text": s.get("text", ""),
                    "speaker": s.get("speaker", ""),
                    "start_timestamp": seconds_to_mmss(st2),
                    "end_timestamp": seconds_to_mmss(en2),
                })
            offset += max(0.0, float(c.get("duration_sec") or 0.0))

        merged_dir = out_root
        merged_tr_path = merged_dir / "merged_transcript.json"
        with open(merged_tr_path, 'w') as mf:
            json.dump(merged, mf, indent=2)
        _log_step(record_id, f"Merged transcript saved -> {merged_tr_path} (segments={len(merged.get('segments') or [])})")
        log_progress("Merged transcription completed")

        # Choose largest call for tech/video
        largest = None
        for c in call_meta:
            if largest is None or float(c.get("duration_sec") or 0.0) > float(largest.get("duration_sec") or 0.0):
                largest = c

        merged_qa_path = merged_dir / "merged_qa_report.json"
        if analyze_qa and (force or (not merged_qa_path.exists())):
            try:
                _log_step(record_id, "Merged QA analysis")
                qdict = analyze_qa(merged, mer_md, api_key or os.environ.get("GEMINI_API_KEY", ""))
                la = (largest or {}).get("audibility") or {}
                qdict.setdefault('technical_status', {})
                qdict['technical_status'].update({
                    'recording_exists': bool(la.get('recording_exists')),
                    'audibility_level': la.get('audibility_level') or 'unknown',
                    'avg_dbfs': la.get('avg_dbfs')
                })
                lv = (largest or {}).get("video_info") or {}
                if lv:
                    qdict.setdefault('video_analysis', {})
                    qdict['video_analysis'].update({'screenshots': lv.get('screenshots', [])})
                with open(merged_qa_path, 'w') as f:
                    json.dump(qdict, f, indent=2)
                _log_step(record_id, f"Merged qa_report saved -> {merged_qa_path}")
                log_progress("Merged QA completed")
            except Exception:
                _log_step(record_id, "Merged QA failed")
                log_progress("Merged QA failed")

        merged_qc2_path = merged_dir / "merged_qa_report_part2.json"
        if analyze_qc_part2 and (force or (not merged_qc2_path.exists())):
            try:
                _log_step(record_id, "Merged QC Part2 analysis")
                qc2 = analyze_qc_part2(merged, api_key or os.environ.get("GEMINI_API_KEY", ""))
                with open(merged_qc2_path, 'w') as f:
                    json.dump(qc2, f, indent=2)
                _log_step(record_id, f"Merged qa_report_part2 saved -> {merged_qc2_path}")
                log_progress("Merged analysis completed")
            except Exception:
                _log_step(record_id, "Merged QC Part2 failed")
                log_progress("Merged analysis failed")

        results["merged"] = {"transcript": str(merged_tr_path), "qa_report": str(merged_qa_path), "qc_report": str(merged_qc2_path)}
    except Exception:
        _log_step(record_id, "Merged pipeline failed")
        log_progress("Merged pipeline failed")

    return results


# Removed complex processing functions - using medb.py subprocess instead

def analyze_audibility(path: str) -> Dict[str, Any]:
    """Rudimentary audibility via ffmpeg loudnorm to get input_i (dBFS)."""
    if not os.path.exists(path):
        return {"recording_exists": False, "audibility_level": "not_audible", "avg_dbfs": None}
    try:
        # Use ffmpeg to compute integrated loudness; suppress output
        cmd = f"ffmpeg -i {shlex.quote(path)} -filter_complex loudnorm=I=-23:TP=-1.5:LRA=11:print_format=json -f null -"
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
        text = (proc.stderr or '') + '\n' + (proc.stdout or '')
        # Accept quoted numeric strings
        m = re.search(r'\{[\s\S]*?\"input_i\"\s*:\s*\"?([\-0-9\.]+)\"?', text)
        val = float(m.group(1)) if m else None
        # Fallback: use volumedetect mean_volume if loudnorm parse failed
        if val is None:
            cmd2 = f"ffmpeg -i {shlex.quote(path)} -af volumedetect -f null -"
            p2 = subprocess.run(cmd2, shell=True, capture_output=True, text=True, timeout=60)
            t2 = (p2.stderr or '') + '\n' + (p2.stdout or '')
            m2 = re.search(r'mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB', t2)
            if m2:
                val = float(m2.group(1))
        level = 'audible'
        if val is None:
            level = 'unknown'
        elif val < -40:
            level = 'not_audible'
        return {"recording_exists": True, "audibility_level": level, "avg_dbfs": val}
    except Exception:
        return {"recording_exists": True, "audibility_level": "unknown", "avg_dbfs": None}


def extract_video_screenshots(video_path: str, out_dir: Path) -> List[str]:
    """Extract up to 3 screenshots at ~2-3 minute intervals using ffmpeg."""
    shots: List[str] = []
    try:
        # Probe duration via ffprobe
        cmd_dur = f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 {shlex.quote(video_path)}"
        dur_out = subprocess.check_output(cmd_dur, shell=True, text=True, timeout=30).strip()
        dur = float(dur_out) if dur_out else 0.0
        if dur <= 0:
            return shots
        # Choose 3 timestamps across duration
        ts = [max(1, int(dur * p)) for p in (0.2, 0.5, 0.8)]
        for i, sec in enumerate(ts, start=1):
            out_path = out_dir / f"frame_{i:02d}.jpg"
            cmd = f"ffmpeg -ss {sec} -i {shlex.quote(video_path)} -frames:v 1 -q:v 2 {shlex.quote(str(out_path))} -y"
            subprocess.run(cmd, shell=True, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if out_path.exists():
                shots.append(str(out_path))
    except Exception:
        return shots
    return shots


def load_json_safe(path: Path) -> Dict[str, Any]:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def load_transcript_from_path(path: Path) -> Dict[str, Any]:
    """Load a transcript JSON and normalize shape to {segments:[...]},
    supporting the code-fenced JSON stored under raw_text."""
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        return {"segments": []}
    if isinstance(data, dict) and data.get('segments'):
        return data
    if isinstance(data, dict) and isinstance(data.get('raw_text'), str):
        parsed = parse_codefenced_json(data['raw_text'])
        if isinstance(parsed, dict) and parsed.get('segments'):
            return parsed
    return {"segments": []}


def read_json_file(file_path: str) -> Dict[str, Any]:
    if not os.path.exists(file_path):
        return {}
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def parse_codefenced_json(text: str) -> Optional[Dict[str, Any]]:
    """Extract JSON from strings like ```json\n{...}\n``` or ```\n{...}\n```"""
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if cleaned.startswith('```') and cleaned.endswith('```'):
        cleaned = cleaned[3:-3].strip()
        # Remove optional language hint
        if cleaned.lower().startswith('json'):
            cleaned = cleaned[4:].lstrip('\n').lstrip()
    try:
        return json.loads(cleaned)
    except Exception:
        return None


def load_transcript() -> Dict[str, Any]:
    data = read_json_file(TRANSCRIPT_PATH)
    # If already in expected shape
    if isinstance(data, dict) and data.get('segments'):
        return data
    # If raw_text holding JSON in codefence
    if isinstance(data, dict) and isinstance(data.get('raw_text'), str):
        parsed = parse_codefenced_json(data['raw_text'])
        if isinstance(parsed, dict) and parsed.get('segments'):
            return parsed
    # Fallback empty structure
    return {"segments": []}


def parse_mmss_to_seconds(ts: str) -> Optional[float]:
    try:
        parts = ts.split(':')
        if len(parts) != 2:
            return None
        minutes = int(parts[0])
        seconds = int(parts[1])
        return minutes * 60 + seconds
    except Exception:
        return None


def seconds_to_mmss(total_seconds: float) -> str:
    try:
        s = int(round(total_seconds))
        m = s // 60
        r = s % 60
        return f"{m}:{r:02d}"
    except Exception:
        return "0:00"


def media_duration_seconds(path: str) -> Optional[float]:
    """Probe media duration using ffprobe for any container/codec."""
    try:
        cmd = f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 {shlex.quote(path)}"
        out = subprocess.check_output(cmd, shell=True, text=True, timeout=30).strip()
        return float(out) if out else None
    except Exception:
        return None


def split_audio_into_chunks(src_path: str, out_dir: Path, chunk_seconds: int = 600) -> List[str]:
    """Split audio into ~chunk_seconds sized mp3 chunks. Returns list of file paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # Clean previous chunks
    try:
        for f in out_dir.glob('chunk_*.mp3'):
            try: f.unlink()
            except Exception: pass
    except Exception:
        pass
    try:
        cmd = (
            f"ffmpeg -y -hide_banner -loglevel error -i {shlex.quote(src_path)} "
            f"-vn -acodec libmp3lame -q:a 2 -f segment -segment_time {int(chunk_seconds)} "
            f"{shlex.quote(str(out_dir / 'chunk_%03d.mp3'))}"
        )
        subprocess.run(cmd, shell=True, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1200)
    except Exception:
        pass
    return [str(p) for p in sorted(out_dir.glob('chunk_*.mp3'))]


def _transcribe_one(path: str, api_key: Optional[str]) -> Dict[str, Any]:
    if not transcribe_audio:
        return {"segments": []}
    try:
        _log_step("transcribe_one", f"START transcription: {path}")
        import time as _time
        import concurrent.futures as _cf
        t0 = _time.monotonic()
        # Run the model call with a hard timeout to avoid indefinite hangs
        try:
            with _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="tx-one") as _exe:
                _future = _exe.submit(transcribe_audio, path, api_key or os.environ.get("GEMINI_API_KEY", ""))
                result = _future.result(timeout=240.0)
        except _cf.TimeoutError:
            dur = _time.monotonic() - t0
            _log_step("transcribe_one", f"TIMEOUT transcription: {path} after {dur:.1f}s")
            return {"segments": []}
        # Handle case where Gemini returns raw_text instead of parsed JSON
        if isinstance(result, dict) and "raw_text" in result and "segments" not in result:
            import json
            import re
            raw_text = result["raw_text"]
            json_match = re.search(r'```json\s*(\{.*?\})\s*```', raw_text, re.DOTALL)
            if json_match:
                try:
                    parsed = json.loads(json_match.group(1))
                    dur = _time.monotonic() - t0
                    _log_step("transcribe_one", f"DONE transcription (codefence JSON): {path} in {dur:.1f}s segs={len(parsed.get('segments', []) or [])}")
                    return parsed
                except json.JSONDecodeError:
                    pass
            json_match = re.search(r'(\{.*"segments".*\})', raw_text, re.DOTALL)
            if json_match:
                try:
                    parsed = json.loads(json_match.group(1))
                    dur = _time.monotonic() - t0
                    _log_step("transcribe_one", f"DONE transcription (inline JSON): {path} in {dur:.1f}s segs={len(parsed.get('segments', []) or [])}")
                    return parsed
                except json.JSONDecodeError:
                    pass
        dur = _time.monotonic() - t0
        segs_len = 0
        try:
            if isinstance(result, dict):
                segs_len = len(result.get('segments', []) or [])
        except Exception:
            segs_len = 0
        _log_step("transcribe_one", f"DONE transcription: {path} in {dur:.1f}s segs={segs_len}")
        return result
    except Exception as e:
        _log_step("transcribe_one", f"Transcription failed: {e}")
        return {"segments": []}


def transcribe_in_parallel(paths: List[str], api_key: Optional[str], record_id: str, call_idx: int) -> List[Dict[str, Any]]:
    """Transcribe multiple audio chunk paths in parallel; keep order of paths with per-chunk timeouts."""
    import concurrent.futures
    import time as _time
    if not paths:
        return []
    per_chunk_timeout = 240.0
    max_workers = min(len(paths), 8)
    _log_step(record_id, f"Call {call_idx}: starting parallel transcription for {len(paths)} chunk(s) with up to {max_workers} workers")
    results: List[Optional[Dict[str, Any]]] = [None] * len(paths)
    start_times: List[float] = [0.0] * len(paths)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"call{call_idx}-tx") as exe:
        future_by_index: Dict[int, concurrent.futures.Future] = {}
        for i, p in enumerate(paths):
            _log_step(record_id, f"Call {call_idx}: QUEUE chunk {i+1}/{len(paths)} -> {p}")
            start_times[i] = _time.monotonic()
            future_by_index[i] = exe.submit(_transcribe_one, p, api_key)
        for i in range(len(paths)):
            try:
                res = future_by_index[i].result(timeout=per_chunk_timeout)
                elapsed = _time.monotonic() - start_times[i]
                segs_len = len((res or {}).get('segments', []) or []) if isinstance(res, dict) else 0
                _log_step(record_id, f"Call {call_idx}: DONE chunk {i+1}/{len(paths)} in {elapsed:.1f}s segs={segs_len}")
                results[i] = res
            except concurrent.futures.TimeoutError:
                _log_step(record_id, f"Call {call_idx}: TIMEOUT chunk {i+1}/{len(paths)} after {per_chunk_timeout:.0f}s; skipping")
                results[i] = {"segments": []}
            except Exception as e:
                _log_step(record_id, f"Call {call_idx}: ERROR chunk {i+1}/{len(paths)} -> {e}")
                results[i] = {"segments": []}
    _log_step(record_id, f"Call {call_idx}: all chunk transcriptions finished")
    return [r or {"segments": []} for r in results]


def compute_speaker_distribution(transcript: Dict[str, Any], total_duration: Optional[float]) -> Dict[str, Any]:
    segments: List[Dict[str, Any]] = transcript.get('segments', []) or []
    speaker_to_secs: Dict[str, float] = {"agent": 0.0, "customer": 0.0}
    covered_intervals: List[Tuple[float, float]] = []

    for seg in segments:
        start = parse_mmss_to_seconds(seg.get('start_timestamp', ''))
        end = parse_mmss_to_seconds(seg.get('end_timestamp', ''))
        spk_raw = str(seg.get('speaker', '')).lower()
        # Normalize speaker aliases: treat "doctor" the same as legacy "agent"
        spk = 'agent' if spk_raw == 'doctor' else spk_raw
        if start is None or end is None or end <= start:
            continue
        duration = max(0.0, end - start)
        if spk in speaker_to_secs:
            speaker_to_secs[spk] += duration
        covered_intervals.append((start, end))

    # Estimate dead air as gaps in union of intervals if total_duration known
    dead_air = 0.0
    if total_duration and covered_intervals:
        covered_intervals.sort(key=lambda x: x[0])
        merged: List[Tuple[float, float]] = []
        cur_start, cur_end = covered_intervals[0]
        for s, e in covered_intervals[1:]:
            if s <= cur_end:
                cur_end = max(cur_end, e)
            else:
                merged.append((cur_start, cur_end))
                cur_start, cur_end = s, e
        merged.append((cur_start, cur_end))
        spoken_total = sum(e - s for s, e in merged)
        dead_air = max(0.0, float(total_duration) - spoken_total)

    total_parts = sum(speaker_to_secs.values()) + (dead_air if total_duration else 0.0)
    def pct(x: float) -> float:
        return round((x / total_parts) * 100.0, 2) if total_parts > 0 else 0.0

    return {
        "agent_seconds": round(speaker_to_secs["agent"], 2),
        "customer_seconds": round(speaker_to_secs["customer"], 2),
        "dead_air_seconds": round(dead_air, 2),
        "agent_pct": pct(speaker_to_secs["agent"]),
        "customer_pct": pct(speaker_to_secs["customer"]),
        "dead_air_pct": pct(dead_air if total_duration else 0.0),
    }


def compute_qc_score(qa_report: Dict[str, Any], qc2: Dict[str, Any], duration_sec: Optional[float]) -> Dict[str, Any]:
    def pct_to_score(p: float) -> int:
        # Only full marks at 100%; otherwise banded as per guide
        if p >= 100: return 100
        if p >= 95: return 80
        if p >= 85: return 60
        if p >= 70: return 40
        return 20

    def graded(val: str) -> int:
        v = (val or '').strip().lower()
        if v == 'yes': return 100
        if v == 'partial': return 50
        return 0

    def binary(val: str) -> int:
        return 100 if (val or '').strip().lower() == 'yes' else 0

    def contextual(val: str) -> int:
        v = (val or '').strip().lower()
        return 100 if v in ('yes', 'na') else 0

    qc = (qc2 or {}).get('qc_parameters', {})

    # QA-derived percentages using computed UI summary
    summary = compute_ui_summary(qa_report)
    complete_mer_pct = (summary['questions_asked'] / summary['total_questions'] * 100.0) if summary['total_questions'] else 0.0
    correct_doc_pct = summary['overall_compliance_score'] or 0.0

    dur_min = (float(duration_sec) / 60.0) if duration_sec else 0.0
    if dur_min >= 10:
        duration_score = 100
    elif dur_min >= 7:
        duration_score = 70
    else:
        duration_score = 30

    # Rate of speech score derived from WPM (prefer value embedded in qa_report)
    doctor_wpm = None
    try:
        doctor_wpm = float((qa_report or {}).get('meta', {}).get('doctor_wpm'))
    except Exception:
        doctor_wpm = None

    def ros_score(wpm: Optional[float]) -> int:
        # Scoring per guide:
        # 120-160 => 100; 100-119 or 161-180 => 70; 80-99 or 181-200 => 30; <80 or >200 => 0; 0/null => 50
        if wpm is None or wpm == 0:
            return 50
        if 120 <= wpm <= 160:
            return 100
        if (100 <= wpm <= 119) or (161 <= wpm <= 180):
            return 70
        if (80 <= wpm <= 99) or (181 <= wpm <= 200):
            return 30
        if wpm < 80 or wpm > 200:
            return 0
        return 0
    rate_of_speech_score = ros_score(doctor_wpm)

    scores = {
        'greetings': binary(qc.get('greetings', {}).get('value')),
        'call_opening': graded(qc.get('call_opening', {}).get('value')),
        'language_preference': binary(qc.get('language_preference', {}).get('value')),
        'id_validation': binary(qc.get('id_validation', {}).get('value')),
        'disclaimer': binary(qc.get('disclaimer', {}).get('value')),
        'politeness': graded(qc.get('politeness', {}).get('value')),
        'empathy': contextual(qc.get('empathy', {}).get('value')),
        'communication_skills': graded(qc.get('communication_skills', {}).get('value')),
        'probing': contextual(qc.get('probing', {}).get('value')),
        'observations': contextual(qc.get('observations', {}).get('value')),
        'call_closure': graded(qc.get('call_closure', {}).get('value')),
        'complete_mer_questions': pct_to_score(complete_mer_pct),
        'correct_documentation': pct_to_score(correct_doc_pct),
        'call_duration': duration_score,
        'rate_of_speech': rate_of_speech_score,
        'visual_presentation': 100,
    }

    total = sum(scores.values())
    max_total = 1600
    percentage = round((total / max_total) * 100.0, 2)
    if total >= 1500: category = 'Good'
    elif total >= 1400: category = 'Above Average'
    elif total >= 1300: category = 'Average'
    else: category = 'Poor'

    return {
        'total_score': total,
        'max_score': max_total,
        'percentage': percentage,
        'category': category,
        'breakdown': scores,
        'derived': {
            'complete_mer_pct': round(complete_mer_pct, 2),
            'correct_documentation_pct': round(correct_doc_pct, 2),
            'call_duration_min': round(dur_min, 2),
            'doctor_wpm': round(doctor_wpm, 2) if isinstance(doctor_wpm, (int, float)) else None,
        }
    }


def compute_wpm(transcript: Dict[str, Any]) -> Dict[str, float]:
    segments: List[Dict[str, Any]] = transcript.get('segments', []) or []
    totals = {
        'doctor_words': 0,
        'doctor_seconds': 0.0,
        'customer_words': 0,
        'customer_seconds': 0.0,
    }
    for seg in segments:
        text = str(seg.get('text', '') or '')
        start = parse_mmss_to_seconds(seg.get('start_timestamp', ''))
        end = parse_mmss_to_seconds(seg.get('end_timestamp', ''))
        if start is None or end is None or end <= start:
            continue
        duration = end - start
        spk_raw = str(seg.get('speaker', '')).lower()
        role = 'doctor' if spk_raw in ('doctor', 'agent') else ('customer' if spk_raw == 'customer' else None)
        if not role:
            continue
        word_count = len([w for w in text.strip().split() if w])
        totals[f'{role}_words'] += word_count
        totals[f'{role}_seconds'] += duration

    def safe_wpm(words: int, seconds: float) -> float:
        return round((words / (seconds / 60.0)), 2) if seconds > 0 else 0.0

    return {
        'doctor_wpm': safe_wpm(totals['doctor_words'], totals['doctor_seconds']),
        'customer_wpm': safe_wpm(totals['customer_words'], totals['customer_seconds']),
    }


def _normalize_status(value: Optional[str]) -> str:
    s = (value or '').strip().lower()
    if 'incorrect' in s:
        return 'incorrect'
    if 'missing' in s:
        return 'missing'
    if 'clubbed' in s:
        return 'clubbed'
    if 'paraphrased' in s:
        return 'paraphrased'
    if 'correct' in s:
        return 'correct'
    return s or 'unknown'


def compute_ui_summary(report: Dict[str, Any]) -> Dict[str, Any]:
    qa_items: List[Dict[str, Any]] = report.get('qa_matrix', []) or []
    total = 0
    asked = 0
    missed = 0
    incorrect = 0
    paraphrased = 0
    clubbed = 0
    correct = 0

    for item in qa_items:
        qid = str(item.get('question_id', '')).strip()
        status = _normalize_status(item.get('status'))
        expected = str(item.get('expected_response', '') or '').strip().lower()

        # Exclude non-applicable Personal Particulars from scoring
        if qid.startswith('PP.') and (expected in {'', 'na', 'n/a', 'not applicable', 'null', 'none'}):
            continue

        total += 1
        if status == 'missing':
            missed += 1
        else:
            asked += 1

        if status == 'incorrect':
            incorrect += 1
        elif status == 'paraphrased':
            paraphrased += 1
        elif status == 'clubbed':
            clubbed += 1
        elif status == 'correct':
            correct += 1

    accuracy = round((correct / total) * 100, 2) if total > 0 else 0.0
    return {
        'overall_compliance_score': accuracy,
        'total_questions': total,
        'questions_asked': asked,
        'questions_missed': missed,
        'incorrect_responses': incorrect,
        'paraphrased_responses': paraphrased,
        'clubbed_questions': clubbed,
        'critical_errors': len((report.get('summary') or {}).get('critical_issues', []) or []),
    }


def derive_top_metrics(report: Dict[str, Any], audio_duration: Optional[float]) -> Dict[str, Any]:
    # Prefer our computed metrics derived from qa_matrix with PP rules
    computed = compute_ui_summary(report)

    duration_str = None
    if audio_duration:
        minutes = int(audio_duration // 60)
        seconds = int(audio_duration % 60)
        duration_str = f"{minutes}:{seconds:02d}"

    # Optional metadata if present in report
    meta_id = report.get('meta', {}).get('id') if isinstance(report, dict) else None
    employee = report.get('meta', {}).get('employee') if isinstance(report, dict) else None

    return {
        "accuracy": computed['overall_compliance_score'],
        "questions_asked": computed['questions_asked'],
        "total_questions": computed['total_questions'],
        "documentation_errors": computed['incorrect_responses'],
        "questions_missed": computed['questions_missed'],
        "paraphrased_responses": computed['paraphrased_responses'],
        "clubbed_questions": computed['clubbed_questions'],
        "critical_errors": computed['critical_errors'],
        "id": meta_id,
        "employee": employee,
        "duration": duration_str,
    }


app = Flask(__name__, static_folder='static', template_folder='templates')


@app.route('/')
def index():
    return render_template('index.html')

@app.route('/index.html')
def index_html():
    # Allow direct linking like /index.html?rid=...&call=...
    return render_template('index.html')


@app.route('/records')
def records_page():
    # React dashboard page that consumes /api/records_dashboard
    return render_template('records_react.html')


@app.route('/record/<rid>')
def record_page(rid: str):
    # Simple per-record UI with call tabs
    return render_template_string('''<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Record {{ rid }}</title><link rel="stylesheet" href="/static/styles.css"></head>
    <body>
      <div class="topbar" style="justify-content:space-between;padding:12px 24px">
        <div style="font-weight:700">Record: {{ rid }}</div>
        <a class="btn" href="/records">Back to Records</a>
      </div>
      <div id="root" style="padding:24px">Loading...</div>
      <script>
      var RID = "{{ rid }}";
      (async function(){
        var root = document.getElementById('root');
        try{
          var r = await fetch('/api/records/' + encodeURIComponent(RID));
          var j = await r.json();
          if(j.error){ root.textContent = 'Not processed yet. Use API to process.'; return; }
          var calls = j.calls||[];
          var tabs = calls.map(function(c){ return '<button class="tab" data-idx="'+c.index+'">Call '+c.index+'</button>'; }).join('') + '<button class="tab" data-idx="agg">Aggregated</button>';
          var shell = '<div class="tabs">'+tabs+'</div><div id="view" class="card"></div>';
          root.innerHTML = shell;
          var view = document.getElementById('view');
          function render(idx){
            if(idx==='agg'){
              var a=j.aggregate||{}; var br=a.breakdown||{}; var fd=j.final_decision||{};
              var header = '<div style="display:flex;gap:12px;flex-wrap:wrap;color:#374151"><div><b>'+(a.total_score||0)+' / '+(a.max_score||1600)+'</b> Total</div><div><b>'+(a.percentage||0)+'%</b> Score</div><div><b>'+(a.category||'-')+'</b></div></div>'+
                '<div style="margin-top:12px">'+Object.keys(br).sort().map(function(k){return '<div>'+k+': <b>'+br[k]+'</b></div>';}).join('')+'</div>';
              function renderIssues(cat, list){
                list = Array.isArray(list)? list : [];
                if(!list.length) return '';
                var items = list.map(function(it){
                  var d = it.details;
                  var det = (typeof d==='string')? d : (d? JSON.stringify(d) : '');
                  return '<li style="margin-left:16px">'+it.issue+(det? ' — <span style="color:#6B7280">'+det+'</span>':'')+'</li>';
                }).join('');
                return '<div class="question-card"><div style="display:flex;justify-content:space-between;align-items:center"><b>'+cat+'</b><span class="badge">'+list.length+'</span></div><ul style="margin:8px 0 0 0;padding:0;list-style:disc">'+items+'</ul></div>';
              }
              var fdHtml = ''+
                '<h3 style="margin:16px 0 8px 0">Final Decision</h3>'+
                renderIssues('ASSIGNBACK', fd.ASSIGNBACK) +
                renderIssues('OPS_ATTENTION', fd.OPS_ATTENTION) +
                renderIssues('FLAGS', fd.FLAGS) +
                renderIssues('TECH_ISSUES', fd.TECH_ISSUES);
              view.innerHTML = header + fdHtml;
              return;
            }
            var c = calls.find(function(x){ return String(x.index)===String(idx); });
            if(!c){ view.textContent='Missing call'; return; }
            // Render the full dashboard UI in this page by loading index.html with endpoints set via query params
            var q = '?rid='+encodeURIComponent(RID)+'&call='+encodeURIComponent(c.index);
            view.innerHTML = '<iframe src="/index.html'+q+'" style="width:100%;height:80vh;border:0"></iframe>';
          }
          document.querySelectorAll('.tab').forEach(function(b){ b.addEventListener('click', function(){ document.querySelectorAll('.tab').forEach(function(x){x.classList.remove('active');}); b.classList.add('active'); render(b.dataset.idx); }); });
          var first = document.querySelector('.tab'); if(first){ first.classList.add('active'); render(first.dataset.idx); }
        }catch(e){ root.textContent = 'Failed to load record'; }
      })();
      </script>
    </body></html>''', rid=rid)


@app.route('/api/transcript')
def api_transcript():
    data = load_transcript()
    return jsonify(data)


@app.route('/api/report')
def api_report():
    data = read_json_file(QA_REPORT_PATH)
    return jsonify(data)
@app.route('/api/report2')
def api_report2():
    data = read_json_file(QA_REPORT_PART2_PATH)
    return jsonify(data)


@app.route('/api/metadata')
def api_metadata():
    transcript = load_transcript()
    report = read_json_file(QA_REPORT_PATH)
    duration_sec = effective_duration_seconds(AUDIO_PATH, transcript)
    speaker_stats = compute_speaker_distribution(transcript, duration_sec)
    top = derive_top_metrics(report, duration_sec)
    return jsonify({
        "top": top,
        "speaker": {**speaker_stats, **compute_wpm(transcript)},
    })

@app.route('/api/qcscore')
def api_qcscore():
    report = read_json_file(QA_REPORT_PATH)
    qc2 = read_json_file(QA_REPORT_PART2_PATH)
    duration_sec = effective_duration_seconds(AUDIO_PATH, load_transcript())
    return jsonify(compute_qc_score(report, qc2, duration_sec))


@app.route('/api/records')
def api_records():
    recs = scan_records()
    # shape: {id: {mer_pdf, calls:[{index,name}]}}
    payload = []
    for rid, rec in recs.items():
        payload.append({
            "id": rid,
            "mer_pdf": rec.get("mer_pdf"),
            "num_calls": len(rec.get("calls", [])),
            "calls": [{"index": c["index"], "name": c["name"]} for c in rec.get("calls", [])]
        })
    return jsonify({"records": sorted(payload, key=lambda x: x["id"])})


@app.route('/api/records_dashboard')
def api_records_dashboard():
    recs = scan_records()
    out = []
    for rid, rec in recs.items():
        base = Path(RECORDS_DIR) / rid / '_processed'
        qa = load_json_safe(base / 'merged_qa_report.json')
        qc2 = load_json_safe(base / 'merged_qa_report_part2.json')
        # compute metrics
        duration = None
        try:
            tr = load_json_safe(base / 'merged_transcript.json')
            duration = None
            # estimate via last segment end if available
            max_end = 0.0
            for s in (tr.get('segments') or []):
                try:
                    m, s2 = str(s.get('end_timestamp','0:00')).split(':')
                    t = int(m)*60 + int(s2)
                    if t>max_end: max_end = t
                except Exception:
                    pass
            duration = max_end if max_end>0 else None
        except Exception:
            pass
        top = derive_top_metrics(qa, duration)
        # categorize based on final_decision.json if exists
        decision = load_json_safe(base / 'final_decision.json')
        category = 'pass'
        if decision:
            if (decision.get('ASSIGNBACK') or []):
                category = 'assignback'
            elif (decision.get('OPS_ATTENTION') or []):
                category = 'ops_attention'
            elif (decision.get('TECH_ISSUES') or []):
                category = 'tech_issues'
            elif (decision.get('FLAGS') or []):
                category = 'flags'
        out.append({
            'id': rid,
            'customerName': qa.get('personal_particulars', {}).get('name') or '-',
            'doctorName': (qa.get('meta', {}) or {}).get('doctor_name') or '-',
            'insurerName': (qa.get('meta', {}) or {}).get('insurance_company') or '-',
            'date': (qa.get('meta', {}) or {}).get('date') or '-',
            'duration': top.get('duration') or '-',
            'accuracy': top.get('accuracy') or 0,
            'questionsAsked': f"{top.get('questions_asked') or 0}/{top.get('total_questions') or 0}",
            'category': category,
            'issues': (qa.get('summary') or {}).get('critical_issues') or [],
            'qcScore': (compute_qc_score(qa, qc2, duration) or {}).get('total_score', 0)
        })
    return jsonify({'records': out})


@app.route('/api/records/<rid>/process', methods=['POST'])
def api_process_record(rid: str):
    api_key = request.headers.get('X-API-Key') or os.environ.get('GEMINI_API_KEY')
    force = (request.args.get('force','false').lower() in ('1','true','yes'))
    # Run processing in a background thread so UI stays responsive
    print(f"[api] /api/records/{rid}/process force={force} key={'yes' if api_key else 'no'}", flush=True)
    t = threading.Thread(target=process_record, args=(rid, api_key, force), daemon=True)
    t.start()
    return jsonify({"status": "started", "id": rid, "force": force})


@app.route('/api/records/<rid>')
def api_record_details(rid: str):
    base = Path(RECORDS_DIR) / rid / "_processed"
    if not base.exists():
        return jsonify({"error": "not_processed"}), 404

    # Check if we have medb.py generated summary
    summary_path = base / 'processing_summary.json'
    if summary_path.exists():
        summary = load_json_safe(summary_path)
        if summary:
            # Convert medb.py format to expected format
            calls = []
            for call_data in summary.get('individual_calls', []):
                calls.append({
                    "index": call_data.get('call_index', 0),
                    "qa": {},  # Individual QA not generated by medb.py
                    "qc": {},  # Individual QC not generated by medb.py  
                    "transcript": load_json_safe(Path(call_data.get('transcript_path', ''))),
                    "duration_sec": call_data.get('duration', 0),
                })
            
            # Use merged results from medb.py
            merged = {
                "qa": summary.get('qa_part1', {}),
                "qc": summary.get('qa_part2', {}),
                "transcript": load_json_safe(base / 'merged_transcript.json'),
            }
            
            # Simple aggregate based on merged results
            aggregate = {"breakdown": {}, "total_score": 0, "max_score": 1600, "percentage": 0.0, "category": "Unknown"}
            
            final_decision = load_json_safe(base / 'final_decision.json')
            return jsonify({"id": rid, "calls": calls, "aggregate": aggregate, "merged": merged, "final_decision": final_decision})

    # Fallback to old format for backwards compatibility
    calls = []
    for call_dir in sorted(base.glob('call*')):
        idx = int(call_dir.name.replace('call',''))
        qa_path = call_dir / 'qa_report.json'
        qc_path = call_dir / 'qa_report_part2.json'
        tr_path = call_dir / 'transcript.json'
        qa = load_json_safe(qa_path)
        qc = load_json_safe(qc_path)
        tr = load_json_safe(tr_path)
        dur = effective_duration_seconds(str(call_dir / 'audio.mp3'), tr) or effective_duration_seconds(AUDIO_PATH, tr)
        calls.append({
            "index": idx,
            "qa": qa,
            "qc": qc,
            "transcript": tr,
            "duration_sec": dur,
        })

    # Include merged artifacts if present
    merged = {}
    mqa = base / 'merged_qa_report.json'
    mqc = base / 'merged_qa_report_part2.json'
    mtr = base / 'merged_transcript.json'
    if mqa.exists() or mqc.exists() or mtr.exists():
        merged = {
            "qa": load_json_safe(mqa) if mqa.exists() else {},
            "qc": load_json_safe(mqc) if mqc.exists() else {},
            "transcript": load_json_safe(mtr) if mtr.exists() else {},
        }

    final_decision = load_json_safe(base / 'final_decision.json')
    return jsonify({"id": rid, "calls": sorted(calls, key=lambda x: x["index"]), "aggregate": {}, "merged": merged, "final_decision": final_decision})


@app.route('/api/records/<rid>/final_decision')
def api_record_final_decision(rid: str):
    base = Path(RECORDS_DIR) / rid / "_processed"
    if not base.exists():
        return jsonify({})
    return jsonify(load_json_safe(base / 'final_decision.json'))


# Per-call API proxies to reuse dashboard UI
@app.route('/api/records/<rid>/calls/<int:idx>/report')
def api_record_call_report(rid: str, idx: int):
    base = Path(RECORDS_DIR) / rid / "_processed" / f"call{idx}"
    data = load_json_safe(base / 'qa_report.json')
    if not data:
        # Fallback to merged
        mbase = Path(RECORDS_DIR) / rid / "_processed"
        data = load_json_safe(mbase / 'merged_qa_report.json')
    return jsonify(data)

@app.route('/api/records/<rid>/calls/<int:idx>/report2')
def api_record_call_report2(rid: str, idx: int):
    base = Path(RECORDS_DIR) / rid / "_processed" / f"call{idx}"
    data = load_json_safe(base / 'qa_report_part2.json')
    if not data:
        mbase = Path(RECORDS_DIR) / rid / "_processed"
        data = load_json_safe(mbase / 'merged_qa_report_part2.json')
    return jsonify(data)

@app.route('/api/records/<rid>/calls/<int:idx>/transcript')
def api_record_call_transcript(rid: str, idx: int):
    base = Path(RECORDS_DIR) / rid / "_processed" / f"call{idx}"
    t = load_transcript_from_path(base / 'transcript.json')
    if not (t.get('segments') or []):
        mbase = Path(RECORDS_DIR) / rid / "_processed"
        try:
            with open(mbase / 'merged_transcript.json') as f:
                t = json.load(f)
        except Exception:
            t = {"segments": []}
    return jsonify(t)

@app.route('/api/records/<rid>/calls/<int:idx>/metadata')
def api_record_call_metadata(rid: str, idx: int):
    # Build top & speaker metadata from this call
    base = Path(RECORDS_DIR) / rid / "_processed" / f"call{idx}"
    tr = load_transcript_from_path(base / 'transcript.json')
    qa = load_json_safe(base / 'qa_report.json')
    # Fallback to merged artifacts if per-call files are missing/empty
    if not qa:
        mbase = Path(RECORDS_DIR) / rid / "_processed"
        qa = load_json_safe(mbase / 'merged_qa_report.json')
    if not (tr.get('segments') or []):
        try:
            with open(Path(RECORDS_DIR) / rid / "_processed" / 'merged_transcript.json') as f:
                tr = json.load(f)
        except Exception:
            tr = {"segments": []}
    duration_sec = effective_duration_seconds(str(base / 'audio.mp3'), tr)
    speaker_stats = compute_speaker_distribution(tr, duration_sec)
    top = derive_top_metrics(qa, duration_sec)

    # Resolve MER PDF S3 URL (if local file replaced with URL pointer)
    mer_url = None
    try:
        recs = scan_records()
        rec = recs.get(rid, {})
        mer_pdf_path = rec.get('mer_pdf')
        if mer_pdf_path and os.path.exists(mer_pdf_path):
            mer_url = _read_url_pointer_if_any(mer_pdf_path)
        if (not mer_url):
            fallback = Path(RECORDS_DIR) / f"{rid}_MER.pdf"
            if fallback.exists():
                mer_url = _read_url_pointer_if_any(str(fallback))
        # Final fallback: lookup in s3_manifest.json if present in repo
        if (not mer_url):
            man_path = Path(__file__).parent / 's3_manifest.json'
            if man_path.exists():
                try:
                    with open(man_path, 'r', encoding='utf-8') as mf:
                        man = json.load(mf)
                    target_rel = f"reports and recordings/{rid}_MER.pdf"
                    for item in man.get('items', []):
                        if item.get('local_path') == target_rel:
                            mer_url = item.get('presigned_url') or (f"s3://{item.get('bucket')}/{item.get('key')}")
                            break
                except Exception:
                    pass
    except Exception:
        mer_url = None

    return jsonify({"top": top, "speaker": {**speaker_stats, **compute_wpm(tr)}, "mer_pdf_url": mer_url})

@app.route('/api/records/<rid>/calls/<int:idx>/audio')
def api_record_call_audio(rid: str, idx: int):
    # If we had extracted audio, serve it; else 404 to fall back to default
    base = Path(RECORDS_DIR) / rid / "_processed" / f"call{idx}"
    audio_path = str(base / 'audio.mp3')
    if os.path.exists(audio_path):
        url = _read_url_pointer_if_any(audio_path)
        if url:
            return redirect(url, code=302)
        return partial_response(audio_path)
    # Fallback to original scanned file if found
    recs = scan_records()
    rec = recs.get(rid, {})
    calls = rec.get('calls', [])
    for c in calls:
        if c.get('index') == idx and os.path.exists(c.get('path','')):
            cpath = c['path']
            url = _read_url_pointer_if_any(cpath)
            if url:
                return redirect(url, code=302)
            return partial_response(cpath)
    return Response("Audio not found", status=404)


def _guess_audio_mime(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext == '.mp3': return 'audio/mpeg'
    if ext == '.wav': return 'audio/wav'
    if ext == '.m4a': return 'audio/mp4'
    if ext == '.ogg': return 'audio/ogg'
    if ext == '.webm': return 'audio/webm'
    if ext == '.mp4': return 'audio/mp4'
    return 'application/octet-stream'


def partial_response(path: str) -> Response:
    file_size = os.path.getsize(path)
    range_header = request.headers.get('Range', None)
    if not range_header:
        return send_file(path)

    bytes_unit, bytes_range = range_header.split('=')
    if bytes_unit != 'bytes':
        return send_file(path)
    start_str, end_str = bytes_range.split('-')
    start = int(start_str) if start_str else 0
    end = int(end_str) if end_str else file_size - 1
    end = min(end, file_size - 1)
    length = end - start + 1

    with open(path, 'rb') as f:
        f.seek(start)
        data = f.read(length)

    rv = Response(data, 206, mimetype=_guess_audio_mime(path), direct_passthrough=True)
    rv.headers.add('Content-Range', f'bytes {start}-{end}/{file_size}')
    rv.headers.add('Accept-Ranges', 'bytes')
    rv.headers.add('Content-Length', str(length))
    return rv


def _read_url_pointer_if_any(path: str) -> Optional[str]:
    """If the given file is a small text file containing a single HTTP(S) URL, return it."""
    try:
        size = os.path.getsize(path)
        if size > 2048:
            return None
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read(2048).strip()
        first_line = content.splitlines()[0].strip() if content else ''
        if first_line.startswith('http://') or first_line.startswith('https://'):
            return first_line
    except Exception:
        return None
    return None


def _s3_manifest_lookup_urls(pattern_fn) -> List[str]:
    """Return list of presigned URLs (or s3:// URIs) from s3_manifest.json matching the given predicate on local_path."""
    results: List[str] = []
    try:
        man_path = Path(__file__).parent / 's3_manifest.json'
        if not man_path.exists():
            return results
        with open(man_path, 'r', encoding='utf-8') as mf:
            man = json.load(mf)
        for item in man.get('items', []):
            lp = item.get('local_path') or ''
            try:
                if pattern_fn(lp):
                    url = item.get('presigned_url') or (f"s3://{item.get('bucket')}/{item.get('key')}")
                    if url:
                        results.append(url)
            except Exception:
                continue
    except Exception:
        return []
    return results


@app.route('/audio')
def audio_stream():
    if not os.path.exists(AUDIO_PATH):
        return Response("Audio not found", status=404)
    url = _read_url_pointer_if_any(AUDIO_PATH)
    if url:
        return redirect(url, code=302)
    return partial_response(AUDIO_PATH)


if __name__ == '__main__':
    host = os.environ.get('HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', '5000'))
    app.run(host=host, port=port, debug=True, threaded=True)


