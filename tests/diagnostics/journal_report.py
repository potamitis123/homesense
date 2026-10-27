#!/usr/bin/env python3
"""Render the journal report from results.json and a prose template.

    python tests/diagnostics/journal_report.py

Reads reports/journal_2026-09-10/report_template.md, fills every {{key}} from
the experiment results and writes:

    reports/journal_2026-09-10/results_section.txt      plain text, ready to paste
    reports/journal_2026-09-10/data/report_render.json  blocks for journal_build_docx.js

A placeholder with no value is an error, never a blank: every number in the
text comes from results.json or does not appear.

Template syntax, one block per blank-line-separated paragraph:
    TITLE: text          # text          ## text
    [[FIGURE:file_stem:width_in]] Fig. N. Caption...
    [[TABLE:T2]]
    anything else is a paragraph; *x* marks italic.
"""
import json
import os
import re
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
OUT = os.path.join(ROOT, "reports", "journal_2026-09-10")
DATA = os.path.join(OUT, "data")

RESIDENTS = ("Ilyas", "Voula", "Rafael")
WHO = {"Ilyas": "Ilyas (adult)", "Voula": "Voula (adult)", "Rafael": "Rafael (child)"}
CONFIG_KEYS = {"B1": "B1", "B2": "B2", "P4": "P4", "B1+B2": "B1B2", "B1+P4": "B1P4",
               "B2+P4": "B2P4", "B1+B2+P4": "ALL", "oracle": "ORACLE"}
SUBSETS = ("B1", "B2", "P4", "B1+B2", "B1+P4", "B2+P4", "B1+B2+P4", "none")


def pc(x, digits=1):
    return "–" if x is None else f"{100 * x:.{digits}f}%"


def span(ci, digits=1):
    return "–" if not ci or ci[0] is None else f"{100 * ci[0]:.{digits}f}–{100 * ci[1]:.{digits}f}%"


def f2(x):
    return "–" if x is None else f"{x:.2f}".replace("-", "−")


def sgn(x):
    return "–" if x is None else f"{x:+.2f}".replace("-", "−")


def pv(p):
    if p is None:
        return "–"
    return "< 0.001" if p < 0.001 else f"{p:.3f}"


def extent(values):
    got = [v for v in values if v is not None]
    return f"{f2(min(got))}–{f2(max(got))}" if got else "–"


# ── numbers ────────────────────────────────────────────────────────────
def numbers(res):
    n = {}
    c = res["corpus"]
    rec = res["recognition"]
    n["takes"] = str(c["takes"])
    n["lines"] = str(c["lines"])
    n["words"] = str(int(round(rec["B1+B2+P4"]["ref_words"])))
    for who, v in c["lines_by_speaker"].items():
        n[f"lines_{who.lower()}"] = str(v)
    n["minutes"] = f"{c['take_seconds_total'] / 60:.1f}"
    n["session_start"] = c["session"][0][11:16]
    n["session_end"] = c["session"][1][11:16]
    for dev, v in c["clips_by_device"].items():
        n[f"clips_{dev}"] = str(v)
    for dev, v in c["speech_clips_by_device"].items():
        n[f"speechclips_{dev}"] = str(v)
    n["clips_total"] = str(sum(c["clips_by_device"].values()))
    n["speechclips_total"] = str(sum(c["speech_clips_by_device"].values()))
    for g, v in c["groups"].items():
        n[f"group_{g}_n"] = str(v)

    for name, key in CONFIG_KEYS.items():
        r = rec[name]
        n[f"{key}_k"] = str(r["recognised"])
        n[f"{key}_recall"] = pc(r["recall"])
        n[f"{key}_recall_ci"] = span(r["recall_ci"])
        n[f"{key}_wer"] = pc(r["wer"])
        n[f"{key}_wer_ci"] = span(r["wer_ci"])
        n[f"{key}_cer"] = pc(r["cer"])
        n[f"{key}_spk"] = pc(r["speaker_correct"] / r["speaker_n"]) if r["speaker_n"] else "–"
        n[f"{key}_spk_k"] = str(r["speaker_correct"])
        n[f"{key}_spk_n"] = str(r["speaker_n"])
        n[f"{key}_raw"] = str(r["turns_raw"])
        n[f"{key}_kept"] = str(r["turns_kept"])
        n[f"{key}_merged"] = str(r["copies_merged"])
    n["oracle_gap_pts"] = f"{100 * (rec['B1+B2+P4']['wer'] - rec['oracle']['wer']):.1f}"
    n["ALL_missed"] = str(rec["B1+B2+P4"]["lines"] - rec["B1+B2+P4"]["recognised"])
    n["gap"] = str(res["provenance"].get("conversation_gap_seconds"))

    for name, key in CONFIG_KEYS.items():
        if name == "oracle":
            continue
        for who, v in res["recognition_by_speaker"][name].items():
            n[f"{key}_{who.lower()}_recall"] = pc(v["recall"], 0)
            n[f"{key}_{who.lower()}_wer"] = pc(v["wer"])
            n[f"{key}_{who.lower()}_k"] = str(v["recognised"])

    for mode, a in res["merge_ablation"].items():
        k = {"none": "abl_none", "containment": "abl_cont", "deployed": "abl_dep"}[mode]
        n[f"{k}_kept"] = str(a["turns_kept"])
        n[f"{k}_merged"] = str(a["copies_merged"])
        n[f"{k}_lost"] = str(a["lines_lost"])
        n[f"{k}_resid"] = str(a["residual_duplicates"])
        n[f"{k}_resid_cross"] = str(a["residual_cross"])
        n[f"{k}_k"] = str(a["recognised"])
        n[f"{k}_recall"] = pc(a["recall"])
        n[f"{k}_wer"] = pc(a["wer"])

    sp = res["speaker"]
    n["spk_acc"] = pc(sp["metrics"]["_accuracy"])
    n["spk_n"] = str(sp["metrics"]["_n"])
    n["spk_correct"] = str(sum(sp["confusion"][w][w] for w in RESIDENTS))
    n["spk_kappa"] = f2(sp["kappa"])
    n["spk_macro_f1"] = f2(sp["metrics"]["_macro_f1"])
    n["spk_unknown"] = str(sum(sp["confusion"][w]["Unknown"] for w in RESIDENTS))
    for who in RESIDENTS:
        m = sp["metrics"][who]
        n[f"spk_{who.lower()}_p"] = pc(m["precision"])
        n[f"spk_{who.lower()}_r"] = pc(m["recall"])
        n[f"spk_{who.lower()}_f1"] = f2(m["f1"])
        for other in RESIDENTS + ("Unknown",):
            n[f"conf_{who.lower()}_{other.lower()}"] = str(sp["confusion"][who][other])

    seg = res["segmentation"]
    for tag, key in (("off", "exchanges_merge_off"), ("on", "exchanges_merge_on")):
        vals = [r[key] for r in seg]
        n[f"seg_{tag}_total"] = str(sum(vals))
        n[f"seg_{tag}_mean"] = f"{np.mean(vals):.1f}"
        n[f"seg_{tag}_median"] = f"{np.median(vals):g}"
        n[f"seg_{tag}_max"] = str(max(vals))
        n[f"seg_{tag}_one"] = str(vals.count(1))
    for tag, key in (("off", "windows_merge_off"), ("on", "windows_merge_on")):
        n[f"seg_win_{tag}_total"] = str(sum(r[key] for r in seg))

    prov = res["provenance"]
    n["git_head"] = prov.get("git_head") or "–"
    n["boot"] = str(prov["bootstrap_resamples"])

    ov = res.get("overlap")
    if ov:
        lines_total = ov["lines"]
        for s in SUBSETS:
            key = s.replace("+", "")
            row = ov["by_subset"][s]
            n[f"ov_{key}_total"] = str(sum(row.values()))
            for who, v in row.items():
                n[f"ov_{key}_{who.lower()}"] = str(v)
        counts = {k: sum(ov["by_number"][w][str(k)] for w in RESIDENTS) for k in range(4)}
        for k, word in ((0, "none"), (1, "one"), (2, "two"), (3, "three")):
            n[f"ov_{word}"] = str(counts[k])
            n[f"ov_{word}_pct"] = pc(counts[k] / lines_total, 0)
        n["ov_multi"] = str(counts[2] + counts[3])
        n["ov_multi_pct"] = pc((counts[2] + counts[3]) / lines_total, 0)
        n["ov_union"] = str(ov["union"])
        n["ov_union_pct"] = pc(ov["union"] / lines_total)
        n["ov_fused_outside_union"] = str(ov["fused_outside_union"])
        n["ov_union_not_fused"] = str(ov["union_not_fused"])
        for who in RESIDENTS:
            bn = ov["by_number"][who]
            total = sum(bn.values())
            multi = bn["2"] + bn["3"]
            n[f"ov_{who.lower()}_multi"] = str(multi)
            n[f"ov_{who.lower()}_multi_pct"] = pc(multi / total, 0) if total else "–"
            n[f"ov_{who.lower()}_single"] = str(bn["1"])
            n[f"ov_{who.lower()}_single_pct"] = pc(bn["1"] / total, 0) if total else "–"
            n[f"ov_{who.lower()}_none"] = str(bn["0"])

    if "semantic" in res:
        sem = res["semantic"]
        runs = sem["runs"]
        dims = sem["dimensions"]
        n["model"] = (sem.get("model") or "").split(":")[-1]
        n["llm_runs"] = str(runs)
        n["llm_runs_word"] = {1: "one", 2: "two", 3: "three", 4: "four",
                              5: "five"}.get(runs, str(runs))
        n["llm_date"] = (sem.get("run_at") or "")[:10]
        n["readings_per_condition"] = str(runs * res["corpus"]["takes"])
        n["readings_total"] = str(2 * runs * res["corpus"]["takes"])
        n["usable_text"] = str(sem["usable_readings"]["text"])
        n["usable_rec"] = str(sem["usable_readings"]["recorded"])
        for d in dims:
            b = sem["between"][d]
            n[f"{d}_n"] = str(b["n"])
            n[f"{d}_mae"] = f2(b["mae"])
            n[f"{d}_bias"] = sgn(b["bias"])
            n[f"{d}_r"] = f2(b["pearson_r"])
            n[f"{d}_r_p"] = pv(b["pearson_p"])
            n[f"{d}_rho"] = f2(b["spearman_rho"])
            n[f"{d}_rho_p"] = pv(b["spearman_p"])
            n[f"{d}_within"] = f"{b['within_025']}/{b['n']}"
            n[f"{d}_within_pct"] = pc(b["within_025"] / b["n"], 0) if b["n"] else "–"
            n[f"{d}_run_mae"] = f2(b["run_paired_mae"])
            for cond, tag in (("text", "text"), ("recorded", "rec")):
                r = sem["retest"][cond][d]
                n[f"{d}_icc_{tag}"] = f2(r["icc_2_1"])
                n[f"{d}_mad_{tag}"] = f2(r["mean_abs_diff"])
                n[f"{d}_same_{tag}"] = pc(r["identical_share"], 0)
        orders = sem["orderings"]
        cons = sem["contrasts"]
        n["order_total"] = str(len(orders))
        n["contrast_total"] = str(len(cons))
        for cond, tag in (("text", "text"), ("recorded", "rec")):
            ce = sem["centroid"][cond]
            n[f"centroid_{tag}"] = f"{ce['correct']}/{ce['total']}"
            n[f"centroid_{tag}_k"] = str(ce["correct"])
            n[f"centroid_{tag}_n"] = str(ce["total"])
            n[f"centroid_{tag}_acc"] = pc(ce["accuracy"])
            n[f"centroid_{tag}_ci"] = span(ce["accuracy_ci"])
            n[f"centroid_{tag}_p"] = pv(ce["binomial_p"])
            for g, row in ce["confusion"].items():
                n[f"centroid_{tag}_{g}_hit"] = str(row.get(g, 0))
            rp = sem["repair"][cond]
            n[f"repair_{tag}_sens"] = pc(rp["sensitivity"], 0)
            n[f"repair_{tag}_fpr"] = pc(rp["false_positive_rate"], 0)
            n[f"repair_{tag}_majority"] = f"{rp['majority_hits']}/{rp['expected']}"
            n[f"order_{tag}_holds"] = str(sum(1 for o in orders if o.get(cond) and o[cond]["holds"]))
            n[f"order_{tag}_ex0"] = str(sum(1 for o in orders if o.get(cond) and o[cond]["ci_excludes_zero"]))
            ps = [o[cond]["mw_p"] for o in orders if o.get(cond) and o[cond]["mw_p"] is not None]
            n[f"order_{tag}_testable"] = str(len(ps))
            n[f"order_{tag}_sig"] = str(sum(1 for p in ps if p < 0.05))
            n[f"contrast_{tag}_holds"] = f"{sum(1 for x in cons if x[cond]['holds'])}/{len(cons)}"
            for i, o in enumerate(orders, 1):
                v = o.get(cond)
                if v:
                    n[f"order{i}_{tag}_delta"] = sgn(v["delta"])
                    n[f"order{i}_{tag}_d"] = f2(v["delta"])
                    n[f"order{i}_{tag}_ci"] = f"{sgn(v['ci'][0])} to {sgn(v['ci'][1])}"
                    n[f"order{i}_{tag}_ci_plain"] = f"{f2(v['ci'][0])} to {f2(v['ci'][1])}"
                    n[f"order{i}_{tag}_mw_p"] = pv(v["mw_p"])
            for g, dvals in sem["group_means"][cond].items():
                for d, v in dvals.items():
                    n[f"gm_{tag}_{g}_{d}"] = f2(v)
            n[f"icc_range_{tag}"] = extent([sem["retest"][cond][d]["icc_2_1"] for d in dims])
            n[f"mad_range_{tag}"] = extent([sem["retest"][cond][d]["mean_abs_diff"] for d in dims])
        n["centroid_chance"] = pc(sem["centroid"]["recorded"]["chance"])
        p_text = sem["centroid"]["text"]["binomial_p"]
        p_rec = sem["centroid"]["recorded"]["binomial_p"]
        n["centroid_p_phrase"] = (
            "*p* < 0.001 in both conditions" if max(p_text, p_rec) < 0.001
            else f"*p* = {pv(p_text)} for the original text and {pv(p_rec)} for the recordings")
        n["mae_range"] = extent([sem["between"][d]["mae"] for d in dims])
        n["run_mae_range"] = extent([sem["between"][d]["run_paired_mae"] for d in dims])
        rs = {d: sem["between"][d]["pearson_r"] for d in dims
              if sem["between"][d]["pearson_r"] is not None}
        n["r_range"] = extent(list(rs.values()))
        n["r_min_dim"] = min(rs, key=rs.get)
        n["r_max_dim"] = max(rs, key=rs.get)
        n["r_p_max"] = pv(max(sem["between"][d]["pearson_p"] for d in dims
                              if sem["between"][d]["pearson_p"] is not None))
        n["bias_absmax"] = f2(max(abs(sem["between"][d]["bias"]) for d in dims))
        shares = [sem["between"][d]["within_025"] / sem["between"][d]["n"]
                  for d in dims if sem["between"][d]["n"]]
        n["within_range"] = f"{100 * min(shares):.0f}–{100 * max(shares):.0f}%"
        dev = sem["take_deviation"]
        top = max((k for k in dev if dev[k] is not None), key=lambda k: dev[k])
        n["wd_top_take"] = top
        n["wd_top_title"] = res["corpus"]["titles"][top]
        n["wd_top_dev"] = f2(dev[top])
        n["wd_top_wer"] = pc(res["take_recognition"][top]["wer"])
        n["take_wer_median"] = pc(float(np.median([v["wer"] for v in res["take_recognition"].values()])))
        wd = res.get("wer_vs_deviation") or {}
        n["wd_rho"] = f2(wd.get("spearman_rho"))
        n["wd_p"] = pv(wd.get("spearman_p"))
        n["wd_n"] = str(wd.get("n"))
    return n


# ── tables ─────────────────────────────────────────────────────────────
def recognition_row(rec, key, label):
    r = rec[key]
    return [label, f"{r['recognised']}/{r['lines']} ({pc(r['recall'])})", span(r["recall_ci"]),
            pc(r["wer"]), span(r["wer_ci"]), pc(r["cer"]),
            f"{r['speaker_correct']}/{r['speaker_n']} ({pc(r['speaker_correct'] / r['speaker_n'])})"
            if r["speaker_n"] else "–"]


def tables(res, n):
    t = {}
    c = res["corpus"]
    rec = res["recognition"]
    t["T1"] = {
        "caption": "Table 1. Evaluation corpus.",
        "header": ["Characteristic", "Value"], "widths": [3, 4],
        "rows": [
            ["Scripted dialogues (takes)", c["takes"]],
            ["Scripted lines", c["lines"]],
            ["Reference words", n["words"]],
            ["Lines per speaker", ", ".join(f"{w} {v}" for w, v in c["lines_by_speaker"].items())],
            ["Design groups (takes)", ", ".join(f"{g} {v}" for g, v in c["groups"].items())],
            ["Session", f"{c['session'][0][:16]} to {c['session'][1][11:16]}"],
            ["Total take duration", f"{c['take_seconds_total'] / 60:.1f} min"],
            ["Sentinels", "B1, B2 (ESP32-S3-BOX-3) and P4 (ESP32-P4), each in a different room"],
            ["Clips delivered (B1 / B2 / P4)",
             " / ".join(str(c["clips_by_device"][d]) for d in ("B1", "B2", "P4"))],
            ["Clips with transcribed speech (B1 / B2 / P4)",
             " / ".join(str(c["speech_clips_by_device"][d]) for d in ("B1", "B2", "P4"))],
        ]}

    recog_header = ["Condition", "Lines recognised", "95% CI", "WER", "95% CI", "CER", "Speaker correct"]
    recog_widths = [2.6, 2.0, 1.9, 1.2, 1.9, 1.2, 2.0]
    recog_note = ("CI: 95% Wilson interval for the share of lines, 95% bootstrap interval over "
                  "takes for the word error rate (WER). CER: character error rate.")
    t["T2"] = {
        "caption": "Table 2. Recognition of the scripted lines by the three sentinels together.",
        "header": recog_header, "widths": recog_widths,
        "rows": [recognition_row(rec, "B1+B2+P4", "Deployed merge of the three sentinels"),
                 recognition_row(rec, "oracle", "Oracle choice among the copies")],
        "note": recog_note + (" The oracle keeps every copy and lets the alignment choose "
                              "the best one for each line using the script.")}

    ov = res["overlap"]
    by = res["recognition_by_speaker"]["B1+B2+P4"]
    t["T3"] = {
        "caption": "Table 3. Recognition by speaker with the three sentinels together.",
        "header": ["Speaker", "Lines", "Recognised", "WER", "Lines delivered by one sentinel",
                   "Lines delivered by two or three"],
        "widths": [2.0, 1.0, 1.7, 1.2, 2.3, 2.3],
        "rows": [[WHO[w], by[w]["lines"], f"{by[w]['recognised']} ({pc(by[w]['recall'])})",
                  pc(by[w]["wer"]), ov["by_number"][w]["1"],
                  ov["by_number"][w]["2"] + ov["by_number"][w]["3"]] for w in RESIDENTS],
        "note": ("The last two columns count the lines of which exactly one, or two or three, "
                 "sentinels, each scored on its own, delivered a recognisable copy.")}

    ab = res["merge_ablation"]
    t["T4"] = {
        "caption": "Table 4. Merging copies of one utterance heard by several sentinels.",
        "header": ["Merge rule", "Turns passed on", "Copies removed", "Lines lost",
                   "Residual cross-sentinel copies", "Lines recognised", "WER"],
        "widths": [2.6, 1.5, 1.4, 1.2, 2.0, 1.6, 1.2],
        "rows": [[a["label"].capitalize(), a["turns_kept"], a["copies_merged"], a["lines_lost"],
                  a["residual_cross"], f"{a['recognised']}/{a['lines']}", pc(a["wer"])]
                 for a in (ab["none"], ab["containment"], ab["deployed"])],
        "note": ("Lines lost: lines that some copy matched but that the rule removed. Residual "
                 "cross-sentinel copies: surviving turns from another sentinel that best match "
                 "a line already recognised.")}

    sp = res["speaker"]
    rows = [[w, pc(sp["metrics"][w]["precision"]), pc(sp["metrics"][w]["recall"]),
             f2(sp["metrics"][w]["f1"]), sp["metrics"][w]["support"]] for w in RESIDENTS]
    rows.append(["Macro average", "", "", f2(sp["metrics"]["_macro_f1"]),
                 f"accuracy {pc(sp['metrics']['_accuracy'])}, κ = {f2(sp['kappa'])}"])
    t["T5"] = {"caption": "Table 5. Speaker identification on the recognised lines.",
               "header": ["Speaker", "Precision", "Recall", "F1", "Support"],
               "widths": [2.6, 1.5, 1.5, 1.2, 2.8], "rows": rows,
               "note": "κ: Cohen's kappa."}

    if "semantic" in res:
        sem = res["semantic"]
        dims = sem["dimensions"]
        names = res["corpus"]["group_names"]
        t["T6"] = {
            "caption": "Table 6. The relational reading from the original text and from the recordings.",
            "header": ["Measure", "Original text", "Recordings"],
            "widths": [4.6, 2.2, 2.2],
            "rows": [
                ["Readings with usable scores", f"{n['usable_text']}/{n['readings_per_condition']}",
                 f"{n['usable_rec']}/{n['readings_per_condition']}"],
                ["Run-to-run stability, ICC(2,1), range over dimensions",
                 n["icc_range_text"], n["icc_range_rec"]],
                ["Mean difference between runs, range over dimensions",
                 n["mad_range_text"], n["mad_range_rec"]],
                ["Agreement with the reading of the original text, MAE (range over dimensions)",
                 "reference", n["mae_range"]],
                ["Agreement with the reading of the original text, Pearson r (range over dimensions)",
                 "reference", n["r_range"]],
                ["Pre-specified orderings holding, 95% CI excluding zero",
                 f"{n['order_text_ex0']}/{n['order_total']}", f"{n['order_rec_ex0']}/{n['order_total']}"],
                ["Orderings with one-sided Mann–Whitney p < 0.05",
                 f"{n['order_text_sig']}/{n['order_text_testable']}",
                 f"{n['order_rec_sig']}/{n['order_rec_testable']}"],
                ["Paired contrasts holding", n["contrast_text_holds"], n["contrast_rec_holds"]],
                ["Takes assigned to their design group (nearest centroid; chance 12.5%)",
                 f"{n['centroid_text']} ({n['centroid_text_acc']})",
                 f"{n['centroid_rec']} ({n['centroid_rec_acc']})"],
                ["Open-conflict takes assigned correctly",
                 f"{n['centroid_text_P5_hit']}/{n['group_P5_n']}",
                 f"{n['centroid_rec_P5_hit']}/{n['group_P5_n']}"],
                ["Repair recognised in the repair takes (share of readings)",
                 n["repair_text_sens"], n["repair_rec_sens"]],
                ["Repair asserted in the other takes (share of readings)",
                 n["repair_text_fpr"], n["repair_rec_fpr"]],
            ],
            "note": (f"{n['llm_runs_word'].capitalize()} independent readings of each of the "
                     f"{n['takes']} takes per condition. Takes assigned: leave-one-out "
                     "nearest-centroid classification into the eight design groups.")}
        t["T7"] = {
            "caption": f"Table 7. Run-to-run stability of the relational reading ({sem['runs']} independent runs per take).",
            "header": ["Dimension", "ICC(2,1), original text", "ICC(2,1), recording",
                       "Mean |Δ| between runs, original text", "Mean |Δ| between runs, recording"],
            "widths": [1.8, 1.8, 1.8, 2.3, 2.3],
            "rows": [[d.capitalize(), f2(sem["retest"]["text"][d]["icc_2_1"]),
                      f2(sem["retest"]["recorded"][d]["icc_2_1"]),
                      f2(sem["retest"]["text"][d]["mean_abs_diff"]),
                      f2(sem["retest"]["recorded"][d]["mean_abs_diff"])] for d in dims],
            "note": "ICC(2,1): two-way random effects, absolute agreement, single rating, over takes scored in every run."}
        t["T8"] = {
            "caption": "Table 8. Agreement between the reading of the recording and the reading of the original text.",
            "header": ["Dimension", "Takes", "MAE", "Bias", "Pearson r", "Spearman ρ", "Within ±0.25"],
            "widths": [2.0, 1.1, 1.1, 1.1, 1.8, 1.8, 1.6],
            "rows": [[d.capitalize(), sem["between"][d]["n"], f2(sem["between"][d]["mae"]),
                      sgn(sem["between"][d]["bias"]),
                      f"{f2(sem['between'][d]['pearson_r'])} ({pv(sem['between'][d]['pearson_p'])})",
                      f"{f2(sem['between'][d]['spearman_rho'])} ({pv(sem['between'][d]['spearman_p'])})",
                      f"{sem['between'][d]['within_025']} ({pc(sem['between'][d]['within_025'] / sem['between'][d]['n'], 0)})"
                      if sem["between"][d]["n"] else "–"] for d in dims],
            "note": ("Per-take means over runs. Bias: mean of recording minus original text. "
                     "p-values in parentheses. Takes: takes scored on that dimension in both conditions.")}
        t["T9"] = {
            "caption": "Table 9. Pre-specified orderings between design groups.",
            "header": ["Ordering", "Groups (n)", "Original text: Δ [95% CI]",
                       "Recording: Δ [95% CI]", "Recording: p"],
            "widths": [2.5, 1.2, 2.4, 2.4, 1.3],
            "rows": [[f"{o['dimension'].capitalize()}: {o['greater']} > {o['less']}",
                      f"{o['recorded']['n_greater']} vs {o['recorded']['n_less']}" if o.get("recorded") else "–",
                      f"{sgn(o['text']['delta'])} [{sgn(o['text']['ci'][0])}, {sgn(o['text']['ci'][1])}]" if o.get("text") else "–",
                      f"{sgn(o['recorded']['delta'])} [{sgn(o['recorded']['ci'][0])}, {sgn(o['recorded']['ci'][1])}]" if o.get("recorded") else "–",
                      pv(o["recorded"]["mw_p"]) if o.get("recorded") else "–"]
                     for o in sem["orderings"]],
            "note": ("Δ: difference between group means on the 0–1 scale; 95% bootstrap interval "
                     "resampling takes within groups. p: one-sided Mann–Whitney U on the recordings; "
                     "not computed where a group holds a single take. Groups: "
                     + "; ".join(f"{g} {names[g]}" for g in names) + ".")}
        rows = []
        symbol = {"<": "<", ">": ">", "<=": "≤", ">=": "≥"}
        for con in sem["contrasts"]:
            dims_ = list(con["expect"])

            def show(cond, con=con, dims_=dims_):
                parts = con[cond]["dimensions"]
                return "; ".join(f"{d} {f2(parts[d]['a'])} {symbol[parts[d]['op']]} "
                                 f"{f2(parts[d]['b'])}" for d in dims_)
            rows.append([f"#{con['a']} vs #{con['b']}: {con['name'].replace('->', '→')}",
                         show("text"), show("recorded"),
                         f"{'yes' if con['text']['holds'] else 'no'} / {'yes' if con['recorded']['holds'] else 'no'}"])
        t["T10"] = {"caption": "Table 10. Paired contrasts between single takes.",
                    "header": ["Contrast", "Original text", "Recording", "Holds (text / recording)"],
                    "widths": [3.4, 2.4, 2.4, 1.6], "rows": rows,
                    "note": "Values are per-take means over runs; each contrast compares two single takes."}

    t["S1"] = {
        "caption": ("Table S1. Recognition by each sentinel and each pair of sentinels on its own. "
                    "Because the residents moved between rooms during the takes, these figures "
                    "describe where the speakers were rather than the quality of the units."),
        "header": ["Sentinels", "Lines recognised", "95% CI", "WER", "95% CI", "CER", "Speaker correct"],
        "widths": recog_widths,
        "rows": [recognition_row(rec, k, k.replace("+", " and ")) for k in
                 ("B1", "B2", "P4", "B1+B2", "B1+P4", "B2+P4")],
        "note": recog_note}
    return t


# ── rendering ──────────────────────────────────────────────────────────
PLACEHOLDER = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


def fill(text, nums):
    missing = [m.group(1) for m in PLACEHOLDER.finditer(text) if m.group(1) not in nums]
    if missing:
        raise KeyError(f"no value for: {', '.join(sorted(set(missing)))}")
    return PLACEHOLDER.sub(lambda m: nums[m.group(1)], text)


def plain(text):
    return re.sub(r"\*([^*]+)\*", r"\1", text)


def render(template, nums, tabs):
    blocks, lines = [], []
    for raw in re.split(r"\n\s*\n", template.strip()):
        para = " ".join(line.strip() for line in raw.strip().splitlines())
        if not para or para.startswith("<!--"):
            continue
        para = fill(para, nums)
        if para.startswith("TITLE: "):
            blocks.append({"type": "title", "text": para[7:]})
            lines += [plain(para[7:]), ""]
        elif para.startswith("## "):
            blocks.append({"type": "h2", "text": para[3:]})
            lines += [plain(para[3:]), ""]
        elif para.startswith("# "):
            blocks.append({"type": "h1", "text": para[2:]})
            lines += [plain(para[2:]).upper(), ""]
        elif para.startswith("[[FIGURE:"):
            m = re.match(r"\[\[FIGURE:([\w\-]+)(?::([\d.]+))?\]\]\s*(.*)", para)
            stem, width, caption = m.group(1), float(m.group(2) or 6.3), m.group(3)
            label = " ".join(caption.split()[:2]) if caption.startswith("Fig") else stem
            blocks.append({"type": "figure", "file": f"{stem}.png", "width_in": width,
                           "caption": caption, "label": label})
            lines += [f"[{label} — figures/{stem}.png]", plain(caption), ""]
        elif para.startswith("[[TABLE:"):
            key = re.match(r"\[\[TABLE:(\w+)\]\]", para).group(1)
            tab = tabs[key]
            blocks.append({"type": "table", **tab,
                           "rows": [[str(v) for v in row] for row in tab["rows"]]})
            lines.append(plain(tab["caption"]))
            lines.append("\t".join(tab["header"]))
            for row in tab["rows"]:
                lines.append("\t".join(str(v) for v in row))
            if tab.get("note"):
                lines.append(plain(tab["note"]))
            lines.append("")
        else:
            blocks.append({"type": "p", "text": para})
            lines += [plain(para), ""]
    return blocks, "\n".join(lines)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    with open(os.path.join(DATA, "results.json"), encoding="utf-8") as fh:
        res = json.load(fh)
    nums = numbers(res)
    tabs = tables(res, nums)
    with open(os.path.join(DATA, "numbers.json"), "w", encoding="utf-8") as fh:
        json.dump(nums, fh, ensure_ascii=False, indent=1, sort_keys=True)
    template_path = os.path.join(OUT, "report_template.md")
    if not os.path.exists(template_path):
        print(f"wrote numbers.json ({len(nums)} values); no template yet at {template_path}")
        return 0
    with open(template_path, encoding="utf-8") as fh:
        blocks, text = render(fh.read(), nums, tabs)
    with open(os.path.join(DATA, "report_render.json"), "w", encoding="utf-8") as fh:
        json.dump(blocks, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(OUT, "results_section.txt"), "w", encoding="utf-8") as fh:
        fh.write(text)
    print(f"rendered {len(blocks)} blocks; wrote results_section.txt and report_render.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
