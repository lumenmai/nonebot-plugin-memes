# 表情包制作

为真寻机器人提供图片和文字表情包制作、表情搜索、黑白名单管理和调用统计，支持头像、回复消息与随机表情。

## 运行环境

- Python 3.10 或更高版本，以及已正常运行的[绪山真寻](https://github.com/zhenxun-org/zhenxun_bot)。
- NoneBot2、OneBot V11，以及 `requirements.txt` 中的依赖。
- 使用 Python 版 `meme-generator==0.1.14` 和 `nonebot-plugin-alconna==0.62.0`。生成器 0.2 及后续版本使用不同接口，升级时需要另行适配。
- 统计功能使用 `nonebot-plugin-orm` 和 `nonebot-plugin-session-orm`；需要完成这两个插件的数据库配置与迁移。

插件使用真寻的机器人昵称、插件状态、群组状态、黑名单和限流接口。部署时保持现有客户端驱动器配置，例如 `DRIVER=~fastapi+~httpx+~websockets`。

## 安装

### 方式一：放入真寻插件目录

下载本仓库，将其中的 `nonebot_plugin_memes/` 目录复制到 `zhenxun/plugins/`。在运行真寻的虚拟环境中安装仓库根目录 `requirements.txt` 中的依赖，然后重启真寻。生成 worker 已包含在插件目录中。

### 方式二：通过外部目录加载

下载本仓库到自选位置，安装 `requirements.txt` 中的依赖。在真寻实际使用的环境配置文件中，将仓库根目录追加到 `EXT_PATH`，例如：

```dotenv
EXT_PATH=["/path/to/插件仓库"]
```

保留已有的其他路径，保存配置后重启真寻。已有同名插件时，请先将其替换为本仓库版本，避免重复注册命令和数据库模型。

### 模板与字体

默认在启动后后台检查并下载固定版本生成器的模板资源，首次部署需要等待资源准备完成。资源取自 [meme-generator v0.1.14](https://github.com/MemeCrafters/meme-generator/tree/v0.1.14)，也可在真寻使用的虚拟环境中执行 `meme download` 预先下载；运行用户需要能写入生成器的模板目录。

生成器配置保存在用户配置目录中的 `meme_generator/config.toml`，例如 Linux 的 `~/.config/meme_generator/config.toml`，可在其中设置可访问的下载源：

```toml
[resource]
resource_url = "https://raw.githubusercontent.com/MemeCrafters/meme-generator/"
```

如真寻项目已通过本地 `vendor` 安装生成器，可继续使用；本插件使用的运行源码与官方 0.1.14 一致。新安装按依赖文件安装官方版本即可。统计图需要系统中安装可用中文字体，例如 Noto Sans CJK SC。

## 使用

命令前缀以真寻的 `COMMAND_START` 为准，制作表情的前缀还可通过 `memes_command_prefixes` 配置。

| 命令 | 作用 |
|---|---|
| `表情包制作` / `表情列表` | 查看可用表情 |
| `表情详情 表情名` | 查看所需图片、文字、参数和预览 |
| `表情搜索 关键词` | 搜索表情 |
| `表情关键词 图片/文字` | 制作表情，可使用“自己”、@用户或回复消息 |
| `随机表情 图片/文字` | 从符合输入数量要求的表情中随机选择 |
| `禁用表情 表情名` / `启用表情 表情名` | 修改当前会话的表情开关 |
| `全局禁用表情 表情名` | 将表情切换为白名单模式 |
| `全局启用表情 表情名` | 将表情切换为黑名单模式 |
| `[我的][全局][时间段]表情调用统计 [表情名]` | 查询调用统计，例如“我的本周表情调用统计 petpet” |

当前会话开关允许超级用户、群管理员和群主操作；私聊允许用户管理自己的开关。全局开关仅限超级用户。OneBot V11 戳一戳事件还可触发 `fencing` 表情，受真寻插件状态、黑名单和独立频率限制控制。

## 生成限制

同一会话用户在30秒内最多触发3次制作请求。首次超限回复提示，此后静默至30秒冷却结束。戳一戳使用真寻的独立限流器。

生成在可终止的独立进程中执行。默认单任务生成，生成超时30秒，请求总有效期60秒；排队时或生成后已过期的请求会被丢弃。适当放大的并发配置可允许更多准备任务，但图片生成仍由一个工作进程串行执行。

超限静态图会在生成前尝试等比例缩小并清除元数据；缩放短边下限为256像素，字节数不满足时最多再缩小两次。动画超过任一限制时直接拒绝。

以下配置写入真寻实际使用的 `.env.*` 文件：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `memes_command_prefixes` | `None` | 继承 `COMMAND_START`；也可设为字符串列表 |
| `memes_disabled_list` | `[]` | 不加载的表情 key 列表 |
| `memes_check_resources_on_startup` | `True` | 启动时检查模板资源 |
| `memes_prompt_params_error` | `False` | 图片或文字数量不符时发送提示 |
| `memes_use_sender_when_no_image` | `False` | 缺少单张输入图时使用发送者头像 |
| `memes_use_default_when_no_text` | `False` | 缺少文字时使用表情默认文字 |
| `memes_random_meme_show_info` | `True` | 随机表情附带关键词 |
| `memes_generation_concurrency` | `1` | 同时进入准备/生成流程的任务数 |
| `memes_generation_timeout_seconds` | `30.0` | 单任务生成超时，秒 |
| `memes_request_expire_seconds` | `60.0` | 请求总有效期，秒 |
| `memes_max_static_image_bytes` | `8388608` | 静态图字节上限，8 MiB |
| `memes_max_animated_image_bytes` | `2097152` | 动画字节上限，2 MiB |
| `memes_max_animated_frames` | `30` | 动画帧数上限 |
| `memes_max_image_pixels` | `2000000` | 单图或动画单帧像素上限 |
| `memes_max_animated_total_pixels` | `12000000` | 动画像素数乘帧数上限 |

`memes_list_image_config` 可配置列表排序、文字模板、类别图标和新/热门标签，字段与默认值见插件中的 `config.py`。

## 数据迁移

表情开关由 Localstore 保存为 `nonebot_plugin_memes/meme_manager.yml`，调用记录保存在 ORM 数据库。迁移时保留插件模块名，单独备份并迁移 Localstore 配置和 ORM 数据库，按原部署方式执行 ORM 迁移。生成器配置、下载模板和缓存按需另行迁移。

## 作者与许可

原作者：meetwq / MeetWq，来源为 [MemeCrafters/nonebot-plugin-memes](https://github.com/MemeCrafters/nonebot-plugin-memes)。本实现沿用上游 0.7.11 的 Python 生成器接口，仓库保留完整上游历史。

代码使用 [MIT License](LICENSE)，保留原版权声明。生成器作为独立依赖安装，其代码许可和模板素材说明见[生成器项目](https://github.com/MemeCrafters/meme-generator/tree/v0.1.14)。
