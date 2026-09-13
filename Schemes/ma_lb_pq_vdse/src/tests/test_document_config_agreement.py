"""The prose documents must agree with the files that own their facts.

Added 2026-09-08, after a rewrite of ``SystemConfiguration.md`` found
**seventeen** false claims in it. Among them: three separate sections asserting
an instance-type pin that was removed from ``global.yaml`` on 2026-08-30, "no
`results.csv` in the repo is reportable yet" against 103 that were stamped
reportable, "30 measured runs per point" against a configured 10, and an
instruction to run a ``.claude`` skill that does not exist.

**Why a test and not a rule.** Prose drifts because nothing executes it. A rule
telling someone to keep the docs current is itself prose and rots the same way —
``README.md`` had one ("the specification's source of truth, do not edit
casually") and still went stale, then was deleted leaving 338 dangling ``§N``
citations behind. The only durable fix is to read the numbers back out of the
file that owns them, which is what ``test_repetition_count_agreement.py``
already does for the replication count and what this file generalises.

**What is deliberately NOT asserted.** Judgement prose, the operational traps,
and the AWS estate. Those are unverifiable experience, and unverifiable is not
the same as wrong — ``ami-0feb3b14b4ea27844`` is the only surviving copy of the
686 MB frozen corpus, and no test can confirm that. Asserting only what is
machine-checkable is the point; the rest is carried verbatim and marked as such.
"""

from __future__ import annotations

import json
import re

# utf-8 everywhere, explicitly. The platform default on Windows is cp1252 and
# these documents contain em-dashes and section signs, so an unqualified
# read_text() dies before it can assert anything — which is how the repetition
# count drifted out of Section VI unnoticed in the first place.
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[4]
GLOBAL_YAML = REPO / "Experiment Configuration" / "global.yaml"
MANIFEST = REPO / "Dataset" / "dataset_manifest.json"
# skill.md, not SystemConfiguration.md. The latter was the operator guide
# until it was deleted in 83b83ff, after which these three tests skipped on
# "SystemConfiguration.md is not present" -- live-looking guards checking
# nothing, which is the same silent-skip failure this file exists to catch.
# CLAUDE.md names skill.md as the manual, so that is what they check now.
OPERATOR_GUIDE = REPO / "skill.md"

#: The two prose documents that survive the 2026-09-08 cull. A third should not
#: appear: every document retired from this repo has left dangling citations
#: behind, and the cost scales with how many there are to retire.
# skill.md replaced SystemConfiguration.md as the prose document whose
# cited paths are checked; the latter was deleted in 83b83ff.
PROSE_DOCS = ("CLAUDE.md", "skill.md")

#: Top-level directories a repo-relative path can begin with.
_REPO_DIRS = (
    "Common/",
    "Dataset/",
    "Experiment Configuration/",
    "Schemes/",
    "infra/",
    "Plots/",
    "References/",
    "Overleaf/",
    ".claude/",
)

#: ``Dataset/derived/`` holds the git-ignored corpus. Citing it is correct; it
#: is simply absent on any machine that has not built it.
_UNTRACKED_PREFIXES = ("Dataset/derived/",)

_CODE_OR_CONFIG = re.compile(r"\.(py|ya?ml|sh|json|csv|tex|ini)$")
_BACKTICKED = re.compile(r"`([^`\n]+)`")


def _configured() -> dict:
    return yaml.safe_load(GLOBAL_YAML.read_text(encoding="utf-8"))


def _cited_paths(text: str):
    """Repo-relative code and config paths a document names in backticks.

    Three exclusions, each for a real reason rather than to make the test pass:

    * **``.md`` is excluded.** A document may legitimately record that another
      document *was deleted* — ``SystemConfiguration.md`` §11 says its traps
      were rescued from ``infra/SESSION_HANDOFF.md`` "before both were deleted",
      which is history, not a broken link.
    * **Globs are excluded.** ``Overleaf/*.tex`` names a pattern, not a file.
    * **``::`` is stripped.** ``sync/dias.py::synchronize`` cites a function.
    """
    for match in _BACKTICKED.finditer(text):
        token = match.group(1).strip().split("::")[0].split()[0]
        if not token.startswith(_REPO_DIRS) or "*" in token:
            continue
        if token.startswith(_UNTRACKED_PREFIXES):
            continue
        if not _CODE_OR_CONFIG.search(token):
            continue
        yield token


@pytest.mark.parametrize("relative", PROSE_DOCS)
def test_documents_cite_only_paths_that_exist(relative):
    """A citation to a moved or deleted file sends a reader nowhere.

    This is the check that would have caught ``sync/ias.py`` (renamed to
    ``dias.py``) and the reference to a ``runtime-table`` skill that was never
    written.
    """
    doc = REPO / relative
    if not doc.is_file():
        pytest.skip(f"{relative} is not present")
    missing = sorted(
        token
        for token in _cited_paths(doc.read_text(encoding="utf-8"))
        if not (REPO / token).exists()
    )
    assert not missing, (
        f"{relative} cites paths that do not exist: {missing}. The file was "
        f"moved or deleted and the document was not updated with it."
    )


def test_operator_guide_agrees_with_the_corpus_manifest():
    """The manifest owns these numbers; the guide restates them.

    ``index.yaml`` restated ``keyword_document_pairs`` as 36,172,487 against the
    manifest's 36,263,865 and nothing caught it — ``verify_corpus_reference``
    compares against the *corpus*, which is git-ignored, so on a machine without
    one it never fired. The manifest is committed, so this always fires.
    """
    if not OPERATOR_GUIDE.is_file():
        pytest.skip("skill.md is not present")
    if not MANIFEST.is_file():
        pytest.skip("dataset_manifest.json is not present")

    doc = OPERATOR_GUIDE.read_text(encoding="utf-8")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    counts = manifest["per_domain_counts"]
    per_domain = list(counts.values()) if isinstance(counts, dict) else list(counts)

    expected = {
        "records": f"{manifest['records']:,}",
        "keyword universe": f"{manifest['keyword_universe_size']:,}",
        "keyword/document pairs": f"{manifest['keyword_document_pairs']:,}",
        "domain count": str(manifest["domains"]),
        "corpus sha256 head": manifest["corpus_sha256"][:8],
        "corpus sha256 tail": manifest["corpus_sha256"][-8:],
        "smallest domain": f"{min(per_domain):,}",
        "largest domain": f"{max(per_domain):,}",
    }
    absent = {name: value for name, value in expected.items() if value not in doc}
    assert not absent, (
        f"skill.md no longer states these manifest facts: "
        f"{absent}. Either the corpus was re-frozen and the document was not "
        f"updated, or a number was restated by hand and is wrong."
    )


#: Sweeps the guide writes as plain integers, so they can be compared directly.
#: Exp. 2 and Exp. 5 are written in powers of ten (``10^6``) and are not
#: asserted. Exp. 7-8 are here because their stated range read "100 -> 5000"
#: for a week after the sweep was extended to 10,000.
NUMERIC_SWEEPS = ("exp7_search_throughput", "exp8_load_balance")


@pytest.mark.parametrize("experiment", NUMERIC_SWEEPS)
def test_operator_guide_states_the_configured_sweep_endpoint(experiment):
    if not OPERATOR_GUIDE.is_file():
        pytest.skip("skill.md is not present")
    top = max(_configured()["experiments"][experiment]["values"])
    doc = OPERATOR_GUIDE.read_text(encoding="utf-8")
    assert f"{top:,}" in doc or str(top) in doc, (
        f"{experiment} sweeps to {top:,}, and skill.md does not "
        f"say so — a reader would plan a campaign against the wrong range"
    )


def test_operator_guide_states_the_configured_repetitions_in_prose():
    """``--runs N`` is guarded elsewhere; this catches the prose form.

    The old text read "30 measured runs per point" against a configured 10, in a
    sentence containing no command — so the flag-level guard in
    ``test_repetition_count_agreement.py`` could not see it.
    """
    if not OPERATOR_GUIDE.is_file():
        pytest.skip("skill.md is not present")
    doc = OPERATOR_GUIDE.read_text(encoding="utf-8")
    stated = re.search(r"\*{0,2}(\d+)\*{0,2} measured runs", doc)
    assert stated, "skill.md no longer states 'N measured runs'"
    configured = int(_configured()["measurement"]["repetitions"])
    assert int(stated.group(1)) == configured, (
        f"skill.md says {stated.group(1)} measured runs; "
        f"global.yaml says {configured}"
    )
#: Prose documents that no longer exist. A comment naming one of these sends a
#: reader to a path that does not exist.
#:
#: `SystemConfiguration.md` was itself the resolver this list used to point at
#: ("section 8 holds the table that replaced them") and was deleted in 83b83ff
#: with the 2026-09-12 structure revision. It went uncaught for a day because
#: the guard against dead pointers did not list the document the guard's own
#: comment cited. The eight-file set in CLAUDE.md is the replacement: skill.md
#: for anything operational, the scheme documents for scheme specifics.
#:
#: Sorted, so a new entry has one obvious place to go.
DELETED_DOCS = (
    "MANUSCRIPT_DIVERGENCE.md",
    "PHASE_III_PLAN.md",
    "PHASE_IV_PLAN.md",
    "SCHEME.md",
    "SystemConfiguration.md",
)

#: Files allowed to name a deleted document, and why. Each is an account of the
#: deletion rather than a pointer at the deleted thing, so rewriting them would
#: erase the history that explains the rule.
_HISTORY = (
    "DECISIONS.md",                        # closed archive, per CLAUDE.md
    "SKILL.md",                            # records that README.md is gone
    "bench-coder.md",                      # records when README.md was deleted
    "test_document_config_agreement.py",   # this file
    "test_repetition_count_agreement.py",  # three comments, all past tense
)

#: Every name above is reachable, so every entry does work. Nothing here names
#: a repo-root document: root prose is not scanned (see ``_source_files``), so
#: an entry for one would be decoration -- and decoration in an exclusion list
#: is how a guard comes to look like it covers something it does not.

#: ``.md`` is included deliberately. Leaving it out would make every ``.md``
#: entry in ``_HISTORY`` inert and the guard would look like it covered
#: prose while covering none of it -- the shape of defect this whole sweep
#: was about.
_SOURCE_SUFFIXES = (".py", ".yaml", ".yml", ".sh", ".csv", ".md")


def _source_files():
    # Repo-root code and config, but NOT root prose. The two surviving prose
    # documents are covered by ``test_documents_cite_only_paths_that_exist``,
    # and a document whose subject IS the deletion has to be free to name what
    # was deleted. This guard is for source.
    for path in sorted(REPO.glob("*")):
        if path.is_file() and path.suffix in _SOURCE_SUFFIXES:
            if path.suffix != ".md" and path.name not in _HISTORY:
                yield path
    for root in _REPO_DIRS:
        base = REPO / root.rstrip("/")
        if not base.is_dir() or root == "References/":
            continue
        for path in base.rglob("*"):
            if path.suffix not in _SOURCE_SUFFIXES:
                continue
            if "__pycache__" in path.parts or ".venv" in path.parts:
                continue
            if path.name in _HISTORY:
                continue
            yield path


def test_no_source_file_cites_a_deleted_document():
    """A comment citing a deleted file is a reader sent nowhere.

    This is the guard for the 2026-09-10 sweep: 79 such citations across 48
    files, none of which any test could see. The prose half is covered by
    ``test_documents_cite_only_paths_that_exist``; nothing covered source
    comments, which is where the great majority of them were.

    ``README.md`` is deliberately NOT in ``DELETED_DOCS``: ``.pytest_cache`` and
    ``References/`` legitimately contain one, and the bare ``README section N``
    form is tracked separately.
    """
    offenders = []
    for path in _source_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for doc in DELETED_DOCS:
            if doc in text:
                offenders.append(f"{path.relative_to(REPO).as_posix()} -> {doc}")
    assert not offenders, (
        "these cite documents deleted in the 2026-09-07/08 cull: "
        + ", ".join(sorted(offenders))
        + ". Cite the surviving section of SystemConfiguration.md, or keep the "
        "decision reference and drop the path (see its section 8)."
    )
#: Any spelling of a reference to a section of the deleted ``README.md``:
#: "README §5", "README's §7", "README section 5", and the mojibake "README S6"
#: that a non-UTF-8 write left behind. All four were live in the tree on
#: 2026-09-10.
_README_SECTION = re.compile(r"README(?:\.md)?(?:'s)?\s*(?:§|S|section)\s?\d+")


def test_no_source_file_cites_a_section_of_the_deleted_readme():
    """The number is the part that rots, and it had already rotted.

    333 of these were live across 87 files. README.md was recovered from
    ``e503655^`` to check them and its own numbering had drifted: ~20 cites of
    "§14" meant the Ground Rules, which is §13 in the recovered file, while
    §14 was Open Issues. So a fifth of them pointed at the wrong section
    *before* the file was deleted, and nothing here could see it.

    Facts a config file owns now cite that file (``global.yaml``,
    ``dataset.yaml``) so a wrong reference is checkable rather than merely
    stale; the rest cite ``SystemConfiguration.md`` with no section number,
    which cannot develop the same fault.
    """
    offenders = []
    for path in _source_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for match in _README_SECTION.finditer(text):
            offenders.append(
                "%s -> %r" % (path.relative_to(REPO).as_posix(), match.group(0))
            )
    assert not offenders, (
        "these cite a section of the deleted README.md: "
        + ", ".join(sorted(set(offenders)))
        + ". Cite the config file that owns the value, or SystemConfiguration.md "
        "with no section number."
    )
