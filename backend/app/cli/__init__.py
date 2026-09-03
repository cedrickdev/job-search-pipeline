"""Command-line entry points for V2 operations.

One module per command, each a thin shell around application code: parse
arguments, build the objects, print the report, choose an exit code. Nothing here
implements behaviour of its own — a command has to be reproducible from a test
that never touches `sys.argv`, and the import in `backend/app/compat/v1_import.py`
is where that logic lives.
"""
