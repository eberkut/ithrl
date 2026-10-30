"""Credential-free checks of live gates and sanitized source generation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from native_support.smoke import (
    FINAL_PROMPT,
    PROFILES,
    RESPONSE,
    SHELL_PROMPT,
    Profile,
    SmokeOptions,
    protected_tree,
    synthetic_case,
)


@pytest.mark.parametrize("profile", PROFILES)
def test_live_case_is_synthetic_and_selects_the_exact_target(
    tmp_path: Path, profile: Profile
) -> None:
    options = SmokeOptions(
        "preflight",
        profile,
        "fixture-model",
        "fixture-provider" if profile == "codex-final" else "github-copilot",
        tmp_path,
    )
    options.validate(ci=False)
    source, case, original = synthetic_case(tmp_path, options)
    assert not tuple(original.iterdir())
    assert case.load().manifest.harness == ("codex" if profile == "codex-final" else "copilot")
    assert case.load().manifest.model_requested == "fixture-model"
    text = source.read_text()
    assert "Private direct trigger" not in text
    assert "fixture-command" not in text
    assert "fixture-auth" not in text
    if profile == "copilot-shell":
        assert SHELL_PROMPT in text
        assert '"command":"pwd"' in text
    else:
        assert FINAL_PROMPT in text
        assert RESPONSE in text
    assert all(isinstance(json.loads(line), dict) for line in text.splitlines())


@pytest.mark.parametrize("mode", ["preflight", "run"])
def test_live_modes_refuse_ci_before_any_probe(tmp_path: Path, mode: str) -> None:
    options = SmokeOptions(
        "run" if mode == "run" else "preflight",
        "codex-final",
        "fixture-model",
        "fixture-provider",
        tmp_path,
        "a" * 64,
    )
    with pytest.raises(ValueError, match="forbidden in CI"):
        options.validate(ci=True)


@pytest.mark.parametrize("digest", [None, "", "wrong", "f" * 63, "G" * 64])
def test_provider_mode_requires_a_separately_approved_exact_digest(
    tmp_path: Path, digest: str | None
) -> None:
    options = SmokeOptions(
        "run", "codex-final", "fixture-model", "fixture-provider", tmp_path, digest
    )
    with pytest.raises(ValueError, match="approved capability digest"):
        options.validate(ci=False)
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("model", "provider"),
    [
        ("", "openai"),
        ("fixture-model", ""),
        ("unsafe model", "openai"),
        ("fixture-model", "unsafe provider"),
    ],
)
def test_live_modes_do_not_infer_model_or_provider(
    tmp_path: Path, model: str, provider: str
) -> None:
    options = SmokeOptions("preflight", "codex-final", model, provider, tmp_path)
    with pytest.raises(ValueError, match="explicit bounded"):
        options.validate(ci=False)


def test_live_modes_refuse_symlink_evidence_parent(tmp_path: Path) -> None:
    parent = tmp_path / "alias"
    parent.symlink_to(tmp_path, target_is_directory=True)
    options = SmokeOptions("preflight", "codex-final", "fixture-model", "fixture-provider", parent)
    with pytest.raises(ValueError, match="symlink"):
        options.validate(ci=False)


def test_copilot_cannot_use_an_inferred_or_custom_destination(tmp_path: Path) -> None:
    options = SmokeOptions("preflight", "copilot-final", "fixture-model", "openai", tmp_path)
    with pytest.raises(ValueError, match="github-copilot"):
        options.validate(ci=False)


def test_budget_is_fixed_not_derived_from_user_options(tmp_path: Path) -> None:
    for profile in PROFILES:
        options = SmokeOptions("preflight", profile, "fixture-model", "fixture-provider", tmp_path)
        assert options.provider_call_bound == (
            32 if profile == "codex-final" else (4 if profile == "copilot-shell" else 2)
        )


@pytest.mark.parametrize("nested", [False, True])
def test_live_evidence_cannot_be_created_inside_the_checkout(nested: bool) -> None:
    checkout = Path(__file__).resolve().parents[2]
    root = checkout / "tests" if nested else checkout
    options = SmokeOptions("preflight", "codex-final", "fixture-model", "fixture-provider", root)
    with pytest.raises(ValueError, match="outside the checkout"):
        options.validate(ci=False)


@pytest.mark.parametrize("allow_case_results", [False, True])
def test_protected_tree_never_excludes_nested_results(
    tmp_path: Path, allow_case_results: bool
) -> None:
    nested = tmp_path / "source" / "results"
    nested.mkdir(parents=True)
    marker = nested / "protected.txt"
    marker.write_text("before")
    before = protected_tree(tmp_path, allow_case_results=allow_case_results)
    marker.write_text("after")
    assert protected_tree(tmp_path, allow_case_results=allow_case_results) != before


@pytest.mark.parametrize("allow_case_results", [False, True])
def test_only_case_results_are_exempted_from_immutability(
    tmp_path: Path, allow_case_results: bool
) -> None:
    before = protected_tree(tmp_path, allow_case_results=allow_case_results)
    results = tmp_path / "results"
    results.mkdir()
    (results / "report.json").write_text("{}")
    assert (protected_tree(tmp_path, allow_case_results=allow_case_results) == before) is (
        allow_case_results
    )
