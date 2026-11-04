"""GitLab configuration fails closed without exposing input values."""

import os
import re
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

from argus.forge.keys import canonical_host
from argus.gitlab import config


DEFAULTS = (
    ("base_url", "ARGUS_GITLAB_URL", None),
    ("token", "ARGUS_GITLAB_TOKEN", ""),
    ("ca_bundle", "ARGUS_GITLAB_CA_BUNDLE", None),
    ("allow", "ARGUS_GITLAB_ALLOW", ()),
    ("publish_mode", "ARGUS_GITLAB_PUBLISH", "0"),
    ("publish_projects", "ARGUS_GITLAB_PUBLISH_PROJECTS", ()),
    ("notes_per_minute", "ARGUS_GITLAB_NOTES_PER_MINUTE", 30),
    ("trusted_level", "ARGUS_GITLAB_TRUSTED_LEVEL", 30),
    ("watch_enabled", "ARGUS_GITLAB_WATCH", False),
    ("reconcile_ticks", "ARGUS_GITLAB_RECONCILE_TICKS", 10),
    ("reply_loop", "ARGUS_GITLAB_REPLY_LOOP", False),
    ("timeout_s", "ARGUS_GITLAB_TIMEOUT_S", 30.0),
    ("max_response_bytes", "ARGUS_GITLAB_MAX_RESPONSE_BYTES", 10485760),
)


@pytest.fixture(autouse=True)
def clear_gitlab_environment(monkeypatch):
    for name in list(os.environ):
        if name.startswith("ARGUS_GITLAB_"):
            monkeypatch.delenv(name)


@pytest.mark.parametrize("value", [None, "", " \t\n"])
def test_all_readers_return_off_defaults(monkeypatch, value):
    for reader, variable, expected in DEFAULTS:
        if value is not None:
            monkeypatch.setenv(variable, value)
        result = getattr(config, reader)()
        assert result == expected
        assert type(result) is type(expected)


@pytest.mark.parametrize(("value", "host", "port", "prefix", "origin"), [
    ("https://GitLab.Example.com", "gitlab.example.com", None, "", "https://gitlab.example.com"),
    ("https://GitLab.Example.com:443/", "gitlab.example.com", None, "", "https://gitlab.example.com"),
    ("https://gitlab.test:8443", "gitlab.test", 8443, "", "https://gitlab.test:8443"),
    ("https://gitlab.test/", "gitlab.test", None, "", "https://gitlab.test"),
    ("https://gitlab.test:8443/git/lab/", "gitlab.test", 8443, "/git/lab", "https://gitlab.test:8443"),
    ("https://gitlab.test/Git_Lab/~v1.2-3/", "gitlab.test", None, "/Git_Lab/~v1.2-3", "https://gitlab.test"),
    ("https://[2001:DB8::1]/gitlab", "2001:db8::1", None, "/gitlab", "https://[2001:db8::1]"),
    ("https://[::1]:443/", "::1", None, "", "https://[::1]"),
    ("https://[::1]:8443/", "::1", 8443, "", "https://[::1]:8443"),
    ("https://192.0.2.1", "192.0.2.1", None, "", "https://192.0.2.1"),
    ("HtTpS://GitLab.test", "gitlab.test", None, "", "https://gitlab.test"),
    ("https://gitlab.test:65535/prefix", "gitlab.test", 65535, "/prefix", "https://gitlab.test:65535"),
])
def test_base_url_normalizes_valid_urls(monkeypatch, value, host, port, prefix, origin):
    monkeypatch.setenv("ARGUS_GITLAB_URL", value)
    base = config.base_url()
    assert base == config.GitLabBase("https", host, port, prefix)
    assert base.origin == origin
    assert base.url == origin + prefix
    with pytest.raises(FrozenInstanceError):
        base.host = "changed.test"


@pytest.mark.parametrize("value", [
    "http://gitlab.test", "ftp://gitlab.test", "gitlab.test", "//gitlab.test",
    "https://user@gitlab.test", "https://user:s3cretpw@gitlab.test", "https://@gitlab.test",
    "https://:s3cretpw@gitlab.test", "https://user:@gitlab.test",
    "https://gitlab.test?x=1", "https://gitlab.test?", "https://gitlab.test#x", "https://gitlab.test#",
    "https://", "https:///path", "https://:443", "https://a..b", "https://gitlab.test.", "https://.gitlab.test",
    "https://gitlab.test:0", "https://gitlab.test:99999", "https://gitlab.test:abc", "https://gitlab.test:",
    "https://gitlab.test:-1", "https://gitlab.test:+443", "https://gitlab.test:４４３",
    "https://gitlab.test/..", "https://gitlab.test/.", "https://gitlab.test//", "https://gitlab.test/a//b",
    "https://gitlab.test/%2e%2e", "https://git%6cab.test", "https://gitlab.test\\path",
    " https://gitlab.test", "https://gitlab.test ", "https://gitlab.test/a b", "https://gitlab.test/\tpath",
    "https://gitlab.test/\npath", "https://gitlab.test/\x7fpath",
    "https://gitlab.test/path;param", "https://gítlab.test", "https://git_lab.test", "https://gitlab.test/a:b",
    "https://[bad]", "https://[::1", "https://[::1]extra", "https://[::1]:", "https://[::1]:0",
    "https://-gitlab.test", "https://gitlab-.test", "https://" + "a" * 64 + ".test",
    "https://" + ".".join(["a" * 60] * 5),
    "https://[v1.fake]", "https://2001:db8::1", "https://[fe80::1%25eth0]", "https://gitlab.test/p[ath",
])
def test_base_url_refuses_unsafe_urls_without_echoing_input(monkeypatch, value):
    for raw in (value, value + "/s3cretpw"):
        monkeypatch.setenv("ARGUS_GITLAB_URL", raw)
        with pytest.raises(config.GitLabConfigError) as error:
            config.base_url()
        message = str(error.value)
        assert "ARGUS_GITLAB_URL:" in message
        assert raw not in message
        assert "s3cretpw" not in message


def test_readers_observe_environment_changes_at_call_time(monkeypatch):
    for value, expected in [("https://first.test/one", "https://first.test/one"),
                            ("https://SECOND.test/two/", "https://second.test/two")]:
        monkeypatch.setenv("ARGUS_GITLAB_URL", value)
        assert config.base_url().url == expected
    monkeypatch.setenv("ARGUS_GITLAB_TOKEN", "  synthetic\ncredential  ")
    assert config.token() == "synthetic\ncredential"
    monkeypatch.setenv("ARGUS_GITLAB_TOKEN", "replacement")
    assert config.token() == "replacement"


def test_ca_bundle_returns_a_path_without_opening_it(monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_CA_BUNDLE", " /does-not-exist/gitlab-ca.pem ")
    assert config.ca_bundle() == "/does-not-exist/gitlab-ca.pem"


@pytest.mark.parametrize("reader,variable", [("allow", "ARGUS_GITLAB_ALLOW"),
                                             ("publish_projects", "ARGUS_GITLAB_PUBLISH_PROJECTS")])
def test_project_lists_preserve_order_and_remove_blank_duplicates(monkeypatch, reader, variable):
    monkeypatch.setenv(variable, " group/one, ,group/two,group/one, group/One, ")
    assert getattr(config, reader)() == ("group/one", "group/two", "group/One")
    monkeypatch.setenv(variable, "group/three")
    assert getattr(config, reader)() == ("group/three",)


def test_allow_accepts_patterns(monkeypatch):
    monkeypatch.setenv("ARGUS_GITLAB_ALLOW", "group/*, other/?, team/[ab]")
    assert config.allow() == ("group/*", "other/?", "team/[ab]")


@pytest.mark.parametrize("value,expected", [("0", "0"), ("dry-run", "dry-run"), ("1", "1"),
                                           ("DRY-RUN", "dry-run"), ("  Dry-Run ", "dry-run")])
def test_publish_mode(monkeypatch, value, expected):
    monkeypatch.setenv("ARGUS_GITLAB_PUBLISH", value)
    assert config.publish_mode() == expected


@pytest.mark.parametrize("reader,variable", [("watch_enabled", "ARGUS_GITLAB_WATCH"),
                                             ("reply_loop", "ARGUS_GITLAB_REPLY_LOOP")])
@pytest.mark.parametrize("value,expected", [("1", True), ("true", True), (" YES ", True), ("On", True),
                                           ("0", False), ("false", False), (" NO ", False), ("Off", False)])
def test_boolean_readers(monkeypatch, reader, variable, value, expected):
    monkeypatch.setenv(variable, value)
    assert getattr(config, reader)() is expected
    monkeypatch.setenv(variable, "0" if expected else "1")
    assert getattr(config, reader)() is not expected


@pytest.mark.parametrize("reader,variable,minimum", [
    ("notes_per_minute", "ARGUS_GITLAB_NOTES_PER_MINUTE", 1),
    ("reconcile_ticks", "ARGUS_GITLAB_RECONCILE_TICKS", 1),
    ("max_response_bytes", "ARGUS_GITLAB_MAX_RESPONSE_BYTES", 1024),
])
def test_integer_boundaries(monkeypatch, reader, variable, minimum):
    monkeypatch.setenv(variable, f"  +{minimum}  ")
    assert getattr(config, reader)() == minimum
    monkeypatch.setenv(variable, str(minimum + 1))
    assert getattr(config, reader)() == minimum + 1
    for invalid in [str(minimum - 1), "-1", "True", "1.0", "1_024", "١٠٢٤", "１２３４", "9" * 5000]:
        monkeypatch.setenv(variable, invalid)
        with pytest.raises(config.GitLabConfigError) as error:
            getattr(config, reader)()
        assert variable in str(error.value)
        assert invalid not in str(error.value)


@pytest.mark.parametrize("level", [0, 5, 10, 15, 20, 30, 40, 50])
def test_trusted_levels(monkeypatch, level):
    monkeypatch.setenv("ARGUS_GITLAB_TRUSTED_LEVEL", f" {level} ")
    assert config.trusted_level() == level


@pytest.mark.parametrize("value,expected", [(" 0.01 ", 0.01), ("1e2", 100.0), ("30", 30.0)])
def test_positive_finite_timeout(monkeypatch, value, expected):
    monkeypatch.setenv("ARGUS_GITLAB_TIMEOUT_S", value)
    assert config.timeout_s() == expected


@pytest.mark.parametrize("reader,variable,values", [
    ("ca_bundle", "ARGUS_GITLAB_CA_BUNDLE", ["s3cretpw\nfile", "s3cretpw\rfile", "\ns3cretpw"]),
    ("publish_mode", "ARGUS_GITLAB_PUBLISH", ["s3cretpw", "2", "true"]),
    ("publish_projects", "ARGUS_GITLAB_PUBLISH_PROJECTS", ["s3cretpw/*", "s3cretpw/?", "s3cretpw/[ab]", "s3cretpw/a b", "s3cretpw/a\tb", "s3cretpw/a\u00a0b"]),
    ("notes_per_minute", "ARGUS_GITLAB_NOTES_PER_MINUTE", ["s3cretpw"]),
    ("trusted_level", "ARGUS_GITLAB_TRUSTED_LEVEL", ["s3cretpw", "-1", "1", "35", "51", "True", "30.0", "３０"]),
    ("watch_enabled", "ARGUS_GITLAB_WATCH", ["s3cretpw", "2"]),
    ("reconcile_ticks", "ARGUS_GITLAB_RECONCILE_TICKS", ["s3cretpw"]),
    ("reply_loop", "ARGUS_GITLAB_REPLY_LOOP", ["s3cretpw", "2"]),
    ("timeout_s", "ARGUS_GITLAB_TIMEOUT_S", ["s3cretpw", "0", "-0.1", "nan", "inf", "-inf", "1e9999"]),
    ("max_response_bytes", "ARGUS_GITLAB_MAX_RESPONSE_BYTES", ["s3cretpw"]),
])
def test_reader_errors_name_variable_without_echoing_secrets(monkeypatch, reader, variable, values):
    credential = "synthetic secret must stay private"
    monkeypatch.setenv("ARGUS_GITLAB_TOKEN", credential)
    for invalid in values:
        monkeypatch.setenv(variable, invalid)
        with pytest.raises(config.GitLabConfigError) as error:
            getattr(config, reader)()
        message = str(error.value)
        assert message.startswith(variable + ":")
        if not invalid.isdecimal():
            assert invalid not in message
        assert "s3cretpw" not in message
        assert credential not in message


@pytest.mark.parametrize("reader,variable,value", [
    ("base_url", "ARGUS_GITLAB_URL", "https://gitlab.test/s3cretpw\x00path"),
    ("ca_bundle", "ARGUS_GITLAB_CA_BUNDLE", "s3cretpw\x00file"),
])
def test_nul_values_are_refused_without_echoing_input(monkeypatch, reader, variable, value):
    # Native environment assignment rejects NUL before the reader can see it.
    monkeypatch.setattr(config.os, "environ", {variable: value})
    with pytest.raises(config.GitLabConfigError) as error:
        getattr(config, reader)()
    assert str(error.value).startswith(variable + ":")
    assert "s3cretpw" not in str(error.value)


@pytest.mark.parametrize("host,port,default,expected", [
    (" GitLab.Example.com ", None, 443, "gitlab.example.com"),
    ("GitLab.Example.com", 443, 443, "gitlab.example.com"),
    ("GitLab.Example.com", 8443, 443, "gitlab.example.com:8443"),
    ("GitLab.Example.com", 80, 80, "gitlab.example.com"),
    ("2001:DB8::1", None, 443, "[2001:db8::1]"),
    ("[2001:DB8::1]", 443, 443, "[2001:db8::1]"),
    ("::1", 8443, 443, "[::1]:8443"),
])
def test_canonical_host(host, port, default, expected):
    assert canonical_host(host, port, default) == expected


def test_gitlab_settings_are_consistent_across_config_surfaces():
    root = Path(__file__).resolve().parents[1]
    source = (root / "src/argus/gitlab/config.py").read_text()
    names = set(re.findall(r"ARGUS_GITLAB_[A-Z_]+", source))
    services = yaml.safe_load((root / "docker-compose.yml").read_text())["services"]
    environment = services["argus-api"]["environment"]
    compose_names = {name for name in environment if name.startswith("ARGUS_GITLAB_")}
    assert names
    assert names == compose_names
    example = (root / ".env.example").read_text()
    for name in names:
        assert re.search(rf"^(?:# )?{name}=$", example, re.MULTILINE)
    assert environment["ARGUS_GITLAB_REPLY_LOOP"] == "${ARGUS_GITLAB_REPLY_LOOP:-0}"
    assert re.search(r"^(?:# )?ARGUS_GITLAB_TOKEN=$", example, re.MULTILINE)
    for service, definition in services.items():
        if service != "argus-api":
            assert not any(name.startswith("ARGUS_GITLAB_") for name in definition.get("environment", {}))
