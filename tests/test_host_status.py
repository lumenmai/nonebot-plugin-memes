from types import SimpleNamespace

import pytest

from nonebot_plugin_memes.matchers import command


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("block_type", "status", "group_id", "is_super", "expected"),
    [
        (None, True, "20000", False, False),
        (None, False, "20000", False, True),
        (None, False, "20000", True, False),
        (command.BlockType.ALL, True, "20000", False, True),
        (command.BlockType.GROUP, True, "20000", True, True),
        (command.BlockType.GROUP, True, None, False, False),
        (command.BlockType.PRIVATE, True, None, False, True),
        (command.BlockType.PRIVATE, True, "20000", False, False),
    ],
)
async def test_plugin_state_respects_group_and_private_scope(
    mocker, block_type, status, group_id, is_super, expected
):
    plugin = SimpleNamespace(
        module="nonebot_plugin_memes", name="表情包制作",
        block_type=block_type, status=status,
    )
    lookup = mocker.patch.object(command.PluginInfo, "get_plugin", return_value=plugin)
    mocker.patch.object(command.BotConsole, "get_or_none", return_value=None)
    mocker.patch.object(command.GroupConsole, "get_group", return_value=SimpleNamespace(is_super=is_super))
    mocker.patch.object(command.GroupConsole, "is_block_plugin", return_value=False)

    assert await command._is_current_plugin_disabled(
        SimpleNamespace(plugin_name=plugin.module), "10000", group_id
    ) is expected
    lookup.assert_awaited_once_with(module="nonebot_plugin_memes")


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["bot", "group"])
async def test_bot_and_group_disables_override_enabled_plugin(mocker, scope):
    plugin = SimpleNamespace(module="nonebot_plugin_memes", name="表情包制作", block_type=None, status=True)
    mocker.patch.object(command.PluginInfo, "get_plugin", return_value=plugin)
    mocker.patch.object(command.BotConsole, "get_or_none", return_value=SimpleNamespace(block_plugins=command.BotConsole.format(plugin.module) if scope == "bot" else ""))
    mocker.patch.object(command.GroupConsole, "get_group", return_value=None)
    mocker.patch.object(command.GroupConsole, "is_block_plugin", return_value=scope == "group")

    assert await command._is_current_plugin_disabled(
        SimpleNamespace(plugin_name=plugin.module), "10000", "20000"
    )
