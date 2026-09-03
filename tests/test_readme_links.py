"""Every relative link in the README must resolve on disk.

A README is the one file that is read far more often than it is executed, so its links rot
without anyone noticing — a renamed directory or a moved result file breaks the reader's
path to the evidence while every test stays green. These are the links that make the
numbers checkable, so they are worth a test.

Only relative links are checked. External URLs are deliberately excluded: a test that
fails because GitHub is briefly unreachable is a test that gets deleted.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RELATIVE = re.compile(r"\]\((?!https?://|#|mailto:)([^)\s]+)\)")


def _links(name: str) -> list[str]:
    text = (ROOT / name).read_text()
    return sorted({m.group(1).split("#")[0] for m in RELATIVE.finditer(text) if m.group(1)})


@pytest.mark.parametrize("target", _links("README.md"))
def test_readme_relative_link_resolves(target):
    assert (ROOT / target).exists(), f"README links to {target}, which does not exist"


@pytest.mark.parametrize("target", _links("deploy/k8s/README.md"))
def test_k8s_readme_relative_link_resolves(target):
    assert (ROOT / "deploy" / "k8s" / target).exists() or (ROOT / target).exists(), \
        f"deploy/k8s/README.md links to {target}, which does not exist"


def test_pyproject_carries_the_metadata_a_reader_needs():
    import tomli

    project = tomli.load((ROOT / "pyproject.toml").open("rb"))["project"]
    for field in ("name", "version", "description", "requires-python", "readme",
                  "license", "authors"):
        assert field in project, f"pyproject is missing {field}"
    assert "Demo" in project["urls"], "pyproject should point at the live demo"
