import subprocess
import sys
import textwrap

import pytest


def run_isolated(code: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_core_and_namespaces_import_without_loading_provider_dependencies():
    run_isolated("""
        import importlib.abc
        import sys

        blocked = {"slack_sdk", "aiohttp", "agents", "openai"}

        class BlockProviders(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split(".")[0] in blocked:
                    raise ModuleNotFoundError("Provider dependency blocked", name=fullname)

        sys.meta_path.insert(0, BlockProviders())

        import agentchat
        import agentchat.channels
        import agentchat.integrations
        from agentchat.channels.base import Channel
        from agentchat.context import MessageContext
        from agentchat.models import Conversation
        from agentchat.state import MemoryState

        assert agentchat.MessageContext is MessageContext
        assert agentchat.Conversation is Conversation
        assert agentchat.channels.Channel is Channel
        assert MemoryState().messages == {}
        assert "Slack" in dir(agentchat.channels)
        assert "should_reply" in dir(agentchat.integrations)
        assert not blocked.intersection(name.split(".")[0] for name in sys.modules)
        assert "agentchat.channels.slack" not in sys.modules
        assert "agentchat.integrations.openai_agents" not in sys.modules
    """)


@pytest.mark.parametrize("first_import", [
    "from agentchat.models import Conversation, MessageContext",
    "from agentchat.context import Conversation, MessageContext",
    "from agentchat import Conversation, MessageContext",
    "from agentchat.models import *",
])
def test_context_import_paths_keep_the_same_types(first_import):
    run_isolated(first_import + "\n" + textwrap.dedent("""
        import agentchat
        import agentchat.context
        import agentchat.models
        from agentchat.app import AgentChat

        assert Conversation is agentchat.Conversation is agentchat.context.Conversation
        assert MessageContext is agentchat.MessageContext is agentchat.context.MessageContext
        assert agentchat.models.MessageContext is MessageContext
        assert agentchat.models.Conversation is Conversation
        assert agentchat.AgentChat is AgentChat
        assert {"Conversation", "MessageContext"}.issubset(dir(agentchat.models))
    """))


def test_provider_and_integration_exports_keep_the_same_objects():
    from agentchat.channels import Slack, SlackUser
    from agentchat.channels.slack import Slack as SlackAdapter
    from agentchat.channels.slack import SlackUser as SlackProfile
    from agentchat.integrations import should_reply, to_openai_input
    from agentchat.integrations.openai_agents import should_reply as openai_should_reply
    from agentchat.integrations.openai_agents import to_openai_input as openai_input

    assert Slack is SlackAdapter
    assert SlackUser is SlackProfile
    assert should_reply is openai_should_reply
    assert to_openai_input is openai_input


def test_documented_slack_helper_imports_keep_the_same_callables():
    from agentchat.channels import slack_files, slack_media, slack_mirror
    from agentchat.channels.slack import events, files, mirroring, rendering

    assert slack_files.slack_attachments is events.slack_attachments
    assert slack_files.download_slack_file is files.download_slack_file
    assert slack_media.upload_slack_files is files.upload_slack_files
    assert slack_media.rich_payload is rendering.rich_payload
    assert slack_media.text_blocks is rendering.text_blocks
    assert slack_mirror.mirror_payload is mirroring.mirror_payload
    assert slack_mirror.read_mirror is mirroring.read_mirror


@pytest.mark.parametrize(("statement", "dependency", "installation"), [
    ("from agentchat.channels import Slack", "slack_sdk", "pip install agentchat"),
    ("from agentchat.channels import SlackUser", "aiohttp", "pip install agentchat"),
    ("from agentchat.integrations import should_reply", "agents", "agentchat[openai]"),
    ("from agentchat.integrations import to_openai_input", "agents", "agentchat[openai]"),
])
def test_requested_provider_reports_its_missing_dependency(statement, dependency, installation):
    run_isolated(f"""
        import importlib.abc
        import sys

        class MissingDependency(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == {dependency!r}:
                    raise ModuleNotFoundError("Dependency unavailable", name=fullname)

        sys.meta_path.insert(0, MissingDependency())

        try:
            {statement}
        except ModuleNotFoundError as exc:
            assert exc.name == {dependency!r}
            assert {installation!r} in str(exc)
        else:
            raise AssertionError("The requested provider dependency was not imported")
    """)


@pytest.mark.parametrize(("statement", "provider_module"), [
    ("from agentchat.channels import Slack", "agentchat.channels.slack"),
    ("from agentchat.integrations import should_reply", "agentchat.integrations.openai_agents"),
])
def test_provider_exports_preserve_unrelated_missing_dependencies(statement, provider_module):
    run_isolated(f"""
        import importlib.abc
        import sys

        class BrokenProvider(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == {provider_module!r}:
                    raise ModuleNotFoundError("Original failure", name="transitive_dependency")

        sys.meta_path.insert(0, BrokenProvider())

        try:
            {statement}
        except ModuleNotFoundError as exc:
            assert exc.name == "transitive_dependency"
            assert str(exc) == "Original failure"
        else:
            raise AssertionError("The provider was not imported")
    """)
