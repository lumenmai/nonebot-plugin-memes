import asyncio
import contextlib
from io import BytesIO
from pathlib import Path
import pickle
import random
import re
import struct
import subprocess
import sys
import time
import traceback
from typing import Any, NoReturn

from arclet.alconna import config as alc_config
from meme_generator import Meme
from meme_generator.exception import MemeGeneratorException
from nonebot import get_driver
from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import PokeNotifyEvent
from nonebot.compat import PYDANTIC_V2, ConfigDict
from nonebot.exception import AdapterException
from nonebot.log import logger
from nonebot.matcher import Matcher
from nonebot.plugin import on_notice
from nonebot.typing import T_State
from nonebot_plugin_alconna import (
    AlcMatches,
    Alconna,
    Args,
    At,
    Image,
    MultiVar,
    Text,
    UniMessage,
    on_alconna,
)
from nonebot_plugin_alconna.builtins.extensions.reply import ReplyMergeExtension
from nonebot_plugin_alconna.uniseg.tools import image_fetch
from nonebot_plugin_session import EventSession, Session, SessionIdType
from nonebot_plugin_userinfo import ImageSource, UserInfo, get_user_info
from PIL import Image as PILImage
from PIL import UnidentifiedImageError

from zhenxun.configs.config import BotConfig
from zhenxun.models.ban_console import BanConsole
from zhenxun.models.bot_console import BotConsole
from zhenxun.models.group_console import GroupConsole
from zhenxun.models.plugin_info import PluginInfo
from zhenxun.utils.enum import BlockType
from zhenxun.utils.limiters import RateLimiter

from ..config import memes_config
from ..generation_worker import ImageRejectedError
from ..manager import meme_manager
from ..rate_limit import GenerationRateLimiter
from ..recorder import record_meme_generation
from ..utils import NetworkError
from .utils import UserId

alc_config.command_max_count += 1000

_poke_rate_limiter = RateLimiter(5, 60)
_generation_rate_limiter = GenerationRateLimiter(3, 30)
_meme_generation_semaphore = asyncio.Semaphore(
    max(1, memes_config.memes_generation_concurrency)
)
_generation_timeout_seconds = max(
    1.0, float(memes_config.memes_generation_timeout_seconds)
)
_request_expire_seconds = max(1.0, float(memes_config.memes_request_expire_seconds))
_generation_process: subprocess.Popen[bytes] | None = None
_generation_process_ready = False
_generation_process_lock = asyncio.Lock()
_max_static_image_bytes = max(1, memes_config.memes_max_static_image_bytes)
_max_animated_image_bytes = max(1, memes_config.memes_max_animated_image_bytes)
_max_animated_frames = max(1, memes_config.memes_max_animated_frames)
_max_image_pixels = max(1, memes_config.memes_max_image_pixels)
_max_animated_total_pixels = max(1, memes_config.memes_max_animated_total_pixels)
_image_reject_message = f"{BotConfig.self_nickname}吃不下这张图啦，换张图再试试吧。"
_generation_rate_limit_message = f"别着急呀，{BotConfig.self_nickname}暂时吞不下了！"
_generation_timeout_message = "表情生成超时了，换张图再试试吧。"


async def _check_generation_rate_limit(matcher: Matcher, session: Session) -> None:
    limiter_key = session.get_id(
        SessionIdType.GROUP_USER,
        include_bot_type=False,
    )
    decision = _generation_rate_limiter.check(limiter_key)
    if decision.allowed:
        return

    matcher.stop_propagation()
    if decision.should_prompt:
        await matcher.finish(
            UniMessage(_generation_rate_limit_message),
            reply_to=True,
        )
    await matcher.finish()


async def _is_current_plugin_disabled(
    matcher: Matcher, bot_id: str, group_id: str | None
) -> bool:
    module = matcher.plugin_name
    if not module:
        return False

    plugin = await PluginInfo.get_plugin(module=module)
    if not plugin:
        logger.warning(f"未找到插件状态记录: {module}")
        return False

    bot_data = await BotConsole.get_or_none(bot_id=bot_id)
    if bot_data and BotConsole.format(plugin.module) in bot_data.block_plugins:
        logger.info(f"插件 {plugin.name}({plugin.module}) 已在 bot {bot_id} 禁用")
        return True

    group = await GroupConsole.get_group(group_id) if group_id else None
    if group_id and await GroupConsole.is_block_plugin(group_id, plugin.module):
        logger.info(f"插件 {plugin.name}({plugin.module}) 已在群 {group_id} 禁用")
        return True

    if group_id and plugin.block_type == BlockType.GROUP:
        logger.info(f"插件 {plugin.name}({plugin.module}) 已全局群组禁用")
        return True

    is_global_disabled = plugin.block_type == BlockType.ALL or (
        not plugin.status and plugin.block_type is None
    )
    if is_global_disabled and not (group and group.is_super):
        logger.info(f"插件 {plugin.name}({plugin.module}) 已全局禁用")
        return True

    if not group_id and plugin.block_type == BlockType.PRIVATE:
        logger.info(f"插件 {plugin.name}({plugin.module}) 已全局私聊禁用")
        return True

    return False


def _format_bytes(size: int) -> str:
    return f"{size / 1024 / 1024:.2f}MiB"


def _get_image_reject_reason(
    image: bytes, *, allow_static_resize: bool = False
) -> str | None:
    image_size = len(image)
    try:
        with PILImage.open(BytesIO(image)) as img:
            width, height = img.size
            pixels = width * height
            is_animated = bool(getattr(img, "is_animated", False))
            frames = int(getattr(img, "n_frames", 1) or 1)
    except UnidentifiedImageError:
        return f"unidentified image, size={_format_bytes(image_size)}"
    except Exception as e:
        return (
            f"inspect image failed, size={_format_bytes(image_size)}, "
            f"error={type(e).__name__}: {e}"
        )

    if allow_static_resize and not is_animated:
        return None

    if pixels > _max_image_pixels:
        return (
            f"image pixels too large, size={width}x{height}, "
            f"pixels={pixels}, limit={_max_image_pixels}"
        )

    if is_animated:
        if image_size > _max_animated_image_bytes:
            return (
                "animated image bytes too large, "
                f"bytes={_format_bytes(image_size)}, "
                f"limit={_format_bytes(_max_animated_image_bytes)}, "
                f"frames={frames}, size={width}x{height}"
            )
        if frames > _max_animated_frames:
            return (
                "animated image frames too many, "
                f"frames={frames}, limit={_max_animated_frames}, "
                f"bytes={_format_bytes(image_size)}, size={width}x{height}"
            )
        total_pixels = pixels * frames
        if total_pixels > _max_animated_total_pixels:
            return (
                "animated image total pixels too large, "
                f"total_pixels={total_pixels}, "
                f"limit={_max_animated_total_pixels}, "
                f"frames={frames}, size={width}x{height}, "
                f"bytes={_format_bytes(image_size)}"
            )
    elif image_size > _max_static_image_bytes:
        return (
            "static image bytes too large, "
            f"bytes={_format_bytes(image_size)}, "
            f"limit={_format_bytes(_max_static_image_bytes)}, "
            f"size={width}x{height}"
        )

    return None


def _get_images_reject_reason(
    images: list[bytes], *, allow_static_resize: bool = False
) -> str | None:
    for index, image in enumerate(images, 1):
        if reason := _get_image_reject_reason(
            image, allow_static_resize=allow_static_resize
        ):
            return f"image_index={index}, {reason}"
    return None


def _read_exact(stream: Any, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError("meme generation worker closed its output")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_generation_response(
    process: subprocess.Popen[bytes],
) -> tuple[str, Any]:
    if process.stdout is None:
        raise RuntimeError("meme generation worker has no stdout pipe")
    header = _read_exact(process.stdout, 8)
    size = struct.unpack("!Q", header)[0]
    return pickle.loads(_read_exact(process.stdout, size))


def _spawn_generation_process() -> subprocess.Popen[bytes]:
    global _generation_process, _generation_process_ready
    _generation_process_ready = False
    _generation_process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "generation_worker.py"), "--serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    return _generation_process


def _start_generation_process() -> subprocess.Popen[bytes]:
    global _generation_process_ready
    if _generation_process is not None and _generation_process.poll() is None:
        process = _generation_process
    else:
        process = _spawn_generation_process()
    if _generation_process_ready:
        return process
    try:
        status, payload = _read_generation_response(process)
    except BaseException:
        _stop_generation_process(process)
        raise
    if status != "ready":
        _stop_generation_process(process)
        raise RuntimeError(f"meme generation worker failed to start: {payload}")
    _generation_process_ready = True
    return process


def _stop_generation_process(
    process: subprocess.Popen[bytes] | None = None,
) -> None:
    global _generation_process, _generation_process_ready
    process = process or _generation_process
    if process is None:
        return
    if process is _generation_process:
        _generation_process = None
        _generation_process_ready = False
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning(f"表情工作进程未能及时回收: pid={process.pid}")
    if process.stdin is not None:
        with contextlib.suppress(Exception):
            process.stdin.close()
    if process.stdout is not None:
        with contextlib.suppress(Exception):
            process.stdout.close()


@get_driver().on_shutdown
async def _shutdown_generation_process() -> None:
    await asyncio.to_thread(_stop_generation_process)


def _generate_meme_sync(
    meme: Meme,
    images: list[bytes],
    texts: list[str],
    args: dict[str, Any],
) -> bytes:
    process = _start_generation_process()
    if process.stdin is None:
        _stop_generation_process(process)
        raise RuntimeError("meme generation worker has no stdin pipe")
    request = pickle.dumps(
        (meme.key, images, texts, args, (_max_image_pixels, _max_static_image_bytes))
    )
    process.stdin.write(struct.pack("!Q", len(request)) + request)
    process.stdin.flush()
    status, payload = _read_generation_response(process)

    if status == "ok":
        return payload
    if status == "image_rejected":
        raise ImageRejectedError(payload)
    if status == "meme_error":
        raise MemeGeneratorException(payload)
    raise RuntimeError(f"meme generation worker failed:\n{payload}")


async def _generate_meme(
    meme: Meme,
    images: list[bytes],
    texts: list[str],
    args: dict[str, Any],
) -> bytes:
    """Run untrusted image decoding/generation in a killable process."""

    async with _generation_process_lock:
        # Spawn on the event-loop thread. Some C runtimes are unsafe when a
        # subprocess is forked from a background thread in a multithreaded bot.
        if _generation_process is None or _generation_process.poll() is not None:
            _spawn_generation_process()
        try:
            return await asyncio.to_thread(
                _generate_meme_sync,
                meme,
                images,
                texts,
                args,
            )
        except asyncio.CancelledError:
            # An executor thread cannot be stopped once Pillow enters a pathological
            # GIF decode/encode. Killing its child process makes the I/O thread exit.
            await asyncio.to_thread(_stop_generation_process)
            raise


def _request_age_seconds(event: Event | None, queued_at: float) -> float:
    timestamp = getattr(event, "time", None) if event is not None else None
    if isinstance(timestamp, int | float) and timestamp > 0:
        return max(0.0, time.time() - float(timestamp))
    return max(0.0, time.monotonic() - queued_at)


async def _finish_expired_request(
    matcher: Matcher,
    meme: Meme,
    event: Event | None,
    queued_at: float,
) -> NoReturn:
    age = _request_age_seconds(event, queued_at)
    logger.info(
        f"表情 {meme.key} 请求已过期，已丢弃: "
        f"age={age:.1f}s, limit={_request_expire_seconds:.1f}s"
    )
    await matcher.finish()
    raise RuntimeError("matcher.finish() returned unexpectedly")


async def _prepare_meme_result(
    matcher: Matcher,
    session: Session,
    meme: Meme,
    image_sources: list[ImageSource],
    texts: list[str],
    user_infos: list[UserInfo],
    args: dict[str, Any],
    reply_on_image_reject: bool,
) -> bytes:
    images: list[bytes] = []

    try:
        for image_source in image_sources:
            images.append(await image_source.get_image())
    except NotImplementedError:
        await matcher.finish("当前平台可能不支持获取图片")
    except (NetworkError, AdapterException):
        logger.warning(traceback.format_exc())
        await matcher.finish("图片下载出错，请稍后再试")

    if reject_reason := _get_images_reject_reason(images, allow_static_resize=True):
        logger.info(f"表情 {meme.key} 输入图片已拒绝: {reject_reason}")
        if reply_on_image_reject:
            await matcher.finish(UniMessage(_image_reject_message), reply_to=True)
        await matcher.finish(_image_reject_message)

    args_user_infos = []
    for user_info in user_infos:
        name = user_info.user_displayname or user_info.user_name or user_info.user_id
        gender = str(user_info.user_gender)
        if gender not in ("male", "female"):
            gender = "unknown"
        args_user_infos.append({"name": name, "gender": gender})
    args["user_infos"] = args_user_infos

    try:
        result = await _generate_meme(meme, images, texts, args)
        await record_meme_generation(session, meme.key)
    except ImageRejectedError as e:
        logger.info(f"表情 {meme.key} 输入图片缩小后仍被拒绝: {e}")
        if reply_on_image_reject:
            await matcher.finish(UniMessage(_image_reject_message), reply_to=True)
        await matcher.finish(_image_reject_message)
    except MemeGeneratorException as e:
        await matcher.finish(e.message)
    return result


async def process(
    matcher: Matcher,
    session: Session,
    meme: Meme,
    image_sources: list[ImageSource],
    texts: list[str],
    user_infos: list[UserInfo],
    args: dict[str, Any] | None = None,
    show_info: bool = False,
    reply_on_image_reject: bool = True,
    event: Event | None = None,
):
    args = dict(args or {})
    queued_at = time.monotonic()
    age = _request_age_seconds(event, queued_at)
    if age >= _request_expire_seconds:
        await _finish_expired_request(matcher, meme, event, queued_at)

    try:
        await asyncio.wait_for(
            _meme_generation_semaphore.acquire(),
            timeout=_request_expire_seconds - age,
        )
    except asyncio.TimeoutError:
        await _finish_expired_request(matcher, meme, event, queued_at)

    try:
        remaining_lifetime = _request_expire_seconds - _request_age_seconds(
            event, queued_at
        )
        if remaining_lifetime <= 0:
            await _finish_expired_request(matcher, meme, event, queued_at)
        try:
            result = await asyncio.wait_for(
                _prepare_meme_result(
                    matcher,
                    session,
                    meme,
                    image_sources,
                    texts,
                    user_infos,
                    args,
                    reply_on_image_reject,
                ),
                timeout=min(_generation_timeout_seconds, remaining_lifetime),
            )
        except asyncio.TimeoutError:
            age = _request_age_seconds(event, queued_at)
            if age >= _request_expire_seconds:
                await _finish_expired_request(matcher, meme, event, queued_at)
            logger.warning(
                f"表情 {meme.key} 生成超时，已终止工作进程: "
                f"timeout={_generation_timeout_seconds:.1f}s"
            )
            await matcher.finish(_generation_timeout_message)
    finally:
        _meme_generation_semaphore.release()

    if _request_age_seconds(event, queued_at) >= _request_expire_seconds:
        await _finish_expired_request(matcher, meme, event, queued_at)

    msg = UniMessage()
    if show_info:
        keywords = "、".join([f'"{keyword}"' for keyword in meme.keywords])
        msg += f"关键词：{keywords}"
    msg += UniMessage.image(raw=BytesIO(result))
    await msg.send()


class AlcImage(ImageSource):
    bot: Bot
    event: Event
    state: T_State
    img: Image

    if PYDANTIC_V2:
        model_config: ConfigDict = ConfigDict(arbitrary_types_allowed=True)
    else:

        class Config:
            arbitrary_types_allowed: bool = True

    async def get_image(self) -> bytes:
        result = await image_fetch(self.event, self.bot, self.state, self.img)
        if isinstance(result, bytes):
            return result
        raise NotImplementedError("image fetch not implemented")


T_MemeParams = Text | Image | At
meme_params_key = "meme_params"
arg_meme_params = Args[meme_params_key, MultiVar(T_MemeParams, "*")]


async def _get_user_info_safely(
    bot: Bot, event: Event, user_id: str
) -> UserInfo | None:
    try:
        return await get_user_info(bot, event, user_id)
    except NotImplementedError:
        logger.warning("当前平台可能不支持获取用户信息")
    except (NetworkError, AdapterException):
        logger.warning(f"用户信息获取出错：\n{traceback.format_exc()}")
    return None


async def handle_params(
    bot: Bot, event: Event, state: T_State, meme_params: list[T_MemeParams]
):
    texts: list[str] = []
    image_sources: list[ImageSource] = []
    user_infos: list[UserInfo] = []

    for msg_seg in meme_params:
        if isinstance(msg_seg, At):
            if user_info := await _get_user_info_safely(bot, event, msg_seg.target):
                if image_source := user_info.user_avatar:
                    image_sources.append(image_source)
                user_infos.append(user_info)

        elif isinstance(msg_seg, Image):
            image_sources.append(
                AlcImage(bot=bot, event=event, state=state, img=msg_seg)
            )

        elif isinstance(msg_seg, Text):
            text = msg_seg.text
            if text.startswith("@") and (user_id := text[1:]):
                if user_info := await _get_user_info_safely(bot, event, user_id):
                    if image_source := user_info.user_avatar:
                        image_sources.append(image_source)
                    user_infos.append(user_info)

            elif text == "自己":
                if user_info := await _get_user_info_safely(
                    bot, event, event.get_user_id()
                ):
                    if image_source := user_info.user_avatar:
                        image_sources.append(image_source)
                    user_infos.append(user_info)

            elif text:
                texts.append(text)

    return texts, image_sources, user_infos


prefixes = list(get_driver().config.command_start)
if (meme_prefixes := memes_config.memes_command_prefixes) is not None:
    prefixes = meme_prefixes


def create_matcher(meme: Meme):
    options = [
        opt.option()
        for opt in (
            meme.params_type.args_type.parser_options
            if meme.params_type.args_type
            else []
        )
    ]
    meme_matcher = on_alconna(
        Alconna(prefixes, meme.keywords[0], *options, arg_meme_params),
        aliases=set(meme.keywords[1:]),
        block=False,
        priority=12,
        extensions=[ReplyMergeExtension()],
    )
    for shortcut in meme.shortcuts:
        meme_matcher.shortcut(
            shortcut.key,
            arguments=shortcut.args,
            prefix=True,
            humanized=shortcut.humanized,
        )

    @meme_matcher.handle()
    async def _(
        bot: Bot,
        event: Event,
        state: T_State,
        matcher: Matcher,
        user_id: UserId,
        session: EventSession,
        alc_matches: AlcMatches,
    ):
        await _check_generation_rate_limit(matcher, session)

        if not meme_manager.check(user_id, meme.key):
            logger.info(f"用户 {user_id} 表情 {meme.key} 被禁用")
            return

        args: dict[str, Any] = {}
        options = alc_matches.options
        for option, option_result in options.items():
            if option_result.value is None:
                args.update(option_result.args)
            else:
                args[option] = option_result.value

        meme_params: list[T_MemeParams] = list(alc_matches.query(meme_params_key, ()))
        texts, image_sources, user_infos = await handle_params(
            bot, event, state, meme_params
        )

        # 当所需图片数为 2 且已指定图片数为 1 时，使用发送者的头像作为第一张图
        if meme.params_type.min_images == 2 and len(image_sources) == 1:
            if user_info := await _get_user_info_safely(bot, event, event.get_user_id()):
                if image_source := user_info.user_avatar:
                    image_sources.insert(0, image_source)
                user_infos.insert(0, user_info)

        # 当所需图片数为 1 且没有已指定图片时，使用发送者的头像
        if memes_config.memes_use_sender_when_no_image and (
            meme.params_type.min_images == 1 and len(image_sources) == 0
        ):
            if user_info := await _get_user_info_safely(bot, event, event.get_user_id()):
                if image_source := user_info.user_avatar:
                    image_sources.append(image_source)
                user_infos.append(user_info)

        # 当所需文字数 >0 且没有输入文字时，使用默认文字
        if memes_config.memes_use_default_when_no_text and (
            meme.params_type.min_texts > 0 and len(texts) == 0
        ):
            texts = meme.params_type.default_texts

        async def finish(msg: str) -> NoReturn:
            logger.info(msg)
            if memes_config.memes_prompt_params_error:
                matcher.stop_propagation()
                await matcher.finish(msg)
            await matcher.finish()

        if not (
            meme.params_type.min_images
            <= len(image_sources)
            <= meme.params_type.max_images
        ):
            await finish(
                f"输入图片数量不符，图片数量应为 {meme.params_type.min_images}"
                + (
                    f" ~ {meme.params_type.max_images}"
                    if meme.params_type.max_images > meme.params_type.min_images
                    else ""
                )
            )
        if not (meme.params_type.min_texts <= len(texts) <= meme.params_type.max_texts):
            await finish(
                f"输入文字数量不符，文字数量应为 {meme.params_type.min_texts}"
                + (
                    f" ~ {meme.params_type.max_texts}"
                    if meme.params_type.max_texts > meme.params_type.min_texts
                    else ""
                )
            )

        matcher.stop_propagation()
        await process(
            matcher,
            session,
            meme,
            image_sources,
            texts,
            user_infos,
            args,
            event=event,
        )


def create_matchers():
    for meme in meme_manager.get_memes():
        create_matcher(meme)


create_matchers()


random_matcher = on_alconna(
    Alconna("随机表情", arg_meme_params),
    block=False,
    priority=12,
    use_cmd_start=True,
    extensions=[ReplyMergeExtension()],
)


@random_matcher.handle()
async def _(
    bot: Bot,
    event: Event,
    state: T_State,
    matcher: Matcher,
    user_id: UserId,
    session: EventSession,
    alc_matches: AlcMatches,
):
    await _check_generation_rate_limit(matcher, session)

    meme_params: list[T_MemeParams] = list(alc_matches.query(meme_params_key, ()))
    texts, image_sources, user_infos = await handle_params(
        bot, event, state, meme_params
    )

    available_memes = [
        meme
        for meme in meme_manager.get_memes()
        if meme_manager.check(user_id, meme.key)
        and (
            (
                meme.params_type.min_images
                <= len(image_sources)
                <= meme.params_type.max_images
            )
            and (meme.params_type.min_texts <= len(texts) <= meme.params_type.max_texts)
        )
    ]
    if not available_memes:
        await matcher.finish("找不到符合参数数量的表情")

    random_meme = random.choice(available_memes)
    await process(
        matcher,
        session,
        random_meme,
        image_sources,
        texts,
        user_infos,
        show_info=memes_config.memes_random_meme_show_info,
        event=event,
    )


poke_matcher = on_notice(priority=12, block=False)


@poke_matcher.handle()
async def handle_poke(
    bot: Bot,
    event: PokeNotifyEvent,
    state: T_State,
    matcher: Matcher,
    user_id: UserId,
    session: EventSession,
):
    group_id = str(event.group_id) if event.group_id else None
    if await _is_current_plugin_disabled(matcher, bot.self_id, group_id):
        return

    if event.self_id == event.target_id:
        user_id_str = str(event.user_id) if event.user_id else None
        if await BanConsole.is_ban(user_id_str, group_id):
            return

    limiter_key = f"{event.group_id}:{event.user_id}"
    if not _poke_rate_limiter.check(limiter_key):
        logger.info(
            f"用户 {event.user_id} 在群 {event.group_id} 频繁戳一戳，"
            "已忽略本次 fencing 表情生成"
        )
        return

    # 获取 fencing 表情包
    fencing_meme = meme_manager.get_meme("fencing")
    if not fencing_meme:
        logger.warning("未找到 fencing 表情包")
        return

    # 检查用户是否有权限使用该表情包
    if not meme_manager.check(user_id, fencing_meme.key):
        logger.info(f"用户 {user_id} 表情 fencing 被禁用")
        return

    # 函数来安全地获取用户信息
    async def safe_get_user_info(user_id: Any) -> UserInfo | None:
        try:
            # 确保 user_id 是字符串
            str_user_id = str(user_id)
            # 检查是否符合QQ号格式
            if not re.match(r"^\d{5,11}$", str_user_id):
                logger.warning(f"Invalid user_id format: {str_user_id}")
                return None
            return await get_user_info(bot, event, str_user_id)
        except Exception as e:
            logger.error(f"获取用户信息失败: {e}")
            return None

    # 获取戳一戳发起人的信息
    sender_info = await safe_get_user_info(event.user_id)
    # 获取被戳者的信息
    target_info = await safe_get_user_info(event.target_id)

    if not sender_info or not target_info:
        logger.warning("无法获取完整的用户信息")
        return

    # 准备图片源和用户信息
    image_sources = []
    user_infos = []

    if sender_avatar := sender_info.user_avatar:
        image_sources.append(sender_avatar)
        user_infos.append(sender_info)

    if target_avatar := target_info.user_avatar:
        image_sources.append(target_avatar)
        user_infos.append(target_info)

    # 如果没有足够的图片，就不处理
    if len(image_sources) < fencing_meme.params_type.min_images:
        logger.warning("没有足够的图片来生成表情包")
        return

    try:
        # 处理并发送表情包
        await process(
            matcher,
            session,
            fencing_meme,
            image_sources,
            [],  # 空文本列表，因为 fencing 不需要文字输入
            user_infos,
            show_info=False,
            reply_on_image_reject=False,
            event=event,
        )
    except Exception as e:
        logger.error(f"生成表情包时发生错误: {e}")
