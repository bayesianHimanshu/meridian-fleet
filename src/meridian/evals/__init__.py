"""Evaluation harness: match findings to planted faults, score, and report.

This package must never import meridian.agents. It reads findings from the ledger,
planted faults from ground truth files and thresholds from settings.
"""
