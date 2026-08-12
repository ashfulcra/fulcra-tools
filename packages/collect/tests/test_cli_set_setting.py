"""`fulcra-collect set-setting` — the headless half of plugin configuration.

Every test here pins a failure whose symptom is SILENCE. A setting written
under a typo'd plugin id, a typo'd key, or the wrong type does not raise; it
lands in config.toml, the command prints success, and the plugin reads its
default forever. The operator's next signal is a plugin that quietly never
does the thing they configured — which is exactly how a misconfiguration
survives for weeks.

So the command validates against the plugin's DECLARED contract and refuses
anything it cannot prove will be read. These tests exist to keep that
refusal, because the natural "simplification" of this command is to drop the
registry lookup and just write the key.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
import tomlkit
from click.testing import CliRunner

from fulcra_collect import cli as cli_mod
from fulcra_collect import config as config_mod
from fulcra_collect.plugin import Credential, Plugin, Setting
from fulcra_collect.registry import RegistryResult


def _plugin() -> Plugin:
    """A stand-in shaped like PurpleAir: an enum mode, a text field, a
    toggle, a port, and a real keychain credential to guard against."""
    return Plugin(
        id="demo",
        name="Demo",
        kind="scheduled",
        collect_mode="live_polled",
        default_interval=timedelta(minutes=10),
        run=lambda ctx: None,
        required_credentials=(
            Credential(key="api_key", label="API key", help="", required=False),
        ),
        required_settings=(
            Setting(
                key="mode",
                label="Source",
                kind="enum",
                enum_values=("api", "local"),
            ),
            Setting(key="sensor_index", label="Sensor index", kind="text"),
            Setting(key="verbose", label="Verbose", kind="toggle"),
            Setting(key="web_port", label="Port", kind="port"),
        ),
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolate config.toml, stub the registry, and swallow the daemon reload
    (no daemon is running under test; set-interval already tolerates that)."""
    monkeypatch.setenv("FULCRA_COLLECT_HOME", str(tmp_path))
    monkeypatch.setattr(config_mod, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        cli_mod.registry, "discover", lambda: RegistryResult(plugins={"demo": _plugin()})
    )
    monkeypatch.setattr(
        cli_mod, "send_request", lambda *a, **k: (_ for _ in ()).throw(ConnectionError())
    )
    return tmp_path


def run(*args):
    return CliRunner().invoke(cli_mod.cli, ["set-setting", *args])


def settings(tmp_path) -> dict:
    doc = tomlkit.parse((tmp_path / "config.toml").read_text(encoding="utf-8"))
    return dict(doc.get("plugin_settings", {}).get("demo", {}))


class TestItActuallyWrites:
    def test_writes_a_declared_setting(self, env):
        res = run("demo", "sensor_index", "142559")
        assert res.exit_code == 0, res.output
        assert settings(env)["sensor_index"] == "142559"

    def test_reads_the_value_back_in_its_own_output(self, env):
        # The command prints what it stored, not what it was asked to store.
        # Those differ whenever coercion happens, and the difference is the
        # only thing that tells an operator `verbose true` became a boolean.
        res = run("demo", "verbose", "yes")
        assert "True" in res.output

    def test_preserves_sibling_settings(self, env):
        run("demo", "mode", "api")
        run("demo", "sensor_index", "142559")
        assert settings(env) == {"mode": "api", "sensor_index": "142559"}

    def test_overwrites_rather_than_duplicating(self, env):
        run("demo", "mode", "api")
        run("demo", "mode", "local")
        assert settings(env)["mode"] == "local"


class TestItRefusesWhatWouldNeverBeRead:
    def test_unknown_plugin_id(self, env):
        # A typo'd plugin id is the worst case: the write succeeds, nothing
        # ever reads it, and `set-setting` reported success.
        res = run("dmeo", "mode", "api")
        assert res.exit_code != 0
        assert "dmeo" in res.output

    def test_unknown_setting_key(self, env):
        res = run("demo", "snesor_index", "142559")
        assert res.exit_code != 0
        assert "sensor_index" in res.output  # names the valid keys

    def test_unknown_key_writes_nothing_at_all(self, env):
        # Refusing in the output but writing anyway would be worse than not
        # validating, because the file would disagree with the exit code.
        run("demo", "snesor_index", "142559")
        assert not (env / "config.toml").exists() or settings(env) == {}

    def test_enum_value_outside_the_declared_set(self, env):
        # `mode=cloud` is the plausible wrong guess (the docs say "cloud API").
        # Without this check it is accepted here and raises inside the worker
        # on the next poll — hours later, in a log nobody is reading.
        res = run("demo", "mode", "cloud")
        assert res.exit_code != 0
        assert "api" in res.output and "local" in res.output

    def test_port_that_is_not_a_number(self, env):
        res = run("demo", "web_port", "eighty")
        assert res.exit_code != 0

    def test_toggle_that_is_not_a_boolean(self, env):
        res = run("demo", "verbose", "sometimes")
        assert res.exit_code != 0


class TestItRefusesToPutSecretsInAPlaintextFile:
    """The sibling failure, and the reason this command is riskier than it
    looks. The purpleair handoff is worded "get the API key into the plugin
    keychain slot, wire mode + sensor_index" — three values in one sentence,
    two of which belong here and one of which must never touch config.toml.
    An operator working down that sentence will try it."""

    def test_a_declared_credential_key_is_refused(self, env):
        res = run("demo", "api_key", "SECRET-VALUE")
        assert res.exit_code != 0

    def test_and_points_at_the_command_that_is_correct(self, env):
        res = run("demo", "api_key", "SECRET-VALUE")
        assert "set-credential" in res.output

    def test_and_the_secret_does_not_reach_the_file(self, env):
        run("demo", "api_key", "SECRET-VALUE")
        blob = (
            (env / "config.toml").read_text(encoding="utf-8")
            if (env / "config.toml").exists()
            else ""
        )
        assert "SECRET-VALUE" not in blob

    def test_nor_is_it_echoed_back_to_the_terminal(self, env):
        # Refusing but printing the value would still leak it into shell
        # history, CI logs, and any transcript of the session.
        res = run("demo", "api_key", "SECRET-VALUE")
        assert "SECRET-VALUE" not in res.output

    def test_refused_even_when_also_declared_as_a_plain_setting(self, monkeypatch, env):
        # THE CASE THAT ACTUALLY NEEDS THIS GUARD, and the one the other
        # three do not reach. Above, `api_key` is a Credential and NOT a
        # Setting, so the unknown-key check refuses it and the secret check
        # never has to fire — deleting the secret check leaves those three
        # green. A plugin that declares the same key BOTH ways (a wizard
        # field mirroring a keychain slot) sails straight past the key check
        # and writes the secret to config.toml. Only this test fails then.
        clashing = Plugin(
            id="clash",
            name="Clash",
            kind="scheduled",
            collect_mode="live_polled",
            default_interval=timedelta(minutes=10),
            run=lambda ctx: None,
            required_credentials=(
                Credential(key="api_key", label="API key", help=""),
            ),
            required_settings=(Setting(key="api_key", label="API key", kind="text"),),
        )
        monkeypatch.setattr(
            cli_mod.registry,
            "discover",
            lambda: RegistryResult(plugins={"clash": clashing}),
        )
        res = CliRunner().invoke(
            cli_mod.cli, ["set-setting", "clash", "api_key", "SECRET-VALUE"]
        )
        assert res.exit_code != 0
        assert "set-credential" in res.output
        assert "SECRET-VALUE" not in res.output
        blob = (
            (env / "config.toml").read_text(encoding="utf-8")
            if (env / "config.toml").exists()
            else ""
        )
        assert "SECRET-VALUE" not in blob


class TestSecretKindSettingsAreNotEchoed:
    """`secret`-kind Settings are ALLOWED in config.toml by the contract, so
    they pass every refusal above. The remaining exposure is the confirmation
    line: echoing the stored value copies it into scrollback, shell history
    and CI logs — the leak the refusal exists to prevent, through the door
    that is legitimately open."""

    @pytest.fixture
    def secret_env(self, monkeypatch, env):
        plugin = Plugin(
            id="sec",
            name="Sec",
            kind="scheduled",
            collect_mode="live_polled",
            default_interval=timedelta(minutes=10),
            run=lambda ctx: None,
            required_settings=(Setting(key="token", label="Token", kind="secret"),),
        )
        monkeypatch.setattr(
            cli_mod.registry, "discover", lambda: RegistryResult(plugins={"sec": plugin})
        )
        return env

    def test_it_is_stored(self, secret_env):
        res = CliRunner().invoke(cli_mod.cli, ["set-setting", "sec", "token", "s3cr3t"])
        assert res.exit_code == 0, res.output
        doc = tomlkit.parse((secret_env / "config.toml").read_text(encoding="utf-8"))
        assert doc["plugin_settings"]["sec"]["token"] == "s3cr3t"

    def test_but_never_printed(self, secret_env):
        res = CliRunner().invoke(cli_mod.cli, ["set-setting", "sec", "token", "s3cr3t"])
        assert "s3cr3t" not in res.output
        assert "<set>" in res.output


class TestCoercion:
    @pytest.mark.parametrize("raw", ["true", "True", "yes", "on", "1"])
    def test_truthy_spellings(self, env, raw):
        run("demo", "verbose", raw)
        assert settings(env)["verbose"] is True

    @pytest.mark.parametrize("raw", ["false", "False", "no", "off", "0"])
    def test_falsy_spellings(self, env, raw):
        run("demo", "verbose", raw)
        assert settings(env)["verbose"] is False

    def test_port_is_stored_as_an_int_not_a_string(self, env):
        # tomlkit will happily write "9292"; a plugin doing int(port) survives
        # that, one doing port == 9292 does not. Store the type the contract
        # declares.
        run("demo", "web_port", "9292")
        assert settings(env)["web_port"] == 9292
        assert not isinstance(settings(env)["web_port"], str)
