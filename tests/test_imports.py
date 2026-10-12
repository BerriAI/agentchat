import textwrap
from pathlib import Path

import pytest

from tests.support.imports import run_isolated


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
        assert "Slack" not in dir(agentchat.channels)
        assert "SlackUser" not in dir(agentchat.channels)
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


def test_slack_exports_live_in_the_slack_package():
    from agentchat.channels.slack import Slack, SlackUser
    from agentchat.channels.slack.adapter import Slack as SlackAdapter
    from agentchat.channels.slack.adapter import SlackUser as SlackProfile

    assert Slack is SlackAdapter
    assert SlackUser is SlackProfile


def test_integration_exports_keep_the_same_objects():
    from agentchat.integrations import should_reply, to_openai_input
    from agentchat.integrations.openai_agents import should_reply as openai_should_reply
    from agentchat.integrations.openai_agents import to_openai_input as openai_input

    assert should_reply is openai_should_reply
    assert to_openai_input is openai_input


def test_slack_helpers_are_importable_from_their_own_package():
    from agentchat.channels.slack.events import slack_attachments
    from agentchat.channels.slack.files import download_slack_file, upload_slack_files
    from agentchat.channels.slack.mirroring import mirror_payload, read_mirror
    from agentchat.channels.slack.rendering import rich_payload, text_blocks

    for helper in (
        slack_attachments, download_slack_file, upload_slack_files,
        mirror_payload, read_mirror, rich_payload, text_blocks,
    ):
        assert callable(helper)
        assert helper.__module__.startswith("agentchat.channels.slack.")


def test_channels_root_contains_only_shared_adapter_contracts():
    import agentchat.channels
    from agentchat.channels import base

    channel_directory = Path(agentchat.channels.__file__).parent
    files = {path.name for path in channel_directory.iterdir() if path.is_file()}
    allowed = {"__init__.py", "base.py"}
    assert files == allowed, (
        "Provider files belong in channels/<provider>/, not the shared channel root: "
        f"{sorted(files - allowed)}"
    )
    for package in channel_directory.iterdir():
        if package.is_dir() and package.name != "__pycache__":
            assert (package / "__init__.py").is_file(), f"Expected adapter package: {package.name}"
    assert all(
        getattr(agentchat.channels, name) is getattr(base, name)
        for name in agentchat.channels.__all__
    )


@pytest.mark.parametrize("legacy_module", ["slack_files", "slack_media", "slack_mirror"])
def test_removed_slack_helper_paths_require_explicit_migration(legacy_module):
    """Old paths deliberately break rather than leaving shims or dynamic aliases."""
    run_isolated(f"""
        import importlib

        module_name = "agentchat.channels.{legacy_module}"
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            assert exc.name == module_name
        else:
            raise AssertionError("Slack helpers must live inside the Slack package")
    """)


@pytest.mark.parametrize("legacy_export", ["Slack", "SlackUser"])
def test_channels_root_no_longer_exports_provider_types(legacy_export):
    run_isolated(f"""
        try:
            from agentchat.channels import {legacy_export}
        except ImportError:
            pass
        else:
            raise AssertionError("Import provider types from agentchat.channels.slack")
    """)


@pytest.mark.parametrize("dependency", ["slack_sdk", "aiohttp"])
def test_canonical_slack_import_reports_missing_dependencies(dependency):
    run_isolated(f"""
        import importlib.abc
        import sys

        class MissingDependency(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == {dependency!r}:
                    raise ModuleNotFoundError("Dependency unavailable", name=fullname)

        sys.meta_path.insert(0, MissingDependency())

        try:
            from agentchat.channels.slack import Slack
        except ModuleNotFoundError as exc:
            assert exc.name == {dependency!r}
        else:
            raise AssertionError("The requested provider dependency was not imported")
    """)


@pytest.mark.parametrize(("statement", "dependency", "installation"), [
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
    ("from agentchat.channels.slack import Slack", "agentchat.channels.slack"),
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
