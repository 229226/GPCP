"""Sweep parameter context.

Scenarios stay static files (the frozen verb list forbids reading environment
or files), so a sweep injects its overrides here.  Reference models may read
current() to generate stimulus/golden for that parameter point.  Models that
ignore it behave exactly as before - which is what keeps the v1 lock valid.
"""
_CURRENT = {}


def set_current(d):
    global _CURRENT
    _CURRENT = dict(d or {})


def current():
    return dict(_CURRENT)
