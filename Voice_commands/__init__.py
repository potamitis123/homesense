"""Voice-command feature: 'Kokora' commands detected in ASR transcriptions.

Everything for this feature lives in this package:
  state.py        -- mute flag + last-command record (common_files/rooms.json-style)
  detector.py      -- trigger-phrase matching against ASR transcription text
  broadcaster.py   -- pushes an acknowledgement WAV to the sentinel over MQTT PTT
  commands_tab.py  -- the "Commands" Dash page

ast_dash/pages/commands.py is a two-line loader so Dash's page auto-discovery
(which only scans ast_dash/pages/) finds this page; it contains no logic.
ast_src/main.py imports detector/state/broadcaster directly to hook into the
live pipeline.
"""
