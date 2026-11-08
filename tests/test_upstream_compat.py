from types import SimpleNamespace

from nonebot.exception import AdapterException
from nonebot_plugin_alconna import At, Text
from nonebot_plugin_userinfo import UserInfo
import pytest

from nonebot_plugin_memes import manager
from nonebot_plugin_memes.matchers import command


@pytest.mark.parametrize("humanized", [None, "Readable Name"])
def test_shortcut_raw_and_display_names_both_resolve(mocker, tmp_path, humanized):
    meme = SimpleNamespace(
        key="example", keywords=["Example"], tags=set(),
        shortcuts=[SimpleNamespace(key="Raw Shortcut", humanized=humanized)],
    )
    mocker.patch.object(manager, "get_memes", return_value=[meme])
    meme_manager = manager.MemeManager(tmp_path / "manager.yml")

    assert meme_manager.find("RAW SHORTCUT") == [meme]
    if humanized:
        assert meme_manager.find("READABLE NAME") == [meme]
    assert meme_manager.search("Raw Shortcut") == [meme]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [NotImplementedError(), command.NetworkError(), AdapterException("test")]
)
@pytest.mark.parametrize(
    "target", [At(flag="user", target="12345"), Text("@12345"), Text("自己")]
)
async def test_failed_user_lookup_preserves_other_parameters(mocker, error, target):
    lookup = mocker.patch.object(command, "get_user_info", side_effect=error)
    event = mocker.MagicMock()
    event.get_user_id.return_value = "12345"

    texts, images, users = await command.handle_params(
        mocker.MagicMock(), event, {}, [target, Text("hello")]
    )

    assert texts == ["hello"]
    assert images == []
    assert users == []
    assert lookup.await_count == 1


@pytest.mark.asyncio
async def test_successful_user_lookup_is_preserved(mocker):
    user = UserInfo(user_id="12345", user_name="name")
    mocker.patch.object(command, "get_user_info", return_value=user)

    assert await command._get_user_info_safely(None, None, user.user_id) is user


@pytest.mark.asyncio
async def test_unexpected_user_lookup_errors_still_propagate(mocker):
    mocker.patch.object(command, "get_user_info", side_effect=ValueError("unexpected"))

    with pytest.raises(ValueError, match="unexpected"):
        await command._get_user_info_safely(None, None, "12345")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("displayname", "username", "expected"),
    [("display", "name", "display"), (None, "name", "name"), (None, "", "12345")],
)
async def test_generation_name_falls_back_to_user_id(
    mocker, displayname, username, expected
):
    user = UserInfo(user_id="12345", user_name=username, user_displayname=displayname)
    generate = mocker.patch.object(command, "_generate_meme", return_value=b"GIF")
    mocker.patch.object(command, "record_meme_generation")

    result = await command._prepare_meme_result(
        mocker.MagicMock(), mocker.MagicMock(), mocker.MagicMock(),
        [], [], [user], {}, True,
    )

    assert result == b"GIF"
    assert generate.await_args.args[3]["user_infos"][0]["name"] == expected
