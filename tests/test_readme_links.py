"""Every relative link in every markdown file must resolve.

The earlier version of this checked README.md and deploy/k8s/README.md only, and missed two
broken links in docs/METHOD.md — introduced when that file was split out of the README and
a rewrite rule applied `../` to paths that were already relative, producing `../../`. The
lesson is the narrow one: a check scoped to the files you were thinking about at the time
does not cover the file you add next month.

Only relative links are checked. External URLs are excluded on purpose — a test that fails
because GitHub is briefly unreachable is a test that gets deleted.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "data", "__pycache__", "mlruns", ".pytest_cache", "build"}
RELATIVE = re.compile(r"\]\((?!https?://|#|mailto:)([^)\s]+)\)")


def _markdown_files() -> list[Path]:
    return sorted(
        p for p in ROOT.rglob("*.md")
        if not any(part in SKIP_DIRS for part in p.parts)
        and "hf-space/build" not in p.as_posix()
    )


def _cases() -> list[tuple[str, str]]:
    out = []
    for md in _markdown_files():
        for match in RELATIVE.finditer(md.read_text(encoding="utf-8", errors="ignore")):
            target = match.group(1).split("#")[0]
            if target:
                out.append((md.relative_to(ROOT).as_posix(), target))
    return out


@pytest.mark.parametrize("source,target", _cases(),
                         ids=lambda v: v.replace("/", "-") if isinstance(v, str) else v)
def test_relative_link_resolves(source, target):
    """Resolved from the linking file's own directory, which is the bug the old test could
    not see: a path that is correct from the repo root is wrong from docs/."""
    linked_from = (ROOT / source).parent
    assert (linked_from / target).resolve().exists(), (
        f"{source} links to {target}, which does not resolve from {linked_from.name}/")


def test_every_markdown_file_is_covered():
    """Guards the guard. If a new doc lands somewhere this glob does not reach, the link
    check silently stops covering it, which is how the last two broken links survived."""
    covered = {source for source, _ in _cases()}
    have_links = {
        md.relative_to(ROOT).as_posix() for md in _markdown_files()
        if RELATIVE.search(md.read_text(encoding="utf-8", errors="ignore"))
    }
    assert have_links == covered, f"markdown with links but not checked: {have_links - covered}"


def test_pyproject_carries_the_metadata_a_reader_needs():
    import tomli

    project = tomli.load((ROOT / "pyproject.toml").open("rb"))["project"]
    for field in ("name", "version", "description", "requires-python", "readme",
                  "license", "authors"):
        assert field in project, f"pyproject is missing {field}"
    assert "Demo" in project["urls"], "pyproject should point at the live demo"
