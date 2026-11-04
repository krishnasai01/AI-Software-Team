import stat

import pytest

from onboarding.console import Quit, ScriptedConsole
from onboarding.secrets import SecretStore
from onboarding.shell import Result, Shell

# Shell


async def test_local_commands_run_and_report_what_happened():
    sh = Shell("local")
    ok = await sh.run(["echo", "hi"])
    assert ok.ok and ok.out == "hi\n"
    failed = await sh.run(["sh", "-c", "echo oops >&2; exit 3"])
    assert failed.code == 3 and failed.problem() == "oops"
    assert (await sh.run(["cat"], stdin="piped")).out == "piped"
    assert (await sh.run(["no-such-command-here"])).code == 127


async def test_a_command_that_hangs_is_killed():
    result = await Shell("local").run(["sleep", "5"], timeout=0.2)
    assert result.code == 124 and "timed out" in result.err


async def test_output_can_be_streamed_and_large_input_does_not_deadlock():
    lines: list[str] = []
    await Shell("local").run(["printf", "a\\nb\\n"], on_line=lines.append)
    assert lines == ["a", "b"]
    # One 1 MB line in and out: no deadlock, and no failure on a line longer than asyncio's 64 KiB limit.
    result = await Shell("local").run(["cat"], stdin="x" * 1_000_000)
    assert result.ok and len(result.out) == 20_000  # only the tail is kept


async def test_only_the_end_of_long_output_is_kept():
    result = await Shell("local").run(["sh", "-c", "seq 1 100000"])
    assert result.out.rstrip().endswith("100000") and len(result.out) <= 20_000


def test_remote_commands_are_quoted_for_the_remote_shell():
    command = Shell("deploy@10.0.0.2").command(["echo", "a b; rm -rf /"])
    assert command[:3] == ["ssh", "-o", "BatchMode=yes"]
    assert command[-2:] == ["--", "echo 'a b; rm -rf /'"]  # one argument, quoted: nothing is run by accident
    assert Shell("local").command(["ls"]) == ["ls"]


@pytest.mark.parametrize("host", ["-oProxyCommand=evil", "a b", "a;b", "host$(x)", "", "@host"])
def test_a_host_that_could_be_an_ssh_option_or_a_command_is_refused(host):
    with pytest.raises(ValueError):
        Shell(host)


@pytest.mark.parametrize("host", ["local", "10.0.0.2", "deploy@10.0.0.2", "deploy@worker-1.example.com"])
def test_ordinary_hosts_are_accepted(host):
    assert Shell(host).host == host


def test_a_problem_is_one_line():
    assert Result(1, "", "first\nlast line").problem() == "last line"
    assert Result(2).problem() == "exit code 2"


# Secrets


def test_persisted_secrets_live_in_a_private_file_and_ephemeral_ones_only_in_memory(tmp_path):
    path = tmp_path / "state" / "secrets.json"
    store = SecretStore(path)
    store.set("kept", "kept-value-123")
    store.set("token", "token-value-456", persist=False)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "token-value-456" not in path.read_text()
    reread = SecretStore(path)
    assert reread.get("kept") == "kept-value-123" and reread.get("token") is None
    assert store.get("token") == "token-value-456"
    assert store.names() == ["kept", "token"]


def test_redaction_scrubs_every_known_value_and_leaves_short_ones_alone():
    store = SecretStore(None)
    store.set("api", "sk-ant-abcdef123456")
    store.set("short", "yes")
    text = "key=sk-ant-abcdef123456 and also sk-ant-abcdef123456; they said yes"
    assert store.redact(text) == "key=<hidden:api> and also <hidden:api>; they said yes"


def test_a_longer_secret_is_scrubbed_before_a_shorter_one_inside_it():
    store = SecretStore(None)
    store.set("short", "abcdef")
    store.set("long", "abcdef-and-more")
    assert store.redact("x abcdef-and-more y") == "x <hidden:long> y"


def test_generated_secrets_are_random_and_wipe_removes_the_file(tmp_path):
    path = tmp_path / "secrets.json"
    store = SecretStore(path)
    first, second = store.generate("a"), store.generate("b")
    assert first != second and len(first) >= 32
    store.wipe()
    assert not path.exists() and store.get("a") is None


# Console


async def test_confirm_defaults_and_rejects_nonsense():
    console = ScriptedConsole(["", "maybe", "Y", "n", ""])
    assert await console.confirm("ok?") is False  # Enter takes the default, which is no
    assert await console.confirm("ok?") is True  # "maybe" is asked again, then Y
    assert await console.confirm("ok?", default=True) is False
    assert await console.confirm("ok?", default=True) is True
    assert "Please answer y or n." in console.text


async def test_the_scripted_console_quits_when_the_answers_run_out_and_records_prompts():
    console = ScriptedConsole(["a", lambda: "b"])
    assert await console.ask("one") == "a" and await console.ask("two", secret=True) == "b"
    assert console.prompts == [("one", False), ("two", True)]
    with pytest.raises(Quit):
        await console.ask("three")
