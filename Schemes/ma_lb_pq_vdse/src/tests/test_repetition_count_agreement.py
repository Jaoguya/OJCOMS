"""The repetition count must read the same in the config and the paper.

This drift is not hypothetical. On 2026-09-03 the campaign moved 30 -> 10 in
``global.yaml`` while sixteen docstrings, one ``ConfigError`` message and
Section VI of the manuscript still said 30 -- and the run_meta reportability
gate in ``provenance.py`` still *enforced* 30, stamping correct numbers as not
reportable. Fixing one gate did not fix the other, so the count is pinned here
from all three sources at once rather than trusted to discipline.

Deliberately NOT asserted:

* ``30 s ramp`` (Exp. 7-8) -- seconds, not repetitions.
* ``test_ci_uses_student_t_not_the_normal_approximation`` -- its 30-sample
  vector is the test's own fixture, chosen so t and z are distinguishable.
* Historical measurements recorded at n=30 (the hold-out sweep,
  ``EXP78_DIAGNOSIS.md``) -- those are what was observed
  then, and rewriting them would falsify the record.
"""

from __future__ import annotations

import re

# Every read below is explicitly utf-8. Without it Python picks the platform
# default -- cp1252 on Windows -- and these tests die on the first non-ASCII
# byte in the manuscript before they can assert anything. They
# were failing that way silently, which is how the Section VI repetition
# count drifted from the config without anyone noticing.
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[4]
GLOBAL_YAML = REPO / "Experiment Configuration" / "global.yaml"
MANUSCRIPT = REPO / "Overleaf" / "MA-LB-PQ-VDSE.tex"


def _configured_repetitions() -> int:
    data = yaml.safe_load(GLOBAL_YAML.read_text(encoding="utf-8"))
    return int(data["measurement"]["repetitions"])


def test_config_is_the_single_source_of_truth():
    """Everything below compares against this one value, not a literal 10."""
    assert _configured_repetitions() >= 2, "a CI needs at least two runs"




@pytest.mark.skipif(not MANUSCRIPT.exists(), reason="manuscript not checked out")
def test_manuscript_section_v_matches_the_config():
    """Section VI's claimed replication count is the one a reviewer checks."""
    match = re.search(r"experiment was repeated (\d+) times", MANUSCRIPT.read_text(encoding="utf-8"))
    assert match, "Section VI no longer states 'experiment was repeated N times'"
    assert int(match.group(1)) == _configured_repetitions(), (
        "Section VI and global.yaml disagree on the replication count -- "
        "the paper would state a count the data does not have"
    )


def test_no_source_file_still_quotes_the_old_count():
    """Catches the docstrings that survived the 30 -> 10 change.

    Matches only phrasings that ASSERT the campaign replication count. A bare
    ``\\d+ runs`` would fire on "Exp. 7-8 runs are refused" and "need at least
    2 runs", which say nothing about replication.
    """
    n = _configured_repetitions()
    claims = [
        r"(\d+) runs per point",
        r"(\d+) retained runs",
        r"restore n=(\d+)",
        r"\*\*(\d+) runs after \d+ discarded warm-ups",
        r"the usual (\d+) \+ \d+ warm-ups",
        r"(\d+) independent runs",
        r'"Report mean . 95% CI" over (\d+) runs',
        r"before (?:the )?(\d+) repetitions",
        r"take the (\d+) runs on a warm system",
        r"ramp (\d+) times per point",
    ]
    allowed = {"test_repetition_count_agreement.py"}
    stale = []
    for path in sorted(REPO.glob("Schemes/**/*.py")) + sorted(REPO.glob("Schemes/**/*.md")):
        if path.name in allowed:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for pattern in claims:
                found = re.search(pattern, line)
                if found and int(found.group(1)) != n:
                    stale.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()}")
    assert not stale, (
        "these still assert a replication count that is not the configured "
        f"{n}:\n" + "\n".join(stale)
    )


# ===========================================================================
# The documents that print a runnable command
# ===========================================================================
# The 2026-09-03 migration fixed §7's prose and the provenance gate, and the
# tests above pin those. It did not reach §8's parameter table, §9's CSV example
# and reportability sentence, or §11's campaign commands, and nothing here
# noticed for a day. On 2026-09-04 that stale `n_runs must be 30` was read as
# authoritative and a rerun was launched at --runs 30; it had to be killed and
# restarted. A reviewer reading the same line would instead have rejected
# correct n=10 data. Each is pinned below against global.yaml.
#
# Still deliberately NOT asserted, for the reasons in this module's docstring:
# the `30 s ramp` (seconds), and every historical n=30 measurement in §14-17 --
# those record what was observed at the time and rewriting them would falsify
# the record.




#: Every OTHER document that prints a runnable command. This test read README.md
#: and the manuscript ONLY, which is why the 30 -> 10 migration of 2026-09-03
#: reached both of those and reached NONE of these: on 2026-09-07 all five
#: SCHEME.md files and SystemConfiguration.md still printed `--runs 30`, against
#: a config of 10 and 85 banked run_metas at n_runs=10. Three separate fixes of
#: this same number passed a green suite because nothing here looked at them.
#:
#: A doc that prints a command is a doc someone copies onto a node.
#: The five `SCHEME.md` files were deleted on 2026-09-08 along with fourteen
#: other prose documents. The list is kept pointed at what exists rather than
#: allowed to empty out, because an empty list here passes silently and this
#: guard is the only thing that has ever caught a document printing a stale
#: `--runs`.
#:
#: `SystemConfiguration.md` -> `skill.md`, 2026-09-13. The former was deleted
#: in 83b83ff and this list was not updated, so the guard silently HALVED: it
#: scanned CLAUDE.md alone while skill.md -- the manual, and the document that
#: actually prints `python3 -m Schemes... --runs 10` twice -- went unscanned.
#: A guard that cannot empty out can still go half-blind, which is the same
#: failure one file over.
COMMAND_PRINTING_DOCS = (
    "CLAUDE.md",
    "skill.md",
)


#: Commands that are DELIBERATELY not at the campaign count. `dev_runner.py`
#: bypasses the corpus pin, writes to `_dev_output/` and is documented as "not
#: reportable"; `--runs 3 --max-records 300` is a smoke test, and forcing it to
#: 10 would make it slower for no gain. Matched against the whole logical
#: command, so a continuation line cannot smuggle one past.
NON_CAMPAIGN_RUNNERS = ("dev_runner",)


def _logical_lines(text: str):
    """Yield (first line number, joined command), backslash-continuations merged.

    These documents wrap commands across lines, so `--runs N` and the runner it
    belongs to are usually on DIFFERENT physical lines. Checking line by line
    would judge a flag without seeing which command it modifies.
    """
    start, buffer = None, []
    for i, line in enumerate(text.splitlines(), 1):
        if start is None:
            start = i
        buffer.append(line.rstrip())
        if line.rstrip().endswith("\\"):
            continue
        yield start, " ".join(b.rstrip("\\").strip() for b in buffer)
        start, buffer = None, []
    if buffer:
        yield start, " ".join(b.rstrip("\\").strip() for b in buffer)


@pytest.mark.parametrize("relative", COMMAND_PRINTING_DOCS)
def test_every_document_printing_a_command_matches_the_config(relative):
    """`--runs N` anywhere a reader can copy it must be the configured N."""
    path = REPO / relative
    if not path.is_file():
        pytest.skip(f"{relative} is not present")
    n = _configured_repetitions()
    stale = []
    for lineno, command in _logical_lines(path.read_text(encoding="utf-8")):
        found = re.search(r"--runs (\d+)", command)
        if not found or int(found.group(1)) == n:
            continue
        if any(runner in command for runner in NON_CAMPAIGN_RUNNERS):
            continue
        stale.append(f"{relative}:{lineno}: {command.strip()}")
    assert not stale, (
        f"{relative} prints a command at a replication count that is not {n}; "
        f"anyone copying it onto a node produces data that cannot be compared "
        f"to the campaign:\n" + "\n".join(stale)
    )
