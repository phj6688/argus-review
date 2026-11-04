import json
import pickle
from pathlib import Path
import subprocess
import sys

import pytest

import argus.forge as forge
from argus.forge import ForgeAuthError, ForgeDenied, ForgeError, ForgeRateLimited, forge_for
from argus.github import GitHubAuthError, GitHubDenied, GitHubRateLimited, GraphQLRateLimited


@pytest.mark.parametrize(
    "error_type,neutral_type,args",
    [
        (GitHubAuthError, ForgeAuthError, ("bad credentials",)),
        (GitHubDenied, ForgeDenied, ("denied",)),
        (GitHubRateLimited, ForgeRateLimited, (5.0,)),
        (GraphQLRateLimited, ForgeRateLimited, (5.0,)),
    ],
)
def test_github_errors_keep_runtime_error_compatibility(error_type, neutral_type, args):
    error = error_type(*args)

    assert isinstance(error, neutral_type)
    assert isinstance(error, ForgeError)
    assert isinstance(error, RuntimeError)
    assert error.forge == "github"


@pytest.mark.parametrize("error_type", [GitHubRateLimited, GraphQLRateLimited])
def test_github_rate_limit_message_and_args_are_unchanged(error_type):
    error = error_type(5.0)

    assert error.reset_at == 5.0
    assert str(error) == "GitHub rate limited until 5.0"
    assert error.args == ("GitHub rate limited until 5.0",)


@pytest.mark.parametrize("error_type", [ForgeError, ForgeAuthError, ForgeDenied])
def test_neutral_errors_have_no_default_forge(error_type):
    error = error_type("failure")

    assert isinstance(error, ForgeError)
    assert error_type.forge == ""
    assert error.forge == ""
    assert error.args == ("failure",)


@pytest.mark.parametrize("forge_name,label", [("", "forge"), ("gitlab", "gitlab")])
def test_neutral_rate_limit_preserves_reset_and_forge(forge_name, label):
    error = ForgeRateLimited(3.0, forge_name)

    assert isinstance(error, ForgeError)
    assert error.reset_at == 3.0
    assert error.forge == forge_name
    assert str(error) == f"{label} rate limited until 3.0"
    assert error.args == (f"{label} rate limited until 3.0",)


def test_neutral_rate_limit_accepts_an_unknown_forge():
    error = ForgeRateLimited(3.0)

    assert error.forge == ""
    assert str(error) == "forge rate limited until 3.0"


@pytest.mark.parametrize(
    "error",
    [
        GitHubAuthError("bad credentials"),
        GitHubDenied("denied"),
        GitHubRateLimited(5.0),
        GraphQLRateLimited(5.0),
        ForgeRateLimited(3.0, "gitlab"),
        ForgeRateLimited(3.0),
    ],
    ids=["github-auth", "github-denied", "github-rate", "graphql-rate", "gitlab-rate", "unknown-rate"],
)
@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_errors_pickle_round_trip(error, protocol):
    restored = pickle.loads(pickle.dumps(error, protocol=protocol))

    assert type(restored) is type(error)
    assert restored.args == error.args
    assert str(restored) == str(error)
    assert restored.forge == error.forge
    if isinstance(error, ForgeRateLimited):
        assert restored.reset_at == error.reset_at


def test_forge_import_does_not_load_provider_or_application_modules():
    src_path = Path(__file__).resolve().parents[1] / "src"
    code = (
        "import json, sys; "
        f"sys.path.insert(0, {str(src_path)!r}); "
        "import argus.forge; "
        "print(json.dumps(sorted(name for name in sys.modules if name.startswith('argus'))))"
    )

    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )

    modules = json.loads(result.stdout)
    assert "argus.forge" in modules
    assert "argus.forge.types" in modules
    assert "argus.forge.errors" in modules
    assert set(modules).isdisjoint({"argus.api", "argus.review", "argus.publish", "argus.github"})


def test_forge_exports_the_public_contract():
    expected = {
        "RepoRef", "ChangeRef", "DiffRefs", "ChangeMeta", "Comparison", "PostedComment",
        "Note", "Thread", "RepoRecord", "ChangeListing", "ForgeError", "ForgeAuthError",
        "ForgeDenied", "ForgeRateLimited", "forge_for",
    }

    assert set(forge.__all__) == expected
    assert all(hasattr(forge, name) for name in expected)


def test_forge_factory_is_deferred():
    with pytest.raises(NotImplementedError) as raised:
        forge_for("owner/name")

    assert str(raised.value) == "forge_for is wired in a later change"
