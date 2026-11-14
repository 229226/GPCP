"""LTE numerology extension point.

v1 LTE scenarios use the shared 38.101-1 table in nr.py so that the frozen IR
is byte-identical.  LTE-specific tables (36.101) go here when a new protocol
revision requires them, guarded by a new schema_version.
"""
LTE_NOTE = "inherits nr.N_RB (v1 compatibility)"
