"""The LLM report page on its own, over plain HTTP, to look at in a browser.

    python tests/manual/llm_report_page.py            # http://127.0.0.1:8077/llm_report

The application serves HTTPS with a self-signed certificate and starts MQTT
clients, video routes and the housekeeper on the way up, so a second copy of
it cannot be run beside the live one just to look at a page. This starts Dash
with that one page and nothing else. It reads the same store and the same
database as the application and opens no connection to the broker.

Confirm and Reject work, but write to a file in the temporary directory and
never to common_files/incident_judgements.jsonl: clicks made while trying the
page out must not enter the household's record, which is append-only and has
no way to take them out again.

"Do you want me to prepare the list?" works too, and a Yes here READS NOTHING.
It starts a stand-in that waits a few seconds and ends, so the waiting and the
"no list came of it" that follows can be looked at without a day's transcript
going to a model or a day being written to the store. The answers are kept in
the temporary directory, so a No given here is not remembered by the
application. Pass --stand-in-seconds N to make the wait longer.

GENERATE REPORT IS NOT REDIRECTED. It does here exactly what it does in the
application: it overwrites assets/daily_report.txt and saves an analysis under
ast_dash/llm_report/reports/, and every analysis in that folder is read as
history by the next report (load_historical_level4 keeps the first file, by
name, that covers a date). A report made over ten minutes to try the page out
therefore becomes that day's baseline. Delete the file it wrote afterwards.
With a local model it also loads that model onto the GPU the running
application is using.
"""

import os
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import dash                                                   # noqa: E402
import dash_bootstrap_components as dbc                       # noqa: E402
from dash import html                                         # noqa: E402

from graphrag import blind, judgements, prepare               # noqa: E402

PORT = 8077
TRIAL_RECORD = os.path.join(tempfile.gettempdir(),
                            "homesense_trial_incident_judgements.jsonl")
judgements.JUDGEMENTS_PATH = TRIAL_RECORD
# The blind labels likewise: tags given while trying the page are not data.
blind.LABELS_PATH = os.path.join(tempfile.gettempdir(),
                                 "homesense_trial_blind_labels.jsonl")

TRIAL_ASKINGS = os.path.join(tempfile.gettempdir(), "homesense_trial_preparing")
prepare.STATE_DIR = TRIAL_ASKINGS
STAND_IN_SECONDS = (int(sys.argv[sys.argv.index("--stand-in-seconds") + 1])
                    if "--stand-in-seconds" in sys.argv else 12)


def stand_in(day, stored):
    """In place of the ingest: say so, wait, and end without making a list."""
    return [sys.executable, "-c",
            "import time; print('stand-in for the reading of %s: nothing was "
            "sent and nothing was written'); time.sleep(%d)"
            % (day, STAND_IN_SECONDS)]


prepare.command_for = stand_in

DBC_CSS = "https://cdn.jsdelivr.net/gh/AnnMarieW/dash-bootstrap-templates/dbc.min.css"

app = dash.Dash(
    __name__, use_pages=True, pages_folder="",
    external_stylesheets=[dbc.themes.BOOTSTRAP, dbc.icons.FONT_AWESOME, DBC_CSS],
    assets_folder=os.path.join(ROOT, "assets"))

import ast_dash.pages.llm_report as page                      # noqa: E402  registers the page

# Dash attaches a layout only to pages it finds in a pages folder. One that
# is imported by hand, as here, is registered without it.
dash.page_registry[page.__name__]["layout"] = page.layout

app.layout = html.Div([dash.page_container])

if __name__ == "__main__":
    print(f"LLM report page alone: http://127.0.0.1:{PORT}/llm_report")
    print(f"Trial verdicts are written to {TRIAL_RECORD}")
    print(f"A Yes starts a {STAND_IN_SECONDS}-second stand-in, not a reading; "
          f"its state is kept in {TRIAL_ASKINGS}")
    app.run(host="127.0.0.1", port=PORT, debug=False)
