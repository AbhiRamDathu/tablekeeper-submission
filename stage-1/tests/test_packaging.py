"""Unit 1b: prove every `COPY` source survives `.dockerignore`, without Docker.

This is the guard that makes the packaging invariant *provable* today. The invariant is one line:
every source named by a `COPY` in the `Dockerfile` must be reachable in the build context. Reading
`.dockerignore` once by eye is not that invariant, and it is what let `app/` sit on line 1 while
`COPY app ./app` sat on line 10 -- a `Dockerfile` that cannot build, committed as if it could.

**This is not Docker.** It is a stdlib stand-in for Docker's ignore matcher, so the invariant can be
checked on a machine with no container tool. It is an approximation and must never be reported as a
build result. The authoritative check remains `docker build -t tablekeeper-s1 .` from cwd `stage-1`,
which is Tier 3 and is UNRUN here -- no Docker on this host. The two are not equivalent.

Why the matcher is hand-written rather than `fnmatch`
---------------------------------------------------
`fnmatch` is the obvious shortcut and it is wrong in three independent ways. The Reviewer found this
before this file existed and the Planner reproduced it against the broken tree:

    fnmatch("app/main.py",           "app/")              -> False
    fnmatch("app/store.py",          "app/")              -> False
    fnmatch("app/__pycache__/x.pyc", "**/__pycache__/")  -> False
    fnmatch("docs/verification.md",  "*.md")              -> True   <- wrong, Docker says included

(a) A pattern like `app/` carries a trailing separator that `app/main.py` does not, so a *directory*
    pattern never matches the files beneath it -- the one case this test exists to catch.
(b) `fnmatch`'s `*` crosses `/`, so `*.md` over-matches `docs/verification.md`, where Docker's matcher
    would not.
(c) On win32 `fnmatch` case-folds through `os.path.normcase`; Docker's matcher is case-sensitive POSIX.

All three divergences point the same way -- toward hiding an exclusion. Using `fnmatch` here reports
**green on the very tree the guard exists to catch**, so Unit 1a would land with no proof and the guard
would ship already open. That is the same shape as the silent zero-test green in commit `756629a`, one
level up: a test that runs, asserts, and passes without ever testing the thing.

The matcher below is therefore spelled out: patterns are normalised first (which is the step that fixes
(a)), wildcards are translated so that `*` cannot cross a separator (fixing (b)), and comparison is
case-sensitive and never routed through `fnmatch` or `normcase` (fixing (c)).

Why two verdicts, not one
-------------------------
Honouring negation has a hole. Docker's documented rule is last-match-wins with `!` re-including, so a
file listed as both `app/` and `!app/` is *included* -- and a single-pass guard therefore exits 0 while
`app/` still excludes the entire application directory. The plan forbids that line, but a forbidden
line nobody checks is a request, not a property. So every pattern is evaluated twice, once with
negation honoured and once with it disabled, and a bare exclusion of any `COPY` source fails
regardless of what follows it:

    case                             single-pass   double-eval
    broken tree, `app/` present      FAIL          FAIL
    sanctioned fix, `app/` deleted   PASS          PASS
    forbidden fix, `app/` + `!app/`  PASS          FAIL

When the two verdicts disagree for a source, the failure names both the offending pattern and the
pattern masking it, so the message reads "`app/` is excluded and `!app/` hides it" rather than the
much less useful "packaging broke".

Read the failure, not just the exit code: both directions are demonstrated. A guard that is red on
both trees is broken in the other direction and would block the fix forever, which is as bad as the
vacuous green and easier to miss.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

STAGE1 = Path(__file__).resolve().parent.parent
DOCKERFILE = STAGE1 / "Dockerfile"
DOCKERIGNORE = STAGE1 / ".dockerignore"


# ---- .dockerignore parsing ---------------------------------------------------------------

class Pattern:
    """One `.dockerignore` line, normalised. `negated` is the leading `!`."""

    __slots__ = ("line_number", "raw", "negated", "text", "_regex")

    def __init__(self, line_number: int, raw: str):
        self.line_number = line_number
        self.raw = raw
        body = raw.strip()
        self.negated = body.startswith("!")
        if self.negated:
            body = body[1:]
        self.text = _normalise_pattern(body)
        self._regex = None if not _has_wildcard(self.text) else _compile(self.text)

    def matches(self, path: str) -> bool:
        """Whether this one pattern excludes `path` (ignoring negation and ordering).

        Docker's ignore is directory-semantic: `**/__pycache__` excludes the directory *and everything
        in it*. Normalisation has already stripped the trailing `/`, so the subtree has to be covered
        explicitly -- both for a wildcard-free pattern (`app/` must cover `app/main.py`) and for a
        wildcard one (`**/__pycache__` must cover `app/__pycache__/x.pyc`). Both are done by also
        matching the path's ancestor prefixes, which is what Docker's own traversal does.
        """
        if not self.text:
            return False
        candidate = path
        while candidate:
            if self._matches_one(candidate):
                return True
            cut = candidate.rfind("/")
            if cut < 0:
                return False
            candidate = candidate[:cut]
        return False

    def _matches_one(self, path: str) -> bool:
        if self._regex is None:
            # The step that fixes (a): `app` and `app/` reduce to the same pattern.
            return path == self.text or path.startswith(self.text + "/")
        return self._regex.fullmatch(path) is not None

    def __str__(self) -> str:
        return f".dockerignore:{self.line_number} {self.raw!r}"


def _normalise_pattern(body: str) -> str:
    text = body.replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def _has_wildcard(text: str) -> bool:
    return any(ch in text for ch in "*?[")


def _compile(text: str) -> re.Pattern:
    """Translate a normalised Docker pattern to a case-sensitive fullmatch regex.

    `**` spans separators, `*` and `?` never do, and everything else is escaped literally.
    """
    out = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "*":
            if text.startswith("**", index):
                index += 2
                if text.startswith("/", index):
                    # `**/` also matches zero directories, so `**/__pycache__` covers a top-level one.
                    out.append("(?:.*/)?")
                    index += 1
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        index += 1
    return re.compile("".join(out))


def read_dockerignore() -> list[Pattern]:
    patterns = []
    for number, raw in enumerate(DOCKERIGNORE.read_text(encoding="utf-8").splitlines(), start=1):
        body = raw.split("#", 1)[0].strip()
        if body:
            patterns.append(Pattern(number, body))
    return patterns


# ---- the two verdicts --------------------------------------------------------------------

def excluded(path: str, patterns: list[Pattern], *, honour_negation: bool) -> tuple[bool, Pattern | None]:
    """`(is_excluded, deciding_pattern)` for `path`. Last matching line wins.

    A leading `!` re-includes, so a negated match clears the verdict rather than setting it. When
    `honour_negation` is False, `!` lines decide nothing at all -- that is the second of the two
    evaluations the rev. 3.26 clause requires, and it is what makes a bare exclusion fail even when
    a later `!app/` would otherwise hide it.
    """
    verdict, is_excluded = None, False
    for pattern in patterns:
        if not pattern.matches(path):
            continue
        if pattern.negated:
            if honour_negation:
                verdict, is_excluded = pattern, False
        else:
            verdict, is_excluded = pattern, True
    return is_excluded, verdict


# ---- Dockerfile parsing ------------------------------------------------------------------

def copy_sources() -> list[str]:
    """Every source named by a `COPY`, normalised. Parsed, never hard-coded."""
    sources = []
    for statement in _logical_lines(DOCKERFILE.read_text(encoding="utf-8")):
        parts = statement.split()
        if not parts or parts[0].upper() != "COPY":
            continue
        operands = [p for p in parts[1:] if not p.startswith("--")]
        for source in operands[:-1]:  # the last operand is the destination
            normalised = _normalise_pattern(source.strip('"'))
            if normalised:
                sources.append(normalised)
    return sources


def _logical_lines(text: str) -> list[str]:
    """Dockerfile statements, with `\` continuations joined."""
    lines, pending = [], ""
    for raw in text.splitlines():
        if raw.lstrip().startswith("#"):
            continue
        stripped = raw.rstrip()
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        lines.append((pending + stripped).strip())
        pending = ""
    if pending.strip():
        lines.append(pending.strip())
    return [line for line in lines if line]


# ---- the guard ---------------------------------------------------------------------------

class Packaging(unittest.TestCase):
    """Every `COPY` source in the `Dockerfile` must be reachable in the build context."""

    def test_dockerignore_excludes_copied_source(self):
        sources = copy_sources()
        patterns = read_dockerignore()
        self.assertTrue(sources, "no COPY source parsed out of the Dockerfile")

        # `COPY app ./app` copies a whole subtree, so the source is matched as a directory: the
        # prefix rule in `Pattern.matches` is what makes one pattern cover everything beneath it.
        unreachable, masked, bare = [], [], []
        for source in sources:
            is_excluded, decided = excluded(source, patterns, honour_negation=True)
            if is_excluded:
                unreachable.append(f"{source!r} is excluded by {decided}"
                                   f" (Docker will fail to COPY it)")
            unnegated_excluded, unnegated = excluded(source, patterns, honour_negation=False)
            if unnegated_excluded:
                # Excluded either way, or reachable only because a `!` line hides the exclusion.
                offending = f"{source!r} -> {unnegated}"
                bare.append(offending)
                if not is_excluded:
                    masked.append(f"{offending}, masked by {_masking_pattern(source, patterns)}")

        problems = []
        if unreachable:
            problems.append(
                f"{len(unreachable)} of {len(sources)} COPY source(s) are not in the build context:\n"
                + "\n".join(f"  {line}" for line in unreachable))
        if bare:
            problems.append(
                f"{len(bare)} COPY source(s) are excluded by a bare pattern:\n"
                + "\n".join(f"  {line}" for line in bare)
                + "\n  A later `!` line re-includes them, so a last-match-wins check alone exits 0"
                  " here while the exclusion is still in the file. Do not add `!` to re-include"
                  " something the Dockerfile still needs; delete the excluding line.")
        if masked:
            problems.append(
                f"the two verdicts disagree for {len(masked)} source(s) -- these pass only because a"
                f" `!` line hides a bare exclusion:\n"
                + "\n".join(f"  {line}" for line in masked))

        self.assertEqual(problems, [], "\n\n".join(problems))


def _masking_pattern(source: str, patterns: list[Pattern]) -> str:
    """The `!` line that re-includes a source whose excluding line is still present."""
    for pattern in reversed(patterns):
        if pattern.negated and pattern.matches(source):
            return str(pattern)
    return "(no `!` line found -- the two verdicts disagree for another reason)"


if __name__ == "__main__":
    unittest.main()