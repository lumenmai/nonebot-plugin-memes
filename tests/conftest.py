"""Load the plugin with real NoneBot dependencies and isolated host/storage APIs."""

from enum import Enum
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import nonebot
from nonebot.adapters.onebot.v11 import Adapter
import pytest

_storage = None
_patch = None


def pytest_configure(config):
    global _storage, _patch
    _storage = tempfile.TemporaryDirectory(prefix="memes-test-")
    storage = Path(_storage.name)
    _patch = pytest.MonkeyPatch()
    for variable, directory in {
        "XDG_CONFIG_HOME": "xdg-config",
        "XDG_DATA_HOME": "xdg-data",
        "XDG_CACHE_HOME": "xdg-cache",
        "MPLCONFIGDIR": "matplotlib",
    }.items():
        _patch.setenv(variable, str(storage / directory))
    nonebot.init(
        driver="~fastapi+~httpx",
        command_start={""},
        superusers={"10000"},
        memes_check_resources_on_startup=False,
        localstore_data_dir=storage / "data",
        localstore_cache_dir=storage / "cache",
        localstore_config_dir=storage / "config",
        sqlalchemy_database_url=f"sqlite+aiosqlite:///{storage / 'test.sqlite3'}",
        alembic_startup_check=False,
    )
    nonebot.get_driver().register_adapter(Adapter)
    for name in [
        "zhenxun", "zhenxun.configs", "zhenxun.configs.config",
        "zhenxun.models", "zhenxun.models.ban_console", "zhenxun.models.bot_console",
        "zhenxun.models.group_console", "zhenxun.models.plugin_info",
        "zhenxun.utils", "zhenxun.utils.enum", "zhenxun.utils.limiters",
    ]:
        module = ModuleType(name)
        module.__path__ = []
        _patch.setitem(sys.modules, name, module)
        if "." in name:
            parent, attr = name.rsplit(".", 1)
            setattr(sys.modules[parent], attr, module)

    class BlockType(str, Enum):
        ALL = "ALL"
        GROUP = "GROUP"
        PRIVATE = "PRIVATE"

    class RateLimiter:
        def __init__(self, *args):
            pass

        def check(self, key):
            return True

    sys.modules["zhenxun.configs.config"].BotConfig = SimpleNamespace(self_nickname="小真寻")
    sys.modules["zhenxun.models.ban_console"].BanConsole = SimpleNamespace(is_ban=AsyncMock(return_value=False))
    sys.modules["zhenxun.models.bot_console"].BotConsole = SimpleNamespace(get_or_none=AsyncMock(return_value=None), format=lambda module: f"<{module}>")
    sys.modules["zhenxun.models.group_console"].GroupConsole = SimpleNamespace(get_group=AsyncMock(return_value=None), is_block_plugin=AsyncMock(return_value=False))
    sys.modules["zhenxun.models.plugin_info"].PluginInfo = SimpleNamespace(get_plugin=AsyncMock(return_value=None))
    sys.modules["zhenxun.utils.enum"].BlockType = BlockType
    sys.modules["zhenxun.utils.limiters"].RateLimiter = RateLimiter
    assert nonebot.load_plugin("nonebot_plugin_memes") is not None


def pytest_unconfigure(config):
    if _patch:
        _patch.undo()
    if _storage:
        _storage.cleanup()
