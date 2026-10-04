#!/usr/bin/env python3
"""Cadence import-boundary checker — language-agnostic.

Reads ``.cadence/cadence.yaml`` and enforces ``boundaries:`` rules across
any language whose imports are line-oriented (Python, JS/TS, Go, Rust,
Dart, Java, C/C++, Swift, Kotlin, …).

Exit codes:
    0   no violations (or no boundaries configured)
    1   at least one violation
    2   configuration error (missing config, malformed YAML, missing deps)

Usage:
    python tool/check_boundaries.py
    python tool/check_boundaries.py --config .cadence/cadence.yaml
    python tool/check_boundaries.py --root . --quiet

The checker is line-pattern based. It recognises ``import``, ``from``,
``require(``, ``use``, ``include``, ``#include``, and ``mod`` at the
start of a (stripped) line, then checks the line two ways. A forbidden
pattern fires on the line when either one hits (still at most one
violation per pattern per line):

    Tokens      the line contains a path-shaped token of the pattern
                (``_forbidden_tokens``), for every language above.
    Targets     the line's import resolves, lexically, to a repo path the
                pattern matches (``import_targets``, ``_target_matches``),
                for TS/JS and Python files only.

Targets catch what tokens cannot: a directory-index import such as
``from '../db'`` names no ``db/`` token, yet it imports ``src/db``.
Resolution never touches the filesystem, so a fixture proves it:

    TS/JS (.ts .tsx .js .jsx .mjs .cjs)
        every relative specifier (``.``, ``..``, ``./x``, ``../x``) is
        posix-normalised against the importing file's directory.
        Aliases (``@/db``), packages and absolute paths give no target.
    Python (.py)
        ``from M import a, b`` gives M, M/a and M/b; ``import M1, M2 as x``
        gives M1 and M2 (dots become ``/``, no ``src/`` fallback). A
        relative ``from ..M import a`` resolves against the file's package,
        climbing dots-1 levels; ``from . import a`` gives <package>/a.

A target that is the root or leaves it is dropped. A target fires a
pattern when ``fnmatch.fnmatchcase`` matches, or when the pattern ends in
``/**`` or ``/*`` and the target is the directory itself (it matches the
pattern without that suffix; for a literal prefix, plain equality). So
``../db``, ``../db/index`` and ``../db/index.js`` from ``src/http/`` fire
``src/db/**``; ``../dbutils`` (``src/dbutils``) and ``./db``
(``src/http/db``) do not. A pattern with no literal prefix
(``**/db/**``) has no tokens, so only a resolved target can fire it.

Line patterns catch ~95 percent of real violations across the languages
above without needing per-language ASTs. Higher-accuracy stack-specific
adapters land in Cadence Phase 2.

Rule ids:
    Each rule has an id. An explicit ``id`` must match ``^[LB]-[0-9a-f]{8}$``
    (``L-`` for rules the factory learned, see docs/LEARNING.md); anything
    else is a configuration error (exit 2). A rule without one gets the
    seed id ``B-`` + the first 8 hex digits of
    ``sha256(where + "|" + "|".join(forbidden))``. Violations carry the id.

What is scanned:
    Files with a source extension under ``--root``, except directories
    named in ``_SKIP_DIRS`` (matched against the path relative to the
    root, so a root inside a ``build/`` directory still works), the
    generated retro fixtures under ``tests/fixtures/retro/`` (each holds a
    deliberate violation; ``tool/emit_rule.py --replay`` checks them), and
    symbolic links. ``find_violations(root, rules, paths=[...])`` scans only
    the given repo-relative paths, with the same rules.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import os
import posixpath
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    import yaml
except ImportError:
    print(
        'ERROR: PyYAML is required. Install with: pip install pyyaml',
        file=sys.stderr,
    )
    sys.exit(2)


# Line prefixes (after lstrip) that indicate an import-like statement.
_IMPORT_PREFIXES: tuple[str, ...] = (
    'import ',
    'import\t',
    'from ',
    'require(',
    'use ',
    '#include',
    'include ',
    'mod ',
    'export * from ',
    'export {',
)

# File extensions worth scanning.
_SOURCE_EXTS: frozenset[str] = frozenset(
    {
        '.py',
        '.js',
        '.jsx',
        '.ts',
        '.tsx',
        '.mjs',
        '.cjs',
        '.go',
        '.rs',
        '.dart',
        '.java',
        '.kt',
        '.kts',
        '.swift',
        '.c',
        '.h',
        '.cc',
        '.cpp',
        '.hpp',
        '.m',
        '.mm',
    }
)

# Directories to skip while walking.
_SKIP_DIRS: frozenset[str] = frozenset(
    {
        'node_modules',
        '.git',
        '.venv',
        'venv',
        'build',
        'dist',
        'target',
        '.dart_tool',
        '__pycache__',
        '.tox',
        '.pytest_cache',
        '.ruff_cache',
        '.mypy_cache',
    }
)


# Learned rules (L-) and seed rules (B-). Always used with fullmatch.
RULE_ID_RE = r"^[LB]-[0-9a-f]{8}$"
_RULE_ID = re.compile(RULE_ID_RE)

# Generated retro fixtures each hold a deliberate violation; never scan them
# as part of the project.
RETRO_FIXTURE_PREFIX = "tests/fixtures/retro/"

# Files whose imports ``import_targets`` resolves.
_TS_EXTS: frozenset[str] = frozenset({'.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs'})
_PY_EXT = '.py'

# Loose TS/JS specifier extraction (the same expression as signals.py's
# _TS_SPEC; copied, not imported, so the checker stays standalone).
_TS_SPEC = re.compile(
    r"""(?:\bfrom\s*|^\s*import\s*|\bimport\s*\(\s*|\brequire\s*\(\s*)(['"])([^'"\\\s]{1,150})\1"""
)
_PY_FROM = re.compile(
    r'^\s*from\s+(\.*)([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)?\s+import\b(.*)$'
)
_PY_IMPORT = re.compile(r'^\s*import\s+(.+)$')
_PY_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
_PY_DOTTED = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*')


class ConfigError(Exception):
    """The cadence config cannot be used. ``_load_rules`` exits 2 on it."""


@dataclass(frozen=True)
class Rule:
    where: str
    forbidden: tuple[str, ...]
    reason: str
    id: str = ""


@dataclass(frozen=True)
class Violation:
    path: str
    line_no: int
    line: str
    forbidden: str
    reason: str
    rule_id: str = ""

    def format(self) -> str:
        snippet = self.line if len(self.line) <= 120 else self.line[:117] + '...'
        return (
            f'{self.path}:{self.line_no}\n'
            f'  {snippet}\n'
            f'  forbidden: {self.forbidden}\n'
            f'  reason:    {self.reason}'
        )


def _is_import_line(line: str) -> bool:
    stripped = line.lstrip()
    if not stripped:
        return False
    # Comments — skip. `#include` is a C preprocessor directive, not a
    # comment, so we let it fall through to the prefix check below.
    if stripped.startswith(('//', '/*', '*', '--', ';;')):
        return False
    if stripped.startswith('#') and not stripped.startswith('#include'):
        return False
    if any(stripped.startswith(prefix) for prefix in _IMPORT_PREFIXES):
        return True
    # CommonJS / dynamic require — usually preceded by `const`/`let`/`var`.
    if 'require(' in stripped:
        return True
    return False


def _forbidden_tokens(pattern: str) -> list[str]:
    """Return all substring tokens to search import lines for.

    A single glob pattern like ``src/data/sources/**`` is rendered in
    different ways depending on the language's import syntax:

    - Path form (TS/JS/Dart/Go):  ``src/data/sources/``
    - Dotted form (Python/Java):  ``src.data.sources.``
    - Colon form (Rust):          ``src::data::sources::``

    Relative imports drop leading project segments (``../../...``), so
    we also generate every suffix in each form, down to the last segment
    alone. ``data/sources/`` catches ``../../data/sources/X``;
    ``data.sources.`` catches ``from x.data.sources.X import y``;
    ``sources/`` catches ``../sources/X``. ``_line_contains_token`` needs
    a non-identifier character to the token's left, so ``sources/`` does
    not match inside ``mysources/``.

    Every token ends in a separator, so an import that ends at the
    directory itself (``from '../sources'``) has no token. The resolved
    import target catches that case (``import_targets``).
    """
    prefix = pattern
    for i, ch in enumerate(pattern):
        if ch in '*?[':
            prefix = pattern[:i]
            break

    prefix = prefix.rstrip('/')
    if not prefix:
        return []

    segments = [s for s in prefix.split('/') if s]
    if not segments:
        return []

    tokens: list[str] = []
    for n in range(len(segments), 0, -1):
        suffix = segments[-n:]
        tokens.append('/'.join(suffix) + '/')
        tokens.append('.'.join(suffix) + '.')
        tokens.append('::'.join(suffix) + '::')
    return tokens


def _matches_where(path: str, where: str) -> bool:
    return fnmatch.fnmatch(path, where)


def _line_contains_token(line: str, token: str) -> bool:
    """True if ``token`` appears in ``line`` with a non-identifier char to its left.

    Substring matching is too loose: the dotted token ``app.`` would
    spuriously match inside ``myapp.``. We require a left-boundary
    (start-of-line or non-identifier character) so ``app.`` matches
    ``src.app.providers`` but not ``src.myapp.features``.
    """
    if not token:
        return False
    start = 0
    while True:
        idx = line.find(token, start)
        if idx == -1:
            return False
        if idx == 0:
            return True
        prev = line[idx - 1]
        if not (prev.isalnum() or prev == '_'):
            return True
        start = idx + 1


# --- Resolved import targets ----------------------------------------------------


def _norm_target(path: str) -> str | None:
    """A normalised repo-relative posix path; None for the root or outside it."""
    if not path:
        return None
    joined = posixpath.normpath(path)
    if joined in ('.', '..') or joined.startswith(('/', '../')):
        return None
    return joined


def _py_names(text: str) -> list[str]:
    """The imported names of ``from M import <text>`` (``*`` gives none)."""
    text = text.split('#', 1)[0].strip().strip('()').strip()
    names: list[str] = []
    for part in text.split(','):
        tokens = part.strip().strip('()').split()
        if tokens and _PY_NAME.fullmatch(tokens[0]):
            names.append(tokens[0])
    return names


def _py_targets(rel: str, line: str) -> list[str]:
    match = _PY_FROM.match(line)
    if match:
        dots, module, rest = match.group(1), match.group(2), match.group(3)
        names = _py_names(rest)
        if dots:
            package = posixpath.dirname(rel)
            for _ in range(len(dots) - 1):
                if package == '':
                    return []  # above the root
                package = posixpath.dirname(package)
            if not module:
                # ``from . import a``: the names, never the package itself.
                return [posixpath.join(package, name) for name in names]
            base = posixpath.join(package, module.replace('.', '/'))
        elif module:
            base = module.replace('.', '/')
        else:
            return []
        return [base] + [posixpath.join(base, name) for name in names]
    match = _PY_IMPORT.match(line)
    if not match:
        return []
    targets: list[str] = []
    for part in match.group(1).split('#', 1)[0].split(','):
        tokens = part.strip().split()
        if tokens and _PY_DOTTED.fullmatch(tokens[0]):
            targets.append(tokens[0].replace('.', '/'))
    return targets


def _ts_targets(rel: str, line: str) -> list[str]:
    here = posixpath.dirname(rel)
    targets: list[str] = []
    for _, spec in _TS_SPEC.findall(line):
        if spec in ('.', '..') or spec.startswith(('./', '../')):
            targets.append(posixpath.join(here, spec))
    return targets


def import_targets(rel: str, line: str) -> list[str]:
    """Repo-relative posix paths the import on ``line`` of file ``rel`` names.

    Lexical only: nothing is read from disk, so ``../db`` from
    ``src/http/a.ts`` gives ``src/db`` whether or not that is a directory.
    Only TS/JS and Python files resolve; any other file gives ``[]``. TS/JS
    aliases, packages and absolute specifiers give nothing; Python names
    are taken as written (no ``src/`` fallback). Targets that are the root
    or leave it are dropped. The result is de-duplicated, in line order.
    """
    ext = posixpath.splitext(rel)[1]
    if ext in _TS_EXTS:
        raw = _ts_targets(rel, line)
    elif ext == _PY_EXT:
        raw = _py_targets(rel, line)
    else:
        return []
    targets: list[str] = []
    for item in raw:
        target = _norm_target(item)
        if target is not None and target not in targets:
            targets.append(target)
    return targets


def _target_matches(target: str, pattern: str) -> bool:
    """True if the resolved import ``target`` falls under ``pattern``.

    ``fnmatch.fnmatchcase`` on the whole target, or, for a pattern ending
    in ``/**`` or ``/*``, the target is the directory itself (it matches
    the pattern minus that suffix; for a literal prefix, plain equality).
    The root and targets outside it never match.
    """
    if not target or not pattern or _norm_target(target) != target:
        return False
    if fnmatch.fnmatchcase(target, pattern):
        return True
    for suffix in ('/**', '/*'):
        if pattern.endswith(suffix):
            base = pattern[: -len(suffix)]
            return bool(base) and fnmatch.fnmatchcase(target, base)
    return False


def seed_rule_id(where: str, forbidden: Sequence[str]) -> str:
    """The id of a rule that has no explicit ``id``."""
    digest = hashlib.sha256((where + "|" + "|".join(forbidden)).encode("utf-8"))
    return "B-" + digest.hexdigest()[:8]


def rules_from_config(cfg: Any) -> list[Rule]:
    """The boundary rules of a parsed cadence.yaml. Raises ConfigError."""
    if not isinstance(cfg, dict):
        raise ConfigError('cadence config must be a YAML mapping')

    raw_rules = cfg.get('boundaries', []) or []
    if not isinstance(raw_rules, list):
        raise ConfigError("'boundaries' must be a list")

    rules: list[Rule] = []
    for idx, raw in enumerate(raw_rules):
        if not isinstance(raw, dict):
            raise ConfigError(f'boundaries[{idx}] must be a mapping')
        try:
            where = str(raw['where'])
            forbidden = tuple(str(f) for f in raw['forbidden'])
            reason = str(raw['reason'])
        except KeyError as exc:
            raise ConfigError(
                f'boundaries[{idx}] missing required key: {exc}'
            ) from exc
        except TypeError as exc:
            raise ConfigError(
                f'boundaries[{idx}].forbidden must be a list'
            ) from exc
        rule_id = raw.get('id')
        if rule_id is None:
            rule_id = seed_rule_id(where, forbidden)
        elif not isinstance(rule_id, str) or not _RULE_ID.fullmatch(rule_id):
            raise ConfigError(
                f'boundaries[{idx}].id must match {RULE_ID_RE} (got {rule_id!r})'
            )
        rules.append(Rule(where=where, forbidden=forbidden, reason=reason, id=rule_id))
    return rules


def load_rules(config_path: Path) -> list[Rule]:
    """Read the boundary rules from ``config_path``. Raises ConfigError."""
    if not config_path.exists():
        raise ConfigError(f'cadence config not found: {config_path}')
    try:
        with config_path.open('r', encoding='utf-8') as fh:
            cfg = yaml.safe_load(fh)
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        raise ConfigError(f'malformed YAML in {config_path}: {exc}') from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f'could not read {config_path}: {exc}') from exc
    return rules_from_config(cfg)


def _load_rules(config_path: Path) -> list[Rule]:
    try:
        return load_rules(config_path)
    except ConfigError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(2)


def _skipped_rel(rel: str) -> bool:
    """True if the repo-relative posix path ``rel`` is never scanned."""
    if rel.startswith(RETRO_FIXTURE_PREFIX):
        return True
    return bool(set(rel.split('/')) & _SKIP_DIRS)


def _iter_source_files(root: Path) -> Iterable[Path]:
    """Source files under ``root``, in a stable order.

    Skip dirs are matched on the path relative to ``root``, so a root that
    itself sits under a directory named ``build`` is still scanned.
    Symbolic links are skipped, and never followed into.
    """
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        rel_dir = here.relative_to(root).as_posix()
        rel_dir = '' if rel_dir == '.' else rel_dir + '/'
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in _SKIP_DIRS
            and not (rel_dir + d + '/').startswith(RETRO_FIXTURE_PREFIX)
            and not (here / d).is_symlink()
        )
        for name in sorted(filenames):
            path = here / name
            if path.suffix not in _SOURCE_EXTS:
                continue
            if _skipped_rel(rel_dir + name):
                continue
            if path.is_symlink() or not path.is_file():
                continue
            yield path


def _iter_given_paths(root: Path, paths: Iterable[str]) -> Iterable[Path]:
    """The scannable files among the repo-relative posix ``paths``.

    The same extension, skip and symlink rules as a full scan apply. A path
    that leaves the root, or runs through a symlinked directory, is
    ignored.
    """
    seen: set[str] = set()
    for rel in paths:
        if not isinstance(rel, str) or not rel or rel in seen:
            continue
        seen.add(rel)
        parts = rel.split('/')
        if rel.startswith('/') or '\\' in rel or any(
            part in ('', '.', '..') for part in parts
        ):
            continue
        if Path(rel).suffix not in _SOURCE_EXTS or _skipped_rel(rel):
            continue
        current = root
        mode = 0
        for part in parts:
            current = current / part
            try:
                mode = os.lstat(current).st_mode
            except OSError:
                mode = 0
                break
            if stat.S_ISLNK(mode):
                mode = 0
                break
        if stat.S_ISREG(mode):
            yield current


def find_violations(
    root: Path,
    rules: Sequence[Rule],
    paths: Iterable[str] | None = None,
) -> list[Violation]:
    """Violations of ``rules`` under ``root``.

    With ``paths``, only those repo-relative posix paths are scanned. A
    forbidden pattern fires on an import line when one of its tokens is in
    the line, or when one of the line's resolved targets matches it; at
    most one violation per forbidden pattern per line.
    """
    violations: list[Violation] = []
    forbidden_entries: list[tuple[Rule, str, list[str]]] = [
        (rule, forbidden, _forbidden_tokens(forbidden))
        for rule in rules
        for forbidden in rule.forbidden
    ]

    files = (
        _iter_source_files(root) if paths is None else _iter_given_paths(root, paths)
    )
    for path in files:
        rel = path.relative_to(root).as_posix()

        applicable: list[tuple[Rule, str, list[str]]] = [
            (rule, forbidden, tokens)
            for rule, forbidden, tokens in forbidden_entries
            if _matches_where(rel, rule.where)
        ]
        if not applicable:
            continue

        try:
            with path.open('r', encoding='utf-8', errors='ignore') as fh:
                lines = fh.readlines()
        except OSError:
            continue

        for line_no, raw in enumerate(lines, start=1):
            line = raw.rstrip('\n\r')
            if not _is_import_line(line):
                continue
            targets: list[str] | None = None
            for rule, forbidden, tokens in applicable:
                hit = any(_line_contains_token(line, token) for token in tokens)
                if not hit:
                    if targets is None:
                        targets = import_targets(rel, line)
                    hit = any(_target_matches(t, forbidden) for t in targets)
                if hit:
                    # one violation per forbidden pattern per line
                    violations.append(
                        Violation(
                            path=rel,
                            line_no=line_no,
                            line=line.strip(),
                            forbidden=forbidden,
                            reason=rule.reason,
                            rule_id=rule.id,
                        )
                    )
    return violations


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Cadence import-boundary checker (language-agnostic).',
    )
    parser.add_argument(
        '--config',
        default='.cadence/cadence.yaml',
        help='Path to cadence.yaml (default: .cadence/cadence.yaml)',
    )
    parser.add_argument(
        '--root',
        default='.',
        help='Project root to scan (default: cwd)',
    )
    parser.add_argument(
        '--quiet',
        action='store_true',
        help="Suppress 'imports ok' on clean runs",
    )
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    config_path = (root / args.config).resolve()

    rules = _load_rules(config_path)
    if not rules:
        if not args.quiet:
            print('imports ok (no boundary rules configured)')
        return 0

    violations = find_violations(root, rules)
    if not violations:
        if not args.quiet:
            print('imports ok')
        return 0

    print(
        f'Import boundary violations ({len(violations)}):',
        file=sys.stderr,
    )
    for v in violations:
        print(file=sys.stderr)
        print(v.format(), file=sys.stderr)
    print(file=sys.stderr)
    print(
        'See docs/PATTERNS.md (Layer 1) and docs/ADR/ for the rationale '
        'behind each rule.',
        file=sys.stderr,
    )
    return 1


if __name__ == '__main__':
    sys.exit(main())
