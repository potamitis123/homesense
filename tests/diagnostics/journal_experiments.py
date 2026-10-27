#!/usr/bin/env python3
"""Experiments behind the journal report on the recorded 2026-09-10 corpus.

    python tests/diagnostics/journal_experiments.py --llm-runs 3 --refresh-llm
    python tests/diagnostics/journal_experiments.py          # reuse cached LLM runs

Every number the report states is computed here from four inputs: the events
the live pipeline wrote to ast_audio.db, the script (dialogues_gr.txt), the
group design (dialogue_groups.json) and the take manifest (takes_fixed.txt).
Results go to reports/journal_2026-09-10/data/; journal_figures.py draws from
results.json alone, so a figure can never disagree with the text.

The LLM readings are the one expensive and non-deterministic input, so they
are cached in data/llm_runs.json together with the model and a hash of the
prompt. A rerun reuses them unless --refresh-llm is given: the tables can be
rebuilt without re-reading, and so perturbing, the interpretations.

  E1  recognition by sentinel configuration (7 subsets and an oracle)
  E2  recognition by speaker and configuration
  E3  cross-sentinel merge ablation
  E4  speaker identification
  E5  emotion recognition
  E6  relational reading: recording against script, run-to-run stability,
      group structure, orderings, contrasts, repair
  E7  take level: does recognition error predict reading error?
  E8  segmentation in the deployed report (exchanges per take)
"""
from __future__ import annotations

import argparse
import copy
import csv
import difflib
import hashlib
import itertools
import json
import math
import os
import random
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import numpy as np
from scipy import stats

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
for _p in (HERE, os.path.join(ROOT, "ast_dash", "llm_report"), ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import derive_home_dynamics as H                                   # noqa: E402
import run_dialogue_suite as S                                     # noqa: E402
import score_recorded as R                                         # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:                                                  # pragma: no cover
    pass

OUT = os.path.join(ROOT, "reports", "journal_2026-09-10")
DATA = os.path.join(OUT, "data")
DEVICES = ("box3-1", "box3-2", "p4-door-1")
SHORT = {"box3-1": "B1", "box3-2": "B2", "p4-door-1": "P4"}
CONFIGS = [("B1", ("box3-1",)), ("B2", ("box3-2",)), ("P4", ("p4-door-1",)),
           ("B1+B2", ("box3-1", "box3-2")), ("B1+P4", ("box3-1", "p4-door-1")),
           ("B2+P4", ("box3-2", "p4-door-1")), ("B1+B2+P4", DEVICES)]
RESIDENTS = ("Ilyas", "Voula", "Rafael")
EMOTIONS = ("happy", "sad", "angry", "neutral")
UNNAMED = {None, "", "Unknown", "unidentified"}
CONDITIONS = ("text", "recorded")
SEED = 20260910
BOOT = 5000


# ── corpus ──────────────────────────────────────────────────────────────
def load_corpus():
    cfg = H.load_config(os.path.join(ROOT, "ast_dash", "llm_report",
                                     "home_dynamics_config.json"))
    scen = {s[0]: s for s in S.parse_file(os.path.join(HERE, "dialogues_gr.txt"))}
    spec = S.load_groups(os.path.join(HERE, "dialogue_groups.json"))
    takes = []
    for t in sorted(R.load_manifest(os.path.join(HERE, "takes_fixed.txt")),
                    key=lambda t: t["scenario"]):
        _n, title, script, _dynamic = scen[t["scenario"]]
        takes.append({**t, "title": title, "script": script,
                      "events": R.load_window(t["since"], t["until"], None)})
    return cfg, scen, spec, takes


def as_row(turn):
    return {"speaker": turn.speaker, "text": turn.text or "",
            "event": turn.event_id, "device": turn.device,
            "start": turn.start, "end": turn.end,
            "emotion": (turn.emotion or {}).get("label")}


def merge_variant(turns, cfg, same_speaker=True):
    """The deployed merge rule with its speaker condition made optional.

    Used only by the E3 ablation. With same_speaker=True it must reproduce
    H.merge_cross_sentinel_turns exactly; main() asserts that on every take,
    so the ablation cannot quietly drift from the rule it ablates.
    """
    thresholds = cfg.get("thresholds", {})
    slack = float(thresholds.get("cross_sentinel_slack_seconds", 3.0))
    floor = float(thresholds.get("cross_sentinel_min_containment", 0.7))
    order = sorted(turns, key=lambda t: (-len(H._flat_text(t.text).split()), t.start))
    kept, pairs = [], []
    for turn in order:
        text = H._flat_text(turn.text)
        match = None
        if text and turn.device:
            turn_end = turn.end or turn.start
            for other in kept:
                if not other.device or other.device == turn.device:
                    continue
                if (same_speaker and turn.speaker not in UNNAMED
                        and other.speaker not in UNNAMED
                        and turn.speaker != other.speaker):
                    continue
                if ((turn.start - (other.end or other.start)).total_seconds() > slack
                        or (other.start - turn_end).total_seconds() > slack):
                    continue
                other_text = H._flat_text(other.text)
                if len(text) < 8:
                    same = difflib.SequenceMatcher(None, text, other_text).ratio() >= 0.8
                else:
                    same = H._contained(text, other_text) >= floor
                if same:
                    match = other
                    break
        if match is None:
            kept.append(turn)
        else:
            pairs.append((turn, match))
    kept.sort(key=lambda t: (t.start, str(t.event_id)))
    return kept, pairs


def turns_for(take, devices, cfg, mode="deployed"):
    """Turns one configuration of sentinels delivers for one take.

    mode: "none" keeps every copy, "containment" merges without the speaker
    condition, "deployed" is the rule the report runs.
    """
    events = [e for e in take["events"] if e["Device"] in devices]
    level0 = H.derive_level0(events, cfg)
    raw = [t for t in H._collect_turns(level0, cfg, merge=False)
           if (t.text or "").strip()]
    if mode == "none":
        kept, pairs = raw, []
    elif mode == "containment":
        kept, pairs = merge_variant(raw, cfg, same_speaker=False)
    else:
        kept, pairs = H.merge_cross_sentinel_turns(raw, cfg)
    return level0, raw, kept, pairs


# ── statistics helpers ─────────────────────────────────────────────────
def wilson(k, n, z=1.959964):
    if not n:
        return [None, None]
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return [(c - h) / d, (c + h) / d]


def pct(values):
    return [float(np.percentile(values, 2.5)), float(np.percentile(values, 97.5))]


def icc_2_1(Y):
    """ICC(2,1): two-way random effects, absolute agreement, single rating."""
    Y = np.asarray(Y, float)
    n, k = Y.shape
    if n < 3 or k < 2:
        return None
    grand = Y.mean()
    ms_rows = k * ((Y.mean(axis=1) - grand) ** 2).sum() / (n - 1)
    ms_cols = n * ((Y.mean(axis=0) - grand) ** 2).sum() / (k - 1)
    resid = Y - Y.mean(axis=1, keepdims=True) - Y.mean(axis=0, keepdims=True) + grand
    ms_err = (resid ** 2).sum() / ((n - 1) * (k - 1))
    denom = ms_rows + (k - 1) * ms_err + k * (ms_cols - ms_err) / n
    if denom <= 1e-12:
        return None        # every rating identical: agreement is undefined, not zero
    value = (ms_rows - ms_err) / denom
    return 0.0 if abs(value) < 1e-9 else float(value)


def kappa(conf, labels):
    M = np.array([[conf.get(a, {}).get(b, 0) for b in labels] for a in labels], float)
    n = M.sum()
    if not n:
        return None
    po = np.trace(M) / n
    pe = float(M.sum(axis=1) @ M.sum(axis=0)) / (n * n)
    return float((po - pe) / (1 - pe)) if pe < 1 else None


def confusion(lines, truth, pred, rows, cols, other):
    conf = {r: {c: 0 for c in cols} for r in rows}
    for line in lines:
        if not line["matched"] or line[truth] not in conf:
            continue
        guess = line[pred] if line[pred] in cols else other
        conf[line[truth]][guess] += 1
    return conf


def class_metrics(conf, rows):
    out = {}
    for r in rows:
        tp = conf[r].get(r, 0)
        fn = sum(conf[r].values()) - tp
        fp = sum(conf[o].get(r, 0) for o in rows if o != r)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        out[r] = {"precision": precision, "recall": recall, "f1": f1, "support": tp + fn}
    total = sum(sum(conf[r].values()) for r in rows)
    out["_accuracy"] = sum(conf[r].get(r, 0) for r in rows) / total if total else None
    out["_macro_f1"] = sum(out[r]["f1"] for r in rows) / len(rows)
    out["_n"] = total
    return out


# ── E1-E3: recognition ─────────────────────────────────────────────────
def score_condition(takes, cfg, ac, devices, mode="deployed"):
    """Per-line records and per-take counts for one configuration."""
    lines, per_take = [], []
    for take in takes:
        _level0, raw, kept, pairs = turns_for(take, devices, cfg, mode)
        rows = [as_row(k) for k in kept]
        aligned, extra = R.align(take["script"], rows)
        for index, ((speaker, ref, match, ratio), (_s, _t, emotion)) in enumerate(
                zip(aligned, take["script"])):
            hyp = match["text"] if match else ""
            pair = [(ac.normalise(ref), ac.normalise(hyp))]
            w = ac.error_rate(pair, "word")
            c = ac.error_rate(pair, "char")
            lines.append({
                "take": take["scenario"], "line": index, "speaker": speaker,
                "emotion": emotion, "matched": match is not None,
                "similarity": round(float(ratio), 3), "reference": ref, "hypothesis": hyp,
                "pred_speaker": match["speaker"] if match else None,
                "pred_emotion": match["emotion"] if match else None,
                "device": match["device"] if match else None,
                "w_err": w["rate"] * w["reference_units"], "w_ref": w["reference_units"],
                "c_err": c["rate"] * c["reference_units"], "c_ref": c["reference_units"],
            })
        matched_lines = {i for i, (_s, _x, m, _r) in enumerate(aligned) if m}
        residual = residual_cross = 0
        for turn in extra:
            scores = [difflib.SequenceMatcher(None, R.norm(line), R.norm(turn["text"])).ratio()
                      for _s, line, _e in take["script"]]
            best = max(range(len(scores)), key=scores.__getitem__)
            if scores[best] >= 0.45 and best in matched_lines:
                residual += 1
                residual_cross += aligned[best][2]["device"] != turn["device"]
        per_take.append({"take": take["scenario"], "raw": len(raw), "kept": len(kept),
                         "merged": len(pairs), "extras": len(extra),
                         "residual_duplicates": residual, "residual_cross": residual_cross})
    return lines, per_take


def summarise(lines, per_take, rng):
    n = len(lines)
    k = sum(line["matched"] for line in lines)
    takes = sorted({line["take"] for line in lines})
    agg = np.array([[sum(l["matched"] for l in lines if l["take"] == t),
                     sum(1 for l in lines if l["take"] == t),
                     sum(l["w_err"] for l in lines if l["take"] == t),
                     sum(l["w_ref"] for l in lines if l["take"] == t)] for t in takes], float)
    draws = rng.integers(0, len(takes), size=(BOOT, len(takes)))
    sums = agg[draws].sum(axis=1)
    matched = [l for l in lines if l["matched"]]
    tagged = [l for l in matched if l["emotion"]]
    return {
        "lines": n, "recognised": k, "recall": k / n, "recall_ci": wilson(k, n),
        "recall_boot_ci": pct(sums[:, 0] / sums[:, 1]),
        "wer": sum(l["w_err"] for l in lines) / sum(l["w_ref"] for l in lines),
        "wer_ci": pct(sums[:, 2] / sums[:, 3]),
        "cer": sum(l["c_err"] for l in lines) / sum(l["c_ref"] for l in lines),
        "ref_words": sum(l["w_ref"] for l in lines),
        "speaker_correct": sum(1 for l in matched if l["pred_speaker"] == l["speaker"]),
        "speaker_n": len(matched),
        "emotion_correct": sum(1 for l in tagged if (l["pred_emotion"] or "none") == l["emotion"]),
        "emotion_n": len(tagged),
        "turns_raw": sum(p["raw"] for p in per_take),
        "turns_kept": sum(p["kept"] for p in per_take),
        "copies_merged": sum(p["merged"] for p in per_take),
        "extras": sum(p["extras"] for p in per_take),
        "residual_duplicates": sum(p["residual_duplicates"] for p in per_take),
        "residual_cross": sum(p["residual_cross"] for p in per_take),
    }


def by_speaker(lines):
    out = {}
    for who in RESIDENTS:
        mine = [l for l in lines if l["speaker"] == who]
        k = sum(l["matched"] for l in mine)
        out[who] = {"lines": len(mine), "recognised": k,
                    "recall": k / len(mine) if mine else None,
                    "recall_ci": wilson(k, len(mine)),
                    "wer": (sum(l["w_err"] for l in mine) / sum(l["w_ref"] for l in mine))
                    if mine else None}
    return out


# ── E6: relational reading ─────────────────────────────────────────────
def record_dict(rec, dims):
    if rec is None:
        return None
    usable = rec.semantic_status in H.SEMANTIC_USABLE
    return {"status": rec.semantic_status,
            "confidence": rec.semantic_confidence,
            "types": list(rec.interaction_types or []),
            "dimensions": ({d: rec.relational_dimensions.get(d) for d in dims}
                           if usable else None)}


def llm_readings(takes, scen, cfg, dims, runs, workers, refresh):
    """Read every take `runs` times per condition; `refresh` names the
    conditions to read again, the rest come from the cache."""
    path = os.path.join(DATA, "llm_runs.json")
    cached = None
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            cached = json.load(fh)
    todo = [c for c in CONDITIONS if c in refresh or not (cached or {}).get(c)]
    if cached and not todo:
        print(f"  LLM readings: reusing {path} ({cached.get('model')}, "
              f"{cached.get('runs_per_condition')} runs)")
        return cached

    import dash
    dash.Dash(__name__, use_pages=True, pages_folder="")
    from ast_dash.pages import llm_report as LR
    interpreter = LR.make_semantic_interpreter("openai", "gpt-4o-mini")
    if interpreter is None:
        raise SystemExit("no L1.5 prompt available; cannot run the reading")
    prompt = LR.load_prompt_from_file("LLM_prompt_L1_5.txt")
    prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    kept = [c for c in CONDITIONS if c not in todo]
    if cached and kept and (cached.get("prompt_sha256") != prompt_sha
                            or int(cached.get("runs_per_condition", 0)) != runs):
        raise SystemExit("the prompt or the run count changed since the cached readings; "
                         "refresh both conditions so they are read alike")

    def patient(payload):
        """The interpreter, retried through rate limits.

        A 429 is the API declining to answer, not the model reading the take,
        yet stored as a reading it is indistinguishable from an uninterpretable
        exchange. The first full run lost 59 of 120 recorded readings that way.
        Refusals are waited out with backoff; one that never clears is marked
        'failed' by the caller and excluded from every measure.
        """
        delay = 5.0
        for attempt in range(8):
            try:
                return interpreter(payload)
            except RuntimeError as exc:
                message = str(exc)
                if ("429" not in message and "Rate limit" not in message) or attempt == 7:
                    raise
                time.sleep(delay + random.uniform(0.0, 2.0))
                delay = min(delay * 2, 60.0)

    def one(job):
        condition, take, run = job
        failed = []

        def guarded(payload):
            try:
                return patient(payload)
            except Exception:
                failed.append(True)
                raise

        if condition == "text":
            rec = S.interpret(scen[take["scenario"]], guarded, cfg)[3]
        else:
            whole = []
            for obs in H.derive_level0(take["events"], cfg):
                joined = copy.copy(obs)
                joined.room = "(all sentinels)"
                whole.append(joined)
            records = H.derive_level1_5(whole, cfg, interpreter=guarded, cache={})
            rec = records[0] if records else None
        reading = record_dict(rec, dims)
        if failed:
            reading = {"status": "failed", "confidence": None, "types": [], "dimensions": None}
        return condition, take["scenario"], run, reading

    now = datetime.now().isoformat(timespec="seconds")
    out = cached or {}
    out.update({"model": "openai:gpt-4o-mini", "prompt_sha256": prompt_sha,
                "runs_per_condition": runs, "run_at": now})
    out.setdefault("run_at_by_condition", {})
    for c in todo:
        out[c] = {}
        out["run_at_by_condition"][c] = now
    jobs = [(c, t, r) for c in todo for t in takes for r in range(runs)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, job) for job in jobs]
        for done, future in enumerate(as_completed(futures), 1):
            condition, number, run, rec = future.result()
            out[condition].setdefault(str(number), [None] * runs)[run] = rec
            if done % 20 == 0 or done == len(jobs):
                print(f"  LLM readings {done}/{len(jobs)}", flush=True)
    failures = sum(1 for c in todo for readings in out[c].values()
                   for r in readings if (r or {}).get("status") == "failed")
    print(f"  readings that failed after retries: {failures}")
    os.makedirs(DATA, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    return out


def semantic_analysis(llm, spec, takes, rng):
    dims = spec["dimensions"]
    groups = spec["groups"]
    numbers = [t["scenario"] for t in takes]
    runs = int(llm["runs_per_condition"])

    def run_values(condition, n, d):
        return [((rec or {}).get("dimensions") or {}).get(d)
                for rec in (llm[condition].get(str(n)) or [])]

    def avg(values):
        got = [v for v in values if v is not None]
        return sum(got) / len(got) if got else None

    means = {c: {n: {d: avg(run_values(c, n, d)) for d in dims} for n in numbers}
             for c in CONDITIONS}
    usable = {c: sum(1 for n in numbers for rec in (llm[c].get(str(n)) or [])
                     if rec and rec.get("dimensions")) for c in CONDITIONS}
    statuses = {c: dict(Counter((rec or {}).get("status", "missing")
                                for n in numbers for rec in (llm[c].get(str(n)) or [])))
                for c in CONDITIONS}

    retest = {}
    for c in CONDITIONS:
        retest[c] = {}
        for d in dims:
            rows = [run_values(c, n, d) for n in numbers]
            full = [r for r in rows if len(r) >= 2 and all(v is not None for v in r)]
            diffs = [abs(a - b) for r in rows
                     for a, b in itertools.combinations([v for v in r if v is not None], 2)]
            retest[c][d] = {
                "icc_2_1": icc_2_1(full) if len(full) >= 3 else None,
                "mean_abs_diff": float(np.mean(diffs)) if diffs else None,
                "identical_share": float(np.mean([len(set(r)) == 1 for r in full])) if full else None,
                "n_takes": len(full)}

    between = {}
    for d in dims:
        pairs = [(means["text"][n][d], means["recorded"][n][d]) for n in numbers
                 if means["text"][n][d] is not None and means["recorded"][n][d] is not None]
        x = np.array([p[0] for p in pairs], float)
        y = np.array([p[1] for p in pairs], float)
        run_paired = [abs(a - b) for n in numbers
                      for a, b in zip(run_values("text", n, d), run_values("recorded", n, d))
                      if a is not None and b is not None]
        varied = len(x) > 2 and x.std() > 0 and y.std() > 0
        pr = stats.pearsonr(x, y) if varied else None
        sr = stats.spearmanr(x, y) if varied else None
        between[d] = {
            "n": len(pairs),
            "mae": float(np.mean(np.abs(x - y))) if len(x) else None,
            "bias": float(np.mean(y - x)) if len(x) else None,
            "pearson_r": float(pr.statistic) if pr else None,
            "pearson_p": float(pr.pvalue) if pr else None,
            "spearman_rho": float(sr.statistic) if sr else None,
            "spearman_p": float(sr.pvalue) if sr else None,
            "within_025": int(np.sum(np.abs(x - y) <= 0.25 + 1e-9)),
            "run_paired_mae": float(np.mean(run_paired)) if run_paired else None,
            "points": [[float(a), float(b)] for a, b in pairs]}

    group_means = {c: {g: {d: avg([means[c][n][d] for n in grp["scenarios"]]) for d in dims}
                       for g, grp in groups.items()} for c in CONDITIONS}

    orderings = []
    for rule in spec.get("orderings", []):
        d, hi, lo = rule["dimension"], rule["greater"], rule["less"]
        row = {"dimension": d, "greater": hi, "less": lo, "note": rule["note"]}
        for c in CONDITIONS:
            A = np.array([means[c][n][d] for n in groups[hi]["scenarios"]
                          if means[c][n][d] is not None], float)
            B = np.array([means[c][n][d] for n in groups[lo]["scenarios"]
                          if means[c][n][d] is not None], float)
            if not len(A) or not len(B):
                row[c] = None
                continue
            boot = (A[rng.integers(0, len(A), size=(BOOT, len(A)))].mean(axis=1)
                    - B[rng.integers(0, len(B), size=(BOOT, len(B)))].mean(axis=1))
            ci = pct(boot)
            mw = (stats.mannwhitneyu(A, B, alternative="greater")
                  if len(A) > 1 and len(B) > 1 else None)
            row[c] = {"delta": float(A.mean() - B.mean()), "ci": ci,
                      "holds": bool(A.mean() > B.mean()), "ci_excludes_zero": bool(ci[0] > 0),
                      "n_greater": int(len(A)), "n_less": int(len(B)),
                      "mw_p": float(mw.pvalue) if mw else None}
        orderings.append(row)

    ops = {"<": lambda a, b: a < b, ">": lambda a, b: a > b,
           "<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b}
    contrasts = []
    for con in spec.get("contrasts", []):
        row = {"a": con["a"], "b": con["b"], "name": con["name"], "expect": con["expect"]}
        for c in CONDITIONS:
            va, vb = means[c].get(con["a"]), means[c].get(con["b"])
            parts, ok = {}, True
            for d, op in con["expect"].items():
                x, y = (va or {}).get(d), (vb or {}).get(d)
                good = x is not None and y is not None and ops[op](x, y)
                ok &= good
                parts[d] = {"a": x, "b": y, "op": op, "holds": good}
            row[c] = {"holds": ok, "dimensions": parts}
        contrasts.append(row)

    member = {n: g for g, grp in groups.items() for n in grp["scenarios"]}
    centroid = {}
    for c in CONDITIONS:
        vecs = {n: means[c][n] for n in numbers
                if any(v is not None for v in means[c][n].values())}
        conf = {g: {h: 0 for h in groups} for g in groups}
        correct = total = 0
        for n, vec in vecs.items():
            best, best_d = None, None
            for g, grp in groups.items():
                peers = [vecs[m] for m in grp["scenarios"] if m in vecs and m != n]
                if not peers:
                    continue
                cen = {d: S._mean([p[d] for p in peers]) for d in dims}
                dist = S._distance(vec, cen)
                if dist is not None and (best_d is None or dist < best_d):
                    best, best_d = g, dist
            if best is None:
                continue
            conf[member[n]][best] += 1
            total += 1
            correct += best == member[n]
        test = stats.binomtest(correct, total, 1 / len(groups), alternative="greater")
        centroid[c] = {"confusion": conf, "correct": correct, "total": total,
                       "accuracy": correct / total if total else None,
                       "accuracy_ci": wilson(correct, total),
                       "chance": 1 / len(groups), "binomial_p": float(test.pvalue)}

    repair_types = set(H.REPAIR_TYPES)
    expected = list(spec.get("repair_expected", []))
    others = [n for n in numbers if n not in expected]
    repair = {}
    for c in CONDITIONS:
        def flagged(n, r):
            recs = llm[c].get(str(n)) or []
            rec = recs[r] if r < len(recs) else None
            return bool(rec and set(rec.get("types") or []) & repair_types)
        hits = [sum(flagged(n, r) for n in expected) for r in range(runs)]
        false = [sum(flagged(n, r) for n in others) for r in range(runs)]
        majority = sum(1 for n in expected if sum(flagged(n, r) for r in range(runs)) * 2 > runs)
        repair[c] = {"expected": len(expected), "others": len(others),
                     "hits_per_run": hits, "false_per_run": false,
                     "sensitivity": float(np.mean(hits)) / len(expected),
                     "false_positive_rate": float(np.mean(false)) / len(others),
                     "majority_hits": majority}

    deviation = {}
    for n in numbers:
        diffs = [abs(means["recorded"][n][d] - means["text"][n][d]) for d in dims
                 if means["recorded"][n][d] is not None and means["text"][n][d] is not None]
        deviation[n] = float(np.mean(diffs)) if diffs else None

    return {"dimensions": dims, "usable_readings": usable, "statuses": statuses, "runs": runs,
            "means": {c: {str(n): v for n, v in means[c].items()} for c in CONDITIONS},
            "retest": retest, "between": between, "group_means": group_means,
            "orderings": orderings, "contrasts": contrasts, "centroid": centroid,
            "repair": repair, "take_deviation": {str(n): v for n, v in deviation.items()}}


# ── E8: segmentation in the deployed report ────────────────────────────
def segmentation(takes, cfg):
    off = copy.deepcopy(cfg)
    off["thresholds"]["cross_sentinel_merge"] = False
    rows = []
    for take in takes:
        level0 = H.derive_level0(take["events"], cfg)

        def spoken(c):
            return [cl for cl in H.cluster_speech_turns(level0, c)
                    if any((t.text or "").strip() for t in cl)]
        rows.append({"take": take["scenario"],
                     "exchanges_merge_off": len(spoken(off)),
                     "exchanges_merge_on": len(spoken(cfg)),
                     "windows_merge_off": len(H.build_semantic_windows(level0, off)),
                     "windows_merge_on": len(H.build_semantic_windows(level0, cfg))})
    return rows


# ── output ─────────────────────────────────────────────────────────────
def write_csv(name, header, rows):
    with open(os.path.join(DATA, name), "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def provenance(cfg):
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True).stdout.strip()
    except Exception:
        head = None
    try:
        with open(os.path.join(ROOT, "common_files", "parameters.json"), encoding="utf-8") as fh:
            params = json.load(fh)
    except Exception:
        params = {}
    th = cfg["thresholds"]
    return {"git_head": head, "generated": datetime.now().isoformat(timespec="seconds"),
            "merge": {k: th.get(k) for k in ("cross_sentinel_merge",
                                             "cross_sentinel_slack_seconds",
                                             "cross_sentinel_min_containment")},
            "conversation_gap_seconds": th.get("conversation_gap_seconds"),
            "dedupe": params.get("dedupe"),
            "asr_language": (params.get("asr") or {}).get("language"),
            "bootstrap_resamples": BOOT, "seed": SEED}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--llm-runs", type=int, default=3)
    ap.add_argument("--refresh-llm", nargs="?", const="text,recorded", default="",
                    help="conditions to read again (text, recorded); the bare flag means both")
    ap.add_argument("--skip-llm", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args(argv)

    os.makedirs(DATA, exist_ok=True)
    rng = np.random.default_rng(SEED)
    cfg, scen, spec, takes = load_corpus()
    ac = R._wer_tools()
    print(f"corpus: {len(takes)} takes, {sum(len(t['script']) for t in takes)} scripted lines")

    for take in takes:          # the ablation must match the deployed rule
        _l0, raw, kept, _p = turns_for(take, DEVICES, cfg, "deployed")
        mine, _ = merge_variant(raw, cfg, same_speaker=True)
        assert [id(t) for t in mine] == [id(t) for t in kept], take["scenario"]

    results = {"provenance": provenance(cfg)}

    # corpus summary
    all_lines = [line for t in takes for line in t["script"]]
    events = [e for t in takes for e in t["events"]]
    span = sum((datetime.fromisoformat(t["until"]) - datetime.fromisoformat(t["since"])).total_seconds()
               for t in takes)
    results["corpus"] = {
        "takes": len(takes), "lines": len(all_lines),
        "lines_by_speaker": {w: sum(1 for s, _t, _e in all_lines if s == w) for w in RESIDENTS},
        "lines_by_emotion": {e: sum(1 for _s, _t, x in all_lines if x == e) for e in EMOTIONS},
        "groups": {g: len(grp["scenarios"]) for g, grp in spec["groups"].items()},
        "group_names": {g: grp["name"] for g, grp in spec["groups"].items()},
        "clips_by_device": {SHORT[d]: sum(1 for e in events if e["Device"] == d) for d in DEVICES},
        "speech_clips_by_device": {SHORT[d]: sum(1 for e in events if e["Device"] == d
                                               and (e["transcription"] or "").strip()
                                               and not (e["transcription"] or "").startswith("No Speech"))
                                   for d in DEVICES},
        "session": [takes[0]["since"], max(t["until"] for t in takes)],
        "take_seconds_total": span,
        "titles": {str(t["scenario"]): t["title"] for t in takes}}

    # E1/E2
    print("E1-E2 recognition by configuration ...", flush=True)
    configs, speakers_by_config, lines_by_config = {}, {}, {}
    for name, devices in CONFIGS:
        lines, per_take = score_condition(takes, cfg, ac, devices, "deployed")
        configs[name] = summarise(lines, per_take, rng)
        speakers_by_config[name] = by_speaker(lines)
        lines_by_config[name] = lines
    lines, per_take = score_condition(takes, cfg, ac, DEVICES, "none")
    configs["oracle"] = summarise(lines, per_take, rng)
    lines_by_config["oracle"] = lines
    results["recognition"] = configs
    results["recognition_by_speaker"] = speakers_by_config
    fused = lines_by_config["B1+B2+P4"]
    heard_by = {w: {SHORT[d]: sum(1 for l in fused if l["speaker"] == w and l["device"] == d)
                    for d in DEVICES} for w in RESIDENTS}
    results["heard_by"] = heard_by

    # E9: which sentinels, each scored on its own, delivered a recognisable
    # copy of each line. The residents walked between rooms while performing,
    # so a single sentinel's share of lines records where they were, not how
    # good the unit is; how the sentinels overlapped is what the single runs
    # are good for.
    alone = {name: {(l["take"], l["line"]) for l in lines_by_config[name] if l["matched"]}
             for name in ("B1", "B2", "P4")}
    fused_set = {(l["take"], l["line"]) for l in fused if l["matched"]}
    subsets = ("B1", "B2", "P4", "B1+B2", "B1+P4", "B2+P4", "B1+B2+P4", "none")
    by_subset = {s: {w: 0 for w in RESIDENTS} for s in subsets}
    by_number = {w: {k: 0 for k in range(4)} for w in RESIDENTS}
    union = fused_outside_union = union_not_fused = 0
    for line in fused:
        key = (line["take"], line["line"])
        members = [s for s in ("B1", "B2", "P4") if key in alone[s]]
        by_subset["+".join(members) if members else "none"][line["speaker"]] += 1
        by_number[line["speaker"]][len(members)] += 1
        union += bool(members)
        fused_outside_union += (key in fused_set) and not members
        union_not_fused += bool(members) and key not in fused_set
    results["overlap"] = {"by_subset": by_subset, "by_number": by_number, "union": union,
                          "fused": len(fused_set), "lines": len(fused),
                          "fused_outside_union": fused_outside_union,
                          "union_not_fused": union_not_fused}

    # E3
    print("E3 merge ablation ...", flush=True)
    ablation = {}
    reference = {(l["take"], l["line"]) for l in lines_by_config["oracle"] if l["matched"]}
    for mode, label in (("none", "no merge"), ("containment", "containment only"),
                        ("deployed", "containment + same speaker")):
        lines, per_take = score_condition(takes, cfg, ac, DEVICES, mode)
        summary = summarise(lines, per_take, rng)
        summary["lines_lost"] = sum(1 for l in lines
                                    if (l["take"], l["line"]) in reference and not l["matched"])
        summary["label"] = label
        ablation[mode] = summary
    results["merge_ablation"] = ablation

    # E4/E5
    print("E4-E5 speaker and emotion ...", flush=True)
    spk_cols = RESIDENTS + ("Unknown",)
    spk_conf = confusion(fused, "speaker", "pred_speaker", RESIDENTS, spk_cols, "Unknown")
    emo_cols = EMOTIONS + ("none",)
    emo_conf = confusion(fused, "emotion", "pred_emotion", EMOTIONS, emo_cols, "none")
    results["speaker"] = {"confusion": spk_conf, "metrics": class_metrics(spk_conf, RESIDENTS),
                          "kappa": kappa(spk_conf, list(spk_cols)),
                          "by_config": {name: [configs[name]["speaker_correct"], configs[name]["speaker_n"]]
                                        for name, _d in CONFIGS}}
    results["emotion"] = {"confusion": emo_conf, "metrics": class_metrics(emo_conf, EMOTIONS),
                          "kappa": kappa(emo_conf, list(emo_cols)),
                          "predicted": {c: sum(emo_conf[r][c] for r in EMOTIONS) for c in emo_cols}}

    # E8
    print("E8 segmentation ...", flush=True)
    results["segmentation"] = segmentation(takes, cfg)

    # E6/E7
    per_take_wer = {}
    for t in takes:
        mine = [l for l in fused if l["take"] == t["scenario"]]
        per_take_wer[t["scenario"]] = {
            "wer": sum(l["w_err"] for l in mine) / sum(l["w_ref"] for l in mine),
            "recall": sum(l["matched"] for l in mine) / len(mine)}
    results["take_recognition"] = {str(n): v for n, v in per_take_wer.items()}
    if not args.skip_llm:
        print("E6 relational reading ...", flush=True)
        llm = llm_readings(takes, scen, cfg, spec["dimensions"], args.llm_runs, args.workers,
                           {c.strip() for c in args.refresh_llm.split(",") if c.strip()})
        sem = semantic_analysis(llm, spec, takes, rng)
        sem["model"] = llm.get("model")
        sem["prompt_sha256"] = llm.get("prompt_sha256")
        sem["run_at"] = llm.get("run_at")
        results["semantic"] = sem
        xs, ys = [], []
        for n, rec in per_take_wer.items():
            dev = sem["take_deviation"].get(str(n))
            if dev is not None:
                xs.append(rec["wer"])
                ys.append(dev)
        rho = stats.spearmanr(xs, ys)
        results["wer_vs_deviation"] = {"n": len(xs), "spearman_rho": float(rho.statistic),
                                       "spearman_p": float(rho.pvalue),
                                       "points": [[float(a), float(b)] for a, b in zip(xs, ys)]}

    # tables
    write_csv("table_recognition.csv",
              ["configuration", "recognised", "lines", "recall", "recall_ci_lo", "recall_ci_hi",
               "wer", "wer_ci_lo", "wer_ci_hi", "cer", "speaker_correct", "speaker_n",
               "turns_kept", "copies_merged"],
              [[name, c["recognised"], c["lines"], round(c["recall"], 4),
                round(c["recall_ci"][0], 4), round(c["recall_ci"][1], 4), round(c["wer"], 4),
                round(c["wer_ci"][0], 4), round(c["wer_ci"][1], 4), round(c["cer"], 4),
                c["speaker_correct"], c["speaker_n"], c["turns_kept"], c["copies_merged"]]
               for name, c in configs.items()])
    write_csv("table_lines_fused.csv",
              ["take", "line", "speaker", "emotion", "matched", "similarity", "pred_speaker",
               "pred_emotion", "device", "reference", "hypothesis"],
              [[l["take"], l["line"], l["speaker"], l["emotion"], l["matched"], l["similarity"],
                l["pred_speaker"], l["pred_emotion"], l["device"], l["reference"], l["hypothesis"]]
               for l in fused])
    with open(os.path.join(DATA, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1,
                  default=lambda o: o.item() if hasattr(o, "item") else str(o))
    print(f"wrote {os.path.join(DATA, 'results.json')}")

    # console digest
    for name, c in configs.items():
        print(f"  {name:<9} recall {c['recognised']:>3}/{c['lines']} ({c['recall']:.1%})  "
              f"WER {c['wer']:.1%} [{c['wer_ci'][0]:.1%}, {c['wer_ci'][1]:.1%}]  "
              f"speaker {c['speaker_correct']}/{c['speaker_n']}")
    for mode, a in ablation.items():
        print(f"  merge {a['label']:<28} kept {a['turns_kept']:>3}  merged {a['copies_merged']:>3}"
              f"  lost {a['lines_lost']}  residual dup {a['residual_duplicates']}"
              f" (cross {a['residual_cross']})  WER {a['wer']:.1%}")
    ov = results["overlap"]
    print(f"  overlap: union {ov['union']}, fused {ov['fused']}, fused outside union "
          f"{ov['fused_outside_union']}, union not fused {ov['union_not_fused']}")
    for s, row in ov["by_subset"].items():
        print(f"    {s:<9} {sum(row.values()):>3}  {row}")
    print(f"    by number of sentinels: {ov['by_number']}")
    if "semantic" in results:
        sem = results["semantic"]
        for d in sem["dimensions"]:
            b = sem["between"][d]
            rt = sem["retest"]
            print(f"  {d:<11} MAE {b['mae']:.3f}  r {b['pearson_r'] if b['pearson_r'] is None else round(b['pearson_r'], 2)}"
                  f"  retest ICC text {rt['text'][d]['icc_2_1']}  rec {rt['recorded'][d]['icc_2_1']}")
        for c in CONDITIONS:
            print(f"  centroid {c}: {sem['centroid'][c]['correct']}/{sem['centroid'][c]['total']}"
                  f"  p={sem['centroid'][c]['binomial_p']:.4f}   repair sens "
                  f"{sem['repair'][c]['sensitivity']:.2f} fpr {sem['repair'][c]['false_positive_rate']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
