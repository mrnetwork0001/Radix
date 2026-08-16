"""npm semver range matching in pure Python.

Reimplements node-semver's default semantics (strict parsing, but tolerant of
a leading ``v`` and surrounding whitespace, ``includePrerelease=False``) for
the two operations the closure needs:

    satisfies(version, range_)      -> bool
    max_satisfying(versions, range_) -> str | None

The desugaring pipeline mirrors node-semver's ``Range.parseRange`` step for
step - hyphen replace, operator-whitespace trim, then per-token caret / tilde /
x-range expansion into primitive comparators - so that edge behaviour (the
``^0.0.x`` leftmost-nonzero rule, ``<=1.2.x`` becoming ``<1.3.0-0``, the
prerelease gate applying per ``||`` alternative) matches the real thing. The
suite in ``backend/tests/test_semver_npm.py`` cross-checks every branch
against node-semver itself via a generated oracle fixture.

Invalid input never raises: ``satisfies`` returns ``False`` and
``max_satisfying`` returns ``None``, exactly like node-semver's own
try/catch behaviour.

Pure stdlib.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable, NamedTuple

__all__ = ["satisfies", "max_satisfying"]

# node-semver rejects components above Number.MAX_SAFE_INTEGER and version
# strings longer than 256 characters.
_MAX_SAFE_INTEGER = 2**53 - 1
_MAX_LENGTH = 256

# --- grammar (transcribed from node-semver's re.js, strict variants) --------

_NUM = r"0|[1-9]\d*"
_PRE_ID = rf"(?:{_NUM}|\d*[A-Za-z-][0-9A-Za-z-]*)"
_PRE = rf"(?:-({_PRE_ID}(?:\.{_PRE_ID})*))"
_BUILD = r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))"

_FULL_RE = re.compile(rf"^v?({_NUM})\.({_NUM})\.({_NUM}){_PRE}?{_BUILD}?$")

_XID = rf"{_NUM}|x|X|\*"
# XRANGEPLAIN: optional v/= noise, then 1-3 dotted parts; prerelease and build
# are only grammatical after a full major.minor.patch triple.
_XRANGE_PLAIN = rf"[v=\s]*({_XID})(?:\.({_XID})(?:\.({_XID}){_PRE}?{_BUILD}?)?)?"

_CARET_RE = re.compile(rf"^\^{_XRANGE_PLAIN}$")
_TILDE_RE = re.compile(rf"^~>?{_XRANGE_PLAIN}$")          # ~> is a tilde alias
_XRANGE_RE = re.compile(rf"^([<>]=?|=)?\s*{_XRANGE_PLAIN}$")
_HYPHEN_RE = re.compile(rf"^{_XRANGE_PLAIN} - {_XRANGE_PLAIN}$")

# Collapses "  >= 1.2.3" style gaps between an operator and its operand, like
# node's COMPARATORTRIM / TILDETRIM / CARETTRIM passes.
_OP_WS_RE = re.compile(r"([<>]=?|=|~>?|\^)\s+")
# node strips build metadata from every range alternative before parsing
# (Range.parseRange's BUILDSTRIPRE) - '1.2.3+b - 2' hyphens fine, '+b' is ANY.
_BUILD_STRIP_RE = re.compile(r"\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*")
_WS_RE = re.compile(r"\s+")
_COMPARATOR_RE = re.compile(r"^([<>]=?|=)?(.*)$", re.S)


class _Version(NamedTuple):
    major: int
    minor: int
    patch: int
    prerelease: tuple[int | str, ...]  # () means a release version


class _Comparator(NamedTuple):
    op: str          # '', '<', '<=', '>', '>=' - '' is equality
    version: _Version


# --- version parsing and SemVer §11 ordering --------------------------------


def _parse_version(text: object) -> _Version | None:
    if not isinstance(text, str):
        return None
    if len(text) > _MAX_LENGTH:  # node checks the raw length, pre-trim
        return None
    text = text.strip()
    if not text:
        return None
    match = _FULL_RE.match(text)
    if match is None:
        return None
    major, minor, patch = int(match[1]), int(match[2]), int(match[3])
    if max(major, minor, patch) > _MAX_SAFE_INTEGER:
        return None
    pre = match[4]
    prerelease: tuple[int | str, ...] = ()
    if pre:
        # Purely numeric identifiers compare numerically, so store them as int.
        prerelease = tuple(int(p) if p.isdigit() else p for p in pre.split("."))
    return _Version(major, minor, patch, prerelease)  # build metadata ignored


def _compare_ids(a: int | str, b: int | str) -> int:
    a_num = isinstance(a, int)
    b_num = isinstance(b, int)
    if a_num is not b_num:
        return -1 if a_num else 1  # numeric identifiers sort below alphanumeric
    if a == b:
        return 0
    return -1 if a < b else 1  # type: ignore[operator]  # same-type by now


def _compare(a: _Version, b: _Version) -> int:
    for x, y in ((a.major, b.major), (a.minor, b.minor), (a.patch, b.patch)):
        if x != y:
            return -1 if x < y else 1
    ap, bp = a.prerelease, b.prerelease
    if ap and not bp:
        return -1  # 1.0.0-alpha < 1.0.0
    if bp and not ap:
        return 1
    for x, y in zip(ap, bp):
        cmp = _compare_ids(x, y)
        if cmp:
            return cmp
    return (len(ap) > len(bp)) - (len(ap) < len(bp))  # alpha < alpha.1


# --- range desugaring (mirrors node-semver's Range.parseRange) --------------


def _is_x(part: str | None) -> bool:
    return part is None or part in ("x", "X", "*")


def _replace_caret(token: str) -> str:
    match = _CARET_RE.match(token)
    if match is None:
        return token
    major, minor, patch, pre, _build = match.groups()
    if _is_x(major):
        return ""
    if _is_x(minor):
        return f">={major}.0.0 <{int(major) + 1}.0.0-0"
    if _is_x(patch):
        if major == "0":
            return f">={major}.{minor}.0 <{major}.{int(minor) + 1}.0-0"
        return f">={major}.{minor}.0 <{int(major) + 1}.0.0-0"
    # Leftmost-nonzero rule: the caret pins every component left of (and
    # including) the first nonzero one.
    lo = f">={major}.{minor}.{patch}" + (f"-{pre}" if pre else "")
    if major != "0":
        return f"{lo} <{int(major) + 1}.0.0-0"
    if minor != "0":
        return f"{lo} <{major}.{int(minor) + 1}.0-0"
    return f"{lo} <{major}.{minor}.{int(patch) + 1}-0"


def _replace_tilde(token: str) -> str:
    match = _TILDE_RE.match(token)
    if match is None:
        return token
    major, minor, patch, pre, _build = match.groups()
    if _is_x(major):
        return ""
    if _is_x(minor):
        return f">={major}.0.0 <{int(major) + 1}.0.0-0"
    if _is_x(patch):
        return f">={major}.{minor}.0 <{major}.{int(minor) + 1}.0-0"
    lo = f">={major}.{minor}.{patch}" + (f"-{pre}" if pre else "")
    return f"{lo} <{major}.{int(minor) + 1}.0-0"


def _replace_xrange(token: str) -> str:
    match = _XRANGE_RE.match(token)
    if match is None:
        return token
    op = match[1] or ""
    major_s, minor_s, patch_s = match[2], match[3], match[4]
    # node's invalidXRangeOrder: an x left of a concrete part ('x.1', '1.x.3')
    # makes the comparator invalid - returned untouched so parsing fails.
    # Only this pass checks it; carets/tildes/hyphens are more forgiving.
    if (_is_x(major_s) and not _is_x(minor_s)) or (
        _is_x(minor_s) and patch_s is not None and not _is_x(patch_s)
    ):
        return token
    x_major = _is_x(major_s)
    x_minor = x_major or _is_x(minor_s)
    x_patch = x_minor or _is_x(patch_s)
    if op == "=" and x_patch:
        op = ""
    if x_major:
        # '*' / 'x' - anything; '>*' / '<*' - nothing can match.
        return "<0.0.0-0" if op in (">", "<") else "*"
    if op and x_patch:
        major = int(major_s)
        minor = 0 if x_minor else int(minor_s)
        patch = 0
        if op == ">":
            # >1.x -> >=2.0.0 ; >1.2.x -> >=1.3.0
            op = ">="
            if x_minor:
                major, minor = major + 1, 0
            else:
                minor += 1
        elif op == "<=":
            # <=1.x -> <2.0.0-0 ; <=1.2.x -> <1.3.0-0
            op = "<"
            if x_minor:
                major += 1
            else:
                minor += 1
        suffix = "-0" if op == "<" else ""
        return f"{op}{major}.{minor}.{patch}{suffix}"
    if x_minor:
        return f">={major_s}.0.0 <{int(major_s) + 1}.0.0-0"
    if x_patch:
        return f">={major_s}.{minor_s}.0 <{major_s}.{int(minor_s) + 1}.0-0"
    return token  # a full comparator - nothing to desugar


def _hyphen_replace(match: re.Match[str]) -> str:
    """'1.2.3 - 2.3.4' → '>=1.2.3 <=2.3.4', partials widened per node-semver."""
    f_major, f_minor, f_patch, f_pre, f_build = match.groups()[:5]
    t_major, t_minor, t_patch, t_pre, t_build = match.groups()[5:]

    if _is_x(f_major):
        lo = ""
    elif _is_x(f_minor):
        lo = f">={f_major}.0.0"
    elif _is_x(f_patch):
        lo = f">={f_major}.{f_minor}.0"
    else:
        lo = f">={f_major}.{f_minor}.{f_patch}"
        if f_pre:
            lo += f"-{f_pre}"
        if f_build:
            lo += f"+{f_build}"

    if _is_x(t_major):
        hi = ""
    elif _is_x(t_minor):
        hi = f"<{int(t_major) + 1}.0.0-0"
    elif _is_x(t_patch):
        hi = f"<{t_major}.{int(t_minor) + 1}.0-0"
    elif t_pre:
        hi = f"<={t_major}.{t_minor}.{t_patch}-{t_pre}"
    else:
        hi = f"<={t_major}.{t_minor}.{t_patch}"
        if t_build:
            hi += f"+{t_build}"

    return f"{lo} {hi}".strip()


def _parse_comparator(token: str) -> _Comparator | None:
    match = _COMPARATOR_RE.match(token)
    if match is None:  # pragma: no cover - the pattern matches any string
        return None
    op = match[1] or ""
    if op == "=":
        op = ""
    version = _parse_version(match[2])
    if version is None:
        return None
    return _Comparator(op, version)


def _parse_set(text: str) -> tuple[_Comparator, ...] | None:
    """One '||'-alternative into primitive comparators; None if malformed.

    'Any' comparators ('*', bare 'x', the empty range) are dropped rather than
    stored: they match every valid version and are skipped by the prerelease
    gate, so an empty comparator tuple is exactly node-semver's ``[ANY]`` set.
    """
    s = _BUILD_STRIP_RE.sub("", _WS_RE.sub(" ", text.strip()))
    hyphen = _HYPHEN_RE.match(s)
    if hyphen is not None:
        s = _hyphen_replace(hyphen)
    s = _OP_WS_RE.sub(r"\1", s)

    pieces = [p for p in s.split(" ") if p]
    for stage in (_replace_caret, _replace_tilde, _replace_xrange):
        pieces = [out for p in pieces for out in stage(p).split(" ") if out]

    comparators: list[_Comparator] = []
    for piece in pieces:
        if piece == "*":
            continue
        comparator = _parse_comparator(piece)
        if comparator is None:
            return None
        comparators.append(comparator)
    return tuple(comparators)


@lru_cache(maxsize=4096)
def _parse_range(range_: str) -> tuple[tuple[_Comparator, ...], ...] | None:
    sets = []
    for alternative in range_.split("||"):
        comparators = _parse_set(alternative)
        if comparators is None:
            return None
        sets.append(comparators)
    return tuple(sets)


# --- evaluation -------------------------------------------------------------


def _test_comparator(comparator: _Comparator, version: _Version) -> bool:
    cmp = _compare(version, comparator.version)
    op = comparator.op
    if op == "":
        return cmp == 0
    if op == "<":
        return cmp < 0
    if op == "<=":
        return cmp <= 0
    if op == ">":
        return cmp > 0
    return cmp >= 0  # '>='


def _test_set(comparators: tuple[_Comparator, ...], version: _Version) -> bool:
    for comparator in comparators:
        if not _test_comparator(comparator, version):
            return False
    if version.prerelease:
        # npm's prerelease gate: a prerelease only satisfies a set in which
        # some comparator mentions a prerelease of the same [M, m, p] tuple.
        for comparator in comparators:
            cv = comparator.version
            if cv.prerelease and cv[:3] == version[:3]:
                return True
        return False
    return True


def satisfies(version: str, range_: str) -> bool:
    """True when ``version`` lies in npm range ``range_``; False on bad input."""
    parsed = _parse_version(version)
    if parsed is None or not isinstance(range_, str):
        return False
    sets = _parse_range(range_)
    if sets is None:
        return False
    return any(_test_set(comparators, parsed) for comparators in sets)


def max_satisfying(versions: Iterable[str], range_: str) -> str | None:
    """The highest version in ``versions`` satisfying ``range_``, else None.

    Returns the winning element exactly as it appeared in the input (so a
    'v1.2.3' stays 'v1.2.3'); on ties the earliest occurrence wins, matching
    node-semver's maxSatisfying. Unparseable entries are skipped.
    """
    if not isinstance(range_, str):
        return None
    sets = _parse_range(range_)
    if sets is None or versions is None:
        return None
    best: str | None = None
    best_parsed: _Version | None = None
    for candidate in versions:
        parsed = _parse_version(candidate)
        if parsed is None:
            continue
        if not any(_test_set(comparators, parsed) for comparators in sets):
            continue
        if best_parsed is None or _compare(parsed, best_parsed) > 0:
            best, best_parsed = candidate, parsed
    return best
