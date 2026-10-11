"""Generic building blocks (config, logging, security, DB, ...).

WHY: nothing in this package may import Project 1 business code (agents, orchestrator,
models). That rule keeps it copy-pasteable into Project 2 (Finance Reconciliation Agent).
"""
