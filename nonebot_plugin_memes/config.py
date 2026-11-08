import asyncio
from datetime import timedelta
from typing import Literal

from nonebot import get_plugin_config
from nonebot.log import logger
from pydantic import BaseModel


class MemeListImageConfig(BaseModel):
    sort_by: Literal["key", "keywords", "date_created", "date_modified"] = "keywords"
    sort_reverse: bool = False
    text_template: str = "{keywords}"
    add_category_icon: bool = True
    label_new_timedelta: timedelta = timedelta(days=30)
    label_hot_threshold: int = 21
    label_hot_days: int = 7


class Config(BaseModel):
    memes_command_prefixes: list[str] | None = None
    memes_disabled_list: list[str] = []
    memes_check_resources_on_startup: bool = True
    memes_prompt_params_error: bool = False
    memes_use_sender_when_no_image: bool = False
    memes_use_default_when_no_text: bool = False
    memes_random_meme_show_info: bool = True
    memes_generation_concurrency: int = 1
    memes_generation_timeout_seconds: float = 30.0
    memes_request_expire_seconds: float = 60.0
    memes_max_static_image_bytes: int = 8 * 1024 * 1024
    memes_max_animated_image_bytes: int = 2 * 1024 * 1024
    memes_max_animated_frames: int = 30
    memes_max_image_pixels: int = 2_000_000
    memes_max_animated_total_pixels: int = 12_000_000
    memes_list_image_config: MemeListImageConfig = MemeListImageConfig()


memes_config = get_plugin_config(Config)


if memes_config.memes_check_resources_on_startup:
    from meme_generator.download import check_resources
    from nonebot import get_driver

    driver = get_driver()
    _resource_check_task: asyncio.Task | None = None

    @driver.on_startup
    async def _():
        global _resource_check_task
        logger.info("正在检查资源文件...")
        _resource_check_task = asyncio.create_task(check_resources())
