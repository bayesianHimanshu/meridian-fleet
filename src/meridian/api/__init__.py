"""The Fleet API: the trusted read and decision layer over the ledger (phase 3).

It runs on the host, binds to 127.0.0.1 only, and never updates or deletes anything: a
decision is a new HumanDecision event. Identity is development-only (see identity.py).
"""
