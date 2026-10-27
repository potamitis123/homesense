"""Loader only -- Dash's page auto-discovery scans this folder, so this file
must live here. All real layout/callback/state code for the Commands tab
lives in Voice_commands/commands_tab.py.
"""

from Voice_commands.commands_tab import layout  # noqa: F401 -- import triggers dash.register_page
