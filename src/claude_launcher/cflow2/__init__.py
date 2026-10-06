"""cflow v2: combinator-syntax workflows compiled to a statechart IR.

Runs beside cflow v1 (``claude_launcher.cflow``) with its own runtime and
state directory (``.cflow2/``). Spec: ``docs/cflow-v2.md``.

Modules:

- ``ir``        -- the statechart IR (plain JSON-able data)
- ``expr``      -- the guard/value expression language
- ``frontend``  -- v2 YAML -> IR (fragments, rules, combinators, call inlining)
- ``check``     -- static checks and spec verification over the IR
- ``caps``      -- atomic capabilities, their layering, and fallback planning
- ``layers``    -- discovery of v2 definitions (``workflows/v2/``)
- ``runtime``   -- the interpreter: state, advance, journal
- ``executors`` -- inline / subagent / headless / session delegation
- ``mcp``       -- the ``flow_*`` agent tools
"""
