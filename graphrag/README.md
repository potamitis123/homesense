# graphrag — asking the household record questions

Turns a window of Sentinel events into a searchable graph, so that abstract
questions can be answered from years of data without ever putting years of
data into a prompt.

Independent of the running application. `ast_audio_db` is opened read-only,
`derive_home_dynamics.py` is imported and used unchanged, and everything this
produces lands in `graphrag/store/`.

```bash
python -m graphrag.ingest --catch-up          # every day not yet built
python -m graphrag.ingest --day 2026-08-28    # just tonight's
python -m graphrag.ask "What does Rafael talk about?"
```

## Why a graph rather than a bigger prompt

The LLM tab already answers questions about a week: the four pipeline stages
run, the result is compacted, and one model call reads it. That does not
extend. Five days of analysis is 8.8 million characters, compacted to 164k. A
year would be roughly twelve million after compaction — far beyond any context
window, local or hosted.

So the expensive reading is moved to write time. Each utterance is read once,
at ingest, and what it was about becomes structure. A question then traverses
that structure and only a small, already-computed result reaches the model.

## The three stages

Adapted from `potamitis123/dementia-partB/graphs`, whose separation of
planning from execution is what makes a local model safe to use here.

**CLASSIFY** — the planner sees the question, the list of residents and a
closed list of operations. It sees no data. It returns JSON naming one
operation and its arguments. An invented person or an unknown operation is
rejected by validation, and a deterministic keyword rule takes over if the
model is unavailable.

**EXECUTE** — `engine.py`. Ordinary Python over the store, no model involved.
Every number in an answer is computed here and can be recomputed by hand.

**INTERPRET** — the narrator sees the question and the operation's output, and
nothing else. It cannot invent a figure because it is not the thing doing
arithmetic.

Each stage is printed separately by `ask.py`, which is what lets you tell a
wrong answer from a wrongly planned one.

## A day is the unit, and it is written once

The store is one file per calendar day plus an index:

```
store/index.json            who lives here, which days exist
store/days/2026-08-27.json  that day, complete and immutable
store/topic_cache.json      labelled utterances, keyed by text
```

A day is built from its own events, written whole via a temporary file, and
never rewritten -- `--rebuild` is the only way to change one. So an answer
that cited 12 June still cites the same evidence months later, and two
ingests can be diffed to see exactly what a pipeline change did.

Building one day costs a pipeline pass over that day plus labelling only the
utterances never seen before: about a minute, which is small enough to run
nightly from a scheduled task.

Levels 2 and 3 merge events across gaps of up to ten minutes, so an episode
can begin at 23:55 and finish after midnight. Each day is therefore built with
three hours of preceding context, and every episode is attributed to the date
it *starts* on. Days stay disjoint: an episode belongs to exactly one of them
and can never be counted twice.

Queries merge days -- nodes unioned by id, edges appended, series keyed by
date. A question about one day opens one file (43 ms); a question spanning
everything opens everything (89 ms for six days). At three years that is
around 1,100 files, still a fraction of a second, because merging day
dictionaries is only counting.

## What the graph holds

Nodes are people, days, rooms, topics, utterances and Level 3 social episodes.
Edges are `spoke`, `mentions`, `on_day`, `in_room` and `participated`.

Beside the graph sits a daily series — per person and per pair — lifted from
Level 4. Trends and comparisons are arithmetic over a table; forcing them
through edge traversal would only obscure them.

The utterance is the unit of evidence. Every claim in an answer resolves down
to one: speaker, day, room, originating event id, and the audio file on disk.

## The topic layer

Levels 1 to 4 abstract away from words on purpose — `"Water tap"` becomes
`water_use`. That is what makes them summarisable, and why they cannot answer
what somebody talks about. Ingest adds one stage: every utterance of four
words or more goes once to a local model, which returns up to three short
subject labels in English, so the same subject groups across days and
languages.

Results are cached by the hash of the utterance text, checkpointed every 25
labels. A re-run costs nothing and a longer window pays only for what is new.

## The incident layer

The operations that read what an exchange meant — `tension_moments`,
`warm_moments`, `complaints` — rank **exchanges**, and an exchange is whatever
stretch of talk the pipeline grouped. It can run for twenty minutes and cover
five subjects, with one line of reading standing for all of it and only its own
start and end known. On 2 October 2026 a resident complained about her
workplace inside a 24-minute exchange that was read as neutral conversation; no
operation could retrieve it, with a local model or with GPT-4o doing the asking.

So each day is also read for **incidents**: a quarrel, a complaint, a worry, a
making-up, a warm moment, a rebuff, each with who was in it, what it was about,
and the first and last recorded line that show it. They are listed at ingest
with no question in mind, five minutes at a time, and a question then chooses
from the list. (They are called incidents because an *event* here is a recorded
clip.)

```bash
python -m graphrag.ingest --day 2026-10-02 --add-incidents --incident-model gpt-4o-mini
python -m graphrag.ask "Was there a quarrel today? When did it start and finish?"
python -m graphrag.ask "Did anyone complain about their work?"
python -m graphrag.ask --op incidents --arg day=2026-10-02 --no-narrate   # the whole list
```

`--add-incidents` gives a day that is already written its incident layer, or
replaces the one it has, and changes nothing else in it. A new day gets the
layer at ingest whenever `--semantic-model` or `--incident-model` is given.

Reading every window *with* the question was tried and is not used. It found
the same incidents and far too many others — it said yes to "quarrel" in 34 of
66 windows, and reported talk of a dentist three times on a day in which the
word does not occur.

**Start and end** are settled by a second pass that depends on the kind,
because the rule that is right for one is wrong for the other. A quarrel is
followed into the neighbouring windows for as long as the conflict goes on; a
complaint is one person on one subject inside a longer conversation, so the ten
minutes either side are re-read and its first and last line marked. Both were
measured on one example each. On the full day the quarrel rule also stretched
two mild disputes past twenty minutes, and a stricter wording did not shorten
them; `tests/diagnostics/graphrag_retrieval/make_dispute_list.py` writes the
day's disputes out for a resident to judge, which is what will settle it.

An incident carries no intensity of its own. `tension` on a result is the
sharpest Level 1.5 reading that overlaps it, so on a day with several disputes
the real quarrel comes first.

**A day that was never read is not a day on which nothing happened.** Every
answer says which days in the range have an incident list and which do not.

**The household memory.** `common_files/household_memory.txt` is filled in by
the residents: who they are, where each works, whose names come up, what they
tend to worry about. The reader of a day is given only part of it — who lives
here, the language, and for each resident the work or school and what "work"
means for them (`READER_ENTRIES` in `incidents.py`). That part travels with
every reading, so with a hosted model it leaves the house with the transcript.
The whole of it is used only when a question names a subject, to choose among
incidents already listed.

The rest is kept from the reader because of what it did to one day, 2 October
2026, read with four versions of the same filled-in file:

| Given to the reader | Lines | Incidents listed | Quarrel found | Work complaint found |
|---|---|---|---|---|
| Nothing | 0 | 22, 23 | 2 of 2 | 1 of 2 |
| Who they are, where they work | 12 | 17 to 22 | 5 of 5 | 5 of 5 |
| All but what tends to happen | 43, 46 | 17 to 20 | 6 of 6 | 0 of 6 |
| Everything filled in | 49 | 35 to 39 | 3 of 3 | 3 of 3 |

With everything, the reader copies the memory into the day. One resident's line
of recurring worries came back as an incident in its own words: a worry about
exactly those things, placed in ninety seconds of a quarrel over something
else, in which the first of them is not mentioned once. A line that says what
tends to happen is a question put to the reader, and it is answered the way a
question is. With the plain background lines the reader
stopped listing the work complaint and read the same minutes as a disagreement
between two people; why is not known. One day, one model, and a judgement of
"found" that rests on two incidents the residents confirmed.

**The model.** Reading a day takes about twenty seconds with `gpt-4o-mini`:
over a hundred calls, eight at a time. `llama3.2` did not finish one day in 34
minutes, so the layer as it stands needs a hosted model, and the transcript of
the day goes to it. No audio does. A day is about two hundred thousand tokens,
which is a whole per-minute allowance on a small account, so a refusal for
being over the rate limit is waited out and retried; a window that still could
not be read, and a span that could not be settled, are counted on the day and
printed, never passed off as a quiet five minutes.

**It is not the same list twice.** Two readings of one day differ by an
incident or two: five readings with the same memory gave between 17 and 22. A
day's list is written once and kept, so an answer cites the same incidents
later, but a miss can be a reading and not the household. Which of those lists
is the right size is not known until a resident has judged one.

**A question is answered at the grain of the list.** An incident is kept as a
few words — "work-related grievances" — so a question about one particular
grievance is answered by the whole complaint. Asked whether she complained
about funding, the stage chose that complaint, in minutes where money comes up
in one garbled line.

**Whether the list is right is asked of the residents.** The report page (LLM
tab) shows the stored incidents of its window under the lamps, each with the
lines it rests on and two buttons, Confirm and Reject. Every answer is appended
to `common_files/incident_judgements.jsonl` with the incident as it was shown
and the reading it came from; nothing in that file is rewritten, and a day that
is read again starts unjudged while the answers about its old list stay on file.

```bash
python -m graphrag.judgements                  # confirmed and rejected, by kind
python tests/manual/llm_report_page.py         # the page alone, on port 8077
```

The count is the list's precision: of what it claimed and somebody judged, how
much had happened. What the list missed is not in it.

**The page asks before it makes a list.** Opening it never reads a day: that
sends the day's transcript to a model. For the latest day in its window that is
over and has no list, the page asks "Do you want me to prepare the list for
Saturday 3 October?" and says what a Yes sends. Yes starts the same command a
person would type, as its own process (`prepare.py`), one day at a time; the
list appears on the page when it is ready, and if none comes of it the page says
why and asks again. No is remembered, so a day is not asked about on every
visit, and the page offers to ask again. Today is never asked about: a day is
written to the store once, so it is read only when it is over.

Asking adds one stage for this operation only: when a question names a subject
("about work"), a model is shown the incidents in scope and returns the numbers
of those that answer it. A question that names none ("was there a quarrel")
skips that stage and gets every incident of the kind, sharpest first. A question
that asks whether or when something happened is sent to this operation by rule,
whatever the planner chose: the local planner kept sending such questions to
`complaints`.

## Two honest limits, enforced in code

**Emotion is aggregate-only.** It is a first-class part of the graph — a node
kind, an attribute on every utterance, and distributions per person, per pair,
per day and per household. But blind annotation of five clips put the per-clip
label at 12.5% balanced accuracy, while the aggregate distribution over a week
(67% neutral, 27% happy, 5% sad, 1% angry) sits close to the household's own
estimate. Both can be true: correct marginals, unreliable single labels. So
the narrator may reason from a distribution over many utterances and is
forbidden from resting a claim on any one clip.

One approximation is worth knowing: the recogniser labels a whole clip, not
each utterance in it, so two people speaking in one clip receive the same
emotion.

**Attribution error propagates.** Every per-person and per-pair figure rests
on speaker identification, which is closed-set over three residents and has no
way to say "nobody I know". `evidence_strength` from Level 4 is carried into
results so an answer can hedge on it.

## Answers commit, but in proportion

The narrator is required to answer the question rather than refuse it, and
equally required to scale its wording to the evidence, quote the utterances
behind every claim, and end with a stated confidence. A conclusion from four
utterances on one day is worded very differently from one drawn from forty
across a week — and the answer says which it is.

## Files

| | |
|---|---|
| `schema.py` | Node and edge kinds, the closed operation vocabulary, plan validation |
| `ingest.py` | Events → pipeline → topic extraction → graph and series |
| `incidents.py` | The incident layer: listing, settling each span, the household memory |
| `judgements.py` | What the residents answered about each listed incident, and the count |
| `prepare.py` | Starting a day's reading when the report page is answered Yes |
| `engine.py` | The deterministic operations. No model runs here |
| `llm.py` | Planner, incident selection and narrator |
| `ask.py` | CLI |

## Useful flags

```bash
python -m graphrag.ingest --catch-up --since 2026-06-01   # backfill
python -m graphrag.ingest --day 2026-08-27 --rebuild      # force one day
python -m graphrag.ask "Was Ilyas angry today?" --show-plan
python -m graphrag.ask "What does Rafael talk about?" --since 2026-08-01
python -m graphrag.ask --op family_summary --no-narrate
```

Family words resolve to names, so "the father", "his son" and "wife" all work
without naming anyone; "today" and "yesterday" resolve to real dates.

`--no-narrate` stops after the deterministic result, which is the honest way
to check a number before letting a model describe it.
