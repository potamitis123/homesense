"""
Audio logger + AST classifier with 5‑min burglary-score + Brevo email alert (MP3 attachment)
────────────────────────────────────────────────────────────────────────────────────────────
- Lowercases predicted labels for rule matching.
- Starts a 5‑minute window on first match against BURGLARY_FLAGS.
- Sums the matched probabilities across the window, logs the SCORE.
- If SCORE > THRESHOLD, sends an email with the FIRST MINUTE of that window as an MP3.

Dependencies
────────────
pip install transformers torch sounddevice soundfile numpy maad pydub
# Also install ffmpeg (needed by pydub to export mp3).

Configure (no env vars)
───────────────────────
Edit the "Brevo SMTP settings" block below with your exact SMTP key (starts with xkeysib-).
By default the email "From:" will be your Brevo SMTP login; change FROM_EMAIL if you prefer.
"""

import asyncio
import datetime as dt
from pathlib import Path
from typing import List, Tuple

import numpy as np
import sounddevice as sd
import soundfile as sf
import torch
from email.message import EmailMessage
from maad.spl import wav2dBSPL
from maad.util import mean_dB
from transformers import ASTForAudioClassification, AutoFeatureExtractor

# ───── user-configurable parameters ─────
BLOCK_SEC     = 10               # seconds per block
FS            = 16_000           # sample rate (Hz)
TOP_K         = 3                # top-k labels to log
ALERTS_DIR    = Path("alerts")   # MP3 attachments saved here
LOG_FILE      = Path("classification_log.txt")
ALERTS_LOG    = Path("alerts_log.txt")

# 5-minute accumulation + alert
WINDOW_MINUTES = 1
FIRST_MIN_SEC  = 60
THRESHOLD      = 0.0             # starter value; tune as needed

# Email settings
ALERT_TO       = ""  # recipient

# ───── SPL helper ─────
GAIN_DB, SENS_DB, V_ADC = 25, -18, 1.25
def block_spl(x: np.ndarray) -> float:
    return float(mean_dB(wav2dBSPL(x, GAIN_DB, SENS_DB, V_ADC)))

# ───── Watchlist (lowercased) ─────
CONFIG_burglary = {
    'chink, clink', 'thunk',' thump, thud','glass','tools', 'drill', 'throat clearing','cough', 'breaking',
    'power tool', 'blender','slam', 'ratchet, pawl', 'filing (rasp)', 'keys jangling', 'crushing', 'speech',
    'narration, monologue', 'female speech, woman speaking', 'male speech, man speaking', 'hubbub, speech noise, speech',
    'hammer', 'breaking', 'shatter', 'smash', 'crash', 'bang', 'scrape', "dental drill, dentist's drill",
    'knock', 'yell', 'cash register', 'mechanisms','creak', 'rub', 'ambulance (siren)', 'police car (siren)',
    'thunk', 'alarm clock', 'fire alarm', 'fire engine, fire truck (siren)', 'alarm', 'thump, thud', 'walk, footsteps'
}
BURGLARY_FLAGS = {s.lower().strip() for s in CONFIG_burglary}

# ───── directories ─────
ALERTS_DIR.mkdir(exist_ok=True)

# ───── AST classifier (PyTorch) ─────
class AudioClassifier:
    """AST classifier; uses GPU if available, otherwise CPU."""
    def __init__(self, hub_id: str = "MIT/ast-finetuned-audioset-10-10-0.4593", top_k: int = TOP_K):
        self.top_k = top_k
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.model = ASTForAudioClassification.from_pretrained(hub_id).to(self.device).eval()
        self.feat = AutoFeatureExtractor.from_pretrained(hub_id)
        self.rate = self.feat.sampling_rate

    @torch.inference_mode()
    def predict(self, audio) -> List[Tuple[str, float]]:
        inputs = self.feat(audio, sampling_rate=self.rate, return_tensors="pt").to(self.device)
        logits = self.model(**inputs).logits[0]
        probs  = torch.sigmoid(logits)
        top    = torch.topk(probs, k=self.top_k)
        return [(self.model.config.id2label[int(i)], float(s)) for i, s in zip(top.indices, top.values)]

# ───── Brevo SMTP settings (no env vars) ─────
BREVO_SMTP_HOST  = "smtp-relay.brevo.com"
BREVO_SMTP_PORT  = 587
BREVO_SMTP_USER  = ""   # your Brevo SMTP login
BREVO_SMTP_KEY   = ""   # your Brevo SMTP key
FROM_EMAIL       = ""  # MUST be your verified Gmail address

def send_email_brevo(subject: str, body: str, to_email: str, attachment_path: Path) -> str:
    """Send an email via Brevo SMTP with optional attachment."""
    import smtplib, mimetypes
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"]    = FROM_EMAIL  # Use your verified Gmail address
        msg["To"]      = FROM_EMAIL
        #msg["To"]      = to_email
        msg["Reply-To"]= FROM_EMAIL
        msg.set_content(body)

        if attachment_path is not None and attachment_path.exists():
            file_size_mb = attachment_path.stat().st_size / (1024 * 1024)
            print(f"[EMAIL] Attaching {attachment_path.name} ({file_size_mb:.2f} MB)")
            
            ctype, enc = mimetypes.guess_type(str(attachment_path))
            if ctype is None or enc is not None:
                ctype = "application/octet-stream"
            maintype, subtype = ctype.split("/", 1)
            with open(attachment_path, "rb") as f:
                msg.add_attachment(f.read(), maintype=maintype, subtype=subtype, filename=attachment_path.name)

        print(f"[EMAIL] Connecting to {BREVO_SMTP_HOST}:{BREVO_SMTP_PORT}...")
        smtp = smtplib.SMTP(BREVO_SMTP_HOST, BREVO_SMTP_PORT, timeout=30)
        smtp.set_debuglevel(1)  # Enable debug output
        smtp.ehlo()
        print("[EMAIL] Starting TLS...")
        smtp.starttls()
        smtp.ehlo()
        print("[EMAIL] Logging in...")
        smtp.login(BREVO_SMTP_USER.strip(), BREVO_SMTP_KEY.strip())
        print("[EMAIL] Sending message...")
        result = smtp.send_message(msg)
        smtp.quit()
        return f"EMAIL_SENT_BREVO to {to_email} - Server response: {result}"
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        return f"EMAIL_SEND_ERROR_BREVO: {e!r}\n{error_details}"

# ───── mp3 export helper ─────
def save_first_minute_mp3(blocks: List[np.ndarray], fs: int, outfile: Path):
    """
    Concatenate float32 mono blocks into 60 s (or less) and export as MP3 via pydub/ffmpeg.
    """
    from pydub import AudioSegment

    if len(blocks) == 0:
        raise RuntimeError("No audio blocks to save")

    # Concatenate all blocks
    x = np.concatenate(blocks, axis=0)
    
    # Ensure we have valid audio data
    if len(x) == 0:
        raise RuntimeError("Concatenated audio is empty")
    
    # Clip and convert to int16
    x = np.clip(x, -1.0, 1.0)
    pcm16 = (x * 32767.0).astype(np.int16)

    # Create AudioSegment and export
    seg = AudioSegment(data=pcm16.tobytes(), sample_width=2, frame_rate=fs, channels=1)
    seg.export(outfile, format="mp3", bitrate="128k")
    
    print(f"[MP3] Saved {len(x)/fs:.1f}s of audio to {outfile}")

# ───── async helpers ─────
async def record_block() -> np.ndarray:
    block = sd.rec(int(BLOCK_SEC * FS), samplerate=FS, channels=1, dtype="float32")
    await asyncio.to_thread(sd.wait)
    return block.flatten()

async def producer(q: asyncio.Queue):
    while True:
        await q.put(await record_block())

async def consumer(q: asyncio.Queue, clf: AudioClassifier):
    event_id = 0

    # state for 5-min accumulation
    session_active = False
    session_start  = None            # tz-aware datetime
    session_sum    = 0.0
    first_minute_blocks: List[np.ndarray] = []
    first_minute_secs = 0

    with LOG_FILE.open("a", encoding="utf-8") as log, ALERTS_LOG.open("a", encoding="utf-8") as alog:
        while True:
            block = await q.get()
            event_id += 1

            ts = dt.datetime.now().astimezone()
            local_str = ts.strftime("%d-%b-%Y  %H:%M:%S")

            # inference
            labels = await asyncio.to_thread(clf.predict, block)
            label_str = ", ".join(f"{lab.lower()}:{prob:.2f}" for lab, prob in labels)

            # SPL
            spl = block_spl(block)

            # print & log per-block
            line = f"Event_Id: {event_id}, {local_str}, {spl:.1f} dB, {label_str}"
            print(line)
            log.write(line + "\n")
            log.flush()

            # watchlist matches
            matches = [(lab.lower(), p) for lab, p in labels if lab.lower() in BURGLARY_FLAGS]
            matched_prob_sum = float(sum(p for _, p in matches))

            # start window on first match
            if not session_active and matched_prob_sum > 0.0:
                session_active = True
                session_start  = ts
                session_sum    = 0.0
                first_minute_blocks = []
                first_minute_secs   = 0
                msg = f"[START] 5-min window at {session_start.isoformat()} — triggered by: {', '.join(m for m,_ in matches)}"
                print(msg); alog.write(msg + "\n"); alog.flush()

            if session_active:
                # accumulate score
                session_sum += matched_prob_sum

                # capture first minute - ALWAYS add the block if we're still in first minute
                if first_minute_secs < FIRST_MIN_SEC:
                    first_minute_blocks.append(block.copy())
                    first_minute_secs += BLOCK_SEC
                    print(f"[RECORDING] Captured {first_minute_secs}s of {FIRST_MIN_SEC}s for alert")

                # end window?
                if (ts - session_start) >= dt.timedelta(minutes=WINDOW_MINUTES):
                    score = session_sum
                    end_str = f"[END] 5-min window ended at {ts.isoformat()} — SCORE={score:.2f} (THRESHOLD={THRESHOLD})"
                    print(end_str); alog.write(end_str + "\n"); alog.flush()
                    log.write(f"BURGLARY_SCORE: {score:.2f} at {ts.isoformat()}\n"); log.flush()

                    # Always save MP3 if we have blocks
                    mp3_path = None
                    if len(first_minute_blocks) > 0:
                        try:
                            stamp = session_start.isoformat(timespec="seconds").replace(":", "-")
                            mp3_path = ALERTS_DIR / f"alert_{stamp}_score{score:.2f}.mp3"
                            
                            # Save in a thread to avoid blocking
                            await asyncio.to_thread(save_first_minute_mp3, first_minute_blocks, FS, mp3_path)
                            
                            alog.write(f"Saved MP3: {mp3_path} ({len(first_minute_blocks)} blocks)\n")
                            alog.flush()
                        except Exception as e:
                            error_msg = f"MP3_SAVE_ERROR: {e!r}"
                            print(error_msg)
                            alog.write(error_msg + "\n")
                            alog.flush()
                            mp3_path = None

                    # email if score > THRESHOLD
                    if score > THRESHOLD and mp3_path is not None and mp3_path.exists():
                        subject = f"BURGLARY ALERT — score {score:.2f} at {session_start.strftime('%Y-%m-%d %H:%M:%S %Z')}"
                        body = (
                            f"5-min burglary score: {score:.2f} (threshold: {THRESHOLD})\n"
                            f"Start: {session_start.isoformat()}\nEnd:   {ts.isoformat()}\n"
                            f"Attached: first minute MP3 from the window.\n"
                            f"Total blocks captured: {len(first_minute_blocks)}\n"
                        )
                        status = await asyncio.to_thread(send_email_brevo, subject, body, ALERT_TO, mp3_path)
                        print(status); alog.write(status + "\n"); alog.flush()
                    elif score > THRESHOLD and mp3_path is None:
                        msg = f"[ALERT] Score {score:.2f} exceeds threshold but MP3 failed to save"
                        print(msg); alog.write(msg + "\n"); alog.flush()

                    # reset
                    session_active = False
                    session_start  = None
                    session_sum    = 0.0
                    first_minute_blocks = []
                    first_minute_secs   = 0

            q.task_done()

# ───── main ─────
async def main():
    q: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=8)
    clf = AudioClassifier()
    print(f"Starting audio surveillance...")
    print(f"Block duration: {BLOCK_SEC}s, Sample rate: {FS}Hz")
    print(f"Window: {WINDOW_MINUTES} min, Threshold: {THRESHOLD}")
    print(f"Alerts will be saved to: {ALERTS_DIR.absolute()}")
    print("-" * 60)
    await asyncio.gather(producer(q), consumer(q, clf))

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")