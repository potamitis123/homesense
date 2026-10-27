import dash
from dash import html, dcc, Input, Output, State, callback
import dash_bootstrap_components as dbc
import sqlite3
import pandas as pd
from contextlib import contextmanager
import sys
import os
sys.path.append('./misc')
from misc.load_parameters import parameters


dash.register_page(
    __name__,
    path='/audio_classes',
    title='Audio Classes',
    name='Audio Classes'
)

@contextmanager
def get_db_connection():
    db_name = parameters.get_parameters()['sqlite']['db_name']
    db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', db_name))
    print(f"DEBUG: Attempting to connect to database at: {db_path}")
    conn = None # Initialize conn to None
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        print(f"DEBUG: Successfully connected to database: {db_path}")
        yield conn
    except sqlite3.Error as e:
        print(f"ERROR: Database connection failed: {e}")
        raise # Re-raise the exception after logging
    finally:
        if conn:
            conn.close()
            print(f"DEBUG: Database connection closed for: {db_path}")

def get_data():
    try:
        with get_db_connection() as conn:
            query = "SELECT id, name, description, used FROM ontology ORDER BY name"
            df = pd.read_sql_query(query, conn)
            df.insert(0, 'index', range(1,len(df)+1))
            print(f"DEBUG: get_data loaded {len(df)} rows from ontology table.")
        return df
    except Exception as e:
        print(f"ERROR: Failed to retrieve data from ontology table: {e}")
        raise

def save_changes(values, ids, data):
    """Commit the checkbox state to ontology.used -- the permissible set.

    Only the checkboxes currently rendered are written, so saving while a
    search filter is active updates just the classes on screen and leaves
    every other class exactly as it was.
    """
    update_pairs = [(1 if value else 0, id_dict['index'])
                    for value, id_dict in zip(values, ids)]
    return update_ontology_bulk(update_pairs)


def signal_ontology_changed():
    """Tell the running pipeline to re-read the class filter.

    ast_src/main.py polls for this file, reloads the names where used = 0 as
    its reject list, then deletes it -- so writing it is what makes a save
    take effect without restarting anything.
    """
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    ontology_file = os.path.join(project_root, 'common_files', 'ontology_updated.txt')
    with open(ontology_file, 'w') as f:
        f.write('1')


def count_enabled():
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT SUM(used), COUNT(*) FROM ontology").fetchone()
        return int(row[0] or 0), int(row[1] or 0)
    except sqlite3.Error as e:
        print(f"Error counting enabled classes: {e}")
        return 0, 0


def update_ontology_bulk(id_used_pairs):
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany("UPDATE ontology SET used = ? WHERE id = ?", id_used_pairs)
            conn.commit()
        return True
    except sqlite3.Error as e:
        print(f"ERROR: Bulk database update error: {e}")
        return False

def update_database(id, used):
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE ontology SET used = ? WHERE id = ?", (used, id))
            conn.commit()
        return True
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return False


# Group presets (group_of_clases / clases_of_group) are no longer part of this
# page: the system is a home installation with one permissible set, and the
# preset buttons only offered ways to overwrite it wholesale. The tables are
# left in the database untouched -- nothing reads them now, and dropping them
# would throw away the curated lists for no gain. ontology.used is the single
# source of truth, which is what ast_src/main.py has always actually read.

def layout(**kwargs):
    enabled, total = count_enabled()
    return dbc.Container([
    dbc.Row([
        dbc.Col([
            html.H2("Audio Classes Management", className="text-primary mb-4"),
            html.P("Tick the classes the sentinels should report. Everything unticked "
                   "is filtered out of detections. Changes take effect when you press "
                   "Save Changes.", className="text-muted mb-4")
        ], width=12)
    ]),

    dbc.Card([
        dbc.CardHeader([
            dbc.Row([
                dbc.Col(html.Span("Search & Selection", className="font-weight-bold"), width="auto"),
                dbc.Col(
                    dbc.Input(
                        id="search-input",
                        type="text",
                        placeholder="Search by name...",
                        className="w-100"
                    ), width=4
                ),
                dbc.Col([
                    dbc.ButtonGroup([
                        dbc.Button([html.I(className="fas fa-check-double me-2"), "Select All"], id="toggle-all", color="secondary"),
                        dbc.Button([html.I(className="fas fa-save me-2"), "Save Changes"], id="save-button", color="primary"),
                    ])
                ], width="auto"),
                dbc.Col(html.Div(id='save-message',
                                 children=f"{enabled} of {total} classes enabled",
                                 className="text-muted small"), width="auto")
            ], align="center", justify="between")
        ], className="bg-light"),
        dbc.CardBody([
            dcc.Store(id='data-store'),
            html.Div([
                dbc.Spinner(
                    html.Div(id='grid-container', className="mb-4"),
                    color="primary",
                    type="grow"
                )
            ], style={"minHeight": "400px"})
        ])
    ], className="shadow-sm"),
], fluid=True, className="p-4")

@callback(
    Output('data-store', 'data'),
    Input('search-input', 'value'),
)
def load_data(search_term):
    print(f"DEBUG: load_data callback triggered. search_term: {search_term}")
    try:
        df = get_data()
        if search_term:
            print(f"DEBUG: Filtering DataFrame with search_term: {search_term}")
            df = df[df['name'].str.contains(search_term, case=False, na=False)]
        
        data_to_return = df.to_dict('records')
        print(f"DEBUG: load_data returning {len(data_to_return)} records for data-store.")
        return data_to_return
    except Exception as e:
        print(f"ERROR: load_data callback failed: {e}")
        raise # Re-raise the exception to ensure Dash reports it.

@callback(
    Output('grid-container', 'children'),
    Input('data-store', 'data'),
    prevent_initial_call=False 
)
def update_grid(data):
    print(f"DEBUG: update_grid received data type: {type(data)}")
    if data:
        print(f"DEBUG: update_grid received {len(data)} items")
    else:
        print("DEBUG: update_grid received empty data")

    if not data:
        # Fallback to loading everything if data is empty on initial call
        try:
            df = get_data()
            data = df.to_dict('records')
        except:
            return html.Div("No data found", className="text-muted")
    
    if not data:
        return html.Div("No data found", className="text-muted")
    
    # Create rows of 7 items each
    items = []
    current_row = []
    
    for item in data:
        card = dbc.Col([
            dbc.Card([
                dbc.CardBody([
                    # Name with Popover
                    html.Div([
                        html.Div(
                            f"{item['index']}. {item['name']}",
                            id={'type': 'name-text', 'index': item['id']},
                            className="text-primary text-truncate",
                            style={
                                'cursor': 'pointer',
                                'maxWidth': '100%',
                                'fontSize': '0.9rem'
                            }
                        ),
                        dbc.Popover(
                            [
                                dbc.PopoverBody(item['description'] or "No description available")
                            ],
                            target={'type': 'name-text', 'index': item['id']},
                            trigger="hover",
                            id={'type': 'name-popover', 'index': item['id']},
                            placement="auto"
                        ),
                    ]),
                    # Checkbox
                    dbc.Checkbox(
                        id={'type': 'used-checkbox', 'index': item['id']},
                        value=item['used'],
                        label="Used",
                        className="mt-2",
                        style={'fontSize': '0.8rem'}
                    ),
                ], className="p-2")
            ], className="h-100 shadow-sm")
        ], xs=12, sm=6, md=4, lg=2, xl=True, className="mb-2")
        
        current_row.append(card)
        
        if len(current_row) == 7:
            items.append(dbc.Row(current_row, className="g-2"))
            current_row = []
    
    # Add any remaining items
    if current_row:
        items.append(dbc.Row(current_row, className="g-2"))
    
    return html.Div(items)

@callback(
    Output('save-message', 'children'),
    Input('save-button', 'n_clicks'),
    State({'type': 'used-checkbox', 'index': dash.ALL}, 'value'),
    State({'type': 'used-checkbox', 'index': dash.ALL}, 'id'),
    State('data-store', 'data'),
    prevent_initial_call=True
)
def btn_save_changes(n_clicks, values, ids, data):
    """Save is the only thing that changes what the sentinels report."""
    if not n_clicks:
        return dash.no_update
    if not save_changes(values, ids, data):
        return "Save failed - see the server log."
    signal_ontology_changed()
    enabled, total = count_enabled()
    return f"Saved. {enabled} of {total} classes enabled."


@callback(
    Output('data-store', 'data', allow_duplicate=True),
    Output('toggle-all', 'children'),
    Input('toggle-all', 'n_clicks'),
    State('toggle-all', 'children'),
    State('data-store', 'data'),
    prevent_initial_call=True,
)
def toggle_select_all(n_clicks, children, data):
    """Tick or untick everything currently listed, without saving.

    Previously this wrote straight to the database, so an accidental click
    changed the live filter with no way to reconsider. Now it only moves the
    checkboxes -- Save Changes remains the single point where anything is
    committed, which is what the button labelled "Save" should mean.

    It acts on the rows on screen, so with a search active it ticks just the
    matches rather than all 527 classes.
    """
    # dbc.Button children is a list ([icon, label]); pull the text out of it.
    def label_text(node):
        if isinstance(node, str):
            return node
        if isinstance(node, (list, tuple)):
            return ' '.join(label_text(n) for n in node)
        return ''

    selecting = 'Select All' in label_text(children)
    used_val = 1 if selecting else 0
    new_label = [html.I(className="fas fa-check-double me-2"),
                 "Unselect All" if selecting else "Select All"]

    rows = [dict(row, used=used_val) for row in (data or [])]
    return rows, new_label
