import dash
from dash import html, dcc,callback, get_app
from dash.dependencies import Input, Output, State
import time
import os

dash.register_page(
    __name__,
    path='/logfile',
    title='Log File',
    name='Log File'
)
app=get_app()


class LogTail:
    def __init__(self, filename):
        self.filename = filename
        self.last_position = 0
        self.lines = []
        self.load_initial_history()

    def load_initial_history(self):
        """Read existing lines to populate history on tab load."""
        try:
            if not os.path.exists(self.filename):
                return
            with open(self.filename, 'r', encoding='utf-8') as file:
                all_lines = file.readlines()
                # Keep last 500 lines for history
                self.lines = all_lines[-500:] if len(all_lines) > 500 else all_lines
                self.last_position = file.tell()
        except Exception as e:
            self.lines = [f"Error loading initial history: {str(e)}"]

    def read_new_lines(self):
        try:
            if not os.path.exists(self.filename):
                return self.lines
            with open(self.filename, 'r', encoding='utf-8') as file:
                file.seek(self.last_position)
                new_lines = file.readlines()
                if new_lines:
                    self.lines.extend(new_lines)
                    self.last_position = file.tell()
                    if len(self.lines) > 500:
                        self.lines = self.lines[-500:]
                return self.lines
        except Exception as e:
            return [f"Error reading log file: {str(e)}"]


def layout():
    # Re-initialize tailer on layout call to ensure fresh data when tab is opened
    # Point to the root logs directory (up two levels from pages folder)
    log_file_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'logs_ast_app', 'logs_ast_app.log'))
    global log_tailer
    log_tailer = LogTail(log_file_path)
    
    return html.Div([
        html.H3("Application Logs (Real-time)", className="mb-3"),
        dcc.Interval(
            id='interval-component',
            interval=1000, 
            n_intervals=0
        ),
        html.Div(id='log-content', style={
            'whiteSpace': 'pre',
            'fontFamily': 'Consolas, "Liberation Mono", Menlo, Courier, monospace',
            'backgroundColor': '#1e1e1e',
            'color': '#d4d4d4',
            'padding': '15px',
            'border': '1px solid #333',
            'borderRadius': '4px',
            'height': '700px',
            'overflowY': 'scroll',
            'fontSize': '13px',
            'lineHeight': '1.4'
        }),
        # Hidden div to signal new content
        html.Div(id='scroll-trigger', style={'display': 'none'})
    ])

# Callback to refresh content
@callback(
    Output('log-content', 'children'),
    Output('scroll-trigger', 'children'),
    Input('interval-component', 'n_intervals')
)
def update_log_content(n):
    lines = log_tailer.read_new_lines()
    content = ''.join(lines)
    # Use length as trigger for scroll
    return content, len(content)

# Clientside scroll to bottom
app.clientside_callback(
    """
    function(trigger) {
        var element = document.getElementById('log-content');
        if (element) {
            element.scrollTop = element.scrollHeight;
        }
        return window.dash_clientside.no_update;
    }
    """,
    Output('scroll-trigger', 'style'), # Dummy output
    Input('scroll-trigger', 'children')
)

