# prescreen

Task-agnostic pre-screening pipeline for OMOP concept_id extraction.
Runs once per task. Produces an exhaustive list of relevant
concept_ids per OMOP table and per concept-id column, ready to be
handed to downstream event-extraction code.

Five phases:

- **Phase 1 — direct event search.** For each task, find concept_ids
  that *directly* code the event (e.g. for NEC: SNOMED "Necrotizing
  enterocolitis", ICD10 P77.x).
- **Phase 2 — proxy search.** For each task, ask Nemotron to propose
  *clinical workflow proxies* — patterns of EHR footprint that
  strongly indicate the event occurred even when it was not formally
  coded (e.g. for sepsis: blood culture + empiric antibiotics +
  monitoring escalation). Run the Phase 1 machinery against each
  proxy independently.
- **Phase 3 — patient coverage analysis.** For each task, count
  unique patients reachable via direct evidence and via each proxy,
  compute overlap and proxy-only value-add. Useful both as a sanity
  check on proxy quality and as a downstream input.
- **Phase 4 — LLM-written extraction scripts.** Hand Nemotron the
  concept_ids from Phase 1 + Phase 2 and the task description, and
  ask it to write a Python script that combines them per the task's
  clinical logic and outputs the final patient list. The script is
  statically validated, then executed in a subprocess. A self-healing
  retry loop feeds errors back to Nemotron for correction.
- **Phase 5 — audit on N.** A fresh LLM call audits the patient count
  against clinical expectations for the cohort. If N is implausible
  (too_low / too_high / uncertain), the audit identifies suspected
  gaps and feeds them back into a new extraction cycle. Bounded
  outer loop; only a "plausible" verdict counts as complete.

## What it does

For each clinical event-detection task (defined in a small YAML file):

1. Asks Nemotron which OMOP tables to search for this task.
2. For each selected table, runs **two passes** — one against the
   standard concept-id column, one against the source concept-id
   column.
3. Per pass: asks Nemotron for a wide net of regex patterns, applies
   them to the OMOP `concept` table, expands hits to their descendants
   via `concept_ancestor`, matches against the cohort's data, and asks
   Nemotron to filter the survivors for clinical relevance.

The output is a per-task directory with the final concept_id list,
plus the intermediate artifacts at every stage of the funnel so you
can audit and diagnose.

## Pipeline

```
                       ┌─────────────────────────────────┐
                       │ Nemotron #0: table selector      │  one call per task
                       └─────────────────────────────────┘
                                       │
                                       ▼   chosen tables
                                       │
  ─── per (task, table) ─────────────────────────────────────────────────────
                                       │
                       splits into two passes:
                       ┌─────────────────┬─────────────────┐
                       │ standard pass    │ source pass     │
                       │ *_concept_id     │ *_source_concept_id
                       │ domain filter on │ domain filter off
                       │ standard_only=T  │ standard_only=F │
                       └─────────────────┴─────────────────┘
                                       │
                                       ▼
                       vocab metadata for this column
                       (which vocabularies live here)
                                       │
                                       ▼
                       ┌─────────────────────────────────┐
                       │ Nemotron #1: wide-net regex     │
                       └─────────────────────────────────┘
                                       │
                                       ▼   include_patterns / exclude_patterns
                       ┌─────────────────────────────────┐
                       │ concept-table query              │  filters: domain,
                       └─────────────────────────────────┘  vocabularies, standard
                                       │
                                       ▼   wide_net_seeds
                       ┌─────────────────────────────────┐
                       │ descendant expansion             │  via concept_ancestor;
                       └─────────────────────────────────┘  re-applies filters
                                       │
                                       ▼   wide_net_concepts
                                            (+ match_type: regex_seed | descendant)
                       ┌─────────────────────────────────┐
                       │ cohort match                     │  count rows per concept_id
                       └─────────────────────────────────┘  in cohort data
                                       │
                                       ▼   cohort_match_concepts
                       ┌─────────────────────────────────┐
                       │ Nemotron #2: relevance filter    │  batched, json_schema strict
                       └─────────────────────────────────┘
                                       │
                                       ▼
                              final_concepts (per pass)
                                       │
            unioned across all (table, pass) into
                            final_candidates.parquet
```

## OMOP tables searched

Each table has two searchable columns: the canonical standard
`*_concept_id` and the source `*_source_concept_id` holding non-
standard codes (Epic EDG, ICD9CM source, MSDW Attrib Catalog, etc.).
Both are searched per task by default.

| table | standard column | source column | typical content |
|---|---|---|---|
| `measurement` | `measurement_concept_id` | `measurement_source_concept_id` | labs, vitals, POC results (LOINC, SNOMED) |
| `drug_exposure` | `drug_concept_id` | `drug_source_concept_id` | medications, IV fluids (RxNorm, NDC) |
| `condition_occurrence` | `condition_concept_id` | `condition_source_concept_id` | diagnoses (SNOMED, ICD9CM, ICD10CM, Epic EDG) |
| `procedure_occurrence` | `procedure_concept_id` | `procedure_source_concept_id` | interventions (CPT4, HCPCS, SNOMED) |
| `observation` | `observation_concept_id` | `observation_source_concept_id` | surveys, social hx, prenatal exposures |
| `device_exposure` | `device_concept_id` | `device_source_concept_id` | ventilators, tubes, lines, implants |
| `visit_occurrence` | `visit_concept_id` | `visit_source_concept_id` | encounter type (ED, inpatient, outpatient) |
| `visit_detail` | `visit_detail_concept_id` | `visit_detail_source_concept_id` | sub-encounter unit (ICU, NICU, OR) |
| `death` | `cause_concept_id` | `cause_source_concept_id` | cause of death (SNOMED Condition) |

The table-selector LLM call picks which of these to actually run for a
given task. Override via `tables:` in the task YAML for explicit control.

## Layout

```
prescreen/
  __init__.py
  config.py            # paths + EVENT_TABLES (standard + source concept_col per table)
  cohort.py            # load_cohort, cohort_ids (cached parquet)
  concept_table.py     # cached concept table; coerces string concept_id -> Int64
  metadata.py          # reads outputs/vocab_prescreen/{table}_*_vocab_type.csv
  vocab.py             # query_concept(include, exclude, domain, vocabularies, standard_only)
  expand.py            # descendant expansion via concept_ancestor
  cohort_match.py      # count_concepts_in_cohort(table, candidate_ids, concept_col)
  nemotron.py          # NemotronClient (auto-retries on empty / truncated / timeout)
  llm_steps.py         # prompts + json_schemas (table-selector, wide-net, relevance)
  proxy.py             # Phase 2 — proxy hypothesis generation + per-proxy prescreen
  pipeline.py          # prescreen_one_table, prescreen_task, save_results
  extraction.py        # Phase 4 — LLM-written extraction scripts (self-healing)
  extraction_helpers.py# helpers imported by the generated scripts
  audit.py             # Phase 5 — post-extraction audit on N (semantic retry)

tasks/
  n_acut_06_hypoglycemia.yaml
  n_acut_04_necrotizing_enterocolitis.yaml

run_prescreen.py       # Phase 1 runner
run_proxy_prescreen.py # Phase 2 runner
run_extraction.py      # Phase 4 runner
patient_coverage.py    # Phase 3 — direct vs proxies, overlap + value-add
diagnose.py            # walk artifacts and find where a table's funnel collapsed
verify_coverage.py     # check whether a known list of concept_ids was captured
```

## Run

```bash
# 1. Confirm the vLLM is reachable
python run_prescreen.py --healthcheck

# 2. Run one task
python run_prescreen.py tasks/n_acut_06_hypoglycemia.yaml

# 3. Fan out across all tasks
python run_prescreen.py tasks/*.yaml
```

## Outputs

Per task, under `sammy/prescreen/tasks_out/{task_tag}/`:

```
hypoglycemia/
  task.json
  run_summary.json
  final_candidates.parquet           # unified across all (table, pass)

  # per (table, pass) artifacts — one set per pass:
  condition_occurrence__standard__patterns.json
  condition_occurrence__standard__vocab_metadata.parquet
  condition_occurrence__standard__wide_net_seeds.parquet
  condition_occurrence__standard__wide_net_concepts.parquet
  condition_occurrence__standard__cohort_match_concepts.parquet
  condition_occurrence__standard__decisions.parquet
  condition_occurrence__standard__final_concepts.parquet

  condition_occurrence__source__patterns.json
  condition_occurrence__source__vocab_metadata.parquet
  ...

  drug_exposure__standard__...
  drug_exposure__source__...
  measurement__standard__...
  measurement__source__...
```

`final_candidates.parquet` carries `source_table`, `search_pass`
(`standard` / `source`), and `source_concept_col` columns so each
concept tells you which (table, pass) it came from.

## Helper scripts

**`diagnose.py {task_tag} {table}`** — walks the artifacts for one
table, prints sizes at each stage of both passes, and tells you the
first empty stage. Use it whenever a table comes back lighter than
expected.

```bash
python diagnose.py hypoglycemia condition_occurrence            # both passes
python diagnose.py hypoglycemia condition_occurrence --pass source
```

**`verify_coverage.py {task_tag} --csv known_ids.csv`** — given a
known list of concept_ids (e.g. from a gold-standard phenotype, a
prior chart-review set, or an ad-hoc cohort check), reports what
fraction the prescreen captured, broken down by which pass found
them, and prints the names of anything missed.

```bash
python verify_coverage.py hypoglycemia --csv my_known_glycemia_ids.csv
python verify_coverage.py hypoglycemia --ids 4129519,4226798,4280953
```

## YAML knobs

A minimal task only needs `task_id`, `task_tag`, and `description`.
Optional fields, in approximate order of usefulness:

| field | default | effect |
|---|---|---|
| `positive_examples` | `[]` | concept names that SHOULD match; seeds the wide-net LLM call. Cover all plausible domains (LOINC, RxNorm, SNOMED, etc.) you expect Nemotron to generate patterns for. |
| `negative_examples` | `[]` | concept names that should NOT match; used to direct the wide-net's exclude patterns. |
| `tables` | `"auto"` | which tables to search. `"auto"` (or omitted) = Nemotron picks. `"all"` = every configured table. Explicit list = exactly those. |
| `search_source_concepts` | `true` | whether to run the source pass alongside the standard pass for each table. Set false for tasks strictly defined on standard concepts. |
| `expand_descendants` | `true` | whether to expand each regex-matched seed concept to its descendants via `concept_ancestor` before the cohort match. |
| `concept_col_overrides` | `{}` | per-table override for the standard pass's concept_col (rarely needed). |
| `domain_overrides` | `{}` | per-table override for the standard pass's domain filter (rarely needed). |

## Phase 2: proxy prescreen

Clinicians often operationalize treatment before formal diagnosis
coding. The diagnosis sometimes never gets coded, but the workflow
leaves a detectable trail. Sepsis is the canonical example: in a NICU
the workflow

    clinical deterioration → blood cultures drawn → empiric
    broad-spectrum antibiotics → monitoring escalation → maybe
    diagnosis later

leaves an unambiguous electronic footprint even when the sepsis
diagnosis never lands in the record. Phase 2 catches these cases.

For each task, Phase 2 asks Nemotron to propose **proxy hypotheses** —
distinct workflow patterns whose presence in the record strongly
indicates the target event occurred. Each proposed proxy comes with a
**clinical rationale** that gets saved verbatim, and is then run
through the same Phase 1 machinery (table selector, wide-net regex per
(table, pass), descendant expansion, cohort match, relevance filter)
as if it were a hand-written task.

```
parent task
   │
   ▼
┌────────────────────────────────────────┐
│ Nemotron #P: proxy hypothesis generator │   one call per task
└────────────────────────────────────────┘
   │
   ▼  list of proxies, each with:
        proxy_tag, rationale, description,
        positive_examples, negative_examples
   │
   ▼  for each proxy: feed into prescreen_task as if it were hand-written
   │
   ▼
Phase 1 pipeline (table-selector + per-(table, pass) wide-net + …)
   │
   ▼
proxies/{proxy_tag}/  — full Phase-1 artifact set
```

The proxies for one parent task are **siloed** under that task's
output directory but **linked** by their position in the tree. Each
proxy has its own `final_candidates.parquet`; nothing cross-contaminates
the parent task's direct-event final list.

### Run

```bash
# 1. Phase 1 first (so direct-event concepts are on disk).
python run_prescreen.py tasks/n_acut_04_necrotizing_enterocolitis.yaml

# 2. Phase 2 — generate proxies + prescreen each.
python run_proxy_prescreen.py tasks/n_acut_04_necrotizing_enterocolitis.yaml
```

Phase 2 doesn't require Phase 1 to have been run, but most users will
run them in order so the Phase 1 outputs sit next to the `proxies/`
subtree.

### Outputs

```
necrotizing_enterocolitis/                         # parent task (Phase 1)
  task.json
  run_summary.json
  final_candidates.parquet                         # directly-coded NEC concepts
  condition_occurrence__standard__final_concepts.parquet
  ...

  proxies/                                         # Phase 2
    _proxy_hypotheses.json                         # all proxies + overall rationale + Nemotron reasoning trace
    _proxy_index.json                              # proxy_tag -> rationale summary -> output_dir

    surgical_intervention_for_nec/
      rationale.md                                 # WHY this proxy is valid (saved verbatim, human-readable)
      proxy_definition.yaml                        # auto-generated task definition
      task.json                                    # machine-readable copy with _proxy_rationale stowed
      run_summary.json
      final_candidates.parquet                     # THIS proxy's concepts
      procedure_occurrence__standard__patterns.json
      procedure_occurrence__standard__final_concepts.parquet
      ...

    sustained_npo_plus_tpn_initiation/
      ...
```

### Reading saved rationales

For QA, grep across `proxies/*/rationale.md`. For programmatic access
(downstream LLM steps that want to read prior reasoning) pull from the
JSON manifest:

```python
import json
from prescreen.config import TASKS_OUT

manifest = json.loads((TASKS_OUT / "necrotizing_enterocolitis" /
                       "proxies" / "_proxy_hypotheses.json").read_text())
for p in manifest["proxies"]:
    print(p["proxy_tag"])
    print("  rationale:", p["rationale"])
    print("  description:", p["description"])
```

Each proxy's own `task.json` also carries the rationale as
`_proxy_rationale`, so the rationale travels with the task definition
itself.

### Helpers work transparently on proxy paths

`diagnose.py` and `verify_coverage.py` accept any nested path under
`TASKS_OUT`, so you can point them at a proxy directory without
modification:

```bash
python diagnose.py necrotizing_enterocolitis/proxies/surgical_intervention_for_nec procedure_occurrence
python verify_coverage.py necrotizing_enterocolitis/proxies/sustained_npo_plus_tpn --csv known_nec_ids.csv
```

## Phase 3: patient coverage analysis

For each task, count unique cohort patients matched by direct
evidence and by each proxy, then compute overlap and proxy-only
value-add. Useful as a sanity check on proxy quality and as input to
downstream analyses.

```bash
python patient_coverage.py                                    # all tasks
python patient_coverage.py --task-tag necrotizing_enterocolitis
python patient_coverage.py --combined-csv sammy/coverage_all.csv
```

Writes `{task_tag}/patient_coverage.json` (summary across direct + all
proxies) and `{task_tag}/proxies/{proxy_tag}/patient_coverage.json`
(per-proxy detail). See the script docstring for output schema.

Headline numbers per proxy:

- `n_overlap` — patients with both direct and proxy evidence
- `n_only_in_proxy` — patients only the proxy reached (proxy's value-add)
- `n_only_in_direct` — patients with diagnosis but no proxy workflow
- `jaccard_vs_direct` — overlap / union

## Phase 4: LLM-written extraction scripts

The closing of the loop. For each task, Nemotron writes a Python
script that combines the prescreened direct and proxy concept_ids
into the final patient list according to task-specific clinical logic.
The script is statically validated (AST parse, banned-imports
allowlist, required call to `write_patient_ids`), then executed in a
subprocess with a timeout. On failure, the error is fed back to
Nemotron for a corrected attempt — self-healing up to a bounded number
of tries.

```
Phase 1 concept_ids + Phase 2 proxy concept_ids + task description
                          │
                          ▼
       ┌────────────────────────────────────────┐
       │ Nemotron #E: extraction script writer  │   up to N attempts;
       │   sees task + helpers + previous errors│   feeds errors back
       └────────────────────────────────────────┘
                          │
                          ▼   {task_tag}.py
       ┌────────────────────────────────────────┐
       │ Static validation                       │   AST parse, no banned
       │   (banned imports, required call)       │   imports, must call
       └────────────────────────────────────────┘   write_patient_ids()
                          │
                          ▼
       ┌────────────────────────────────────────┐
       │ Subprocess execution                    │   30-min default timeout
       └────────────────────────────────────────┘
                          │
                          ▼
                 patient_ids.parquet
```

### Helpers (used by the generated scripts)

Nemotron is instructed to import from `prescreen.extraction_helpers`
rather than reimplement parquet plumbing. The helpers encode the
canonical pushdown patterns:

```python
from prescreen.extraction_helpers import (
    load_direct_concepts,            # Phase 1 final_candidates as DataFrame
    load_proxy_concepts,             # Phase 2 final_candidates (all or one proxy)
    query_events,                    # pushdown read of one event table
    query_events_from_candidates,    # iterates query_events for a candidates DF
    patient_set,                     # -> set[int] of unique person_ids
    union, intersect, difference,    # set ops sugar
    write_patient_ids,               # REQUIRED — canonical output writer
)
```

### Run

```bash
python run_extraction.py tasks/n_acut_04_necrotizing_enterocolitis.yaml
python run_extraction.py tasks/*.yaml --max-attempts 3
```

### Outputs

```
tasks_out/necrotizing_enterocolitis/
  ...                                # Phase 1 + 2 + 3 artifacts as before

  extraction/                        # NEW (Phase 4)
    necrotizing_enterocolitis.py     # the generated script (latest attempt)
    extraction_rationale.md          # Nemotron's logic + clinical rationale
    extraction_reasoning.txt         # Nemotron's reasoning trace (if available)
    extraction_run.log               # latest attempt's stdout/stderr
    patient_ids.parquet              # the final answer
    extraction_summary.json          # status, n_patients, attempt_count, history

    attempts/                        # per-attempt audit trail
      attempt_1.py
      attempt_1.log
      attempt_2.py
      attempt_2.log
      ...
```

`patient_ids.parquet` schema:

| column | type | description |
|---|---|---|
| `person_id` | int64 | the patient |
| `criteria_met` | bool | always true (for join compatibility) |
| `label` | str | caller-defined (e.g. "combined", "direct_only") |
| `n_supporting_events` | int64 (optional) | how many event rows supported the match |
| `earliest_event_date` | date (optional) | first matching event |

### Self-healing retry

If an attempt fails — validation rejection, runtime non-zero return,
timeout, or missing output — the orchestrator captures the error and
the previous code and asks Nemotron to fix it on the next attempt.
Older attempts get one-line summaries in the retry prompt; the most
recent attempt gets its full code shown so Nemotron can *edit* rather
than regenerate from scratch.

After a batch run, this jq one-liner sorts tasks by how many attempts
they needed (highest first — your "review me" list):

```bash
for f in sammy/prescreen/tasks_out/*/extraction/extraction_summary.json; do
    jq -r '. | "\(.attempt_count) \(.status) \(.task_tag) \(.n_patients // "—")"' "$f"
done | sort -k1,1nr -k2,2
```

### Helpers work transparently on extraction outputs

```bash
python verify_coverage.py necrotizing_enterocolitis --csv known_nec_ids.csv
# (compares against tasks_out/necrotizing_enterocolitis/final_candidates.parquet)
```

For a verify pass against the *extracted patient list* rather than the
concept list, point at `extraction/patient_ids.parquet` instead.

## Phase 5: audit on N

Phase 4 produces a script and an N. Phase 5 asks: is N plausible?

A fresh LLM call (independent of the extraction model, so it can't
defend its own logic) reviews the task description, cohort size,
final N, evidence inventory (direct + per-proxy concept counts), and
the script's combining logic. It returns one of four verdicts:
**plausible**, **too_low**, **too_high**, or **uncertain**. For the
non-plausible cases it lists specific suspected gaps with rationale
and a suggested remedy each.

If the verdict is non-plausible, the gaps are formatted as feedback
and fed into a fresh extraction cycle. The new script sees the
original task brief plus the audit's complaints from the previous
cycle. Bounded by `--max-audit-cycles` (default 2 — so up to 1
initial extraction + 1 audit-driven retry).

The two retry layers nest cleanly:

```
extract_with_audit (outer — semantic retry on N)
    for cycle in 1..max_audit_cycles:
        extract_for_task (inner — code-error retry from Phase 4)
            for attempt in 1..max_attempts:
                Nemotron writes script
                validate + run
                if technical failure: feed error back, retry
        audit the result
        if verdict == "plausible": done
        else: format gaps as external_feedback, loop
```

Different feedback at each layer: technical (traceback) inside,
semantic (clinical gaps) outside.

### Run

```bash
python run_extraction.py tasks/n_acut_01_antibiotic_discontinuation.yaml
# audit is on by default

python run_extraction.py tasks/foo.yaml --max-audit-cycles 3
python run_extraction.py tasks/foo.yaml --no-audit
```

### Outputs (Phase 5 additions)

```
tasks_out/antibiotic_discontinuation/extraction/
  ...                          # Phase 4 artifacts unchanged

  audit_report.md              # NEW — polished markdown of the LATEST
                               #       cycle's audit (verdict + gaps)
  audit_history.md             # NEW — running log across all cycles
  audit_history.json           # NEW — structured record of each cycle:
                               #       verdict, n_patients, gaps, timestamp
```

`audit_report.md` is intended to be readable by a clinical reviewer
with no engineering context. Headline N + percent, verdict badge,
auditor's reasoning paragraph, evidence inventory tables, extraction
logic summary, and a numbered list of suspected gaps each with its
rationale and suggested remedy.

`audit_history.json` lets you mine the audit trail across all 100
tasks programmatically. For instance, to find tasks that survived
audit on cycle 1 vs cycle 2 vs failed:

```bash
for f in sammy/prescreen/tasks_out/*/extraction/audit_history.json; do
    jq -r '.[-1] | "\(.cycle) \(.verdict) \(.n_patients)"' "$f"
done | sort -k1,1n -k2,2
```

### When Phase 5 helps vs doesn't

Helps:
- The script ran cleanly but used `AND` where `OR` belongs
- A source-pass concept inventory was empty for a key table and the
  script ignored it instead of flagging
- A temporal window was too restrictive
- A proxy with rich coverage wasn't combined into the final set

Doesn't help:
- N is *actually* low because the event is genuinely rare in the
  cohort. The auditor will say "plausible" and we move on.
- The task description itself is wrong or ambiguous. Phase 5 will
  fire repeatedly without converging; the fix is upstream.
- Phase 1/2 missed concepts that aren't recoverable from any
  re-combining of what's there. The audit can suggest "include
  source pass for drug_exposure" but if Phase 1 found zero in that
  pass, the rewrite has nothing to work with. The audit will flag
  this with a specific gap pointing at the empty pass — useful
  signal for going back and re-running Phase 1 with better seeds.

## Design notes

- **Three LLM calls per task, then two per (table, pass).** The
  table-selector runs once. Each `(table, pass)` runs a wide-net
  regex generation call and a (batched) relevance-filter call. All
  three use vLLM's strict `json_schema` response format so outputs are
  well-typed and don't require defensive parsing.

- **Two passes per table.** Standard `*_concept_id` columns hold the
  canonical OMOP mapping; source `*_source_concept_id` columns hold
  the original institutional / vocabulary codes (Epic EDG, ICD9CM
  source, MSDW, etc.). The source pass turns off `standard_only` and
  the domain filter, but restricts to vocabularies actually present
  on the source column at this site, then leans on the cohort match
  to prune. This catches non-standard codes that have no clean
  standard mapping (or that map up to a coarser parent).

- **Descendant expansion via `concept_ancestor`.** After the regex
  hits the concept table we expand each seed to its descendants before
  the cohort match. This is what catches brand-named drug products
  ("Baqsimi" for glucagon), SNOMED subtypes, and other hierarchy-only
  members that the concept-name regex would never see. The same
  filters used to produce the seed set are re-applied to descendants
  so they don't drift across categories. Each expanded concept gets a
  `match_type` column (`regex_seed` vs `descendant`) so QC can
  attribute final hits to either path.

- **Wide-net LLM gets the vocab distribution.** For each pass the LLM
  is handed the list of vocabularies on the column being searched
  with their concept counts and row counts. This lets it lean into
  the right vocabularies (RxNorm-shaped patterns for drug standard,
  ICD-shaped patterns for condition source) instead of guessing. The
  source pass also gets an explicit note that the candidates are
  non-standard and may use institution-specific phrasing.

- **Cohort match drops dead candidates.** If a concept has zero rows
  in the cohort we don't waste an LLM token asking whether it's
  relevant. Each surviving candidate carries a per-cohort row count
  so the relevance filter knows what's high-volume.

- **Relevance filter is batched** at 200 concepts per call. Each
  decision carries a one-line reason that gets persisted alongside
  the final list.

- **Caching.** The cohort, concept, and concept_ancestor tables are
  cached as parquet under `sammy/prescreen/cache/` so each task
  starts in seconds rather than re-reading the full vocabulary tables
  from `output/`.

- **String-vs-int concept_id quirk.** The vocab table stores
  `concept_id` as strings at this site. `concept_table.load_concept`
  and `expand.load_concept_ancestor` coerce to nullable `Int64` once
  at load time and cache the result; every downstream caller sees
  ints.

## Open items / next iteration

- Temporal anchoring (turn concept_id list into per-person event
  timestamps relative to a cohort index date). Out of scope for the
  prescreen — that's the next task.
- Chunking the cohort-match parquet filter when the candidate set is
  large (>~10k concept_ids). Rare but possible for very broad tasks
  or generous source passes.
- A "second-look" loop on the wide-net regex: feed the LLM a small
  sample of `concept_name` rows that were *near misses* (matched
  include patterns but were knocked out by exclude patterns) and let
  it refine. Cheap and would catch poorly-worded exclude patterns.
- Per-task cost telemetry: total LLM calls, total tokens, wall clock,
  written to `run_summary.json` for budget tracking across 100 tasks.
