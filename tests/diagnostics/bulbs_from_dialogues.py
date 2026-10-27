#!/usr/bin/env python3
"""Render the lamps for each mock dialogue, side by side.

Runs every scenario in mock_dialogues.py through L0 -> L1.5 -> L3 -> L4 on its
own, derives the bulb states from the resulting relational dimensions, and
writes one HTML page showing all of them. If the lamps look the same across a
conflict, a warm exchange and a withdrawal, the semantic layer is not reaching
the dashboard.

    python tests/diagnostics/bulbs_from_dialogues.py --chatgpt
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import dash                                                        # noqa: E402
dash.Dash(__name__, use_pages=True, pages_folder="")
from ast_dash.pages import llm_report as LR                        # noqa: E402
import mock_dialogues as MD                                        # noqa: E402
import derive_home_dynamics as H                                   # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                                  # pragma: no cover
    pass


def to_html(component):
    """Minimal Dash -> HTML, enough to render the panel outside the app."""
    if component is None:
        return ""
    if isinstance(component, (str, int, float)):
        return str(component)
    if isinstance(component, (list, tuple)):
        return "".join(to_html(c) for c in component)
    props = dict(component.to_plotly_json().get("props", {}))
    children = props.pop("children", None)
    tag = {"Div": "div", "Img": "img", "P": "p", "H3": "h3",
           "Hr": "hr", "H5": "h5"}.get(type(component).__name__, "div")
    attrs = []
    for key, value in props.items():
        if key == "className":
            key = "class"
        if isinstance(value, (str, int, float)):
            attrs.append('%s="%s"' % (key, str(value).replace('"', "&quot;")))
    joined = (" " + " ".join(attrs)) if attrs else ""
    if tag == "img":
        return "<img%s>" % joined
    return "<%s%s>%s</%s>" % (tag, joined, to_html(children), tag)


def panel_for(name, interpreter):
    """One scenario end to end, returning (payload, panel_data)."""
    _cfg, level0, level1_5, level3, level4 = MD.run(interpreter, [name])
    payload = {"level_4_daily_dynamics": level4}
    semantics = LR.bulbs_from_semantics(payload)
    evidence = LR.part_b_evidence(payload)
    # No Part B model call here: the point is to show the lamps moving with the
    # coded exchanges alone, so the model's own reading is deliberately absent.
    data = LR.normalise_part_b({}, evidence, semantics=semantics)
    return level1_5, semantics, data


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--chatgpt", action="store_true")
    ap.add_argument("--model")
    ap.add_argument("--out", default=os.path.join(HERE, "bulbs_from_dialogues.html"))
    args = ap.parse_args(argv)

    interpreter = None
    if args.chatgpt or args.model:
        kind, model = ("openai", "gpt-4o-mini") if args.chatgpt \
            else ("ollama", args.model)
        print(f"interpreting with {kind}:{model}")
        interpreter = LR.make_semantic_interpreter(kind, model)

    sections = []
    for name, spec in MD.SCENARIOS.items():
        print(f"  {name} ...", flush=True)
        level1_5, semantics, data = panel_for(name, interpreter)
        states = " ".join(f"{k}={v['state']}" for k, v in sorted(semantics.items()))
        print(f"    {states or '(no semantic evidence)'}")
        transcript = "".join(
            f"<div class='mdline'><b>{s}</b> {t}</div>" for s, t in spec["turns"])
        sections.append(
            "<section class='scenario'>"
            f"<h2>{name.upper()}</h2>"
            f"<p class='blurb'>{spec['blurb']}</p>"
            f"<div class='transcript'>{transcript}</div>"
            f"{to_html(LR.render_part_b(data))}"
            "</section>")

    css = open(os.path.join(ROOT, "assets", "family_bulbs.css"), encoding="utf-8").read()
    page = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Bulbs react to dialogue</title><style>"
        "body{margin:0;padding:26px;background:#0B1016;color:#E7EDF3;"
        "font-family:system-ui,-apple-system,Segoe UI,sans-serif}"
        ".scenario{max-width:1180px;margin:0 auto 46px}"
        "h1{font-size:26px;margin:0 0 6px}"
        ".lede{color:#93A3B2;font-size:14px;max-width:70ch;margin:0 auto 34px;"
        "max-width:1180px}"
        "h2{font-size:15px;letter-spacing:.12em;color:#63AECE;margin:0 0 4px}"
        ".blurb{color:#93A3B2;font-size:13.5px;margin:0 0 14px}"
        ".transcript{background:#111921;border:1px solid #27333F;border-radius:10px;"
        "padding:14px 18px;margin-bottom:18px;font-size:14px;line-height:1.7}"
        ".mdline b{color:#63AECE;display:inline-block;min-width:70px}"
        "</style><style>" + css + "</style></head><body>"
        '<div class="lede"><h1>Do the lamps react to what was said?</h1>'
        "Each scenario below is a scripted exchange pushed through the real "
        "pipeline on its own. The lamp states are computed from the L1.5 "
        "relational dimensions alone &mdash; no second model judged them. "
        "If these panels look alike, the semantic layer is not reaching the "
        "dashboard.</div>"
        + "".join(sections) + "</body></html>")
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(page)
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
