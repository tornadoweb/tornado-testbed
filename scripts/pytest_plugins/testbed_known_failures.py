"""pytest plugin: apply a package's active known failures.

Loaded into a package's test run by run_one.sh (via PYTEST_PLUGINS, with this
directory on PYTHONPATH) only when that package has entries in its
known_failures.toml whose conditions hold for the Tornado under test.
run_one.sh evaluates the conditions with pkglib and hands this plugin the
active entries as JSON, so pkglib stays the only parser of package
definitions; this file only applies them.

For each active entry:

  action = "xfail"     every matching test gets xfail(strict=True). A test
                       that fails is reported as xfailed; one that passes is
                       XPASS(strict), which fails the run. That is the point:
                       when the upstream fix lands, the run goes red and the
                       entry gets deleted instead of rotting.
  action = "deselect"  matching tests are not run. Only for tests that hang
                       or are flaky, where an xfail could not give a stable
                       answer either way.

An entry naming a test that matches nothing collected is an error (pytest exit
code 4), so a downstream rename cannot silently turn an entry into a no-op.

Environment:
  TESTBED_KNOWN_FAILURES          path to the JSON list of active entries.
                                  Removed from the environment once read, so
                                  a pytest the suite launches in a subprocess
                                  does not apply (and fail to match) them.
  TESTBED_KNOWN_FAILURES_APPLIED  optional path; a JSON record of what was
                                  matched is written there for the report.
"""

from __future__ import annotations

import json
import os

import pytest

_ENV = "TESTBED_KNOWN_FAILURES"
_APPLIED_ENV = "TESTBED_KNOWN_FAILURES_APPLIED"
_KEY = pytest.StashKey[dict]()


def _matches(nodeid: str, pattern: str) -> bool:
    """A node id matches itself, its parametrizations, and anything under it."""
    return (
        nodeid == pattern
        or nodeid.startswith(pattern + "[")
        or nodeid.startswith(pattern + "::")
    )


def pytest_configure(config: pytest.Config) -> None:
    path = os.environ.pop(_ENV, None)
    applied = os.environ.pop(_APPLIED_ENV, None)
    if not path or hasattr(config, "workerinput"):
        return
    with open(path) as f:
        entries = json.load(f)
    config.stash[_KEY] = {"entries": entries, "applied": applied, "matched": {}}


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    state = config.stash.get(_KEY, None)
    if state is None:
        return
    # trylast: run after the package's own --deselect / -k filtering, so an
    # entry for a test that test.sh already deselects is reported as matching
    # nothing rather than quietly overlapping it.
    matched: dict[str, list[str]] = {}
    keep: list[pytest.Item] = []
    dropped: list[pytest.Item] = []
    for item in items:
        hit = None
        for entry in state["entries"]:
            for pattern in entry["tests"]:
                if _matches(item.nodeid, pattern):
                    hit = (entry, pattern)
                    break
            if hit:
                break
        if hit is None:
            keep.append(item)
            continue
        entry, pattern = hit
        matched.setdefault(pattern, []).append(item.nodeid)
        if entry["action"] == "deselect":
            dropped.append(item)
        else:
            item.add_marker(
                pytest.mark.xfail(
                    strict=True,
                    reason=f"known failure: {entry['issue']} -- {entry['reason']}",
                )
            )
            keep.append(item)
    state["matched"] = matched

    unmatched = [
        pattern
        for entry in state["entries"]
        for pattern in entry["tests"]
        if pattern not in matched
    ]
    if unmatched:
        raise pytest.UsageError(
            "known_failures.toml names tests that were not collected "
            "(renamed upstream, or already deselected in test.sh?): "
            + ", ".join(unmatched)
        )
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = keep


def pytest_terminal_summary(terminalreporter, exitstatus, config: pytest.Config) -> None:
    state = config.stash.get(_KEY, None)
    if state is None:
        return
    tr = terminalreporter
    tr.section("testbed known failures")
    for entry in state["entries"]:
        conditions = ", ".join(
            f"{key}{entry[key]}" for key in ("tornado", "version") if key in entry
        )
        count = sum(len(state["matched"].get(p, [])) for p in entry["tests"])
        tr.line(f"{entry['action']} {count} test(s) while {conditions}: {entry['issue']}")
        tr.line(f"  {entry['reason']}")
    if state["applied"]:
        with open(state["applied"], "w") as f:
            json.dump(
                [
                    {
                        **entry,
                        "matched": sum(
                            len(state["matched"].get(p, [])) for p in entry["tests"]
                        ),
                    }
                    for entry in state["entries"]
                ],
                f,
            )
