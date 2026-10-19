"""Isolated, server-managed Claude Code terminal sessions.

This package is deliberately separate from ``backend.app.llm``.  The normal
provider layer remains unable to inject ``ANTHROPIC_*`` variables; this package
only models session ownership and lifecycle for the dedicated terminal manager.
"""
