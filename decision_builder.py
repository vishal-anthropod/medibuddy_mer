#!/usr/bin/env python3
import os
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

BASE_DIR = "/Users/vishalsharma/Downloads/medibuddy/reports and recordings"


def load_json(path: Path) -> Dict[str, Any]:
    try:
        if path.exists():
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def to_bool(val: Any) -> Optional[bool]:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        v = val.strip().lower()
        if v in ("yes", "true"): return True
        if v in ("no", "false"): return False
    return None


def get_str(d: Dict[str, Any], *keys: str) -> Optional[str]:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    if isinstance(cur, str):
        return cur
    return None


def summarize_record(rec_dir: Path) -> Dict[str, Any]:
    out_dir = rec_dir / "_processed"
    qa = load_json(out_dir / 'merged_qa_report.json')
    if not qa:
        # fallback: single-call QA if merged not present
        qa = load_json(out_dir / 'call1' / 'qa_report.json')
    qc2 = load_json(out_dir / 'merged_qa_report_part2.json')
    if not qc2:
        qc2 = load_json(out_dir / 'call1' / 'qa_report_part2.json')

    qa_matrix: List[Dict[str, Any]] = qa.get('qa_matrix') or []
    video = qa.get('video_analysis') or {}
    tech = qa.get('technical_status') or {}
    beh = qa.get('behavioral_flags') or {}
    docq = qa.get('documentation_quality') or {}
    dataval = qa.get('data_validation') or {}
    qc_params = (qc2.get('qc_parameters') or {}) if isinstance(qc2, dict) else {}

    issues = {
        'ASSIGNBACK': [],
        'OPS_ATTENTION': [],
        'FLAGS': [],
        'TECH_ISSUES': [],
    }

    # Counts
    incorrect_items = [it for it in qa_matrix if str(it.get('status','')).lower() == 'incorrect']
    # Count typos in expected response if the model provided the field
    typo_items = [it for it in qa_matrix if isinstance(it.get('typo_in_expected_response'), dict) and bool(it['typo_in_expected_response'].get('has_typo'))]
    clubbed_count = sum(1 for it in qa_matrix if str(it.get('status','')).lower() == 'clubbed')
    missing_items = [it for it in qa_matrix if str(it.get('status','')).lower() == 'missing']

    # Helper to add
    def add(cat: str, title: str, detail: Any):
        issues[cat].append({'issue': title, 'details': detail})

    # ASSIGNBACK rules
    if missing_items:
        add('ASSIGNBACK', 'Questions missing', [{'id': it.get('question_id'), 'text': it.get('question_text')} for it in missing_items])

    # PP.Name incorrect
    for it in qa_matrix:
        if str(it.get('question_id','')).strip().lower() == 'pp.name' and str(it.get('status','')).lower() == 'incorrect':
            add('ASSIGNBACK', 'Customer name incorrect (PP.Name)', {'captured': it.get('captured_response'), 'expected': it.get('expected_response')})
            break

    # Missing ID proof verification
    id_missing = [it for it in qa_matrix if str(it.get('question_id','')).lower().startswith('pp.id.') and str(it.get('status','')).lower() == 'missing']
    if id_missing:
        add('ASSIGNBACK', 'Missing ID proof verification', [{'id': it.get('question_id'), 'expected': it.get('expected_response')} for it in id_missing])

    if clubbed_count >= 2:
        add('ASSIGNBACK', '2+ clubbed questions', {'count': clubbed_count})

    # Incorrect documentation entries thresholding
    if len(incorrect_items) >= 8:
        add('ASSIGNBACK', 'Many incorrect documentation entries', {'count': len(incorrect_items)})

    # OPS ATTENTION: multiple typos in expected responses
    if len(typo_items) >= 3:
        add('OPS_ATTENTION', 'Multiple typos in MER entries', {'count': len(typo_items), 'examples': [
            {'id': it.get('question_id'), 'expected': it.get('expected_response'), 'corrected': (it.get('typo_in_expected_response') or {}).get('corrected_text')} for it in typo_items[:5]
        ]})

    # Disclaimer missing
    disc_val = to_bool(get_str(qc_params, 'disclaimer', 'value'))
    if disc_val is False:
        add('ASSIGNBACK', 'Disclaimer missing', {})

    # Self-introduction missing
    call_open = (qc_params.get('call_opening') or {})
    if str(call_open.get('value','')).strip().lower() == 'no':
        add('ASSIGNBACK', 'Doctor self-introduction missing', {})

    # Major prompting
    pr = beh.get('prompting_detected') or {}
    if to_bool(pr.get('value')):
        examples = pr.get('examples') or []
        ts = pr.get('timestamps') or []
        if (isinstance(examples, list) and len(examples) >= 2) or (isinstance(ts, list) and len(ts) >= 2):
            add('ASSIGNBACK', 'Major agent-led prompting detected', {'examples': examples, 'timestamps': ts})

    # Doctor not wearing apron
    attire = str((video.get('attire_check') or '')).strip().lower()
    if attire in ('no','missing_apron','not_wearing_apron'):
        add('ASSIGNBACK', 'Doctor not wearing apron', {'attire_check': video.get('attire_check')})

    # OPS ATTENTION rules
    try:
        if int(docq.get('spelling_errors_count') or 0) >= 3:
            add('OPS_ATTENTION', '3+ spelling errors in MER', {'count': int(docq.get('spelling_errors_count') or 0)})
    except Exception:
        pass

    # DOB incorrect
    for it in qa_matrix:
        if str(it.get('question_id','')).strip().lower() == 'pp.dob' and str(it.get('status','')).lower() == 'incorrect':
            add('OPS_ATTENTION', 'Incorrect date of birth', {'captured': it.get('captured_response'), 'expected': it.get('expected_response')})
            break

    if 4 <= len(incorrect_items) <= 7:
        add('OPS_ATTENTION', '4-7 incorrect documentation entries', {'count': len(incorrect_items)})

    # Occupation 1.4 incorrect
    for it in qa_matrix:
        if str(it.get('question_id','')).strip().lower() in ('1.4','1.4.') and str(it.get('status','')).lower() == 'incorrect':
            add('OPS_ATTENTION', 'Incorrect occupation (1.4)', {'captured': it.get('captured_response'), 'expected': it.get('expected_response')})
            break

    # FLAGS
    if to_bool(pr.get('value')) and not any(x['issue']=='Major agent-led prompting detected' for x in issues['ASSIGNBACK']):
        add('FLAGS', 'Minor prompting detected', {'examples': pr.get('examples'), 'timestamps': pr.get('timestamps')})

    ch = beh.get('customer_hesitation') or {}
    if to_bool(ch.get('value')):
        add('FLAGS', 'Customer hesitation detected', {'examples': ch.get('examples'), 'timestamps': ch.get('timestamps')})

    try:
        h = float(dataval.get('height_cm')) if dataval.get('height_cm') is not None else None
        if h is not None and (h < 130 or h > 210):
            add('FLAGS', 'Height out of range', {'height_cm': h})
    except Exception:
        pass
    try:
        w = float(dataval.get('weight_kg')) if dataval.get('weight_kg') is not None else None
        if w is not None and (w < 35 or w > 150):
            add('FLAGS', 'Weight out of range', {'weight_kg': w})
    except Exception:
        pass

    # Contradictory responses
    contra = []
    for it in qa_matrix:
        cr = str(it.get('captured_response') or '')
        if 'later revealed' in cr.lower():
            contra.append({'id': it.get('question_id'), 'text': it.get('question_text')})
    if contra:
        add('FLAGS', 'Contradictory responses', contra)

    # Privacy breach
    priv = to_bool(video.get('privacy_maintained'))
    if priv is False:
        add('FLAGS', 'Privacy breach in video', {})

    # Unprofessional behavior
    pol = str(get_str(qc_params, 'politeness', 'value') or '').strip().lower()
    if pol in ('no','partial'):
        add('FLAGS', 'Unprofessional behavior (politeness)', {'value': pol})

    # TECH ISSUES
    rec_exists = tech.get('recording_exists')
    if rec_exists is False:
        add('TECH_ISSUES', 'Recording file missing', {})

    aud = str(tech.get('audibility_level') or '').strip().lower()
    if aud in ('poor','inaudible','not_audible'):
        add('TECH_ISSUES', 'Voice not audible', {'audibility_level': tech.get('audibility_level')})

    vis = str((video.get('visibility_status') or '')).strip().lower()
    if vis and vis != 'both_visible':
        add('TECH_ISSUES', 'Not both participants visible', {'visibility_status': video.get('visibility_status')})

    return issues


def main():
    base = Path(BASE_DIR)
    for item in base.iterdir():
        if not item.is_dir():
            continue
        out_dir = item / '_processed'
        if not out_dir.exists():
            continue
        try:
            result = summarize_record(item)
            with open(out_dir / 'final_decision.json', 'w', encoding='utf-8') as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            print(f"final_decision.json written for {item.name}")
        except Exception as e:
            print(f"Failed {item}: {e}")


if __name__ == '__main__':
    main()


