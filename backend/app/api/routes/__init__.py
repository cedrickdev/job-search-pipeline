"""The three V2 route modules, and nothing else.

A package rather than one module because the three surfaces have different
authorization shapes — `auth` is where a session is created, `me` is where
user-owned data is read and written, `onboarding` is the gate between them — and a
single file would make it easy to add a route to the wrong one.
"""
