from pathlib import Path

from harness.config import parse_config


def test_prompt_mode_is_non_interactive():
    config = parse_config(["--prompt", "hello", "--working-dir", "."])

    assert config.non_interactive is True
    assert config.prompt == "hello"
    assert config.working_directory == Path.cwd()


def test_stdin_mode_accepts_json_output():
    config = parse_config(["--stdin", "--output", "json"])

    assert config.non_interactive is True
    assert config.output == "json"


def test_allow_tools_is_explicitly_opted_in():
    denied = parse_config(["--prompt", "hello"])
    allowed = parse_config(["--prompt", "hello", "--allow-tools"])

    assert denied.allow_tools is False
    assert allowed.allow_tools is True
