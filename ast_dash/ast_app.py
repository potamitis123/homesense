import os
import sys

# Force PyTorch Hub to run in offline mode to prevent GitHub 503 outages from crashing the app
os.environ["TORCH_HUB_OFFLINE"] = "1"
# Allow both Intel and LLVM OpenMP runtimes in the same process (needed for CLAP search).
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

import dash
from dash import  Dash, dcc, html
import ctypes

# Suppress Windows Error Message Boxes
try:
    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
except:
    pass

from dash.dependencies import Output
from dash.dependencies import Input
import dash_bootstrap_components as dbc
import os
import sys
import ssl
import subprocess

# Force transformers to run in offline mode
# os.environ["TRANSFORMERS_OFFLINE"] = "1"

# Add the project root to sys.path to ensure package imports work correctly
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

# Import misc first to apply the SSL certificate store monkeypatch early
import misc

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from misc.load_parameters import parameters
from ast_sql.users import Users
from ast_sql.ast_sql_lite import Ast_Sql_lite 
from misc import hash_password
import diskcache
from dash import DiskcacheManager
from ast_src.housekeeper import start_housekeeper

# Initialize DB to ensure tables are created before any pages attempt to load data
# This needs to be done before `app = Dash(...)` if pages are loaded at module level
project_root_for_db = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
db_path_for_init = os.path.join(project_root_for_db, parameters.get_parameters()['sqlite']['db_name'])

print(f"DEBUG: Initializing Ast_Sql_lite with db_path: {db_path_for_init}")
ast_db_initializer = Ast_Sql_lite(db_path_for_init)
print("DEBUG: Ast_Sql_lite initialization complete. Closing connection.")
ast_db_initializer.close() # Close connection after tables are ensured
print("DEBUG: Ast_Sql_lite connection closed.")

#USER_PASS_MAPPING=dict()
#def load_users(): 
#    ast_db=Ast_Sql_lite("../" + parameters.get_parameters()['sqlite']['db_name'])
#    for x in ast_db.users.get_users():
#        USER_PASS_MAPPING[x[0]]=x[1]

#USER_PASS_MAPPING={ 'user1':'user1',
#                    'user2':'user2',3
#
#                    }
dbc_css = "https://cdn.jsdelivr.net/gh/AnnMarieW/dash-bootstrap-templates/dbc.min.css"

# Project-relative assets folder
project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
ASSETS_PATH = os.path.join(project_root, 'assets')

def create_assets_folder():
    try:
        if not os.path.exists(ASSETS_PATH):
            os.makedirs(ASSETS_PATH)
    except:
        pass
try:
    import diskcache
    cache = diskcache.Cache("./callback_cache")
    background_callback_manager = DiskcacheManager(cache)
except ImportError:
    print("Warning: diskcache not installed, background callbacks will not work.")
    # Fallback to a dummy manager if you want the app to run without bg capability
    from dash.development.base_component import ComponentRegistry
    background_callback_manager = None # Or implement a basic dummy manager if needed

app = Dash(__name__, 
           use_pages=True, 
           external_stylesheets=[dbc.themes.BOOTSTRAP, dbc.icons.FONT_AWESOME, dbc_css],
           #serve_locally=False,
           assets_folder=ASSETS_PATH,
           background_callback_manager = background_callback_manager,

           #assets_url_path='assets'
           )


app.server.secret_key = os.urandom(24) 

# Signaling endpoints for the Visuals tab. The browser is the WebRTC peer; these
# routes only relay its SDP and candidates to the sentinel over MQTT.
from ast_src.webrtc.routes import register as register_webrtc_routes
register_webrtc_routes(app.server)

# Continuous MJPEG uplink from the P4 sentinels. Independent of the call path
# above: the sentinels post frames here whenever they are powered on, and the
# Visuals tab re-serves the newest one.
from ast_src.video_stream.routes import register as register_video_routes
register_video_routes(app.server)


menus_array=[]
for page in dash.page_registry.values():
    menus_array.append(dbc.DropdownMenuItem(page['name'], href=page["relative_path"]))


navbar=dbc.NavbarSimple(
    children=[
        dbc.DropdownMenu(
            children=menus_array,
            nav=True,
            in_navbar=True,
            #right=True,
            #label="More Pages",
        ),
    ],
    brand="HomeSense",
    #brand_href="/",
    color="primary",
    brand_style={"align":"left"},
    dark=True,
    )

def MyNavBar():
    
    navitems=[]
    for page in dash.page_registry.values():
        navitems.append(dbc.NavItem(dbc.NavLink(page['name'], 
                                                href=page["relative_path"],
                                                active="exact",
                                                className="px-3 py-2 mx-1",
                                                )))
    row=html.Div([
          dbc.NavbarBrand([
              # Sized in em, not px, so it tracks the brand's fs-3 font size
              # exactly as the Font Awesome glyph it replaced did (fs-3 is
              # responsive, so a px height would drift at other widths).
              #
              # 1.5em is the original 1em enlarged by half. The alignment moves
              # with it: at 1em the glyph sat from -0.15em to 0.85em, centred
              # 0.35em above the baseline. Growing it while keeping that centre
              # puts the new bottom edge at 0.35 - (1.5 / 2) = -0.4em, so the
              # logo grows evenly about the text rather than hanging below it.
              html.Img(src=app.get_asset_url('homesense.png'),
                       alt="HomeSense",
                       className="me-2",
                       style={"height": "1.5em", "verticalAlign": "-0.4em"}),
              "HomeSense"
          ], className="ms-4 fs-3 fw-bold"),
          dbc.Nav(children=navitems,
                  className="ms-auto me-4",
                  pills=True, 
                  ),

    ], className="d-flex w-100 align-items-center")
    return dbc.Navbar(children=row,
                      color="primary",
                      dark=True,
                      className="mb-4 shadow",
                      sticky="top"
                    )



app.layout=html.Div(children=[
    MyNavBar(),
    dash.page_container
    ]
)




if __name__ == '__main__':
    #load_users()    
    create_assets_folder()
    start_housekeeper()
    if parameters.get_parameters().get('https', {}).get('https'):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        certfile = parameters.get_parameters()['https']['ca_certfile']
        keyfile = parameters.get_parameters()['https']['ca_keyfile']
        if not os.path.isabs(certfile):
            certfile = os.path.join(project_root, certfile)
        if not os.path.isabs(keyfile):
            keyfile = os.path.join(project_root, keyfile)
            
        try:
            context.load_cert_chain(
                certfile=certfile,
                keyfile=keyfile
            )
            print(f"[HTTPS] Loaded cert chain: cert={certfile} key={keyfile}")
        except Exception as e:
            print(f"[HTTPS] Error loading cert chain: {e}")
            
        print(f"[HTTPS] Starting Dash App on HTTPS://0.0.0.0:8050")
        try:
            app.run(
                host='0.0.0.0',
                port=8050,
                debug=False,
                dev_tools_hot_reload=False,
                ssl_context=context
            )
        except Exception as e:
            print(e)
    else:
        app.run(
            host='0.0.0.0',
            port=8050,
            debug=True,
            dev_tools_hot_reload=False,
        )
