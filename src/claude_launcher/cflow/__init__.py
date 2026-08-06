"""claunch workflows (cflow): declarative, step-fed agent workflows.

A workflow YAML declares steps (with branch points and human gates); an MCP
server feeds them to an agent one at a time (``cflow:start`` / ``cflow:next`` /
``cflow:select``), records a journal, enforces machine checks (``verify``) on
step exit, and blocks on human approvals that can only be granted out-of-band
(``claunch cflow approve`` / ``select``) — never by the agent itself.
"""
