# How to run everything

Organised by what you want to do, not by where the file lives. Ports are at the
bottom.

**Environment.** Almost everything runs in `dash_env`:

```
conda activate dash_env
```

The exception is `voice_companion`, which owns its own environment (`audioapp`)
and activates it itself through its `.bat` files — never run it from `dash_env`.

**Before the live system.** Start mosquitto first, and check nothing stale is
already listening on 8050.

---

## 1. The live system

| | |
|---|---|
| **The pipeline** — listens to MQTT, transcribes, identifies, classifies, writes to `ast_audio_db` | `python ast_src/main.py` |
| **The dashboard** — all the tabs, port 8050 | `python ast_dash/ast_app.py` |
| **Mobile API** — FastAPI for the phone app, port 8001 | `python mobile_api/main.py` |
| **Voice companion** — one spoken conversation with a sentinel | `voice_companion\run_conversation.bat --sentinel box3-2` |
| | `voice_companion\run_conversation.bat --mock` (no hardware, no API calls) |

Dashboard tabs, once it is up at `http://127.0.0.1:8050`:

`/` Audio Dashboard · `/audio_classes` · `/daily_events` · `/live` Live Audio ·
`/llm_report` **LLM Report** · `/statistics` · `/visuals` (ESP32-P4) ·
`/upload_files` · `/logfile` · `/settings`

---

## 2. Looking at family dynamics

Three views, at three time scales. This is the part that gets confused.

### Short time — one day, or a few hours of it

**This is `conversation_day`,** a standalone viewer outside the dashboard. Pick
a day and optionally a time range, and it shows who talked with whom, which
lamps each discussion lit, and what the model read in each one.

```
python conversation_day/app.py
```

Then open `http://127.0.0.1:8071`. Readings are cached per window, model, prompt
version and silence rule in `conversation_day/cache`, so reopening a window you
have already read costs nothing. Changing the L1.5 prompt changes its hash and
invalidates those caches on purpose.

A still snapshot of the same thing, without the server:

```
python conversation_day/snapshot.py
```

### Medium — a week, as a report

The **LLM Report** tab of the dashboard (`/llm_report`). It runs the whole L0 →
L1 → L1.5 → L2 → L3 → L4 pipeline over the chosen window and writes the result
to `ast_dash/llm_report/reports/home_dynamics_<from>_to_<to>.json`.

The same pipeline without the dashboard, for one exported window:

```
python ast_dash/llm_report/derive_home_dynamics.py --input events.json --output out.json
```

Note this CLI runs **without** an interpreter, so L1.5 records that nothing read
the exchanges rather than inventing readings. Only the dashboard path and
graphrag pass a real model.

### Long run — months, by question

**graphrag.** Read once at ingest, then ask.

```
python -m graphrag.ingest --day 2026-09-17 --semantic-model gpt-4o-mini
python -m graphrag.ingest --catch-up --since 2026-09-14 --semantic-model gpt-4o-mini
python -m graphrag.ingest --day 2026-09-17 --semantic-model gpt-4o-mini --rebuild
```

`--rebuild` is the only way to rewrite a day already in the store; days are
immutable otherwise. Ingest needs **ollama running** for topic extraction
(llama3.2, local and free) and OpenAI only for the L1.5 readings. Budget roughly
10 minutes and a few cents per day.

Then ask it something:

```
python -m graphrag.ask "What does Rafael talk about?"
python -m graphrag.ask --op tension_moments --since 2026-09-14 --until 2026-09-19
python -m graphrag.ask "When were the quarrels?" --show-plan --show-result
```

Fifteen operations exist; `--op` skips the planner and runs one directly.
Useful ones: `tension_moments`, `warm_moments`, `complaints`, `timeline`,
`person_profile`, `dyad_summary`, `trend`, `household_topics`.

---

## 3. Evaluating the components (section 3.2)

Annotating clips by ear and scoring the perception stack. The store is
self-contained: it freezes the clips and their audio so the sample cannot change
underneath you.

```
python tests/diagnostics/annotate_components.py --store run_sept sample --n 150 --since 2026-09-15 --until 2026-09-19
python tests/diagnostics/annotate_components.py --store run_sept annotate
python tests/diagnostics/annotate_components.py --store run_sept stats
python tests/diagnostics/annotate_components.py --store run_sept figures
```

`annotate` opens a local web page with audio. Other subcommands: `commentary`
(auto-written findings), `prune`, `reparse`.

Results land in `tests/diagnostics/run_sept/` — `results.json`,
`figure_commentary_auto.txt`, `figures/`, and the written section
`results_section.txt` / `.docx`.

---

## 4. Evaluating the relational reading (sections 3.3 and 3.4)

Everything here lives in `tests/diagnostics/dynamics_pilot/`. Read `CODEBOOK.md`
first — it is the manual the coder works to.

### Build a coding sheet and code it

```
python tests/diagnostics/dynamics_pilot/make_sheets.py --label validation --since 2026-09-15 --until 2026-09-19 --n 100
python tests/diagnostics/dynamics_pilot/make_sheets.py --label training --day 2026-09-15 --n 10
```

Serve the sheets, then open the URL and code:

```
cd /d C:\Users\potam\ast_home\tests\diagnostics\dynamics_pilot\sheets
python -m http.server 8073
```

* `http://localhost:8073/validation/coder_A/index.html` — the 100-exchange set
* `http://localhost:8073/training/coder_A/index.html` — the 10-exchange warm-up
* `http://localhost:8073/pool/index.html` — the 24 items graphrag returned, for
  judging retrieval (section 3.4 axis a)

Press **Download ratings** when finished and copy the file into `ratings/`.

### Score what was coded

```
python tests/diagnostics/dynamics_pilot/score.py ratings/ratings_validation_A_2026-09-15_to_2026-09-19.json
python tests/diagnostics/dynamics_pilot/score.py ratings/..._A_....json ratings/..._B_....json
```

One file gives a summary of that sheet; two files give inter-coder agreement
(Krippendorff's α, Cohen's κ, the adjudication queue).

### Compare the coder against the system

Generate the system's readings **after** coding is finished, then compare:

```
python tests/diagnostics/dynamics_pilot/run_interpreter.py validation.sample.json --model gpt-4o-mini --out validation.readings.json
python tests/diagnostics/dynamics_pilot/compare.py ratings/ratings_validation_A_....json validation.readings.json --mismatched withdrawal
```

`--mismatched` flags dimensions the coder and the prompt defined differently, so
their rows are printed but excluded from any headline claim.

### The graphrag evaluation

```
python tests/diagnostics/dynamics_pilot/graphrag_eval.py --since 2026-09-14 --until 2026-09-19 --narrate
python tests/diagnostics/dynamics_pilot/make_pool_sheet.py --since 2026-09-14 --until 2026-09-19
```

The first runs four questions and scores retrieval, grounding and planning; the
second builds the judging sheet for what came back.

---

## 5. Evaluating the scripted dialogues (section 3.1)

The 40 acted takes of 10 September.

```
python tests/diagnostics/journal_experiments.py --llm-runs 3
python tests/diagnostics/score_recorded.py --scenario 1 --since "2026-09-10 20:25" --until "2026-09-10 20:45"
python tests/diagnostics/run_dialogue_suite.py
python tests/diagnostics/bulbs_from_dialogues.py
```

Outputs go to `reports/journal_2026-09-10/`.

---

## 6. Writing the paper

```
python tests/diagnostics/dynamics_pilot/make_figures.py          # section 3.3, five figures
python tests/diagnostics/dynamics_pilot/make_figures_3_4.py      # section 3.4, one figure
python tests/diagnostics/dynamics_pilot/build_docx.py section_3_3
python tests/diagnostics/dynamics_pilot/build_docx.py section_3_4
python tests/diagnostics/dynamics_pilot/build_docx.py section_4_3
python tests/diagnostics/dynamics_pilot/build_docx.py section_5_6_5_7
```

`build_docx.py` reads the `.txt` and writes the `.docx` beside it — the text
file is the source of truth. **Close the .docx in Word first** or the write
fails with a permission error.

---

## 7. Tests

```
python -m pytest tests/dash_tests/ -q
```

---

## Ports

| port | what |
|---|---|
| 8050 | the dashboard (`ast_dash/ast_app.py`) |
| 8071 | conversation viewer (`conversation_day/app.py`) |
| 8073 | coding sheets (plain `http.server`, whatever you point it at) |
| 8001 | mobile API |
| 11434 | ollama — needed by graphrag ingest and ask |

---

## Where things are

| | |
|---|---|
| `ast_audio_db/ast_audio.db` | every clip the system ever recorded |
| `assets/audio_mp3/` | the audio itself |
| `assets/LLM_prompt_L1_5.txt` | the relational reading prompt — editing it invalidates every cache keyed on its hash |
| `common_files/parameters.json` | thresholds, credentials, model choices |
| `ast_dash/llm_report/reports/` | generated week reports |
| `graphrag/store/days/` | one JSON per ingested day |
| `conversation_day/cache/` | cached readings per window |
| `tests/diagnostics/run_sept/` | the component study |
| `tests/diagnostics/dynamics_pilot/` | the relational study, its codebook, sheets and paper sections |
| `reports/journal_2026-09-10/` | the scripted-dialogue study |

---

*Commands in sections 3, 4 and 6 have been run as written. Sections 1, 2 and 5
are documented from the code and its own usage strings — check the first time
you use one.*
