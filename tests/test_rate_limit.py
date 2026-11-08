import asyncio
from io import BytesIO
import time

import pytest


def test_generation_rate_limiter_prompts_once_then_stays_silent():
    from nonebot_plugin_memes.rate_limit import GenerationRateLimiter

    limiter = GenerationRateLimiter(max_calls=3, window_seconds=30)
    key = "qq:bot:group:user"

    assert limiter.check(key, now=0).allowed
    assert limiter.check(key, now=1).allowed
    assert limiter.check(key, now=2).allowed

    first_rejection = limiter.check(key, now=3)
    assert not first_rejection.allowed
    assert first_rejection.should_prompt

    silent_rejection = limiter.check(key, now=32)
    assert not silent_rejection.allowed
    assert not silent_rejection.should_prompt

    assert limiter.check(key, now=33).allowed


def test_generation_rate_limiter_is_scoped_by_session():
    from nonebot_plugin_memes.rate_limit import GenerationRateLimiter

    limiter = GenerationRateLimiter(max_calls=3, window_seconds=30)
    first_session = "qq:bot:group-a:user"
    second_session = "qq:bot:group-b:user"

    for now in (0, 1, 2):
        assert limiter.check(first_session, now=now).allowed

    assert limiter.check(second_session, now=3).allowed
    assert limiter.check(first_session, now=3).should_prompt


@pytest.mark.asyncio
async def test_generation_rate_limit_reply_quotes_trigger_message(mocker):
    from nonebot.exception import FinishedException
    from nonebot_plugin_session import SessionIdType

    from nonebot_plugin_memes.matchers import command
    from nonebot_plugin_memes.rate_limit import GenerationRateLimiter

    limiter = GenerationRateLimiter(
        max_calls=3,
        window_seconds=30,
        clock=lambda: 3,
    )
    for now in (0, 1, 2):
        assert limiter.check("session-key", now=now).allowed
    mocker.patch.object(command, "_generation_rate_limiter", limiter)

    matcher = mocker.MagicMock()
    matcher.finish = mocker.AsyncMock(side_effect=FinishedException)
    session = mocker.MagicMock()
    session.get_id.return_value = "session-key"

    with pytest.raises(FinishedException):
        await command._check_generation_rate_limit(matcher, session)

    session.get_id.assert_called_once_with(
        SessionIdType.GROUP_USER,
        include_bot_type=False,
    )
    matcher.stop_propagation.assert_called_once_with()
    sent_message = matcher.finish.await_args.args[0]
    assert str(sent_message) == command._generation_rate_limit_message
    assert matcher.finish.await_args.kwargs == {"reply_to": True}


@pytest.mark.asyncio
async def test_image_reject_reply_quotes_trigger_message(mocker):
    from nonebot.exception import FinishedException

    from nonebot_plugin_memes.matchers import command

    matcher = mocker.MagicMock()
    matcher.finish = mocker.AsyncMock(side_effect=FinishedException)
    image_source = mocker.MagicMock()
    image_source.get_image = mocker.AsyncMock(return_value=b"image")
    meme = mocker.MagicMock()
    meme.key = "test-meme"
    mocker.patch.object(
        command,
        "_get_images_reject_reason",
        return_value="image too large",
    )

    with pytest.raises(FinishedException):
        await command.process(
            matcher,
            mocker.MagicMock(),
            meme,
            [image_source],
            [],
            [],
        )

    sent_message = matcher.finish.await_args.args[0]
    assert str(sent_message) == command._image_reject_message
    assert matcher.finish.await_args.kwargs == {"reply_to": True}


@pytest.mark.asyncio
async def test_image_reject_does_not_quote_poke_notice(mocker):
    from nonebot.exception import FinishedException

    from nonebot_plugin_memes.matchers import command

    matcher = mocker.MagicMock()
    matcher.finish = mocker.AsyncMock(side_effect=FinishedException)
    image_source = mocker.MagicMock()
    image_source.get_image = mocker.AsyncMock(return_value=b"image")
    meme = mocker.MagicMock()
    meme.key = "fencing"
    mocker.patch.object(
        command,
        "_get_images_reject_reason",
        return_value="image too large",
    )

    with pytest.raises(FinishedException):
        await command.process(
            matcher,
            mocker.MagicMock(),
            meme,
            [image_source],
            [],
            [],
            reply_on_image_reject=False,
        )

    matcher.finish.assert_awaited_once_with(command._image_reject_message)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_on_image_reject", [True, False])
async def test_worker_image_reject_uses_existing_reply(mocker, reply_on_image_reject):
    from nonebot.exception import FinishedException

    from nonebot_plugin_memes.generation_worker import ImageRejectedError
    from nonebot_plugin_memes.matchers import command

    matcher = mocker.MagicMock()
    matcher.finish = mocker.AsyncMock(side_effect=FinishedException)
    mocker.patch.object(command, "_get_images_reject_reason", return_value=None)
    mocker.patch.object(
        command,
        "_generate_meme",
        side_effect=ImageRejectedError("still too large"),
    )
    record = mocker.patch.object(command, "record_meme_generation")

    with pytest.raises(FinishedException):
        await command._prepare_meme_result(
            matcher,
            mocker.MagicMock(),
            mocker.MagicMock(),
            [],
            [],
            [],
            {},
            reply_on_image_reject,
        )

    assert str(matcher.finish.await_args.args[0]) == command._image_reject_message
    assert matcher.finish.await_args.kwargs == (
        {"reply_to": True} if reply_on_image_reject else {}
    )
    record.assert_not_awaited()


def test_preflight_allows_static_resize_but_still_rejects_oversized_animations(mocker):
    from PIL import Image

    from nonebot_plugin_memes.matchers import command

    mocker.patch.object(command, "_max_image_pixels", 100_000)
    static = BytesIO()
    Image.new("RGB", (512, 512), "white").save(static, format="PNG")
    animated = BytesIO()
    Image.new("RGB", (512, 512), "red").save(
        animated,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (512, 512), "blue")],
    )

    assert command._get_images_reject_reason([static.getvalue()]) is not None
    assert (
        command._get_images_reject_reason([static.getvalue()], allow_static_resize=True)
        is None
    )
    assert (
        command._get_images_reject_reason(
            [animated.getvalue()], allow_static_resize=True
        )
        is not None
    )


@pytest.mark.asyncio
async def test_expired_meme_request_is_dropped_before_queueing(mocker):
    from nonebot.exception import FinishedException

    from nonebot_plugin_memes.matchers import command

    matcher = mocker.MagicMock()
    matcher.finish = mocker.AsyncMock(side_effect=FinishedException)
    image_source = mocker.MagicMock()
    image_source.get_image = mocker.AsyncMock(return_value=b"image")
    meme = mocker.MagicMock()
    meme.key = "test-meme"
    event = mocker.MagicMock()
    event.time = time.time() - 61
    mocker.patch.object(command, "_request_expire_seconds", 60.0)
    prepare = mocker.patch.object(command, "_prepare_meme_result")

    with pytest.raises(FinishedException):
        await command.process(
            matcher,
            mocker.MagicMock(),
            meme,
            [image_source],
            [],
            [],
            event=event,
        )

    matcher.finish.assert_awaited_once_with()
    prepare.assert_not_awaited()
    image_source.get_image.assert_not_awaited()


@pytest.mark.asyncio
async def test_meme_request_expires_while_waiting_for_generation_slot(mocker):
    from nonebot.exception import FinishedException

    from nonebot_plugin_memes.matchers import command

    matcher = mocker.MagicMock()
    matcher.finish = mocker.AsyncMock(side_effect=FinishedException)
    meme = mocker.MagicMock()
    meme.key = "test-meme"
    semaphore = asyncio.Semaphore(1)
    await semaphore.acquire()
    mocker.patch.object(command, "_meme_generation_semaphore", semaphore)
    mocker.patch.object(command, "_request_expire_seconds", 0.01)
    prepare = mocker.patch.object(command, "_prepare_meme_result")

    try:
        with pytest.raises(FinishedException):
            await command.process(
                matcher,
                mocker.MagicMock(),
                meme,
                [],
                [],
                [],
            )
    finally:
        semaphore.release()

    matcher.finish.assert_awaited_once_with()
    prepare.assert_not_awaited()


@pytest.mark.asyncio
async def test_meme_generation_is_cancelled_after_execution_timeout(mocker):
    from nonebot.exception import FinishedException

    from nonebot_plugin_memes.matchers import command

    async def never_finishes(*args, **kwargs):
        await asyncio.Event().wait()

    matcher = mocker.MagicMock()
    matcher.finish = mocker.AsyncMock(side_effect=FinishedException)
    meme = mocker.MagicMock()
    meme.key = "test-meme"
    mocker.patch.object(command, "_meme_generation_semaphore", asyncio.Semaphore(1))
    mocker.patch.object(command, "_generation_timeout_seconds", 0.01)
    mocker.patch.object(command, "_request_expire_seconds", 60.0)
    prepare = mocker.patch.object(
        command,
        "_prepare_meme_result",
        side_effect=never_finishes,
    )

    with pytest.raises(FinishedException):
        await command.process(
            matcher,
            mocker.MagicMock(),
            meme,
            [],
            [],
            [],
        )

    prepare.assert_awaited_once()
    matcher.finish.assert_awaited_once_with(command._generation_timeout_message)


def test_stopping_generation_kills_worker_process(mocker):
    from nonebot_plugin_memes.matchers import command

    class FakeProcess:
        def __init__(self):
            self.returncode = None
            self.killed = False
            self.pid = 123
            self.stdin = None
            self.stdout = None

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        def poll(self):
            return self.returncode

        def wait(self, timeout: float) -> int:
            return self.returncode

    process = FakeProcess()
    mocker.patch.object(command, "_generation_process", process)
    mocker.patch.object(command, "_generation_process_ready", True)

    command._stop_generation_process()

    assert process.killed
    assert command._generation_process is None
    assert not command._generation_process_ready


def test_meme_generation_process_smoke_test(monkeypatch, tmp_path):
    from PIL import Image

    from nonebot_plugin_memes.matchers import command

    image = BytesIO()
    Image.new("RGB", (64, 64), "white").save(image, format="PNG")
    meme = command.meme_manager.get_meme("petpet")
    assert meme is not None

    monkeypatch.chdir(tmp_path)

    try:
        result = command._generate_meme_sync(meme, [image.getvalue()], [], {})
    finally:
        command._stop_generation_process()

    assert result.startswith(b"GIF")


def test_worker_resizes_oversized_static_images_and_survives_rejection(mocker):
    from PIL import Image

    from nonebot_plugin_memes.generation_worker import ImageRejectedError
    from nonebot_plugin_memes.matchers import command

    mocker.patch.object(command, "_max_image_pixels", 300_000)
    image = BytesIO()
    Image.new("RGB", (1024, 768), "white").save(image, format="PNG")
    meme = command.meme_manager.get_meme("petpet")
    assert meme is not None

    try:
        mocker.patch.object(command, "_max_static_image_bytes", 1)
        with pytest.raises(ImageRejectedError):
            command._generate_meme_sync(meme, [image.getvalue()], [], {})

        mocker.patch.object(command, "_max_static_image_bytes", 8 * 1024 * 1024)
        result = command._generate_meme_sync(meme, [image.getvalue()], [], {})
    finally:
        command._stop_generation_process()

    assert result.startswith(b"GIF")
