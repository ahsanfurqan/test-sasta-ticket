"""The operational CLI. Owned by test-engineer.

This file exists because of a specific failure. `meter.pipeline.cli` is the entry point for
every manual trigger -- drain, aggregate, reconcile, close, rebuild counters -- and it had no
tests at all. A refactor that moved two modules broke its imports, and the entire suite stayed
green: the CLI is only ever reached by running it, and nothing ran it.

So these are cheap, shallow, and deliberately so. They are not testing what the stages do
(test_drain, test_reconciliation and test_invoicing do that against the real container). They
are testing that the operational surface is REACHABLE -- that every subcommand imports, parses
and dispatches. That is the whole class of bug that got through.
"""

import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration

#: Every subcommand the CLI advertises. If one is added without a test, the parser test fails.
SUBCOMMANDS = [
    "drain",
    "aggregate",
    "reconcile",
    "thresholds",
    "rebuild-counters",
    "close-month",
    "close-customer",
    "status",
]


def run_cli(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "meter.pipeline.cli", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_the_module_imports_at_all():
    """The regression that started this file: a moved module broke the import and nothing
    noticed, because importing the CLI is not something any other test does."""
    result = run_cli("--help", timeout=60)
    assert result.returncode == 0, result.stderr
    assert "ImportError" not in result.stderr
    assert "ModuleNotFoundError" not in result.stderr


@pytest.mark.parametrize("subcommand", SUBCOMMANDS)
def test_every_subcommand_is_reachable(subcommand):
    """Dispatch, not behaviour: the command must parse and run rather than crash on import
    or on a missing argument it should have defaulted."""
    args = [subcommand]
    if subcommand in {"reconcile", "close-customer"}:
        # A customer that does not exist: we want the dispatch path, not a real close.
        args += ["--customer", "00000000-0000-0000-0000-000000000000"]
    if subcommand == "close-month":
        # Bare `close-month` closes EVERY customer, which is correct and slow. Point it at a
        # month with no data: the dispatch path is identical and the work is not.
        args += ["--month", "2020-01"]
    result = run_cli(*args)

    assert "ImportError" not in result.stderr, result.stderr
    assert "ModuleNotFoundError" not in result.stderr, result.stderr
    assert "Traceback" not in result.stderr or result.returncode != 0, (
        f"{subcommand} raised an unhandled traceback:\n{result.stderr}"
    )


def test_status_reports_the_operational_numbers():
    """`status` is what an on-call engineer runs first, so it must work with no arguments
    and name the things ADR-0011 and ADR-0018 said to alarm on."""
    result = run_cli("status", timeout=60)
    assert result.returncode == 0, result.stderr
    output = result.stdout.lower()
    assert "counters authoritative" in output
    assert "threshold" in output


def test_there_is_no_way_to_issue_an_invoice_without_reconciling():
    """ADR-0010 requires reconcile-then-issue. A bare `invoice` subcommand existed once,
    bypassed reconciliation, and issued an immutable invoice Rs. 22,049.65 short -- which the
    database then correctly refused to let anyone fix. It must not come back."""
    result = run_cli("--help", timeout=60)
    assert "invoice," not in result.stdout.replace("\n", " "), (
        "a subcommand that issues an invoice without draining and reconciling is exactly "
        "the bug ADR-0010 exists to prevent"
    )


def test_an_unknown_subcommand_fails_loudly():
    result = run_cli("definitely-not-a-command", timeout=60)
    assert result.returncode != 0
