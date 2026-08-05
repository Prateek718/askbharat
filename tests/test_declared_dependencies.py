"""Every third-party module askbharat imports must be declared in requirements/.

This is a regression test for a defect that shipped twice. The flat
requirements.txt declared crawl4ai, which nothing imported, while omitting
playwright, pypdf and trafilatura, all module-level imports in
askbharat/ingest/adapters/. A clean install produced an environment where the
static harvest died on import.

It stayed invisible because the development venv had the missing packages
installed by hand — the one machine that could have noticed was the one
machine guaranteed not to. Only a fresh environment surfaces it, so this test
compares what the source imports against what the requirements files declare,
rather than against what happens to be installed.

The first fix caught playwright and pypdf and missed trafilatura, because the
audit globbed askbharat/ingest/*.py and never descended into adapters/. Hence
rglob here, and hence a test rather than another one-off sweep.
"""
from __future__ import annotations

import ast
import re
import sys
from importlib.metadata import packages_distributions
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = ROOT / "requirements"

# Local top-level packages: imported by path, never installed from an index.
LOCAL = {"askbharat", "tests", "bench"}


def _normalise(name: str) -> str:
    """PEP 503 normalisation, so PyYAML, pyyaml and py_yaml compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared() -> set[str]:
    """Distribution names declared across every requirements file."""
    out: set[str] = set()
    for path in REQUIREMENTS.glob("*.txt"):
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            # Skip blanks, comments, includes and index directives.
            if not line or line.startswith(("#", "-r", "--")):
                continue
            # "psycopg[binary]==3.3.4" -> "psycopg"
            name = re.split(r"[<>=!~\[;]", line, maxsplit=1)[0].strip()
            if name:
                out.add(_normalise(name))
    return out


def _imported() -> dict[str, str]:
    """Top-level third-party modules imported under askbharat/, to first file."""
    out: dict[str, str] = {}
    for path in sorted((ROOT / "askbharat").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for mod in names:
                if mod and mod not in sys.stdlib_module_names and mod not in LOCAL:
                    out.setdefault(mod, str(path.relative_to(ROOT)))
    return out


def test_every_third_party_import_is_declared():
    declared = _declared()
    # Import name and distribution name differ often enough to matter: yaml
    # comes from PyYAML, sentence_transformers from sentence-transformers.
    mod_to_dists = packages_distributions()

    undeclared: list[str] = []
    for mod, where in sorted(_imported().items()):
        dists = {_normalise(d) for d in mod_to_dists.get(mod, [])}
        if not dists:
            # Not installed at all, so it cannot be declared correctly either.
            undeclared.append(f"{mod} (imported by {where}) — not installed")
        elif not (dists & declared):
            undeclared.append(
                f"{mod} (imported by {where}) — provided by "
                f"{sorted(dists)}, none declared in requirements/"
            )

    assert not undeclared, (
        "module-level imports missing from requirements/:\n  "
        + "\n  ".join(undeclared)
        + "\n\nA clean install would fail on these even though the dev venv works."
    )


def test_no_declared_dependency_is_unused():
    """The other direction: a phantom entry is how crawl4ai survived for weeks.

    Scoped to distributions this project names directly. Transitive pins are
    not the concern; a package nobody imports is.
    """
    # Tooling and runtime pieces are invoked as commands or plugins rather than
    # imported by name, so absence from the source is expected for these.
    NOT_IMPORTED_BY_DESIGN = {
        "ruff",          # linter, run as a binary
        "alembic",       # migrations import it, but from migrations/, not askbharat/
        "uvicorn",       # ASGI server, launched as a command
        "torch",         # pulled in by sentence-transformers, pinned for the CPU build
        # Reached through a string rather than an import, which is exactly why
        # a "is it imported?" check cannot see them and why they are listed:
        "jinja2",        # loaded by fastapi.templating.Jinja2Templates
        "psycopg",       # selected by the postgresql+psycopg:// URL scheme
    }
    declared = _declared() - NOT_IMPORTED_BY_DESIGN
    mod_to_dists = packages_distributions()

    used: set[str] = set()
    for mod in _imported():
        used |= {_normalise(d) for d in mod_to_dists.get(mod, [])}
    # migrations/ and tests/ legitimately import declared packages too.
    for extra in (ROOT / "migrations", ROOT / "tests"):
        for path in extra.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0:
                    names = [(node.module or "").split(".")[0]]
                else:
                    continue
                for mod in names:
                    used |= {_normalise(d) for d in mod_to_dists.get(mod, [])}

    unused = sorted(declared - used)
    if unused:
        pytest.fail(
            "declared in requirements/ but imported nowhere: "
            + ", ".join(unused)
            + " — either something should be using it, or it is a phantom "
            "entry like crawl4ai was."
        )
