"""Annotate EVERY exchange in one hour, so graphrag retrieval can be scored.

    python make_graphrag_slice.py --day 2026-09-17 --from 18:00 --to 19:00

This is not the family-dynamics instrument. tests/diagnostics/dynamics_pilot
asks how warm or tense an exchange was, on a five-point scale, to measure the
READING. This asks a different question -- was this one of the quarrels? -- on
a binary, to measure the RETRIEVAL. The two are not interchangeable and their
answers are not comparable.

Why a complete slice, and why an hour
-------------------------------------
Section 3.4 could not measure retrieval because the coded sample was drawn at
random before the questions were asked: of everything graphrag returned across
four questions, one event fell inside a coded exchange. A pre-drawn random
sample cannot evaluate top-k retrieval, and no amount of extra random coding
fixes it.

Recall needs the COMPLETE relevant set. That is unobtainable over a week and
trivial over an hour: the store averages 10.6 exchanges in a busy hour, so one
slice is about ten judgements. Scope the query to the slice and precision and
recall both become computable, because inside the slice nothing is unjudged.

Pick the hour before looking at any output, and rotate it across the day --
always annotating dinner would measure dinner.

What the judge is NOT shown, and why
------------------------------------
* the dimensions L1.5 scored, its `evidence` sentence, its interaction types,
  and the residents it named -- being told what the machine concluded before
  judging whether it was right is the whole failure this exists to detect;
* the transcript -- as in the dynamics codebook, coding from ASR output
  measures the transcriber. Audio only.

What it IS shown is time, room and the clips.

The population is every candidate exchange, not every stored one
----------------------------------------------------------------
build_semantic_windows decides what is worth interpreting; the graphrag store
holds only those the model then read successfully. An exchange that was a
quarrel but came back uninterpretable can never be retrieved, and hiding it
would report the retrieval layer's recall as the system's. So the sheet covers
the candidate set and records `in_store` per item, which lets both be reported:
recall over the store is the retriever's, recall over the slice is the
system's.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
# find_audio and the stylesheet are shared with the dynamics sheets on purpose:
# both tools play clips out of the same store, and a second copy of the lookup
# would be a second place for it to go wrong.
PILOT = os.path.join(PROJECT_ROOT, "tests", "diagnostics", "dynamics_pilot")
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, PILOT)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                            # pragma: no cover
    pass

from make_sheets import find_audio, raw_events, CONFIG, STYLE  # noqa: E402
from ast_dash.llm_report.derive_home_dynamics import (        # noqa: E402
    load_config, derive_level0, build_semantic_windows)

SHEETS = os.path.join(HERE, "sheets")
RESIDENTS = ["Ilyas", "Voula", "Rafael"]

# One question per retrieval question graphrag is asked, worded for a listener
# rather than for an operation. Binary, because retrieval evaluation needs
# relevance and not intensity -- and because a binary is both cheaper to answer
# and more reliable than a five-point scale.
JUDGEMENTS = [
    ("quarrel", "Is this one of the quarrels?",
     "Sharp words, an argument, an accusation, real irritation between the "
     "people here. Not merely a complaint about someone absent."),
    ("warm", "Is this a warm moment?",
     "Affection, care, reassurance, appreciation, praise. Ordinary politeness "
     "is not warmth."),
    ("complaint", "Is someone complaining?",
     "A grievance is voiced, about anyone or anything -- the person here, "
     "someone absent, an outsider, or a circumstance."),
    ("rebuff", "Did someone reach out and get turned away?",
     "Somebody addressed another person and the reply refused it: a brush-off, "
     "a change of subject, a flat answer that closed the topic. Only score it "
     "if you can HEAR the refusal; a gap is unrecorded time, not a silence."),
]
VERDICTS = [("yes", "Yes"), ("no", "No"), ("unclear", "Cannot tell")]


def pairs_of(residents):
    """Every dyad, plus the answers that mean this is not a dyad at all."""
    out = []
    for i, a in enumerate(residents):
        for b in residents[i + 1:]:
            out.append((f"{a}|{b}", f"{a} and {b}"))
    out.append(("all", "All three"))
    out.append(("alone", "One person alone"))
    out.append(("outside", "Not between residents (TV, phone, a visitor)"))
    out.append(("unclear", "Cannot tell"))
    return out


def in_window(start, day, since, until):
    if start is None or start.strftime("%Y-%m-%d") != day:
        return False
    clock = start.strftime("%H:%M")
    return since <= clock < until


def stored_exchanges(day):
    """source_event_id sets for the exchanges graphrag actually holds.

    Matched by event ids rather than by exchange id: an id encodes the start
    time and room, so any change to how turns are grouped renames every
    exchange, while the clips stay the same clips.
    """
    try:
        from graphrag.engine import Graph
        graph = Graph.load(since=day, until=day)
    except Exception as exc:                                  # noqa: BLE001
        print(f"  (store not readable: {exc}; in_store left unknown)")
        return None
    return [{str(i) for i in (x.get("source_event_ids") or [])}
            for x in graph.exchanges]


def collect(day, since, until):
    cfg = load_config(CONFIG)
    level0 = derive_level0(raw_events(day), cfg)
    windows = build_semantic_windows(level0, cfg)
    stored = stored_exchanges(day)
    # A turn knows its event but not its file; the observation holds the file.
    audio_of = {str(o.event_id): o.source_file for o in level0}

    items = []
    for window in windows:
        if not in_window(window["start"], day, since, until):
            continue
        events = [str(e) for e in window["source_event_ids"]]
        held = None
        if stored is not None:
            have = set(events)
            held = any(len(have & s) > 0 for s in stored)
        items.append({
            "item_id": f"{day}_{window['start'].strftime('%H%M%S')}_"
                       f"{(window['room'] or 'room').replace(' ', '')}",
            "start": window["start"].isoformat(timespec="seconds"),
            "room": window["room"],
            "turn_count": len(window["turns"]),
            "word_count": window["word_count"],
            "event_ids": events,
            "audio_files": [audio_of.get(e) for e in events],
            "in_store": held,
        })
    items.sort(key=lambda i: i["start"])
    return items


def card(index, item, audio_map):
    clock = item["start"][11:19]
    players = []
    for event_id in item["event_ids"]:
        rel = audio_map.get(str(event_id))
        players.append(
            f'<div class="clip"><div class="t">event {event_id}</div>'
            + (f'<audio controls preload="none" src="../audio/{rel}"></audio>'
               if rel else '<div class="missing">audio missing</div>')
            + "</div>")

    heard = "".join(
        f'<label><input type="checkbox" name="{item["item_id"]}__heard"'
        f' value="{name}">{name}</label>' for name in RESIDENTS)
    heard += (f'<label><input type="checkbox" name="{item["item_id"]}__heard"'
              f' value="other">Someone else</label>')

    between = "".join(
        f'<label><input type="radio" name="{item["item_id"]}__between"'
        f' value="{v}">{html.escape(t)}</label>' for v, t in pairs_of(RESIDENTS))

    blocks = []
    for key, ask, hint in JUDGEMENTS:
        choices = "".join(
            f'<label><input type="radio" name="{item["item_id"]}__{key}"'
            f' value="{v}">{t}</label>' for v, t in VERDICTS)
        blocks.append(
            f'<fieldset><legend>{html.escape(ask)}</legend>'
            f'<div class="hint">{html.escape(hint)}</div>'
            f'<div class="row">{choices}</div></fieldset>')

    return f"""
<section class="card" data-id="{html.escape(item['item_id'])}">
  <div class="head">
    <div class="n">{index}</div>
    <div>
      <div><b>{clock}</b> &middot; {html.escape(item['room'] or 'room unknown')}</div>
      <div class="meta">{item['turn_count']} turn(s) &middot;
        {len(item['event_ids'])} clip(s)</div>
    </div>
  </div>
  <div class="clips">{''.join(players)}</div>
  <fieldset><legend>Whose voices do you hear?</legend>
    <div class="hint">Tick everyone you actually hear. Leave all unticked if
      you cannot tell.</div>
    <div class="row">{heard}</div>
  </fieldset>
  <fieldset><legend>Who is this between?</legend>
    <div class="row">{between}</div>
  </fieldset>
  {''.join(blocks)}
  <fieldset><legend>What was it about? <span class="meta">(one line, optional)</span></legend>
    <textarea name="{item['item_id']}__about"></textarea>
  </fieldset>
</section>"""


SCRIPT = """
/* The coder is part of the key. Two people judging the same slice in the same
   browser would otherwise overwrite each other's answers silently, and the
   downloaded file would not say whose judgements it held -- which is fatal
   when the whole point of a second coder is that the two can be compared. */
const KEY = 'graphrag_slice_%(label)s_%(coder)s', N = %(n)d;
const KEYS = %(keys)s;
function load(){ try { return JSON.parse(localStorage.getItem(KEY)||'{}'); }
                 catch(e){ return {}; } }
function save(s){ try { localStorage.setItem(KEY, JSON.stringify(s)); } catch(e){} }
function read(card){
  const id = card.dataset.id, out = {item_id:id};
  out.heard = Array.from(
    card.querySelectorAll('input[name="'+id+'__heard"]:checked')).map(e=>e.value);
  const b = card.querySelector('input[name="'+id+'__between"]:checked');
  out.between = b ? b.value : null;
  KEYS.forEach(k => {
    const p = card.querySelector('input[name="'+id+'__'+k+'"]:checked');
    out[k] = p ? p.value : null;
  });
  const a = card.querySelector('[name="'+id+'__about"]');
  out.about = a && a.value.trim() ? a.value.trim() : null;
  return out;
}
/* A card counts as done when every judgement is answered. `between` and the
   voices are attribution evidence and may legitimately be left blank. */
function complete(a){ return KEYS.every(k => a[k]); }
function refresh(){
  const state = {}; let done = 0;
  document.querySelectorAll('.card[data-id]').forEach(c => {
    const a = read(c); if (complete(a)) done++;
    c.classList.toggle('done', complete(a)); state[a.item_id] = a;
  });
  save(state);
  document.getElementById('progress').textContent = done+' of '+N+' done';
  document.getElementById('saved').textContent =
    'saved '+new Date().toLocaleTimeString();
  return state;
}
function restore(state){
  for (const [id, a] of Object.entries(state)) {
    const card = document.querySelector('[data-id="'+CSS.escape(id)+'"]');
    if (!card) continue;
    (a.heard||[]).forEach(v => {
      const el = card.querySelector('input[name="'+id+'__heard"][value="'+v+'"]');
      if (el) el.checked = true; });
    if (a.between) {
      const el = card.querySelector('input[name="'+id+'__between"][value="'+a.between+'"]');
      if (el) el.checked = true; }
    KEYS.forEach(k => { if (a[k]) {
      const el = card.querySelector('input[name="'+id+'__'+k+'"][value="'+a[k]+'"]');
      if (el) el.checked = true; } });
    const t = card.querySelector('[name="'+id+'__about"]');
    if (t && a.about) t.value = a.about;
  }
}
function download(){
  const state = refresh();
  const blob = new Blob([JSON.stringify(
    {annotated_at:new Date().toISOString(), slice:'%(label)s',
     coder:'%(coder)s', sheet:'graphrag_slice',
     answers:Object.values(state)}, null, 2)],
    {type:'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'graphrag_slice_%(label)s_%(coder)s.json'; a.click();
  URL.revokeObjectURL(a.href);
}
document.addEventListener('DOMContentLoaded', () => {
  restore(load()); refresh();
  document.addEventListener('change', refresh);
  document.addEventListener('input', e => {
    if (e.target.matches('textarea')) refresh(); });
  document.getElementById('download').addEventListener('click', download);
});
"""

INTRO = """
<div class="intro">
  <p>This is <b>every</b> exchange in one hour, in the order it happened.
  Nothing has been selected for you, and the system's own reading is not
  shown.</p>
  <p>Judge each one on its own, from the audio. The questions are the questions
  the system gets asked about the whole week &mdash; answering them here, for a
  complete hour, is what makes it possible to say both how much of what it
  returned belonged, and how much of what belonged it missed.</p>
  <p>&ldquo;Cannot tell&rdquo; is a real answer. Use it rather than guessing.</p>
</div>"""


def build(day, since, until, label, coder):
    items = collect(day, since, until)
    if not items:
        sys.exit(f"No exchanges between {since} and {until} on {day}.")

    audio_dir = os.path.join(SHEETS, "audio")
    os.makedirs(audio_dir, exist_ok=True)
    audio_map, copied, missing = {}, 0, 0
    for item in items:
        for event_id, name in zip(item["event_ids"], item["audio_files"]):
            if str(event_id) in audio_map:
                continue
            source = find_audio(name or "")
            if not source:
                missing += 1
                continue
            base = os.path.basename(source)
            target = os.path.join(audio_dir, base)
            if not os.path.exists(target):
                shutil.copyfile(source, target)
            audio_map[str(event_id)] = base
            copied += 1

    cards = "\n".join(card(i, it, audio_map) for i, it in enumerate(items, 1))
    script = SCRIPT % {"label": label, "n": len(items), "coder": coder,
                       "keys": json.dumps([k for k, *_ in JUDGEMENTS])}
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>graphrag slice &mdash; {day} {since}-{until} &mdash; {html.escape(coder)}</title>
<style>{STYLE}</style></head>
<body>
<header>
  <h1>Every exchange in this hour</h1>
  <span class="pill">{day} &nbsp;{since}&ndash;{until}</span>
  <span class="pill">coder: {html.escape(coder)}</span>
  <span class="grow"></span>
  <span class="pill" id="progress">0 of {len(items)} done</span>
</header>
<main>{INTRO}{cards}</main>
<div class="foot">
  <span class="saved" id="saved">&nbsp;</span>
  <button id="download">Download annotations</button>
</div>
<script>{script}</script>
</body></html>"""

    folder = os.path.join(SHEETS, f"slice_{coder}")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(page)

    frozen = os.path.join(HERE, f"slice.{label}.items.json")
    with open(frozen, "w", encoding="utf-8") as fh:
        json.dump({"day": day, "from": since, "until": until, "label": label,
                   "sheet": "graphrag_slice", "items": items},
                  fh, ensure_ascii=False, indent=2)

    known = [i for i in items if i["in_store"] is not None]
    held = sum(1 for i in known if i["in_store"])
    print(f"exchanges in slice  {len(items)}")
    if known:
        print(f"  in graphrag store {held}   "
              f"(the other {len(known) - held} can never be retrieved)")
    print(f"clips               {copied}"
          + (f"   ({missing} missing)" if missing else ""))
    print(f"sheet               {os.path.join(folder, 'index.html')}")
    print(f"frozen              {frozen}")
    print(f"\nServe it with:\n"
          f"  python -m http.server 8074 --directory \"{SHEETS}\"\n"
          f"then open  http://localhost:8074/slice_{coder}/index.html")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--day", required=True, help="YYYY-MM-DD")
    ap.add_argument("--from", dest="since", default="18:00", help="HH:MM")
    ap.add_argument("--to", dest="until", default="19:00", help="HH:MM")
    ap.add_argument("--label", default=None)
    ap.add_argument("--coder", default="A",
                    help="who is judging. A second coder needs their OWN "
                         "sheet: the browser key and the download are named "
                         "after them, so two people cannot overwrite each "
                         "other or hand in an unattributed file.")
    args = ap.parse_args(argv)
    for value in (args.since, args.until):
        datetime.strptime(value, "%H:%M")
    label = args.label or (args.day.replace("-", "")
                           + "_" + args.since.replace(":", ""))
    build(args.day, args.since, args.until, label, args.coder)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
