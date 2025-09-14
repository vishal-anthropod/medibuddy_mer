"""
MediBuddy Medical Verification QA System

Validates doctor's documentation accuracy by comparing:
- Source of Truth: Customer's responses in the recorded call
- To Be Validated: Doctor's documentation in the MER form

The system checks if the doctor:
1. Asked all required questions during the call
2. Correctly documented customer's answers in the MER
3. Maintained professional conduct during the conversation
"""

import os
import json
import base64
import argparse
import subprocess
import shlex
import re
from typing import Dict, List, Any, Tuple, Optional
from pathlib import Path
import PyPDF2
from google import genai
from google.genai import types


def log_progress(step: str, current: int = 0, total: int = 100):
    """Log progress with percentage"""
    if total > 0:
        percentage = int((current / total) * 100)
        bar = '#' * (percentage // 10) + '-' * (10 - percentage // 10)
        print(f"[{percentage:3d}%] [{bar}] {step}", flush=True)
    else:
        print(f"[---] [----------] {step}", flush=True)


def extract_audio_from_video(video_path: str, output_path: str, timeout: int = 600) -> bool:
    """Extract audio from video file using ffmpeg"""
    try:
        print(f"Extracting audio from {Path(video_path).name}...")
        cmd = f"ffmpeg -y -i {shlex.quote(video_path)} -vn -acodec libmp3lame -q:a 2 {shlex.quote(output_path)}"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return Path(output_path).exists()
    except Exception as e:
        print(f"Audio extraction failed: {e}")
        return False


def get_media_duration(path: str) -> Optional[float]:
    """Get media duration using ffprobe"""
    try:
        cmd = f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 {shlex.quote(path)}"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        return float(result.stdout.strip()) if result.stdout.strip() else None
    except Exception:
        return None

def analyze_audio_technical(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"recording_exists": False, "audibility_level": "not_audible", "avg_dbfs": None}
    try:
        cmd = f"ffmpeg -i {shlex.quote(path)} -filter_complex loudnorm=I=-23:TP=-1.5:LRA=11:print_format=json -f null -"
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
        text = (p.stderr or '') + '\n' + (p.stdout or '')
        m = re.search(r'"input_i"\s*:\s*"?(\-?\d+(?:\.\d+)?)"?', text)
        val = float(m.group(1)) if m else None
        if val is None:
            cmd2 = f"ffmpeg -i {shlex.quote(path)} -af volumedetect -f null -"
            p2 = subprocess.run(cmd2, shell=True, capture_output=True, text=True, timeout=60)
            t2 = (p2.stderr or '') + '\n' + (p2.stdout or '')
            m2 = re.search(r'mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB', t2)
            val = float(m2.group(1)) if m2 else None
        level = 'unknown'
        if val is not None:
            level = 'audible' if val >= -40 else 'not_audible'
        return {"recording_exists": True, "audibility_level": level, "avg_dbfs": val}
    except Exception:
        return {"recording_exists": True, "audibility_level": "unknown", "avg_dbfs": None}


def split_audio_into_chunks(src_path: str, out_dir: Path, chunk_seconds: int = 300) -> List[str]:
    """Split audio into ~chunk_seconds mp3 chunks. Returns list of file paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # Clean previous chunks
    try:
        for f in out_dir.glob('chunk_*.mp3'):
            try:
                f.unlink()
            except Exception:
                pass
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


def transcribe_chunks_and_merge(paths: List[str], api_key: str) -> Dict[str, Any]:
    """Transcribe list of chunk paths sequentially and merge with offset timestamps."""
    merged = {"segments": []}
    offset = 0.0
    for cp in paths:
        try:
            tdict = transcribe_audio(cp, api_key)
        except Exception:
            tdict = {"segments": []}
        segs = (tdict or {}).get('segments', []) or []
        cdur = get_media_duration(cp) or 0.0
        for s in segs:
            try:
                st = parse_timestamp_to_seconds(s.get('start_timestamp') or '0:00')
                en = parse_timestamp_to_seconds(s.get('end_timestamp') or '0:00')
            except Exception:
                st, en = 0.0, 0.0
            merged['segments'].append({
                'segment_id': s.get('segment_id',''),
                'text': s.get('text',''),
                'speaker': s.get('speaker',''),
                'start_timestamp': seconds_to_timestamp(max(0.0, st + offset)),
                'end_timestamp': seconds_to_timestamp(max(0.0, en + offset)),
            })
        offset += cdur
    return merged


def extract_video_frames(video_path: str, output_dir: Path) -> List[str]:
    frames: List[str] = []
    try:
        dur = get_media_duration(video_path) or 0.0
        if dur <= 0:
            return frames
        ts_points = [int(dur * p) for p in (0.2, 0.5, 0.8)]
        for i, sec in enumerate(ts_points, start=1):
            out_path = output_dir / f"frame_{i:02d}.jpg"
            cmd = f"ffmpeg -ss {sec} -i {shlex.quote(video_path)} -frames:v 1 -q:v 2 {shlex.quote(str(out_path))} -y"
            subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
            if out_path.exists():
                frames.append(str(out_path))
    except Exception:
        pass
    return frames


def analyze_video_frames(frames: List[str], api_key: str) -> Dict[str, Any]:
    if not frames:
        return {
            "attire_check": "NA",
            "attire_explanation": "NA",
            "visibility_status": "NA",
            "visibility_explanation": "NA",
            "privacy_maintained": "NA",
            "privacy_explanation": "NA",
            "screenshots": []
        }
    try:
        client = genai.Client(api_key=api_key)
        parts: List[types.Part] = []
        for fp in frames:
            with open(fp, 'rb') as f:
                img = f.read()
            parts.append(types.Part(inlineData=types.Blob(mimeType="image/jpeg", data=img)))
        prompt = """
Analyze these video frames and return STRICT JSON with explanations for each check.

Return JSON exactly in this schema (no extra keys):
{
  "attire_check": "yes|no|unknown",
  "attire_explanation": "string",
  "visibility_status": "both_visible|only_doctor|only_customer|unknown",
  "visibility_explanation": "string",
  "privacy_maintained": true|false|null,
  "privacy_explanation": "string"
}

Rules:
- Attire: evaluate if the doctor is wearing a clinical apron/coat. Output must be yes/no (use unknown only if truly inconclusive). Explain color/pattern if visible.
- Visibility: judge whether both doctor and customer are visible in the frames. Explain with which frame(s) support the conclusion.
- Privacy: ONLY evaluate the doctor's background. Consider breaches such as people walking behind the doctor, visible private boards/files/monitors showing confidential data, or an identifiable patient nearby. The customer's background does NOT affect privacy. Explain clearly.
"""
        contents = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)] + parts)]
        config = types.GenerateContentConfig(temperature=0.1, response_mime_type="application/json")
        resp = client.models.generate_content(model="gemini-2.5-flash", contents=contents, config=config)
        out = json.loads(resp.text)
        # Normalize and attach screenshots
        if not isinstance(out, dict):
            out = {}
        out.setdefault("attire_check", "unknown")
        out.setdefault("attire_explanation", "")
        out.setdefault("visibility_status", "unknown")
        out.setdefault("visibility_explanation", "")
        out.setdefault("privacy_maintained", None)
        out.setdefault("privacy_explanation", "")
        out.setdefault("screenshots", frames)
        return out
    except Exception:
        return {
            "attire_check": "unknown",
            "attire_explanation": "",
            "visibility_status": "unknown",
            "visibility_explanation": "",
            "privacy_maintained": None,
            "privacy_explanation": "",
            "screenshots": frames
        }


def _get_response_text(resp: Any) -> str:
    """Robustly extract text from Gemini response."""
    try:
        txt = getattr(resp, 'text', None)
        if txt:
            return txt
        # Fallback: aggregate candidate parts text
        parts_text: List[str] = []
        for cand in getattr(resp, 'candidates', []) or []:
            content = getattr(cand, 'content', None)
            for part in getattr(content, 'parts', []) or []:
                t = getattr(part, 'text', None)
                if isinstance(t, str):
                    parts_text.append(t)
        return '\n'.join(parts_text)
    except Exception:
        return ''


def parse_timestamp_to_seconds(timestamp: str) -> float:
    """Convert MM:SS timestamp to seconds"""
    try:
        parts = timestamp.split(':')
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        return 0.0
    except:
        return 0.0


def seconds_to_timestamp(seconds: float) -> str:
    """Convert seconds to MM:SS format"""
    try:
        s = int(seconds)
        m = s // 60
        r = s % 60
        return f"{m}:{r:02d}"
    except:
        return "0:00"


def mmss_to_hhmmss(ts: str) -> str:
    """Convert MM:SS to HH:MM:SS, robust to bad input."""
    try:
        parts = ts.split(':')
        if len(parts) != 2:
            return "00:00:00"
        m = int(parts[0]); s = int(parts[1])
        total = m * 60 + s
        h = total // 3600
        rem = total % 3600
        m2 = rem // 60
        s2 = rem % 60
        return f"{h:02d}:{m2:02d}:{s2:02d}"
    except Exception:
        return "00:00:00"


def build_merged_transcript_text(merged: Dict[str, Any]) -> str:
    """Format merged transcript into the requested single-line segment style grouped by Call - N."""
    segments = merged.get('segments', []) or []
    by_call: Dict[int, List[Dict[str, Any]]] = {}
    for seg in segments:
        idx = int(seg.get('call_index') or 1)
        by_call.setdefault(idx, []).append(seg)
    lines: List[str] = []
    for call_idx in sorted(by_call.keys()):
        lines.append(f"Call - {call_idx}")
        parts: List[str] = []
        for seg in by_call[call_idx]:
            sid = seg.get('segment_id') or ''
            st = mmss_to_hhmmss(seg.get('start_timestamp') or '0:00')
            en = mmss_to_hhmmss(seg.get('end_timestamp') or '0:00')
            spk_raw = str(seg.get('speaker') or '').lower()
            speaker = 'agent' if spk_raw in ('doctor','agent') else ('customer' if spk_raw=='customer' else spk_raw or 'agent')
            text = (seg.get('text') or '').strip()
            parts.append(f"[Segment ID - {sid}] [Start Timestamp - {st}] [End Timestamp - {en}] [Speaker - {speaker}] {text} ")
        if parts:
            lines.append(' '.join(parts).strip())
        else:
            lines.append('""')
        lines.append('')
    return '\n'.join(lines).strip()


def merge_transcripts(transcripts: List[Dict[str, Any]], call_durations: List[float]) -> Dict[str, Any]:
    """Merge multiple transcripts with adjusted timestamps"""
    merged = {"segments": []}
    offset = 0.0
    
    for i, (transcript, duration) in enumerate(zip(transcripts, call_durations)):
        segments = transcript.get('segments', [])
        print(f"Merging call {i+1}: {len(segments)} segments, duration: {duration:.1f}s")
        
        for segment in segments:
            # Parse timestamps
            start_str = segment.get('start_timestamp', '0:00')
            end_str = segment.get('end_timestamp', '0:00')
            
            start_sec = parse_timestamp_to_seconds(start_str)
            end_sec = parse_timestamp_to_seconds(end_str)
            
            # Adjust with offset
            new_start = start_sec + offset
            new_end = end_sec + offset
            
            # Create merged segment
            merged_segment = {
                "segment_id": f"call{i+1}_{segment.get('segment_id', '')}",
                "text": segment.get('text', ''),
                "speaker": segment.get('speaker', ''),
                "start_timestamp": seconds_to_timestamp(new_start),
                "end_timestamp": seconds_to_timestamp(new_end),
                "call_index": i + 1
            }
            
            merged["segments"].append(merged_segment)
        
        # Update offset for next call
        offset += duration
    
    print(f"Merged transcript: {len(merged['segments'])} total segments across {len(transcripts)} calls")
    return merged


def find_media_files(directory: Path) -> List[Path]:
    """Find all audio/video files in directory"""
    media_extensions = {'.mp3', '.wav', '.m4a', '.mp4', '.webm', '.ogg', '.flac'}
    media_files = []
    
    for ext in media_extensions:
        media_files.extend(directory.glob(f"*{ext}"))
    
    # Sort by name for consistent ordering
    return sorted(media_files, key=lambda x: x.name)


def get_gemini_transcription_prompt():
    return """
                            Generate a transcript of the call given in the audio file. If the audio is not in english then first translate to english and then transcribe. Include timestamps, speaker identification & emotion of the speaker.

                            Provide your response in the following JSON format:
                            {
                                    "segments": [
                                    {
                                        "segment_id": "",
                                        "text": "",
                                        "speaker": "",
                                        "start_timestamp":"mm:ss",
                                        "end_timestamp": "mm:ss"
                                    }
                                    ]
                                
                            }

                            SPEAKER VALUES:
                                - "doctor" 
                                - "customer" 
                            
                            SEGMENTATION:
                                - New segment when speaker changes
                                - New segment if same speaker exceeds 30 seconds
                                - Overlapping speech gets separate segments with overlapping timestamps

                            TIMESTAMP RULES:
                                - Use MM:SS format (minutes:seconds). Example: "01:23" means 1 minute, 23 seconds
                                - Each segment: start_timestamp < end_timestamp
                                - Segments should not have gaps unless there's actual silence
                                - For overlapping speech: timestamps can overlap, but use unique IDs

                            Important Notes related to output structure:
                            1. Strictly adhere to this JSON format. Do not include any additional text at all.Don't use any markdown formatting, like bolding or italics.
                            2. Special attention to structure inside the segment array.
                                a) Specific adherence to standard json format - key names should be in double quotes with proper ':' separation between key and values.
                                b) Each object in segment should contain all of these keys segment_id, text, speaker, start_timestamp & end_timestamp
                                c) segment_id should be unique across the segment
                                d) Inside segment object, each key value pair should be comma separated.
                                e) Inside key values, you can use single quotes inside values double quotes so that it doesn't cause issue in standard json format
                                f) Don't mention the segment_id and timestamp in the text part, only mention in the timestamp & segment keys.
                            
                            Critical: Only use english language in the text. If you find a different language in audio then translate to english and then give the english text.

                            """


def transcribe_audio(audio_file_path: str, api_key: str) -> Dict:
    """
    Transcribe audio file using Gemini 2.5 Flash
    
    Args:
        audio_file_path: Path to the audio file
        api_key: Gemini API key
    
    Returns:
        Transcription in JSON format
    """
    client = genai.Client(api_key=api_key)
    model = "gemini-2.5-flash"
    
    # Read raw audio bytes
    with open(audio_file_path, "rb") as audio_file:
        audio_bytes = audio_file.read()

    # Detect mime type based on file extension
    def guess_audio_mime_type(file_path: str) -> str:
        ext = Path(file_path).suffix.lower()
        mapping = {
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
            ".m4a": "audio/mp4",
            ".flac": "audio/flac",
            ".ogg": "audio/ogg",
            ".webm": "audio/webm",
        }
        return mapping.get(ext, "application/octet-stream")
    mime_type = guess_audio_mime_type(audio_file_path)
    
    # Prepare the content with transcription prompt
    prompt = get_gemini_transcription_prompt()
    context = (
        "Context: This is a brief medical history verification call. A doctor calls a customer on behalf "
        "of an insurance company. Pay attention to names, IDs, dates (DOB), and medical details."
    )
    
    contents = [
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(text=context),
                types.Part.from_text(text=prompt),
                types.Part(
                    inlineData=types.Blob(
                        mimeType=mime_type,
                        data=audio_bytes,
                    )
                )
            ],
        ),
    ]
    
    # Generate transcription with JSON response requested
    config = types.GenerateContentConfig(response_mime_type="application/json")
    response = client.models.generate_content(model=model, contents=contents, config=config)

    def _parse_codefenced_json(text: str) -> Optional[Dict[str, Any]]:
        try:
            t = (text or '').strip()
            if t.startswith('```') and t.endswith('```'):
                t = t[3:-3].strip()
                if t.lower().startswith('json'):
                    t = t[4:].lstrip('\n').lstrip()
                return json.loads(t)
            m = re.search(r"```json\s*(\{[\s\S]*?\})\s*```", t)
            if m:
                return json.loads(m.group(1))
            m2 = re.search(r"(\{[\s\S]*?\"segments\"[\s\S]*?\})", t)
            if m2:
                return json.loads(m2.group(1))
        except Exception:
            return None
        return None

    # Parse JSON response
    try:
        transcript = json.loads(_get_response_text(response))
        return transcript
    except Exception:
        salvaged = _parse_codefenced_json(_get_response_text(response))
        if isinstance(salvaged, dict):
            return salvaged
        return {"segments": [], "raw_text": _get_response_text(response)}


def extract_pdf_to_markdown(pdf_path: str) -> str:
    """
    Extract PDF content and convert to markdown format
    
    Args:
        pdf_path: Path to the PDF file
    
    Returns:
        Markdown formatted text
    """
    markdown_text = "## Medical Examination Report\n\n"
    
    # Read PDF
    with open(pdf_path, 'rb') as file:
        pdf_reader = PyPDF2.PdfReader(file)
        
        # Extract text from all pages
        for page_num, page in enumerate(pdf_reader.pages, 1):
            text = page.extract_text()
            
            # Add page header
            markdown_text += f"\n### Page {page_num}\n\n"
            
            # Process text - basic cleanup
            lines = text.split('\n')
            
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                    
                # Detect headers (lines with specific patterns)
                if any(keyword in line for keyword in ['Proposal No.', 'Name Of Member', 'DOB Of Member']):
                    markdown_text += f"**{line}**\n\n"
                elif line.startswith('3.'):
                    markdown_text += f"\n#### {line}\n"
                elif 'YES' in line or 'NO' in line:
                    markdown_text += f"- {line}\n"
                else:
                    markdown_text += f"{line}\n"
    
    return markdown_text


def generate_qa_prompt(transcript: Dict, mer_markdown: str) -> str:
    """
    Generate prompt for QA analysis
    """
    return f"""
You are a medical QA auditor for MediBuddy. Analyze the conversation transcript against the MER documentation.

## TRANSCRIPT:
{json.dumps(transcript, indent=2)}

## MER DOCUMENTATION:
{mer_markdown}

## TASK:
Compare the conversation transcript with the MER form to identify:
1. All questions that should have been asked (from MER template)
2. What was actually asked and answered in the conversation
3. Any discrepancies, missing questions, or incorrect information

Additionally, extract and include the following (keep remaining instructions unchanged). Do NOT include any audio/video technical checks in the output (no technical_status, no video_analysis) — those will be computed separately and merged later:
- Personal Particulars: name, date of birth, ID proof details (PAN/Aadhar/Passport/DL/Voter ID/OCI; allow full numbers or last few digits; alphanumeric allowed), nominee name, nominee date of birth. Cross-verify with MER and add a boolean field present_in_mer for each item; if the item is not available in MER, mark present_in_mer=false and keep value from transcript if mentioned, else null. If not present in MER, treat as not applicable.
- Process Compliance (not part of MER but mandatory): Disclaimer reading at the start (capture whether stated, insurer name, and timestamp) and Language Preference asked at the start (capture whether asked, selected language, and timestamp).
- Behavioral Flags: detect rare customer-side prompting/coaching where a third party speaks for or guides the customer, and the customer merely repeats or reads out statements. Look for patterns like background voice feeding answers, customer repeating phrasing verbatim after a pause, or side confirmations. Do not mark normal clarifications. Also detect customer hesitation (only explicit refusal/evasion). Provide brief explanations and timestamps.
- Do NOT perform documentation spelling/typo checks here. That will be evaluated separately from the MER text only and merged later.

Represent these as top-level objects personal_particulars and process_compliance in the JSON output.

Generate a comprehensive QA report in the following JSON format:

{{
    "personal_particulars": {{
        "name": "string|null",
        "dob": "string|null",
        "id_proofs": [
            {{"type":"PAN|Aadhar|Passport|DL|VoterID|OCI","value":"string","present_in_mer": true/false}}
        ],
        "nominee_name": "string|null",
        "nominee_dob": "string|null"
    }},
    "process_compliance": {{
        "disclaimer": {{"stated": true/false, "insurer_name": "string|null", "timestamp": "mm:ss|null"}},
        "language_preference": {{"asked": true/false, "selected_language": "string|null", "timestamp": "mm:ss|null"}}
    }},
    "qa_matrix": [
        {{
            "question_id": "string (e.g., 3.1, 3.2)",
            "question_text": "The question from MER",
            "captured_response": "What was said in conversation (null if not asked)",
            "expected_response": "What's documented in MER",
            "status": "Correct|Incorrect|Missing|Paraphrased|Clubbed|NA",
            "error_type": "Missing Question|Incorrect Data|Mismapped|Incomplete|null",
            "suggested_correction": "What should be corrected (if any)",
            "timestamp": "When it was asked in conversation (mm:ss format)",
            "typo_in_expected_response": {{
                "has_typo": true/false,
                "corrected_text": "string|null"
            }}
        }}
    ],
    "summary": {{
        "critical_issues": ["list of critical problems found"],
        "recommendations": ["list of improvement suggestions"]
    }},
    "sentiment_analysis": {{
        "professional_tone": true/false,
        "inappropriate_language": ["any instances found"],
        "customer_satisfaction_indicators": ["positive or negative indicators"]
    }},
    "data_validation": {{
        "height_cm": "value from conversation",
        "weight_kg": "value from conversation", 
        "dates_mentioned": ["all dates captured"],
        "medications": ["all medications mentioned"],
        "validation_errors": ["any data validation issues"]
    }},
    "behavioral_flags": {{
        "prompting_detected": {{"value": true/false, "timestamps": ["00:00"], "examples": ["..."]}},
        "customer_hesitation": {{"value": true/false, "timestamps": ["00:00"], "examples": ["..."]}}
    }},
    "documentation_quality": {{
        "spelling_errors_count": 0,
        "typos_found": [],
        "notes": "not evaluated in this step"
    }}
}}

IMPORTANT RULES:
- Check EVERY question in the MER document
- Mark as "Missing" if question wasn't asked at all
- Mark as "Incorrect" if answer differs from MER
- Mark as "Paraphrased" if question was asked differently but captured same info
- Mark as "Clubbed" if multiple questions were combined
- Calculate compliance score as: (correctly_captured_questions / total_questions) * 100
- For Personal Particulars: cross-verify with MER; if a particular is not present in MER, mark "present_in_mer": false and keep value from transcript if stated, else null. Include alphanumeric IDs, allow full or partial numbers.
- For Process Compliance: identify whether the disclaimer (with insurer name) and language preference were performed at the start, and capture timestamps.

DO NOT penalize purely formatting/spacing/case differences in documentation when content matches semantically. Treat such as Correct.

For the key "typo_in_expected_response":
- Evaluate ONLY the expected_response (MER/doctor-entered text) for true spelling mistakes.
- Ignore ALL spacing differences and ALL capitalization/case differences; grammar is out of scope.
- Set has_typo=true only if the word(s) are genuinely misspelled (wrong letters/order), otherwise false.
- If has_typo=true, provide corrected_text with your best normalized correction; else null.
For hesitation, only mark when the customer refuses to answer, does not answer, or repeatedly evades a question; do NOT mark normal uncertainty phrases (e.g., "maybe", "I think") as hesitation by themselves.

Sibling applicability: infer number of siblings mentioned in the transcript (e.g., 0/1/2/3...). Only those many sibling sub-questions are applicable. For any additional sibling entries that do not exist for this customer, set expected_response = "NA" and status = "NA" (not applicable), and do not penalize.

Prompting: this is a rare event and refers to customer-side third-party coaching. Flag only when a third party appears to be speaking to/for the customer (background voice feeding answers, customer echoing dictated text). Do NOT flag general doctor guidance or rephrasing.

Height unit rule:
- If height is stated in feet/inches (e.g., 5'8" or 5 ft 8 in), convert to centimeters (1 inch = 2.54 cm; 1 foot = 12 inches). Compare the converted value with MER height in cm with a tolerance of ±1 cm for rounding. If it matches within tolerance, mark the height question as Correct.

DOB normalization rules:
- Customers or doctors may speak DOB quickly as digits like "11993" or phrases like "one one 1992". Interpret these as DD-MM-YYYY or DD-MMM-YYYY with DD=01 and MM=Jan when only year is clearly specified as 1993/1992 and preceding digits imply 01 Jan. For examples: "11993" -> 01-Jan-1993; "one one 1992" -> 01-Jan-1992. Use reasonable inference and cross-check with context.

Additional metadata to extract (top-level "meta"):
- proposal_number or member_id (string)
- doctor_name (string)
- customer_name (string)
- insurance_company (string)

Personal Particulars must also be VERIFIED like usual questions and reflected inside qa_matrix (only for items present in MER):
- Use these standard question_ids and texts:
  - PP.Name: "Member Name"
  - PP.DOB: "Member Date of Birth"
  - PP.ID.<TYPE>: "ID Proof <TYPE>" (e.g., PP.ID.PAN, PP.ID.Aadhar, PP.ID.Passport, PP.ID.DL, PP.ID.VoterID, PP.ID.OCI)
  - PP.NomineeName: "Nominee Name"
  - PP.NomineeDOB: "Nominee Date of Birth"
- For each included PP item: set captured_response from the transcript, expected_response from MER, and status as Correct/Incorrect/Missing under normal rules. If a PP item is not in MER, do NOT add it to qa_matrix.
- For ID proofs, preserve values exactly as heard or summarized (full or last few digits; alphanumeric allowed); treat minor spacing/case as equivalent when comparing.

Note: Do NOT compute or return numeric summary counts (totals, asked, missed, incorrect, paraphrased, clubbed, overall score). These will be computed locally from the returned JSON across all sections.
"""


def analyze_qa(transcript: Dict, mer_markdown: str, api_key: str) -> Dict:
    """
    Perform QA analysis using Gemini
    
    Args:
        transcript: Transcription JSON
        mer_markdown: MER document in markdown
        api_key: Gemini API key
    
    Returns:
        QA analysis report
    """
    client = genai.Client(api_key=api_key)
    model = "gemini-2.5-flash"
    
    prompt = generate_qa_prompt(transcript, mer_markdown)
    
    contents = [
        types.Content(
            role="user",
            parts=[
                types.Part.from_text(text=prompt),
            ],
        ),
    ]
    
    generate_content_config = types.GenerateContentConfig(
        temperature=0.1,  # Low temperature for consistent analysis
        response_mime_type="application/json"  # Request JSON response
    )
    
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=generate_content_config,
    )
    
    # Parse JSON response robustly
    try:
        raw = _get_response_text(response)
        if not isinstance(raw, str) or not raw.strip():
            raw = getattr(response, 'text', '') or ''
        # handle fenced JSON
        t = raw.strip()
        if t.startswith('```') and t.endswith('```'):
            t = t[3:-3].strip()
            if t.lower().startswith('json'):
                t = t[4:].lstrip('\n').lstrip()
        return json.loads(t)
    except Exception:
        return {"error": "Failed to parse QA analysis", "raw_response": _get_response_text(response)}


def save_qa_report(qa_report: Dict, output_path: str):
    """
    Save QA report to JSON file
    
    Args:
        qa_report: QA analysis report
        output_path: Path to save the report
    """
    # Try to append WPM computed from transcript if available in working dir
    try:
        transcript_path = os.environ.get("TRANSCRIPT_PATH", "/Users/vishalsharma/Downloads/medibuddy/transcript.json")
        if os.path.exists(transcript_path):
            with open(transcript_path, 'r', encoding='utf-8') as tf:
                tdata = json.load(tf)
            # If transcript is stored as codefenced JSON inside raw_text, parse it
            if isinstance(tdata, dict) and 'segments' not in tdata and isinstance(tdata.get('raw_text'), str):
                raw = tdata['raw_text'].strip()
                if raw.startswith('```') and raw.endswith('```'):
                    raw = raw[3:-3].strip()
                    if raw.lower().startswith('json'):
                        raw = raw[4:].lstrip('\n').lstrip()
                try:
                    parsed_inner = json.loads(raw)
                    if isinstance(parsed_inner, dict) and parsed_inner.get('segments'):
                        tdata = parsed_inner
                except Exception:
                    pass
            # Lightweight WPM calc to embed into report meta
            def parse_ts(ts: str):
                try:
                    m, s = ts.split(':'); return int(m)*60+int(s)
                except Exception:
                    return None
            totals = {'doctor_words':0,'doctor_seconds':0.0,'customer_words':0,'customer_seconds':0.0}
            for seg in tdata.get('segments', []) or []:
                sp = str(seg.get('speaker','')).lower()
                role = 'doctor' if sp in ('doctor','agent') else ('customer' if sp=='customer' else None)
                if not role: continue
                start = parse_ts(seg.get('start_timestamp','') or '')
                end = parse_ts(seg.get('end_timestamp','') or '')
                if start is None or end is None or end<=start: continue
                text = str(seg.get('text','') or '')
                totals[f'{role}_words'] += len([w for w in text.strip().split() if w])
                totals[f'{role}_seconds'] += (end-start)
            def wpm(words, secs):
                return round((words / (secs/60.0)), 2) if secs>0 else 0.0
            meta_wpm = {
                'doctor_wpm': wpm(totals['doctor_words'], totals['doctor_seconds']),
                'customer_wpm': wpm(totals['customer_words'], totals['customer_seconds']),
            }
            qa_report.setdefault('meta', {}).update(meta_wpm)
    except Exception:
        pass
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(qa_report, f, indent=2, ensure_ascii=False)
    print(f"QA report saved to: {output_path}")


def get_qc_part2_prompt(transcript: Dict) -> str:
    return f"""
Analyze the medical verification call transcript and extract quality control parameters per the schema below.
Only use transcript evidence and provide timestamps as proof. Return STRICT JSON matching the exact schema.
Empathy is only required for serious health concerns (accident, operation, severe illness). Otherwise mark Yes by default.

Hesitation rule: Only mark hesitation if the customer refuses to answer, gives no answer, or repeatedly evades a question after being asked. Do not mark generic uncertainty (e.g., "maybe", "I think") as hesitation.

Prompting rule: This is a rare, customer-side third-party coaching situation. Flag only when a third party appears to feed answers to the customer (background voice dictating, customer repeating phrases verbatim). Do NOT flag normal doctor guidance.

TRANSCRIPT JSON:
{json.dumps(transcript, indent=2)}

OUTPUT SCHEMA:
{{
  "qc_parameters": {{
    "greetings": {{"value": "Yes/No", "explanation": "", "timestamps": ["00:00"]}},
    "call_opening": {{
      "value": "Yes/Partial/No",
      "explanation": "",
      "timestamps": {{"self_intro": "00:00", "client_name": "00:00", "insurer_name": "00:00"}}
    }},
    "language_preference": {{"value": "Yes/No", "explanation": "", "timestamp": "00:00"}},
    "id_validation": {{"value": "Yes/No", "explanation": "", "timestamps": ["00:00"]}},
    "disclaimer": {{"value": "Yes/No", "explanation": "", "timestamp": "00:00"}},
    "politeness": {{"value": "Yes/Partial/No", "explanation": "", "timestamps": ["00:00", "00:00"]}},
    "empathy": {{"value": "Yes/No/NA", "explanation": "", "timestamps": ["00:00"]}},
    "communication_skills": {{
      "value": "Yes/Partial/No",
      "explanation": "",
      "timestamps": {{"good_examples": ["00:00"], "poor_examples": ["00:00"]}}
    }},
    "probing": {{"value": "Yes/No/NA", "explanation": "", "timestamps": ["00:00"]}},
    "observations": {{"value": "Yes/No/NA", "explanation": "", "timestamps": ["00:00"]}},
    "call_closure": {{
      "value": "Yes/Partial/No",
      "explanation": "",
      "timestamps": {{"declaration": "00:00", "thank_you": "00:00"}}
    }}
  }}
}}

Guidelines: 1) Be objective; 2) Provide timestamps; 3) Use NA when not applicable; 4) Keep explanations ≤ 50 words; 5) Focus on doctor's performance.
"""


def analyze_qc_part2(transcript: Dict, api_key: str) -> Dict:
    client = genai.Client(api_key=api_key)
    model = "gemini-2.5-flash"
    prompt = get_qc_part2_prompt(transcript)
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]
    config = types.GenerateContentConfig(temperature=0.1, response_mime_type="application/json")
    response = client.models.generate_content(model=model, contents=contents, config=config)
    try:
        return json.loads(response.text)
    except json.JSONDecodeError:
        return {"error": "Failed to parse QC part 2", "raw_response": response.text}


def save_qc_part2(qc_report: Dict, output_path: str):
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(qc_report, f, indent=2, ensure_ascii=False)
    print(f"QC Part 2 saved to: {output_path}")

def get_mer_typo_prompt(mer_markdown: str) -> str:
    return f"""
You are checking only the MER (Medical Examination Report) text for spelling mistakes that the doctor may have typed/filled.

Rules (strict):
- Consider ONLY fields/lines that are clearly doctor-entered values (names, addresses, free-text notes, medications typed, comments). Ignore template headers, labels, option lists and system-generated placeholders.
- Ignore ALL spacing differences and ALL capitalization/case differences. Grammar is out of scope. Only report genuine misspellings (wrong letters/order).
- When in doubt whether a token is a template label vs filled value, default to not flagging it.

MER TEXT:
{mer_markdown}

Return STRICT JSON:
{{
  "documentation_quality": {{
    "spelling_errors_count": number,
    "typos_found": ["list a few misspelled words or phrases that are clearly doctor-entered"],
    "notes": "short explanation of the approach and examples context if helpful"
  }}
}}
"""

def analyze_mer_typos(mer_markdown: str, api_key: str) -> Dict:
    client = genai.Client(api_key=api_key)
    model = "gemini-2.5-flash"
    prompt = get_mer_typo_prompt(mer_markdown)
    contents = [types.Content(role="user", parts=[types.Part.from_text(text=prompt)])]
    config = types.GenerateContentConfig(temperature=0.1, response_mime_type="application/json")
    resp = client.models.generate_content(model=model, contents=contents, config=config)
    try:
        return json.loads(resp.text)
    except json.JSONDecodeError:
        return {"documentation_quality": {"spelling_errors_count": 0, "typos_found": [], "notes": "parse_error"}, "raw_response": resp.text}
def save_transcript(transcript: Dict, output_path: str):
    """
    Save transcript to JSON file
    
    Args:
        transcript: Transcription JSON
        output_path: Path to save the transcript
    """
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(transcript, f, indent=2, ensure_ascii=False)
    print(f"Transcript saved to: {output_path}")


def process_multi_call_record(record_dir: str, api_key: str, output_dir: Optional[str] = None, skip_transcription: bool = False) -> Dict[str, Any]:
    """Process a complete record with multiple calls"""
    record_path = Path(record_dir)
    if not record_path.exists():
        raise FileNotFoundError(f"Record directory not found: {record_dir}")
    
    record_id = record_path.name
    log_progress(f"Processing record: {record_id}", 0, 10)
    
    # Setup output directory
    if output_dir:
        out_path = Path(output_dir)
    else:
        out_path = record_path / "_processed"
    out_path.mkdir(parents=True, exist_ok=True)
    
    # Find MER PDF
    mer_files = list(record_path.glob("*_MER.pdf"))
    if not mer_files:
        raise FileNotFoundError(f"MER PDF not found in {record_dir}")
    
    mer_pdf = mer_files[0]
    log_progress(f"Found MER: {mer_pdf.name}", 1, 10)
    
    # Extract MER content
    mer_markdown = extract_pdf_to_markdown(str(mer_pdf))
    log_progress("MER extraction completed", 2, 10)
    
    # Find media files
    media_files = find_media_files(record_path)
    if not media_files:
        raise FileNotFoundError(f"No media files found in {record_dir}")
    
    log_progress(f"Found {len(media_files)} media files", 3, 10)
    
    # Process each media file
    transcripts = []
    call_durations = []
    individual_results = []
    
    for i, media_file in enumerate(media_files, 1):
        log_progress(f"Processing call {i}: {media_file.name}", 3 + i, 10)
        
        call_dir = out_path / f"call{i}"
        call_dir.mkdir(parents=True, exist_ok=True)
        
        # Handle video files - extract audio
        audio_path = str(media_file)
        if media_file.suffix.lower() in {'.mp4', '.webm', '.mov'}:
            extracted_audio = call_dir / "audio.mp3"
            if extract_audio_from_video(str(media_file), str(extracted_audio)):
                audio_path = str(extracted_audio)
            else:
                print(f"Warning: Could not extract audio from {media_file.name}")
        
        # Get duration
        duration = get_media_duration(audio_path) or 0.0
        call_durations.append(duration)

        # Technical analysis (audio)
        tech_analysis = analyze_audio_technical(audio_path)

        # Video analysis (only if original is video)
        video_analysis = {"attire_check": "unknown", "visibility_status": "unknown", "privacy_maintained": None, "screenshots": []}
        is_video = media_file.suffix.lower() in {'.mp4', '.webm', '.mov'}
        if is_video:
            frames = extract_video_frames(str(media_file), call_dir)
            if frames:
                video_analysis = analyze_video_frames(frames, api_key)

        # Transcribe or load existing
        try:
            transcript_path = call_dir / "transcript.json"
            transcript = None
            if skip_transcription and transcript_path.exists():
                with open(transcript_path, 'r') as f:
                    transcript = json.load(f)
            elif skip_transcription and not transcript_path.exists():
                transcript = {"segments": []}
            else:
                if duration and duration > 900:
                    chunks_dir = call_dir / 'chunks'
                    chunk_paths = split_audio_into_chunks(audio_path, chunks_dir, chunk_seconds=300)
                    print(f"Call {i}: duration {int(duration)}s -> chunking into {len(chunk_paths)} parts")
                    transcript = transcribe_chunks_and_merge(chunk_paths, api_key)
                else:
                    transcript = transcribe_audio(audio_path, api_key)
                with open(transcript_path, 'w') as f:
                    json.dump(transcript, f, indent=2)

            transcripts.append(transcript)

            individual_results.append({
                "call_index": i,
                "media_file": str(media_file),
                "audio_path": audio_path,
                "duration": duration,
                "transcript_path": str(transcript_path),
                "segments_count": len((transcript or {}).get('segments', [])),
                "technical_analysis": tech_analysis,
                "video_analysis": video_analysis,
            })
        except Exception as e:
            print(f"Transcription failed for call {i}: {e}")
            transcripts.append({"segments": []})
            call_durations[-1] = 0.0
    
    # Merge all transcripts
    log_progress("Merging transcripts", 7, 10)
    merged_transcript = merge_transcripts(transcripts, call_durations)
    
    # Save merged transcript
    merged_transcript_path = out_path / "merged_transcript.json"
    with open(merged_transcript_path, 'w') as f:
        json.dump(merged_transcript, f, indent=2)
    
    # Save a human-readable merged transcript text for LLM prompts
    merged_text = build_merged_transcript_text(merged_transcript)
    merged_text_path = out_path / "merged_transcript.txt"
    try:
        with open(merged_text_path, 'w') as tf:
            tf.write(merged_text)
    except Exception:
        pass

    # QA Part 1 Analysis using merged transcript (or existing text override)
    log_progress("Running QA Part 1 analysis on merged transcript", 8, 10)
    try:
        # If existing text file present, use it; else use newly built
        try:
            with open(out_path / 'merged_transcript.txt', 'r') as tf:
                txt_override = tf.read()
        except Exception:
            txt_override = merged_text
        qa_part1 = analyze_qa({"raw_text": txt_override}, mer_markdown, api_key)
        # Inject technical_status and video_analysis based on longest call's analysis
        # Select the longest call for representative tech/video
        longest = None
        for c in individual_results:
            if (longest is None) or (float(c.get("duration") or 0.0) > float(longest.get("duration") or 0.0)):
                longest = c
        tech = (longest or {}).get("technical_analysis") or {}
        vid = (longest or {}).get("video_analysis") or {}
        # If no video frames (audio-only), hard set NA defaults
        def _na_video(v: dict) -> dict:
            if not v or not isinstance(v, dict):
                return {"attire_check": "NA", "visibility_status": "NA", "privacy_maintained": "NA", "screenshots": []}
            # Treat unknown/None as NA for audio-only
            if not v.get("screenshots"):
                return {"attire_check": "NA", "visibility_status": "NA", "privacy_maintained": "NA", "screenshots": []}
            return v
        qa_part1.setdefault('technical_status', {})
        qa_part1['technical_status'].update({
            'recording_exists': bool(tech.get('recording_exists', True)),
            'audibility_level': tech.get('audibility_level', 'unknown'),
            'avg_dbfs': tech.get('avg_dbfs')
        })
        qa_part1.setdefault('video_analysis', {})
        qa_part1['video_analysis'].update(_na_video(vid))
        qa_part1_path = out_path / "merged_qa_report.json"
        save_qa_report(qa_part1, str(qa_part1_path))
    except Exception as e:
        print(f"QA Part 1 failed: {e}")
        qa_part1 = {}
    
    # QA Part 2 Analysis using merged transcript text (or existing override)
    log_progress("Running QA Part 2 analysis on merged transcript", 9, 10)
    try:
        try:
            with open(out_path / 'merged_transcript.txt', 'r') as tf:
                txt_override = tf.read()
        except Exception:
            txt_override = merged_text
        qa_part2 = analyze_qc_part2({"raw_text": txt_override}, api_key)
        qa_part2_path = out_path / "merged_qa_report_part2.json"
        save_qc_part2(qa_part2, str(qa_part2_path))
    except Exception as e:
        print(f"QA Part 2 failed: {e}")
        qa_part2 = {}
    
    log_progress("Processing completed!", 10, 10)
    
    # Compile results
    results = {
        "record_id": record_id,
        "mer_pdf": str(mer_pdf),
        "total_calls": len(media_files),
        "total_duration": sum(call_durations),
        "individual_calls": individual_results,
        "merged_transcript": {
            "path": str(merged_transcript_path),
            "total_segments": len(merged_transcript.get('segments', [])),
            "total_duration": sum(call_durations)
        },
        "qa_part1": qa_part1,
        "qa_part2": qa_part2,
        "output_directory": str(out_path)
    }
    
    # Save processing summary
    summary_path = out_path / "processing_summary.json"
    with open(summary_path, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    
    return results


def main():
    """
    Main execution function - supports both single file and multi-call processing
    """
    # CLI arguments
    parser = argparse.ArgumentParser(description="MediBuddy MER QA analyzer with multi-call support")
    parser.add_argument("--audio", dest="audio", help="Path to single audio file (legacy mode)")
    parser.add_argument("--mer", dest="mer", help="Path to MER PDF file (legacy mode)")
    parser.add_argument("--record-dir", dest="record_dir", help="Path to record directory containing MER PDF and media files")
    parser.add_argument("--skip-transcription", dest="skip_transcription", action='store_true', help="Use existing transcripts if present and skip re-transcription")
    parser.add_argument("--output", dest="output", help="Output directory or file path")
    parser.add_argument("--transcript-output", dest="transcript_output", default="transcript.json", help="Path to save transcript JSON (legacy mode)")
    parser.add_argument("--api-key", dest="api_key", required=True, help="Gemini API key")
    args = parser.parse_args()

    API_KEY = args.api_key
    
    print("Starting MediBuddy QA Analysis...")
    
    # Multi-call processing mode
    if args.record_dir:
        results = process_multi_call_record(args.record_dir, API_KEY, args.output, skip_transcription=args.skip_transcription)
        
        # Print summary
        print("\n" + "="*60)
        print("MULTI-CALL PROCESSING COMPLETED")
        print("="*60)
        print(f"Record ID: {results['record_id']}")
        print(f"Total calls processed: {results['total_calls']}")
        print(f"Total duration: {results['total_duration']:.1f} seconds")
        print(f"Merged transcript segments: {results['merged_transcript']['total_segments']}")
        
        for call in results['individual_calls']:
            print(f"\nCall {call['call_index']}:")
            print(f"  File: {Path(call['media_file']).name}")
            print(f"  Duration: {call['duration']:.1f}s")
            print(f"  Segments: {call['segments_count']}")
        
        if results['qa_part1']:
            summary = results['qa_part1'].get('summary', {})
            if summary.get('critical_issues'):
                print(f"\nCritical Issues: {len(summary['critical_issues'])}")
            if summary.get('recommendations'):
                print(f"Recommendations: {len(summary['recommendations'])}")
        
        print(f"\nOutput directory: {results['output_directory']}")
        return results
    
    # Legacy single-file mode
    elif args.audio and args.mer:
        audio_file = args.audio
        mer_pdf_file = args.mer
        output_file = args.output or "qa_report.json"

        # Validate inputs
        if not Path(audio_file).exists():
            raise FileNotFoundError(f"Audio file not found: {audio_file}")
        if not Path(mer_pdf_file).exists():
            raise FileNotFoundError(f"MER PDF not found: {mer_pdf_file}")
        
        # Step 1: Transcribe audio
        print("\n1. Transcribing audio...")
        transcript = transcribe_audio(audio_file, API_KEY)
        print(f"   Transcription complete. Found {len(transcript.get('segments', []))} segments.")
        save_transcript(transcript, args.transcript_output)
        
        # Step 2: Extract MER to markdown
        print("\n2. Extracting MER document...")
        mer_markdown = extract_pdf_to_markdown(mer_pdf_file)
        print(f"   MER extraction complete. Document length: {len(mer_markdown)} characters.")
        
        # Step 3: Perform QA Part 1 analysis
        print("\n3. Performing QA Part 1 analysis...")
        qa_report = analyze_qa(transcript, mer_markdown, API_KEY)
        save_qa_report(qa_report, output_file)
        
        # Step 4: Perform QA Part 2 analysis
        print("\n4. Performing QA Part 2 analysis...")
        qa_part2 = analyze_qc_part2(transcript, API_KEY)
        part2_output = output_file.replace('.json', '_part2.json')
        save_qc_part2(qa_part2, part2_output)
        
        # Print summary
        if 'summary' in qa_report:
            summary = qa_report['summary']
            print("\n" + "="*50)
            print("QA ANALYSIS SUMMARY")
            print("="*50)
            print(f"Total Questions: {summary.get('total_questions', 'N/A')}")
            print(f"Questions Asked: {summary.get('questions_asked', 'N/A')}")
            print(f"Questions Missed: {summary.get('questions_missed', 'N/A')}")
            print(f"Incorrect Responses: {summary.get('incorrect_responses', 'N/A')}")
            print(f"Overall Compliance Score: {summary.get('overall_compliance_score', 'N/A')}%")
            
            if summary.get('critical_issues'):
                print("\nCritical Issues:")
                for issue in summary['critical_issues']:
                    print(f"  - {issue}")
            
            if summary.get('recommendations'):
                print("\nRecommendations:")
                for rec in summary['recommendations']:
                    print(f"  - {rec}")
        
        print("\nQA analysis complete!")
        return qa_report
    
    else:
        print("Error: Provide either --record-dir for multi-call processing or both --audio and --mer for single file processing")
        return None


if __name__ == "__main__":
    # Run the analysis
    qa_result = main()
    
    # Optional: Generate a readable report
    if qa_result and 'qa_matrix' in qa_result:
        print("\n" + "="*50)
        print("DETAILED QA MATRIX")
        print("="*50)
        
        for item in qa_result['qa_matrix'][:5]:  # Show first 5 items
            print(f"\nQuestion {item.get('question_id', 'N/A')}: {item.get('question_text', 'N/A')[:50]}...")
            print(f"  Status: {item.get('status', 'N/A')}")
            print(f"  Captured: {item.get('captured_response', 'N/A')[:50] if item.get('captured_response') else 'Not asked'}...")
            print(f"  Expected: {item.get('expected_response', 'N/A')[:50]}...")
            if item.get('error_type'):
                print(f"  Error: {item['error_type']}")
        
        print("\n(Showing first 5 questions. Full report saved to qa_report.json)")