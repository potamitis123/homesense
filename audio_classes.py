import dash
from dash import html, dcc, Input, Output, State, callback
import dash_bootstrap_components as dbc
import sqlite3
import pandas as pd
from contextlib import contextmanager
import sys
sys.path.append('./misc')
from load_parameters import parameters


dash.register_page(
    __name__,
    path='/audio_classes',
    title='Audio Classes',
    name='Audio Classes'
)

@contextmanager
def get_db_connection():
    conn = sqlite3.connect('../' + parameters.get_parameters()['sqlite']['db_name'])
    try:
        yield conn
    finally:
        conn.close()

def get_data():
    with get_db_connection() as conn:
        query = "SELECT id, name, description, used FROM ontology ORDER BY name"
        df = pd.read_sql_query(query, conn)
        df.insert(0, 'index', range(1,len(df)+1))
    return df
def save_changes(values,ids):
    success = True
    for value, id_dict in zip(values, ids):
        if not update_database(id_dict['index'], value):
            success = False
            break
    return success

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

def update_group(group_name):
    try:
        with get_db_connection() as conn:
            query1=f"UPDATE group_of_clases SET group_enable = 0 WHERE NOT group_name = '{group_name}'"
            cursor = conn.cursor()
            cursor.execute(query1)
            conn.commit()
            query2=f"UPDATE group_of_clases SET group_enable = 1 WHERE  group_name = '{group_name}'"
            cursor.execute(query2)
            conn.commit()
        return True
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return False
    
def get_array_of_classes(group_name):
    try:
        with get_db_connection() as conn:
            query=f"SELECT class_id FROM clases_of_group WHERE group_name = '{group_name}'"
            classes=pd.read_sql_query(query,conn)
        return classes.to_numpy()
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None

def get_enable_group():
    try:
        with get_db_connection() as conn:
            query=f"SELECT group_name FROM group_of_clases WHERE group_enable = 1"
            classes=pd.read_sql_query(query,conn)
        return classes.to_numpy()
    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None

layout = html.Div([
    html.H3("Audio Classes Management", className="mb-4"),
    
    # Search, Toggle All and Save row
    dbc.Row([
        dbc.Col([
            dbc.Input(
                id="search-input",
                type="text",
                placeholder="Search by name...",
                className="mb-4"
            ),
        ], width=6),
        dbc.Col([
            dbc.Button(
                "Select All",
                id="toggle-all",
                color="secondary",
                className="mb-4 w-100"
            ),
        ], width=2),
        dbc.Col([
            dbc.Button(
                "Save Changes",
                id="save-button",
                color="primary",
                className="mb-4 w-100"
            ),
        ], width=2),
        dbc.Col([
            html.Div(id='save-message', className="text-success mt-2"),
        ], width=2),
    ], className="align-items-center"),
    dbc.Row([
        dbc.Col([
            dbc.Button(
                "Indoors safety",
                name="Indoors safety",
                id="btn_Indoors_safety",
                color="secondary",
                className="mb-4 w-100"
            ),
        ], width=2),
        dbc.Col([
            dbc.Button(
                "Elderly care",
                name="Elderly care",
                id="btn_Elderly_care",
                color="secondary",
                className="mb-4 w-100"
            ),
        ], width=2),
        dbc.Col([
            dbc.Button(
                "Biodiversity",
                name='Biodiversity',
                id="btn_Biodiversity",
                color="secondary",
                className="mb-4 w-100 "
            ),
        ], width=2),
        dbc.Col([
            dbc.Button(
                "Machines",
                name='Machines',
                id="btn_Machines",
                color="secondary",
                className="mb-4 w-100"
            ),
        ], width=2),
        dbc.Col([
            dbc.Button(
                "Outdoors",
                name='Outdoors',
                id="btn_Outdoors_acoustic_surveillance",
                color="secondary",
                className="mb-4 w-100"
            ),
        ], width=2),
        dbc.Col([
            dbc.Button(
                "Drone Detection",
                name='Drone Detection',
                id="btn_Drone_Detection",
                color="secondary",
                className="mb-4 w-100"
            ),
        ], width=2),
    ], className="align-items-center"),
   # dbc.Row([
   #         dbc.Spinner(id="loading_spinner"),
  #  ], className="align-items-center"),
    # Data storage
    dcc.Store(id='data-store'),
 
    # Grid container
    html.Div([
            html.Div([dbc.Spinner(id="loading_spinner",fullscreen=True)],id='grid-container', className="mb-4"),
        
    ])
])

@callback(
    Output('data-store', 'data'),
    Input('search-input', 'value'),
)
def load_data(search_term):
    df = get_data()
    if search_term:
        df = df[df['name'].str.contains(search_term, case=False, na=False)]
    return df.to_dict('records')

# @callback(
#     [Output('btn_Indoors_safety', 'color'),
#     Output('btn_Elderly_care', 'color'),
#     Output('btn_Biodiversity', 'color'),
#     Output('btn_Machines', 'color'),
#     Output('btn_Outdoors_acoustic_surveillance', 'color'),
#     Output('btn_Drone_Detection', 'color')],
#     Input('data-store', 'data'),
#     [State('btn_Indoors_safety', 'name'),
#     State('btn_Elderly_care', 'name'),
#     State('btn_Biodiversity', 'name'),
#     State('btn_Machines', 'name'),
#     State('btn_Outdoors_acoustic_surveillance', 'name'),
#     State('btn_Drone_Detection', 'name')],
#       prevent_initial_call=True
# )
# def enable_group_button(data,*name):
#     enable_name=get_enable_group()[0][0]
#     ret_value=[]
#     for tmp_name in name:
#         if tmp_name == enable_name:
#             ret_value.append("primary")
#         else:
#             ret_value.append("secondary")
#     return ret_value
@callback(
    [
    Output('btn_Indoors_safety', 'color'),
    Output('btn_Elderly_care', 'color'),
    Output('btn_Biodiversity', 'color'),
    Output('btn_Machines', 'color'),
    Output('btn_Outdoors_acoustic_surveillance', 'color'),
    Output('btn_Drone_Detection', 'color')],
    Input('data-store', 'data'),
    State(dash.ALL, 'id'),
    prevent_initial_call=True
)
def enable_group_button(data,name):
    enable_name=get_enable_group()[0][0]
    ret_value=[]
    for tmp_name in name:
        if tmp_name == enable_name:
            ret_value.append("primary")
        else:
            ret_value.append("secondary")
    return ret_value
  

@callback(
    Output('grid-container', 'children'),
    Input('data-store', 'data'),
    prevent_initial_call=True
)
def update_grid(data):
    if not data:
        return html.Div("No data found", className="text-muted"), "Select All"
    
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
            ], className="h-100")
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
    prevent_initial_call=True
)
def save_changes(n_clicks, values, ids):
    if not n_clicks:
        return ""
    success=save_changes(values, ids)
    if success:
        with open('../common_files/ontology_updated.txt', 'w') as f:
            f.write('1')
        return "Changes saved successfully!"
    else:
        return html.Div("Error saving changes. Please try again.", className="text-danger")

@callback( Output('data-store', 'data',allow_duplicate=True),
           Output('grid-container','children',allow_duplicate=True),
           Output('toggle-all', 'children'),
           Input('toggle-all', 'n_clicks'),
           State('toggle-all','children'),
           prevent_initial_call=True,
)
def toggle_select_all(n_clicks,children):
    df = get_data()

   
    if children=='Select All' :
         button_text = "Unselect All" 
         df['used'] =1
    else:
        button_text = "Select All" 
        df['used'] =0
    return df.to_dict('records'),dbc.Spinner(id="loading_spinner",fullscreen=True),button_text

@callback( Output('data-store', 'data',allow_duplicate=True),
           Output('grid-container','children',allow_duplicate=True),
           Input('btn_Indoors_safety', 'n_clicks'),
           State('btn_Indoors_safety','name'),
           prevent_initial_call=True,
)
def btn_Indoors_safety(n_clicks,name):
    enable_classes=get_array_of_classes(name)
    df = get_data()
    if n_clicks>0:
       f = lambda x: 1 if x in enable_classes else 0
       df['used'] = df['index'].map(f)
    update_group(name)
    return df.to_dict('records'),dbc.Spinner(id="loading_spinner",fullscreen=True)

@callback( Output('data-store', 'data',allow_duplicate=True),
           Output('grid-container','children',allow_duplicate=True),
           Input('btn_Elderly_care', 'n_clicks'),
            State('btn_Elderly_care','name'),
           prevent_initial_call=True,
)
def btn_Elderly_care(n_clicks,name):
    enable_classes=get_array_of_classes(name)
    df = get_data()
    if n_clicks>0:
       f = lambda x: 1 if x in enable_classes else 0
       df['used'] = df['index'].map(f)
    update_group(name)
    return df.to_dict('records'),dbc.Spinner(id="loading_spinner",fullscreen=True)

@callback( Output('data-store', 'data',allow_duplicate=True),
          Output('grid-container','children',allow_duplicate=True),
           Input('btn_Biodiversity', 'n_clicks'),
           State('btn_Biodiversity','name'),
           prevent_initial_call=True,
)
def btn_Biodiversity(n_clicks,name):
    enable_classes=get_array_of_classes(name)
    df = get_data()
    if n_clicks>0:
       f = lambda x: 1 if x in enable_classes else 0
       df['used'] = df['index'].map(f)
    update_group(name)
    return df.to_dict('records'),dbc.Spinner(id="loading_spinner",fullscreen=True)

@callback( Output('data-store', 'data',allow_duplicate=True),
          Output('grid-container','children',allow_duplicate=True),
           Input('btn_Machines', 'n_clicks'),
           State('btn_Machines','name'),
           prevent_initial_call=True,
)
def btn_Machines(n_clicks,name):
    enable_classes=get_array_of_classes(name)
    df = get_data()
    if n_clicks>0:
       f = lambda x: 1 if x in enable_classes else 0
       df['used'] = df['index'].map(f)
    update_group(name)
    return df.to_dict('records'),dbc.Spinner(id="loading_spinner",fullscreen=True)

@callback( Output('data-store', 'data',allow_duplicate=True),
          Output('grid-container','children',allow_duplicate=True),
          Input('btn_Outdoors_acoustic_surveillance', 'n_clicks'),
          State('btn_Outdoors_acoustic_surveillance','name'),
          prevent_initial_call=True,
)
def btn_Outdoors_acoustic_surveillance(n_clicks,name):
    enable_classes=get_array_of_classes(name)
    df = get_data()
    if n_clicks>0:
       f = lambda x: 1 if x in enable_classes else 0
       df['used'] = df['index'].map(f)
    update_group(name)
    return df.to_dict('records'),dbc.Spinner(id="loading_spinner",fullscreen=True)

@callback( Output('data-store', 'data',allow_duplicate=True),
          Output('grid-container','children',allow_duplicate=True),
           Input('btn_Drone_Detection', 'n_clicks'),
           State('btn_Drone_Detection','name'),
           prevent_initial_call=True,
)
def btn_Drone_Detection(n_clicks,name):
    enable_classes=get_array_of_classes(name)
    df = get_data()
    if n_clicks>0:
       f = lambda x: 1 if x in enable_classes else 0
       df['used'] = df['index'].map(f)
    update_group(name)
    return df.to_dict('records'),dbc.Spinner(id="loading_spinner",fullscreen=True)
