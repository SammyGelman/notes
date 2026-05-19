# phi_tagging

PHI flagging and RxNorm/CVX mapping for unmapped OMOP drug source concepts. A three-stage pipeline against the self-hosted Nemotron-3-Super endpoint on Minerva (same B200 vLLM server used by `clinical_extractor`).

The input is a CSV of `(DRUG_SOURCE_CONCEPT_ID, DRUG_SOURCE_CONCEPT_NAME, DRUG_CONCEPT_ID, DRUG_CONCEPT_NAME)` tuples where most rows have `DRUG_CONCEPT_ID = 0` ("No matching concept"). These are legacy free-text entries clinicians and systems typed in place of proper drug codes, often decades ago. They fall into three buckets the pipeline disentangles:

- **PHI-contaminated drug entries** — provider names, site identifiers, study IDs, MRNs that leaked into the drug name field.
- **Devices and non-drug supplies** — colostomy bags, insulin needles, syringes, dressings; these have no RxNorm equivalent.
- **Actual unmapped drugs** — strings like `MILRINONE IV BOLUS` or `LEVOTHYROXINE 37.5 MCG CAPSULE` that need standardization to RxNorm (or CVX for vaccines).

## Pipeline architecture

Three sibling JSONL output files, all joined by `source_concept_id`:

| Stage | Engine | Input | Output | File |
|---|---|---|---|---|
| 1. triage | Nemotron, batched | `(source_id, source_name)` from CSV | PHI flag + categories + evidence; item kind (drug/device/procedure/unknown); cleaned drug string | `triage.jsonl` |
| 2. candidates | rapidfuzz over `concept.parquet` filtered to standard RxNorm + CVX | `cleaned_drug_string` from stage 1 | top-N candidate concepts with fuzzy scores | `candidates.jsonl` |
| 3. mapping | Nemotron, batched | cleaned string + candidate shortlist | best pick (or decline) + confidence + rationale | `mapping.jsonl` |

The split between stage 1 (classification) and stage 3 (mapping) is intentional. Classification is bulk-friendly and benefits from short, mechanical prompts. Mapping needs candidates riding alongside each row and produces denser output. Conflating them in one prompt makes both worse.

Stage 2 is deliberately deterministic — no LLM. It's a fuzzy index lookup, fully reproducible, and several orders of magnitude cheaper than another model call. Two queries per row (see below), unioned by concept_id with max-score-wins.

Every output file is written **append-style with a flush after each batch**, so a job killed mid-run leaves a partial-but-valid file. Re-running the same command resumes in place — each stage skips `source_concept_id`s already present in its output. This matters at 40K rows where the full run takes 20+ hours.

## Stage 1: triage

A single batched call to Nemotron with strict JSON-schema output. The prompt classifies each row along three axes:

1. **PHI status.** Provider/patient names, MRNs, dates, phone numbers, site identifiers that uniquely identify a location, and institutional study identifiers (e.g., `GCO#21-2239`). The model is instructed to be conservative — false positives are cheap to review later, missed PHI is not. The prompt explicitly excludes pharma compound codes (`MK-6482`-style) since those identify molecules, not people.

2. **Item kind.** `drug` / `device` / `procedure_or_other` / `unknown`. Devices and procedures short-circuit stage 2 (no candidates retrieved, auto-decline at stage 3).

3. **`cleaned_drug_string`.** If a drug name is present even alongside PHI or device text, extract just the drug portion (with dose/form/route) and strip the rest. Null when `kind != drug`.

### PHI consistency invariant

Pydantic's `model_validator` on `TriageResult` enforces: `has_phi=True` implies non-empty `phi_categories` and non-null `phi_evidence`. If Nemotron returns `has_phi=true` with empty categories or null evidence (observed in early testing), the validator coerces to `["other"]` and a placeholder evidence string. The inverse contradiction (`has_phi=false` with stray categories) is cleared. Downstream code can rely on the invariant.

### Reliability machinery

Nemotron in reasoning mode tends to either spend its entire token budget on row 1 of a multi-row batch, or emit duplicates and skips, or quietly swap names between rows. The pipeline defends against all three:

- **Reasoning is disabled** via NVIDIA's `detailed thinking off` directive, prepended to the system message. Triage is mechanical classification; per-row reasoning eats tokens and gains nothing. Mapping uses the same setting — the fuzzy candidates do the heavy lifting.

- **`triage_with_retry`** recursively halves the batch on any of these failures:
  - Missing IDs (model returned fewer results than requested)
  - Unknown IDs (model invented an ID we didn't send)
  - Name swaps (model returned a correct ID with the wrong `source_concept_name` attached — corrupts every other field for that row)
  - Down to single-row calls, where there's nothing left to swap or skip.

- **Always-dedup, always in input order.** Even on the happy path the result list is deduplicated by `source_concept_id` and re-sorted by the input order. Guards against the model emitting multiple copies of the same row.

- **`map_with_retry`** does the analogous thing for stage 3.

The default batch size is 10/10. We've validated this is stable. Pushing to 25+ produced short batches with reasoning on; thinking off lets you ramp up, but 10 is the safe baseline.

## Stage 2: candidates

Deterministic fuzzy retrieval against a pre-loaded index over `concept.parquet`, filtered to `vocabulary_id ∈ {RxNorm, CVX}`, `standard_concept = 'S'`, `domain_id = 'Drug'`. Default index size on the peds_fm export: ~155K standard concepts.

### Two-query retrieval

Each row hits the index twice. Results are unioned by `concept_id`, keeping the higher rapidfuzz score per concept, then truncated to top-N.

**Query 1: dose-unit normalized.** The `cleaned_drug_string` is rewritten so EHR-style strength expressions match RxNorm's canonical form:

- `37.5 MCG` → `0.0375 MG`
- `0.9 %` → `9 MG/ML`

Without this, `LEVOTHYROXINE 37.5 MCG CAPSULE` fails to retrieve `levothyroxine sodium 0.0375 MG Oral Capsule` despite the concept being right there in the index — the fuzzy matcher just can't bridge MCG↔MG. Similarly `SODIUM CHLORIDE 0.9 % IV BOLUS` doesn't reach `sodium chloride 9 MG/ML Injectable Solution` without the normalization.

**Query 2: ingredient/brand tokens only.** Numbers, units, dose forms, and routes are stripped from the source string, leaving just ingredient and brand names. This catches brand-name lookups that get drowned out by the dose-form noise in query 1.

The motivating failure: `FANAPT 6 MG TABLET` against the full string scores best against other "X 6 MG Oral Tablet" concepts (sennosides, risperidone, etc.) because the shared `6 MG Oral Tablet` tokens dominate. With just `FANAPT` as the query, `iloperidone 6 MG Oral Tablet [Fanapt]` floats to the top. Same fix for `ADLARITY` → `donepezil ... [Adlarity]`.

The stoplist in `normalize.py:PHARM_STOPWORDS` covers units, dosage forms, routes, common descriptors. Brand and ingredient names are intentionally not on it.

### Auto-decline path

If `triage.kind != drug`, `candidates_for_triage` short-circuits to an empty list — no fuzzy lookup runs. Stage 3 then auto-declines those rows without consulting the LLM, costing zero tokens and zero latency. Devices, procedures, and unknown-kind rows all flow through this path.

## Stage 3: mapping

Per row, Nemotron sees the cleaned source string plus its candidate shortlist (concept_id, name, vocabulary_id, class, fuzzy_score) and picks one or declines. The prompt emphasizes that the fuzzy score is informative but not authoritative — a 95-score candidate can still be wrong (wrong salt form, wrong dose), a 60-score candidate can be right.

Confidence rubric in the system prompt:

- 0.90 – 1.00: exact or near-exact match on ingredient + dose + form.
- 0.70 – 0.90: ingredient + form match; dose missing or approximate.
- 0.50 – 0.70: ingredient match only; dose/form absent or mismatched.
- < 0.50: weak — prefer to decline.

"Prefer specificity" is also explicit: if both an Ingredient concept and a Clinical Drug match, pick the Clinical Drug. In practice the model picks Quant Clinical Drug (with volume) when the source has volume info — `CLEVIDIPINE 25 MG/50 ML INTRAVENOUS EMULSION → 50 ML clevidipine 0.5 MG/ML Injection @ 0.95` is a clean example.

When the model declines, `chosen_concept_id = null` and the rationale explains why. Common decline reasons in practice: combination products where no single candidate covers all ingredients (Zatean-PN Plus, Vivonex RTF, Glucosamine Chondroitin), ambiguous source strings, and form/strength mismatches.

## Deployment

| | |
|---|---|
| Model | `nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4` |
| vLLM | 0.17.1, `--max-num-seqs 4`, `--gpu-memory-utilization 0.85` |
| Host | Minerva B200 (183 GB) |
| Port | 8000, OpenAI-compatible `/v1` |
| Sampling | `temperature=1.0, top_p=0.95` (NVIDIA's guidance) |
| Reasoning | disabled per-call via `detailed thinking off` |
| Structured output | `response_format: {type: "json_schema", strict: true}` |
| Conda env | `/sc/arion/scratch/gelmas07/venvs/nemotron-env` |

The Nemotron server is launched separately under an LSF `bsub` job; see `clinical_extractor`'s README for the launch command. This pipeline assumes the server is reachable at `http://localhost:8000/v1` (override with `NEMOTRON_BASE_URL`).

## Install

```bash
module purge
ml proxies
ml anaconda3/2025.06
conda activate /sc/arion/scratch/gelmas07/venvs/nemotron-env

cd /sc/arion/projects/EHR_ML/sgelman/phi_tagging
pip install -e .
```

## Smoke test

Always run the smoke before the full job. It runs all three stages on 25 rows and prints a per-row summary so you can eyeball whether PHI/device/drug calls look sane and whether mappings are landing on plausible RxNorm concepts.

```bash
python scripts/smoke_test.py \
    --csv phi_tagging/drug_mapping.csv \
    --limit 25 \
    --fresh
```

Expected runtime: 30–90 seconds. Output goes to `/sc/arion/scratch/gelmas07/phi_tagging/smoke/`.

Sanity checks on the smoke:

- All 25 input IDs appear in `triage.jsonl`, each exactly once.
- `kind` distribution roughly 85% drug, 10–15% device, rest procedure/unknown.
- No "Triage: name swap" warnings, or if there are, they're followed by successful retries.
- Mapping picks should hit confidence ≥ 0.85 on common drugs (Clevidipine, Luspatercept, Levothyroxine, Griseofulvin, etc.).
- Declines should land on legitimate combo products and enteral nutrition.

## Full run

```bash
python scripts/run_pipeline.py \
    --csv phi_tagging/drug_mapping.csv \
    --out-dir /sc/arion/scratch/gelmas07/phi_tagging/run01 \
    --triage-batch 10 \
    --mapping-batch 10 \
    --candidates-n 20 \
    2>&1 | tee /sc/arion/scratch/gelmas07/phi_tagging/run01.log
```

Run inside `tmux` or `screen` — the vLLM server lives in the same LSF job, so a dropped SSH session takes the model with it. Verify the LSF job's `-W` wall time covers the projected runtime before launching.

Expected runtime on 40K rows at default batch sizes: **15–25 hours** in the typical case. Watch the tqdm rate during the first 10 minutes to get a real estimate (`Xs/it` × 4000 batches ≈ stage 1 time).

### Resuming after a crash

Re-run the exact same command. Every stage reads its output file and skips IDs already present.

### Re-running just one stage

To re-run stage 3 after editing the mapping prompt:

```bash
rm /sc/arion/scratch/gelmas07/phi_tagging/run01/mapping.jsonl
python scripts/run_pipeline.py \
    --csv phi_tagging/drug_mapping.csv \
    --out-dir /sc/arion/scratch/gelmas07/phi_tagging/run01 \
    --skip-triage --skip-candidates
```

### Running stage 3 concurrently with stage 1

Stage 1 takes most of the wall time. Once triage is partway through, you can launch a second pipeline run in another tmux pane that consumes whatever triage has produced so far and runs stages 2 and 3 on it. vLLM's `--max-num-seqs 4` lets the server multiplex requests across both clients without contention.

```bash
# In tmux pane 2, while the main run is still doing triage:
python scripts/run_pipeline.py \
    --csv phi_tagging/drug_mapping.csv \
    --out-dir /sc/arion/scratch/gelmas07/phi_tagging/run01 \
    --skip-triage
```

This is the way to get early mapping output if you don't want to wait for stage 1 to fully complete.

## Monitoring a running job

```bash
RUN=/sc/arion/scratch/gelmas07/phi_tagging/run01

# Progress
wc -l $RUN/triage.jsonl

# PHI flag rate and kind distribution
python -c "
import json
from collections import Counter
c = Counter()
with open('$RUN/triage.jsonl') as f:
    for line in f:
        r = json.loads(line)
        c[('phi' if r['has_phi'] else 'clean', r['kind'])] += 1
for k, v in sorted(c.items()):
    print(f'{v:7d}  {k}')
"

# Sample recent PHI-flagged rows
grep '\"has_phi\":true' $RUN/triage.jsonl | tail -5 | python -m json.tool
```

If PHI rate looks wildly off (smoke ran roughly 0–10%; if production drifts to 30%+ something is mistuned), pause and inspect.

## Output schemas

See `src/phi_tagging/models.py` for full Pydantic definitions. Headline fields:

**`TriageResult`** (one per input row):
- `source_concept_id: int`, `source_concept_name: str`
- `has_phi: bool`, `phi_categories: list[PHICategory]`, `phi_evidence: Optional[str]`
- `kind: ItemKind` — one of `drug`, `device`, `procedure_or_other`, `unknown`
- `cleaned_drug_string: Optional[str]` — null when `kind != drug`
- `rationale: Optional[str]`

**`CandidatesForRow`** (one per input row):
- `source_concept_id`, `source_concept_name`, `cleaned_drug_string`
- `candidates: list[CandidateMatch]` — each with `concept_id`, `concept_name`, `vocabulary_id`, `concept_class_id`, `concept_code`, `score` (0–100 rapidfuzz)

**`MappingDecision`** (one per input row, declining or picking):
- `source_concept_id`
- `chosen_concept_id: Optional[int]` — null when declined
- `chosen_concept_name`, `chosen_vocabulary_id`
- `confidence: float` (0.0–1.0)
- `rationale: str`

### Loading results in pandas

```python
import pandas as pd

run = "/sc/arion/scratch/gelmas07/phi_tagging/run01"
df_t = pd.read_json(f"{run}/triage.jsonl",     lines=True)
df_c = pd.read_json(f"{run}/candidates.jsonl", lines=True)
df_m = pd.read_json(f"{run}/mapping.jsonl",    lines=True)

# Everything flagged as PHI
phi_rows = df_t.query("has_phi == True")

# All high-confidence mappings ready to commit
high_conf = df_m.dropna(subset=["chosen_concept_id"]).query("confidence >= 0.85")

# Join all three on source_concept_id
joined = df_t.merge(df_m, on="source_concept_id", suffixes=("_t", "_m"))
```

## Tuning knobs

| Knob | Default | When to change |
|---|---|---|
| `--triage-batch` | 10 | Bump to 20 if you see zero short-batch warnings on the smoke; drop to 5 if you see many. |
| `--mapping-batch` | 10 | Same logic. Mapping batches are denser (10 rows × 20 candidates each), so lower is safer. |
| `--candidates-n` | 20 | Raise to 30 if you see many "no candidate matches" declines on real drugs; lower to 10 if stage 3 is getting noisy candidate lists. |
| `score_cutoff` in `vocab.top_n` | 40 | Raise to 50–60 to be stricter about candidate quality; lower to widen the net for unusual products. |
| `thinking` in `client.structured` | False | Set to `True` if mapping picks look dumb on hard cases. Accepts ~5x slowdown for stage 3 in exchange. |

Concept index filter in `vocab.py`: currently `vocabulary_id ∈ {RxNorm, CVX} AND standard_concept = 'S' AND domain_id = 'Drug'`. Including non-standard branded variants might help brand-name retrieval but balloon the index.

## Known limitations

**Combo products often decline.** Multi-ingredient sources like Zatean-PN, Vivonex RTF, Glucosamine Chondroitin consistently decline because fuzzy matching on long combo strings is noisy and the combo Clinical Drug concepts rarely surface in the top-N. RxNorm does have these; we just don't retrieve them well. A fix would be ingredient-token search (split source into ingredient names, retrieve per-token, intersect) but it adds complexity for limited gain on a small fraction of the corpus.

**Devices have no target vocabulary.** RxNorm/CVX cover drugs only. Devices auto-decline at stage 3. Mapping them would require a parallel pipeline against SNOMED device hierarchy or HCPCS supply codes — same architecture, different concept filter. Out of scope for v1.

**Reasoning trace is captured but not persisted.** `NemotronClient.structured` returns `(parsed, reasoning_content)` but the pipeline currently drops the reasoning. If audit needs grow, add a sibling `reasoning.jsonl` per stage keyed by batch index.

**Single-client throughput.** The pipeline runs sequentially against one vLLM endpoint. `--max-num-seqs 4` means the server can handle 4 concurrent requests; running stage 1 and stage 3 in parallel tmux panes (above) is the lightweight way to use that headroom. A real fix would be asyncio + concurrent batch submission, which is a bigger refactor.

## File layout

```
phi_tagging/
├── pyproject.toml
├── README.md                          # this file
├── phi_tagging/
│   └── drug_mapping.csv               # input CSV (place yours here)
├── src/phi_tagging/
│   ├── __init__.py
│   ├── client.py                      # OpenAI-compat wrapper around vLLM
│   ├── models.py                      # Pydantic schemas, PHI validator
│   ├── normalize.py                   # MCG→MG, %→MG/ML, ingredient tokens
│   ├── vocab.py                       # ConceptIndex, two-query top_n
│   ├── triage.py                      # stage 1 prompt + retry-with-name-check
│   ├── candidates.py                  # stage 2 deterministic retrieval
│   ├── mapping.py                     # stage 3 prompt + retry-with-dedup
│   └── pipeline.py                    # orchestration, jsonl I/O, resume
└── scripts/
    ├── smoke_test.py                  # 25-row end-to-end with sample print
    └── run_pipeline.py                # full-run CLI with --skip-* flags
```

Outputs live in `/sc/arion/scratch/gelmas07/phi_tagging/<run-name>/` by default:

```
run01/
├── triage.jsonl          # stage 1 output, append-flushed per batch
├── candidates.jsonl      # stage 2 output
└── mapping.jsonl         # stage 3 output
```
