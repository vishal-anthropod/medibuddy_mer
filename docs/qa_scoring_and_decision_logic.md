# MER QA Scoring And Decision Logic

This document explains the current logic used by the MediBuddy MER dashboard for QA Part 1, QA Part 2, aggregated scoring, and final decision categories.

Current refreshed batch summary:

| Category | Count |
| --- | ---: |
| Assignback | 68 |
| Ops Attention | 14 |
| Flags | 0 |
| Tech Issues | 5 |
| Pass | 4 |
| Staged only | 1 |

The staged-only record is `C330718334`, because it has a MER PDF but no media recording.

## Processing Flow

1. Source records are staged under `reports and recordings/<record_id>/`.
2. Each record is transcribed from all audio/video calls.
3. Transcripts are merged for the record.
4. MER PDF text is extracted.
5. QA Part 1 compares transcript answers against MER documentation.
6. QA Part 2 evaluates call quality and process parameters from the transcript.
7. MER typo/spelling checks are run separately against MER text only.
8. `decision_builder.py` creates `final_decision.json`.
9. The UI reads merged QA, QC, transcript, final decision, and S3 links.

## QA Part 1 Logic

QA Part 1 is generated in `medb.py` by comparing the merged call transcript with the MER PDF.

It checks:

| Area | Logic |
| --- | --- |
| MER questions | Every relevant MER question should be checked against what the doctor asked and what the customer answered. |
| Personal particulars | Name, DOB, ID proofs, nominee name, and nominee DOB are cross-verified if present in MER. |
| Process compliance | Disclaimer and language preference are captured with timestamps. |
| Behavioral flags | Customer-side third-party prompting and real customer hesitation are detected only when supported by transcript evidence. |
| Data validation | Height, weight, dates, medications, and validation errors are extracted. |
| Documentation quality | True MER spelling mistakes are merged from the separate typo check. |

QA row statuses:

| Status | Meaning | Accuracy treatment |
| --- | --- | --- |
| Correct | Asked/captured and matches MER. | Counts in numerator and denominator. |
| Paraphrased | Same information captured using different wording. | Counts in numerator and denominator. |
| Incorrect | Captured answer differs from MER, or row is incomplete. | Counts in denominator only. |
| Missing | Required question was not asked. | Counts in denominator only. |
| Clubbed | Multiple questions were combined. | Counts in denominator only. |
| NA | Not applicable for this customer/context. | Removed from denominator. |

Important normalization rules:

| Rule | Behavior |
| --- | --- |
| NA rows | Removed from the scoring denominator. |
| Paraphrased rows | Accepted as correct for accuracy. |
| Incomplete rows | Normalized as incorrect. |
| Formatting differences | Spacing/case-only differences are not penalized. |
| Height | Feet/inches are converted to cm and compared with +/- 1 cm tolerance. |
| DOB | Spoken shorthand like `11993` or `one one 1992` can be interpreted as `01-Jan-1993` / `01-Jan-1992` when context supports it. |
| Gender applicability | Female-only questions are marked NA for male customers. |
| Sibling applicability | Extra sibling question slots beyond the actual siblings are marked NA. |

Accuracy formula:

```text
Accuracy = (Correct + Paraphrased accepted) / (Total MER questions - NA questions) * 100
```

UI breakdown example:

```text
Total MER questions       33
Not applicable             3
Scored questions          30
Correct                   27
Paraphrased accepted       2
Incorrect / incomplete     1
Accuracy                  29 / 30 = 96.67%
```

## MER Spelling / Typo Logic

Spelling is intentionally strict and rare.

It checks only doctor-entered MER values such as names, addresses, notes, medication text, comments, and free-text fields.

It ignores:

| Ignored item | Example |
| --- | --- |
| OCR/PDF spacing artifacts | `declar e` -> `declare` |
| Split words caused by extraction | `fur nished`, `infor mation`, `af ter` |
| Split names/places caused by extraction | `MAHAR ANA PR ATAP`, `K ANPUR`, `RAIBAREILL Y` |
| Capitalization differences | `amit kumar` vs `Amit Kumar` |
| Grammar/style issues | Wording differences without spelling error |
| Template labels/system text | Headers, option labels, static boilerplate |

A spelling issue is counted only when the letters/order are genuinely wrong after removing extra spaces.

## QA Part 2 Logic

QA Part 2 is generated from transcript evidence and evaluates doctor call quality.

Parameters:

| Parameter | Values | Scoring behavior |
| --- | --- | --- |
| Greetings | Yes/No | Yes = 100, No = 0 |
| Call opening | Yes/Partial/No | Yes = 100, Partial = 50, No = 0 |
| Language preference | Yes/No | Yes = 100, No = 0 |
| ID validation | Yes/No | Yes = 100, No = 0 |
| Disclaimer | Yes/No | Yes = 100, No = 0 |
| Politeness | Yes/Partial/No | Yes = 100, Partial = 50, No = 0 |
| Empathy | Yes/No/NA | Yes/NA = 100, No = 0 |
| Communication skills | Yes/Partial/No | Yes = 100, Partial = 50, No = 0 |
| Probing | Yes/No/NA | Yes/NA = 100, No = 0 |
| Observations | Yes/No/NA | Yes/NA = 100. Also scored 100 if explanation says no special observation was present. |
| Call closure | Yes/Partial/No | Yes = 100, Partial = 50, No = 0 |

Special QA Part 2 rules:

| Rule | Behavior |
| --- | --- |
| Empathy | Required only for serious health concerns. Otherwise NA/Yes is acceptable. |
| Hesitation | Mark only if customer refuses, gives no answer, or repeatedly evades. Normal uncertainty is not hesitation. |
| Prompting | Means customer-side third-party coaching only. Normal doctor clarification or rephrasing is not prompting. |
| Observations | Use NA when there are no special customer-side observations to report. |
| Timestamps | Required as evidence wherever possible. |

## Aggregated Scoring Logic

Aggregated score is computed locally in `app.py` from QA Part 1, QA Part 2, transcript duration, and doctor speaking rate.

There are 16 scoring components, each worth up to 100 points:

| Component | Source |
| --- | --- |
| Greetings | QA Part 2 |
| Call opening | QA Part 2 |
| Language preference | QA Part 2 |
| ID validation | QA Part 2 |
| Disclaimer | QA Part 2 |
| Politeness | QA Part 2 |
| Empathy | QA Part 2 |
| Communication skills | QA Part 2 |
| Probing | QA Part 2 |
| Observations | QA Part 2 |
| Call closure | QA Part 2 |
| Complete MER questions | QA Part 1 completion percentage |
| Correct documentation | QA Part 1 accuracy percentage |
| Call duration | Transcript/media duration |
| Rate of speech | Doctor WPM |
| Visual presentation | Currently defaults to 100 |

Maximum score:

```text
16 components * 100 = 1600
```

Percentage:

```text
Aggregated percentage = total_score / 1600 * 100
```

Category bands:

| Total score | Category |
| ---: | --- |
| 1500 and above | Good |
| 1400-1499 | Above Average |
| 1300-1399 | Average |
| Below 1300 | Poor |

QA-derived scoring bands:

| Percentage | Score |
| ---: | ---: |
| 100% | 100 |
| 95%-99.99% | 80 |
| 85%-94.99% | 60 |
| 70%-84.99% | 40 |
| Below 70% | 20 |

Call duration scoring:

| Duration | Score |
| --- | ---: |
| 10 minutes or more | 100 |
| 7 to under 10 minutes | 70 |
| Under 7 minutes | 30 |

Doctor rate-of-speech scoring:

| Doctor WPM | Score |
| --- | ---: |
| 120-160 | 100 |
| 100-119 or 161-180 | 70 |
| 80-99 or 181-200 | 30 |
| Below 80 or above 200 | 0 |
| Missing/zero WPM | 50 |

## Final Decision Bucket Logic

Final decision is generated in `decision_builder.py`. The UI category is chosen in this priority order:

```text
Assignback > Ops Attention > Tech Issues > Flags > Pass
```

That means if a record has both Assignback and Ops Attention issues, it appears as Assignback.

### Assignback

Record is marked Assignback if any of these are found:

| Trigger | Logic |
| --- | --- |
| Questions missing | Any required QA row is Missing. |
| Customer name incorrect | `PP.Name` is Incorrect. |
| Missing ID proof verification | Any `PP.ID.*` row is Missing. |
| 2+ clubbed questions | Clubbed question count is 2 or more. |
| Many incorrect documentation entries | Incorrect QA rows are 8 or more. |
| Disclaimer missing | QA Part 2 disclaimer value is No. |
| Doctor self-introduction missing | QA Part 2 call opening value is No. |
| Major prompting | Customer-side prompting has at least 2 examples or timestamps. |
| Doctor not wearing apron | Video attire check says no/missing apron. |

### Ops Attention

Record is marked Ops Attention if any of these are found and Assignback is not already higher priority:

| Trigger | Logic |
| --- | --- |
| Multiple typos in MER entries | 3 or more QA rows have true typo flags. |
| 3+ spelling errors in MER | Documentation quality spelling count is 3 or more. |
| Incorrect DOB | `PP.DOB` is Incorrect. |
| 4-7 incorrect documentation entries | Incorrect QA rows are between 4 and 7. |
| Incorrect occupation | QA question `1.4` is Incorrect. |

### Flags

Record is marked Flags if any of these are found and higher-priority categories are empty:

| Trigger | Logic |
| --- | --- |
| Minor prompting | Prompting detected but not major enough for Assignback. |
| Customer hesitation | Customer-side hesitation detected. |
| Height out of range | Height below 130 cm or above 210 cm. |
| Weight out of range | Weight below 35 kg or above 150 kg. |
| Contradictory responses | Captured response contains contradiction language such as `later revealed`. |
| Privacy breach in video | Video privacy check is false. |
| Unprofessional behavior | QA Part 2 politeness is No or Partial. |

### Tech Issues

Record is marked Tech Issues if any of these are found and Assignback/Ops Attention are not already higher priority:

| Trigger | Logic |
| --- | --- |
| Recording missing | Technical status says recording does not exist. |
| Voice not audible | Audibility is poor/inaudible/not audible. |
| Not both participants visible | Video visibility status is not `both_visible`. |

### Pass

Record is marked Pass when Assignback, Ops Attention, Tech Issues, and Flags are all empty.

## Storage / UI Link Logic

The current batch uses S3-backed local pointer files:

| Item | Behavior |
| --- | --- |
| MER PDF | Local file contains a presigned S3 URL pointer. UI route redirects to S3. |
| Audio/video recording | Local file contains a presigned S3 URL pointer. UI route redirects to S3. |
| S3 bucket | `anthropod` |
| S3 prefix | `temp/tata_aia` |
| Manifest | `s3_manifest.json` |

The UI should use app routes such as `/api/records/<record_id>/mer` and `/api/records/<record_id>/calls/<index>/audio`, not raw local files.
