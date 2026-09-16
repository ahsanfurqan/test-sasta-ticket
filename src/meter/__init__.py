"""Metered billing API.

Package seams (each has one owning agent -- see .claude/agents/):

    meter.api       hot-path        request serving, auth, capture, limit enforcement
    meter.domain    billing-domain  PURE pricing math, no I/O
    meter.storage   data-model      postgres + redis access
    meter.pipeline  pipeline        buffering, aggregation, reconciliation, invoicing
    meter.ops       hot-path        health / readiness
"""

__version__ = "0.1.0"
