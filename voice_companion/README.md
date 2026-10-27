# Voice Companion — Phase 1

A server-side AI companion that talks with a person through an
ESP32-S3-BOX3 sentinel: it speaks through the loudspeaker, listens
through the microphone, and writes the conversation up as an HTML
document.

Phase 1 is a **complete vertical slice** — real LangGraph, real database,
real speech in and out, real LLM — with the intelligence deliberately
thin. Memory, planning, findings extraction and safety escalation are
Phases 2–4, and attach to this graph rather than reshaping it.

Nothing here modifies the existing Sentinel application. The MQTT audio
transport in `ast_src/audio_transport` is imported, never edited, and
`dash_env` is untouched.

---

## 1. Architecture

```
                    ┌──────────────── LangGraph ────────────────┐
   BOX3 mic         │                                            │
      │  Opus       │  load_person_context → prepare_daily_plan   │
      ▼             │            ↓                               │
  StreamingAudio    │   generate_agent_response → persist → speak │
   Transport        │            ↑                     ↓         │
      │  PCM        │   choose_next_goal ← retrieve_memory        │
      ▼             │            ↑                     ↓         │
 SentinelAdapter ──►│   persist_turn ← transcribe ← receive_input │
      ▲             │                                  ↓         │
      │  PCM        │              continue_or_finish → close     │
  BOX3 speaker      └────────────────────────────────────────────┘
                                    │
                    SQLite: turns, sessions ──► HTML transcript
                    SQLite: LangGraph checkpoints (separate DB)
```

Two databases, on purpose: LangGraph checkpoints exist so a conversation
can be resumed or inspected after a crash; the application tables are the
durable record a human reads. They are not the same data.

### Latency

Measured on this machine, per turn:

| stage | measured |
|---|---|
| VAD endpoint wait | 0.5–0.8 s *(the dominant cost)* |
| Whisper large-v3 (GPU) | RTF 0.05–0.23 |
| LLM → first sentence (gpt-4o) | ~1.0 s |
| Piper → first audio chunk | **0.10 s** |
| MQTT streaming downlink | ~immediate |
| **first sound from the box** | **~2 s** |

### Choosing the model

The provider is an `.env` edit, per role, and nothing else changes:

```
LLM_PROVIDER=openai          # default. openai | ollama | scripted
CONVERSATION_PROVIDER=ollama # or split the two roles
ANALYSIS_PROVIDER=openai
CONVERSATION_MODEL=          # blank = the provider's default
```

Conversation and analysis are separate because their constraints are
opposite. The conversation reply is **spoken**, so latency is felt by the
person in the room and the timeout is short; the analysis runs after the
call, is parsed rather than heard, and can afford to be slow.

For local models, `ollama serve` must be running and the model pulled.
The default is `llama3.2:latest` for both roles: this machine has a 6 GB
card that already holds Whisper large-v3, so a 9B model beside it spills
out of VRAM and the spoken reply arrives late. `gemma2:9b` is a
reasonable `ANALYSIS_MODEL`, which does not compete with the live turn.

One provider difference worth knowing: the analyst asks for structured
output, and Ollama gets it by constraining generation to the JSON schema
rather than by tool-calling, which several pulled models (`gemma2` among
them) do not support at all. That is handled in `build_analyst()`; if a
model still cannot produce the schema, the analysis returns `None` and
the deterministic safety layer stands in — quietly, so check the log if
findings stop appearing after a model change.

Piper was chosen over Chatterbox on measurement, not preference:
Chatterbox runs at RTF 1.4–3.9 and cannot emit anything until the whole
utterance is done, which put a turn at 6–11 s. Chatterbox remains the
right tool for the avatar videos, where latency is irrelevant.

---

## 2. Install

Everything lives in the **`audioapp`** conda env, which already had
LangChain, LangGraph, OpenAI, FastAPI, SQLAlchemy and faster-whisper.
Phase 1 added only:

```
piper-tts  pathvalidate  paho-mqtt  pyogg
langgraph-checkpoint-sqlite  aiosqlite  sqlite-vec
```

No upgrades to torch, numpy or transformers. `dash_env` is never touched
— running this with `dash_env`'s Python fails immediately with
`No module named langgraph`, which is intended.

The English Piper voice is already downloaded to `data/voices/`. Another:

```bash
python -m piper.download_voices el_GR-rapunzelina-low --data-dir voice_companion/data/voices
```

---

## 3. Configuration

Copy `.env.example` to `.env` and edit. If `OPENAI_API_KEY` is left
blank, the Sentinel checkout's own secret resolution is used, so the key
need not be written twice.

The person's details live in **`data/person_ilyas.txt`** — plain text,
commented, re-read at the start of every conversation.

> **Every field marked `[PLACEHOLDER]` is invented.** The agent treats
> this file as fact and speaks it aloud: if it reads that Ilyas has a
> daughter named Eleni, it will ask after Eleni by name. Correct or
> delete every placeholder before talking to a real person. Placeholders
> are stripped before they reach the prompt, so a deleted line is always
> safer than a wrong one.

---

## 4. Running

```bash
run_conversation.bat --mock                    # no device, no models, no API
run_conversation.bat --mock-audio              # real STT/TTS/LLM, no device
run_conversation.bat --sentinel box3-2         # the real thing
run_conversation.bat --sentinel box3-2 --minutes 1
run_tests.bat
```

Start with `--mock`: it exercises the graph, the database, the state
machine and the HTML report in under a second.

Transcripts land in `reports/` as self-contained HTML — no external CSS,
fonts or JavaScript, so they open anywhere and print cleanly.

---

## 5. Speaker labels

The transcript says **AGENT** and **SPEAKER**, and does not name which
resident was talking.

That is a measured decision rather than a missing feature. The existing
ECAPA profiles were enrolled from clean close-mic FLAC; on real
far-field BOX3 recordings the same model scored a median of **0.164**
with a median margin of **0.078** between first and second place — 9 of 9
correct on clean audio, essentially arbitrary on real audio. Writing a
name into a document that may be acted upon, on that evidence, would be
inventing attribution.

To enable naming later: re-enrol each person **through the BOX3 itself**,
recalibrate the threshold, and implement `SpeakerProvider`. The interface
already exists; the renderer gains a column and nothing else changes.

---

## 6. What Phase 1 deliberately does not do

- **No memory between conversations.** Each session starts from the
  profile file only. (Phase 2)
- **No topic planning or diversity.** `prepare_daily_plan` and
  `choose_next_goal` are real nodes with placeholder bodies. (Phase 2)
- **No findings extraction or summaries.** (Phase 3)
- **No safety escalation and no Brevo email.** (Phase 4)
- **No barge-in.** The agent cannot be interrupted mid-sentence; the
  state machine already has the `SPEAKING → INTERRUPTED → LISTENING`
  edge, and it is tested, but nothing takes it yet. The MQTT play path
  has no "stop playback" command, so real barge-in needs the streaming
  path plus firmware co-operation.
- **No scheduler.** Conversations are started by hand. (Phase 4)

---

## 7. Safety limitations

Read this before pointing it at a real person.

- **This is not a medical or emergency system.** It has no safety
  escalation at all in Phase 1, and even with Phase 4 it will miss
  things. False negatives are the dangerous direction: a missed "I have
  fallen and cannot get up" is far worse than a false alarm. It must
  supplement, never replace, whatever emergency arrangement exists.
- **The agent can be wrong about what it heard.** Whisper hallucinates on
  silence — on real sentinel clips it produced `Υπότιτλοι AUTHORWAVE` and
  `Thank you.` from rooms where nobody was speaking. Three guards catch
  this (VAD gating, `no_speech_prob`, a blocklist), and they will not
  catch everything.
- **Consent matters.** This records and transcribes daily conversations
  with someone who may not fully appreciate that. They should understand
  and agree to it, and be able to stop it.
- **Requests to stop are honoured deterministically**, by rule rather
  than by prompt, so the model cannot be talked out of it. The rule
  distinguishes making a farewell from describing one — "I said goodbye
  to my neighbour" does not end the call.

---

## 8. Connecting your own audio transport

Everything device-specific is in `app/services/sentinel.py`. To use a
different transport, implement:

```python
class SentinelAudioIO(Protocol):
    async def receive_utterance(self, timeout: float) -> bytes: ...   # 16 kHz mono PCM
    async def send_audio(self, chunks: Iterable[bytes]) -> float: ...
    def close(self) -> None: ...
```

`SentinelAudioAdapter` is the reference implementation over the existing
MQTT streaming path; `MockSentinelAdapter` is the stand-in used by
`--mock`.

Note the half-duplex gate: while the agent speaks, captured audio is
discarded. That is what stops the agent hearing itself and answering its
own question, without an echo canceller or diarisation. Barge-in will
need this relaxed — it is one flag, deliberately.
