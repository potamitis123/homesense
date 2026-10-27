"""Start one conversation.

    run_conversation.bat --mock                 # no hardware, no API calls
    run_conversation.bat --sentinel box3-2      # the real thing
    run_conversation.bat --sentinel box3-2 --minutes 1

--mock swaps every external dependency for a stand-in, so the graph, the
database and the HTML report can be exercised without a device, a GPU or
an OpenAI key.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import get_settings                     # noqa: E402
from app.db.session import init_db, load_session_with_turns  # noqa: E402
from app.graph.graph import build_graph                 # noqa: E402
from app.graph.nodes import Nodes                       # noqa: E402
from app.profile import load_profile                    # noqa: E402
from app.report import render_session_html              # noqa: E402
from app.services import llm as llm_mod                 # noqa: E402
from app.services import sentinel as sentinel_mod       # noqa: E402
from app.services import stt as stt_mod                 # noqa: E402
from app.services import tts as tts_mod                 # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(description="Run one companion conversation.")
    p.add_argument("--sentinel", default=None, help="device id, e.g. box3-2")
    p.add_argument("--person", default=None, help="profile txt (default: Ilyas)")
    p.add_argument("--minutes", type=float, default=None,
                   help="override the target duration, for testing")
    p.add_argument("--transport", default=None,
                   choices=["auto", "mqtt", "webrtc"],
                   help="how to reach the device (default: auto, from the "
                        "device's announced capabilities)")
    p.add_argument("--mock", action="store_true",
                   help="no device, no models, no API calls")
    p.add_argument("--mock-audio", action="store_true",
                   help="real STT/LLM/TTS but no device")
    p.add_argument("--no-report", action="store_true")
    p.add_argument("--dry-run-alerts", action="store_true",
                   help="compose the guardian email but do not send it")
    p.add_argument("--no-alerts", action="store_true",
                   help="disable guardian notification entirely")
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S")
    log = logging.getLogger("companion")

    if args.minutes:
        settings.target_conversation_seconds = int(args.minutes * 60)
        settings.max_conversation_seconds = int(args.minutes * 60 * 1.2)
        settings.min_conversation_seconds = int(args.minutes * 60 * 0.8)

    init_db()
    profile = load_profile(args.person or settings.person_file)
    sentinel_id = (args.sentinel or profile.sentinel_id
                   or settings.default_sentinel_id)
    log.info("person=%s sentinel=%s target=%ds", profile.address_as,
             sentinel_id, settings.target_conversation_seconds)
    if profile.unfilled:
        log.warning("unfilled profile fields are ignored: %s",
                    ", ".join(sorted(set(profile.unfilled))))

    # ── services ─────────────────────────────────────────────────────
    if args.mock:
        audio = sentinel_mod.MockSentinelAdapter()
        stt = stt_mod.DummySTT(["I slept well, thank you.",
                                "Yes, I sat in the garden for a while.",
                                "That would be nice. Goodbye for now."])
        tts = tts_mod.DummyTTS()
        llm = llm_mod.build_llm(settings, scripted=True)
    else:
        stt = stt_mod.FasterWhisperSTT(settings.stt_model, settings.stt_language,
                                       settings.stt_beam_size_live,
                                       settings.audio_sample_rate,
                                       device=settings.stt_device,
                                       min_free_vram_mb=settings.stt_min_free_vram_mb,
                                       cpu_threads=settings.stt_cpu_threads)
        tts = tts_mod.build_tts(settings)
        llm = llm_mod.build_llm(settings)
        if args.mock_audio:
            audio = sentinel_mod.MockSentinelAdapter()
        else:
            from app.services.transport import open_audio_io
            audio = await open_audio_io(
                sentinel_id, settings,
                transport=args.transport or settings.audio_transport,
                utterance_config=sentinel_mod.UtteranceConfig(
                    sample_rate=settings.audio_sample_rate,
                    silence_ms=settings.vad_silence_ms,
                    vad_threshold=settings.vad_threshold,
                    continuation_grace_seconds=settings.continuation_grace_seconds,
                    max_utterance_seconds=settings.max_utterance_seconds))

    # Guardian notification. Credentials and recipient come from the
    # dashboard's Settings tab, never from this package.
    from app.services.notifications import GuardianNotifier
    notifier = GuardianNotifier(
        enabled=settings.enable_guardian_email and not args.no_alerts,
        dry_run=settings.enable_dry_run_alerts or args.dry_run_alerts or args.mock,
        include_transcript=settings.alert_include_transcript)
    analyst = None if args.mock else llm_mod.build_analyst(settings)

    # The rolling diary. Disabled under --mock so a scripted test run
    # cannot write invented events into the real person's memory.
    short_memory = None
    if settings.short_memory_enabled and not args.mock:
        from app.services.short_memory import build_short_memory
        short_memory = build_short_memory(settings, profile.person_id)
    if notifier.enabled and not notifier.dry_run:
        log.info("guardian alerts -> %s", notifier.recipient or "(not configured)")

    nodes = Nodes(audio=audio, stt=stt, tts=tts, llm=llm,
                  profile=profile, settings=settings,
                  analyst=analyst, notifier=notifier,
                  short_memory=short_memory)

    session_id = uuid.uuid4().hex
    thread_id = f"{profile.person_id}-{session_id[:8]}"

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    exit_code = 0
    async with AsyncSqliteSaver.from_conn_string(settings.checkpoint_db) as saver:
        graph = build_graph(nodes, checkpointer=saver)
        config = {"configurable": {"thread_id": thread_id},
                  # A five-minute chat is many node visits; the default 25
                  # would abort a real conversation partway through. The
                  # figure allows for every turn spending its full
                  # listen_attempts_per_turn budget re-listening past
                  # bursts that were not speech, which is the worst case
                  # in a noisy room and must not be the thing that ends
                  # the conversation.
                  "recursion_limit": 1000}
        try:
            await graph.ainvoke(
                {"session_id": session_id, "sentinel_id": sentinel_id,
                 "thread_id": thread_id, "phase": "IDLE"}, config)
        except KeyboardInterrupt:
            log.warning("interrupted by user")
            exit_code = 130
        except Exception:
            log.exception("conversation failed")
            exit_code = 1
        finally:
            try:
                audio.close()
            except Exception:
                pass

    if not args.no_report:
        session, turns, person = load_session_with_turns(session_id)
        if session is not None:
            path = render_session_html(session, turns, person)
            log.info("transcript -> %s", path)
            print(f"\nTranscript: {path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
