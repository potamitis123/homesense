"""Renders a finished conversation as a standalone HTML document.

One self-contained file per session: no external CSS, no fonts, no
JavaScript, so it opens anywhere and prints cleanly.

Speaker labels are AGENT and SPEAKER only. Naming which resident spoke is
deliberately out of scope here -- measured on real far-field BOX3 audio,
ECAPA scored a median of 0.164 with a median margin of 0.078 between
first and second place, which is not a basis for writing someone's name
into a document that may be acted upon. When enrolment is redone through
the BOX3 itself, this renderer gains a name column and nothing else
changes.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from .config import REPORTS_DIR

_CSS = """
:root{--bg:#ffffff;--fg:#1a1c20;--muted:#6b7280;--line:#e5e7eb;
--agent:#1d4ed8;--agent-bg:#eff6ff;--human:#065f46;--human-bg:#ecfdf5;}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--fg:#e6e8ec;
--muted:#9aa3b2;--line:#242833;--agent:#93b4ff;--agent-bg:#151b2b;
--human:#7ee0bd;--human-bg:#122019;}}
*{box-sizing:border-box}
body{margin:0;padding:2rem 1rem;background:var(--bg);color:var(--fg);
font:16px/1.6 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:820px;margin:0 auto}
h1{font-size:1.5rem;margin:0 0 .25rem}
.sub{color:var(--muted);margin:0 0 1.5rem}
.meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
gap:.75rem;padding:1rem;border:1px solid var(--line);border-radius:10px;
margin-bottom:1.75rem}
.meta div{font-size:.875rem}
.meta b{display:block;color:var(--muted);font-weight:500;font-size:.75rem;
text-transform:uppercase;letter-spacing:.04em;margin-bottom:.15rem}
.turn{display:grid;grid-template-columns:88px 1fr;gap:.75rem;
padding:.7rem 0;border-bottom:1px solid var(--line)}
.who{font-size:.75rem;font-weight:600;letter-spacing:.04em;padding:.2rem .45rem;
border-radius:5px;height:fit-content;text-align:center}
.agent .who{color:var(--agent);background:var(--agent-bg)}
.human .who{color:var(--human);background:var(--human-bg)}
.text{white-space:pre-wrap}
.time{color:var(--muted);font-size:.7rem;margin-top:.2rem}
.note{margin-top:2rem;padding:.9rem 1rem;border-left:3px solid var(--line);
color:var(--muted);font-size:.85rem}
@media print{body{padding:0}.turn{break-inside:avoid}}
"""

_LABEL = {"AGENT": "AGENT", "HUMAN": "SPEAKER", "UNKNOWN": "SPEAKER"}


def _fmt_time(value) -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    return value.strftime("%H:%M:%S") if value else "--"


def render_session_html(session, turns, person, out_path: str | Path | None = None) -> Path:
    started = session.started_at
    title = f"Conversation - {person.name if person else session.person_id}"
    date_str = started.strftime("%A %d %B %Y") if started else ""

    rows = []
    for t in turns:
        role = "agent" if t.speaker == "AGENT" else "human"
        rows.append(
            f'<div class="turn {role}">'
            f'<div><span class="who">{_LABEL.get(t.speaker, "SPEAKER")}</span></div>'
            f'<div><div class="text">{html.escape(t.text or "")}</div>'
            f'<div class="time">{_fmt_time(t.timestamp)}</div></div></div>')

    duration = session.duration_seconds or 0
    meta = [
        ("Person", person.name if person else session.person_id),
        ("Date", date_str),
        ("Started", _fmt_time(started)),
        ("Duration", f"{int(duration // 60)}m {int(duration % 60)}s"),
        ("Sentinel", session.sentinel_id),
        ("Status", session.status),
        ("Turns", str(len(turns))),
        ("Session", session.id[:8]),
    ]
    meta_html = "".join(f"<div><b>{k}</b>{html.escape(str(v))}</div>" for k, v in meta)

    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{_CSS}</style></head>
<body><div class="wrap">
<h1>{html.escape(title)}</h1>
<p class="sub">{date_str}</p>
<div class="meta">{meta_html}</div>
{"".join(rows) if rows else "<p>No turns were recorded.</p>"}
<div class="note">
Automatically generated from an AI companion conversation. Speech was
transcribed by an automatic system and may contain errors. Turns are
labelled from which side held the floor, not from voice recognition, so
where more than one person was present the SPEAKER label does not
identify which of them was talking.
</div>
</div></body></html>"""

    if out_path is None:
        stamp = started.strftime("%Y%m%d_%H%M%S") if started else "session"
        out_path = REPORTS_DIR / f"conversation_{stamp}_{session.id[:8]}.html"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")
    return out_path
