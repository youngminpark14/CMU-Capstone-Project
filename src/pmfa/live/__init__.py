"""The live modes: triggers, one pass and the foreground loop, and operator sessions.

- ``pmfa.live.triggers``: new-document, price-move and schedule triggers with deterministic ids;
- ``pmfa.live.loop``: ``run_once(world, controller)`` and ``run_loop(...)``;
- ``pmfa.live.session``: ``open_session(mode, config)``, the world and controller a command uses;
- ``pmfa.live.text``: the lines the commands print.

The worlds themselves are built in ``pmfa.world.live``.
"""
