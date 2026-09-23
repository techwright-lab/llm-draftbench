"""Installed metadata closure, without imports of dependency packages."""

from email.message import Message
from types import SimpleNamespace

import pytest

from draftbench import pilot


@pytest.fixture
def graph(monkeypatch):
    packages = {}

    def add(name, requires=(), extras=(), version="1.0", metadata_name=None):
        metadata = Message()
        metadata["Name"] = metadata_name or name
        for extra in extras:
            metadata["Provides-Extra"] = extra
        packages[name] = SimpleNamespace(
            metadata=metadata, requires=list(requires), version=version
        )

    def lookup(name):
        if name not in packages:
            raise pilot.PackageNotFoundError(name)
        return packages[name]

    monkeypatch.setattr(pilot, "distribution", lookup)
    monkeypatch.setattr(pilot, "version", lambda name: lookup(name).version)
    add("draftbench", ["base", 'sdk; extra == "pilot"'], ["pilot", "dev"])
    add("base")
    add("sdk")
    return add, packages


def test_active_markers_extras_cycles_and_canonical_names(graph):
    add, packages = graph
    add(
        "draftbench",
        [
            "later",
            'Shared_Pkg[FAST.path]; extra == "pilot"',
            'missing-dev; extra == "dev"',
            'missing-platform; python_version < "1"',
            'active; python_version >= "3.11"',
        ],
        ["pilot", "dev"],
    )
    add("active")
    add("later", ["SHARED.pkg[second]"])
    add(
        "shared-pkg",
        [
            'fast; extra == "fast-path"',
            'second; extra == "second"',
            'missing-unused; extra == "unused"',
            "draftbench[pilot]",
        ],
        ["fast-path", "second", "unused"],
        metadata_name="Shared_Pkg",
    )
    add("fast")
    add("second")
    add("unrelated-dev", version="99")
    result = pilot.runtime_dependencies()
    assert result == dict.fromkeys(
        ["active", "draftbench", "fast", "later", "second", "shared-pkg"], "1.0"
    )
    assert list(result) == sorted(result)
    for dist in packages.values():
        dist.requires.reverse()
    assert pilot.runtime_dependencies() == result


@pytest.mark.parametrize(
    "requirement",
    [
        "missing",
        "sdk>=2",
        "sdk[missing]",
        "not a requirement!",
        "sdk @ https://invalid.test/x.whl",
    ],
)
def test_unresolvable_requirements_fail_closed(graph, requirement):
    add, _ = graph
    add("sdk", [requirement])
    with pytest.raises(ValueError, match="^pilot_dependencies_unresolvable$"):
        pilot.runtime_dependencies()


@pytest.mark.parametrize("version", ["", "not-a-version", None])
def test_invalid_versions_fail_closed(graph, version):
    add, _ = graph
    add("sdk", version=version)
    with pytest.raises(ValueError, match="^pilot_dependencies_unresolvable$"):
        pilot.runtime_dependencies()


def test_wrong_distribution_identity_fails_closed(graph):
    add, _ = graph
    add("sdk", metadata_name="another-package")
    with pytest.raises(ValueError, match="^pilot_dependencies_unresolvable$"):
        pilot.runtime_dependencies()


def test_conflicting_edge_is_checked_even_after_visit(graph):
    add, _ = graph
    add("sdk", ["base>=2", "base==1"])
    with pytest.raises(ValueError, match="^pilot_dependencies_unresolvable$"):
        pilot.runtime_dependencies()


def test_installed_closure_includes_runtime_not_development():
    for name in ("httpx", "openai", "anthropic"):
        pytest.importorskip(name)
    result = pilot.runtime_dependencies()
    assert {
        "draftbench",
        "packaging",
        "pydantic",
        "pydantic-core",
        "jsonschema",
        "jsonschema-specifications",
        "referencing",
        "httpx",
        "httpcore",
        "anyio",
        "openai",
        "anthropic",
    } <= result.keys()
    assert not {"pytest", "ruff", "inspect-ai", "hatchling"} & result.keys()
    assert result == pilot.runtime_dependencies()
