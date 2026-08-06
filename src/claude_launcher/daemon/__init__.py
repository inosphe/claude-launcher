"""claunch session daemon: a tmux-server-like owner of managed PTY sessions.

The daemon spawns CLI agent harnesses (``claude`` first) under real PTYs,
keeps them alive independently of any terminal, and exposes everything over an
HTTP/WebSocket API consumed by both the ``claunch`` CLI (send-keys /
capture-pane / wait-for) and a browser front-end with live xterm.js terminals.
"""
