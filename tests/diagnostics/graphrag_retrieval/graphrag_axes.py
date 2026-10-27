"""The five things a graphrag answer can get wrong, and nothing else.

Every tool in this folder reports against this list and no other. It is here,
in one file, so that the sheet, the probe harness and the scorer cannot drift
into three different vocabularies for the same five questions -- which is what
happened to the six relational dimensions before they were cut to four.

The wording of `evaluation` is fixed. It is the wording the paper uses, and a
report that paraphrases it is a report a reader has to re-map by hand.
"""

from __future__ import annotations

# key, the name in the table, the question that row answers, and which tool in
# this folder produces the number.
AXES = [
    ("event_retrieval", "Event retrieval",
     "Did the system retrieve the relevant event?",
     "score_graphrag_retrieval"),
    ("temporal_retrieval", "Temporal retrieval",
     "Did it identify the correct day or time interval?",
     "score_graphrag_retrieval"),
    ("person_attribution", "Person attribution",
     "Did it retrieve the correct resident's observations?",
     "score_graphrag_retrieval"),
    ("relational_retrieval", "Relational retrieval",
     "Did it retrieve evidence relevant to the dyadic interaction?",
     "score_graphrag_retrieval"),
    ("negative_query", "Negative query",
     "Did it correctly state that the evidence is absent?",
     "graphrag_negative_probes"),
]

NAME = {key: name for key, name, _q, _t in AXES}
EVALUATION = {key: question for key, _n, question, _t in AXES}
PRODUCED_BY = {key: tool for key, _n, _q, tool in AXES}

HEADER = ("Query type", "Required evaluation", "Result")
NOT_MEASURED = "not measured"


def rows(results: dict) -> list[tuple[str, str, str]]:
    """The table, in order, with a row for every axis whether filled or not.

    An axis nobody measured says so. Dropping the row would let a reader take
    five axes for however many happened to run.
    """
    out = [HEADER]
    for key, name, question, tool in AXES:
        out.append((name, question, results.get(key) or NOT_MEASURED))
    return out


def render(results: dict, tsv: bool = False) -> str:
    """Aligned for reading, or tab-separated for build_docx.py to pick up."""
    table = rows(results)
    if tsv:
        return "\n".join("\t".join(r) for r in table)
    widths = [max(len(r[i]) for r in table) for i in range(3)]
    lines = []
    for index, row in enumerate(table):
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in
                               enumerate(row)).rstrip())
        if index == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)
