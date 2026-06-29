"""Aircall integration — a second telephony source alongside 3CX.

Some BDEs (and the BDM) dial via Aircall rather than 3CX. This package reads their
calls from the Aircall REST API, maps them to the same canonical call shape used by
the 3CX ingest, and feeds them through the identical classify/aggregate pipeline so
the dashboard treats both providers uniformly.
"""
