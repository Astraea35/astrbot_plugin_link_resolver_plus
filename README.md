# 🌟 AstrBot Link Resolver (魔改增强版)

## v1.9.0：规则 V2 + CLIP 自动分类

- 新增快手、视频号、知乎、小黑盒、A站、YouTube、TikTok、Instagram、Pixiv、Iwara、网易云和 NGA 的通用媒体下载通道。
- 新增 `开启解析`、`关闭解析`、`解析状态` 会话命令；开关只作用于当前群或私聊，插件重启后恢复默认开启。
- 新增同会话重复链接防抖、扩展平台代理/Cookie、最大媒体数、下载超时和 `gallery-dl` 兜底配置。
- 原有 B站、抖音、小红书、微博、X 继续使用原生解析与图片后处理流程，不会被通用下载器替代。

本插件基于开源项目 [vacacia/astrbot_plugin_link_resolver](https://github.com/vacacia/astrbot_plugin_link_resolver) **v1.0.10** 版本进行深度定制与性能魔改。在保留原版对 B站、抖音、小红书、微博、X/Twitter 基础解析能力的基础上，新增了 **规则 V2 + CLIP 混合图片分类、Upscayl/SPAN/AnimeJaNai AI 图像超分、FFmpeg AV1/AVIF 极致压缩、B站扫码登录、异步进度查询及异机部署文件传输适配** 等核心功能。

---

## 🎨 魔改核心特性 (vs 原版对比)

### 1. 🧠 规则 V2 + CLIP 混合图片分类器

* **原版**：小红书/图文内容下载后直接发送原图或固定处理。
* **规则 V2**：最长边缩至 `512px`，综合饱和度分布、色彩熵、平坦区域、线条密度、局部纹理和界面结构。
* **CLIP 复核**：规则不能确定时，使用 LAION CLIP ViT-B/32 的仅图像 FP16 ONNX 与预计算英文提示词原型复核。
* **四类路由**：动漫使用 `AnimeJaNai V3.1 Balanced`；照片按尺寸使用 `LiveActionV1 SPAN 2x` / `Nomos8k SPAN 4x`；文字/UI 截图跳过 AI；最终不确定使用 `Remacri`。
* **资源生命周期**：DirectML 推理严格串行，与 AI 升图共用 GPU 门控；开始升图前立即释放，空闲 30 秒自动卸载。
* **尺寸保护**：自动模式按图片长边动态选择 2x/4x，并将结果长边限制在 `3840px`（可配置）；已经达到上限的图片不再重复升图。
* **兼容回退**：自动模型不可用或执行失败时，动漫回退 `digital-art-4x`，照片回退 `remacri-4x`。`ultrasharp-4x` 仍可手动选择，但不再用于自动路由。

### 2. 🖼️ 全平台双后端 AI 图像超分 (全平台支持)

* **原版**：仅支持原图下载/转码发送。
* **魔改版**：针对低分辨率（低于设定阈值，默认 2160px）或模糊度不达标的图片，自动调用 Upscayl NCNN 或 SPAN NCNN 后端。自动模式使用单次推理与输出尺寸保护；旧 Upscayl 模型的双重 Pass 仅保留给手动模式。
* **高质量手动档**：支持 `AnimeJaNai V3.1 Balanced/Sharp`，Windows 使用内置 DirectML 运行器；AVV3 继续保留为手动极速动漫模型。
* **涵盖平台**：小红书、抖音、微博、X (Twitter)。

### 3. 🗜️ FFmpeg AVIF/JXL 图片转码 + JPG 预览

* **原版**：直接发送大图/原图，容易触发 QQ 图片体积超限。
* **魔改版**：
* 可选 FFmpeg `libaom-av1`（CRF 18）AVIF 高压缩度，或 `libjxl` JXL 高质量转码。
* JXL 的 PNG 输入固定无损（`distance=0`）；其他图片可用 `jxl_distance` 调节，默认 `1.0`（视觉无损）。
* **JPG 全图预览**：为 AVIF/JXL 文件同步生成 1920px 内联 JPG 预览图供 QQ 聊天界面直接展示，同时将高质文件以附件形式发送。



### 4. 📱 B站扫码登录与自动下载独立开关

* **原版**：仅支持手动复制粘贴 SESSDATA / Cookie 文本。
* **魔改版**：
* **指令扫码登录**：发送 `扫码登录B站` 自动生成二维码图片，扫码确认后实时写入本地 Cookie 并生效，无需重启。
* **自动下载开关**：新增 `enable_auto_download` 配置，关闭后只解析不下载视频，完美避免与视频总结类插件冲突。



### 5. 📢 动态解析通知与消息自动撤回

* **原版**：解析大视频或图片集时长时间静默，用户无感知。
* **魔改版**：收到链接后立即发送“正在解析”提示，并在解析/发送完成后自动撤回提示消息，保持群聊整洁。

### 6. 📊 实时任务进度查询与后台百分比日志

* **原版**：后台无详细百分比进度，无查询指令。
* **魔改版**：
* 提供 `/解析进度`、`/升图进度`、`/小红书进度` 命令，实时查看当前处理的图片序号、AI 升图阶段、百分比及已耗时。
* 可配置后台日志进度打印间隔（`progress_report_interval`），适配不同 GPU 算力设备。



### 7. 🔗 Base64 异机 API 文件发送

* **原版**：跨机器/容器部署（如 Bot 与 NapCat 不在同一文件系统）时，使用 `file://` 路径导致 NapCat 报 `ENOENT` 错误。
* **魔改版**：大文件与 AVIF 转换使用 Base64 数据流直传 OneBot API，实现 100% 异机部署兼容。

### 8. 🧹 7 天过期缓存自动化清理

* **原版**：本地图片/视频缓存需要手动清理或重启。
* **魔改版**：后台常驻异步任务，每 12 小时自动清理 `cache/` 目录下超过 7 天的过期文件。

---

## 📊 各平台功能增强一览表

| 平台 | 原版功能 | 魔改版增强功能 |
| --- | --- | --- |
| **B站 (Bilibili)** | 视频解析、合并转发、画质/编码选择 | 新增：扫码登录生成 Cookie、`enable_auto_download` 自动下载开关 |
| **小红书 (XHS)** | 无水印解析、多图下载、渲染卡片 | 新增：ONNX+CV 智能分类、Upscayl AI 升图、AVIF 极高压转换、JPG 内联预览、图文合并发送开关 |
| **抖音 (Douyin)** | 视频/图集/动图解析、卡片/摘要 | 新增：图片 AI 升图检测、全平台 AVIF/JXL 压缩与 JPG 预览 |
| **微博 (Weibo)** | 长文展开、原图/高码率视频、Cookie 访客 | 新增：图片 AI 升图检测、AVIF/JXL 压缩、手动 Cookie 命令行交互与持久化 |
| **X (Twitter)** | 推文/图片/视频解析、多媒体混合合并发送 | 新增：图片 AI 升图检测、修复异步下载后处理链路、全平台 AVIF 支持 |

---

## ⌨️ 专属指令说明

| 指令 | 作用描述 | 示例 |
| --- | --- | --- |
| `扫码登录B站` | 在聊天中生成 B站 登录二维码，扫码确认后自动保存 Cookie | `扫码登录B站` |
| `下载B站 <链接>` | 手动触发 B站 视频下载（在关闭自动下载时可用） | `下载B站 [https://www.bilibili.com/video/BV1xxx](https://www.bilibili.com/video/BV1xxx)` |
| `解析进度` / `升图进度` / `小红书进度` | 查询当前正在后台执行的 AI 升图/AVIF 压制/多图处理实时进度 | `/解析进度` |
| `/升图 自动` | CV 分类后按尺寸自动选择 SPAN/AVV3 模型 | `/升图 自动` |
| `/升图 AVV3` | 手动使用 AnimeVideoV3 极速动漫模型 | `/升图 AVV3` |
| `/升图 动漫自然` / `/升图 照片自然` | 手动使用轻量 SPAN 2x 模型 | `/升图 照片自然` |
| `/升图 AnimeJaNai` | 手动使用 AnimeJaNai V3.1 Balanced | `/升图 AnimeJaNai` |
| `/升图 HATGAN` | 手动使用 Real HAT GAN 最高质量档 | `/升图 HATGAN` |

---

## ⚙️ 新增/修改的核心配置项 (`_conf_schema.json`)

### B站配置 (`bili_settings`)

* `enable_auto_download` (bool): 是否自动下载视频，默认 `true`。关闭后收到 B站 链接仅解析不下载，防止与其他总结插件冲突。

### 通用 AI 升图与编码配置 (`general_settings`)

* `upscayl_bin_path` (string): Upscayl 可执行文件路径，默认 `C:/Program Files/Upscayl/resources/bin/upscayl-bin.exe`。留空或路径不可用时自动使用插件 `resources/bin/upscayl-bin.exe`。
* `upscayl_models_path` (string): Upscayl 模型目录，默认 `C:/Program Files/Upscayl/resources/models`。留空或路径不可用时自动使用插件 `resources/models`。
* `upscayl_double_pass` (bool): 是否启用 Pass1 -> Pass2 双重升图，默认 `true`。
* `upscayl_scale` (int): 单次升图倍率，支持 `1-4` 倍，默认 `2` 倍。
* `low_quality_threshold` (int): 低质量图片像素阈值，默认 `2160px`。
* `upscayl_enable_taa` (bool): 是否启用 TAA 抗锯齿（`-x`），默认 `true`。
* `span_bin_path` / `span_models_path` (string): SPAN NCNN 运行器和模型目录；留空时自动使用插件 `resources` 中的内置文件。
* `auto_upscale_max_long_edge` (int): 自动升图结果的最大长边，默认 `3840px`。
* `animejanai_bin_path` / `animejanai_models_path` (string): AnimeJaNai 外部运行器和模型目录。
* `animejanai_command_template` (string): AnimeJaNai 运行命令模板，支持 `{input}`、`{output}`、`{model}`、`{scale}`、`{models}` 占位符。
* `image_classifier.hybrid_classifier_enabled` (bool): 是否启用 CLIP 低置信度复核，默认 `true`。
* `image_classifier.classifier_provider` (string): `DirectML` 或 `CPU`，Windows 默认 `DirectML`。
* `image_classifier.classifier_models_path` (string): 分类模型目录；留空使用实例级 `models/classifier`。
* `image_classifier.classifier_auto_download` (bool): 首次启动后台下载并校验分类资源，默认 `true`。
* `image_classifier.classifier_idle_unload_seconds` (int): CLIP 空闲卸载时间，默认 `30` 秒。
* `enable_ffmpeg_compress` (bool): 是否开启全局图片压缩（默认 `true`）。
* `image_compress_format` (string): 全局图片压缩格式，`AVIF（高压缩度）` 或 `JXL（高质量）`，默认 AVIF。
* `jxl_distance` (string): JXL 有损质量参数，默认 `1.0`（视觉无损）；PNG 自动使用 `0`（无损）。
* `allow_ai_upscale_ffmpeg_concurrent` (bool): 是否允许 AI 升图与 FFmpeg 转码同时运行，默认 `false`（两者互斥，避免同时吃满 CPU/GPU）。
* `ai_upscale_max_concurrent` (int): AI 升图可同时运行的并行任务数，默认 `1`，范围 `1-8`。
* `ffmpeg_max_concurrent` (int): FFmpeg 转码可同时运行的并行任务数，默认 `1`，范围 `1-8`。

### 平台 AI 升图开关

* `xhs_settings.enable_ai_upscale` (bool): 是否对小红书图片启用自动 AI 升图，默认 `true`。
* `douyin_settings.enable_ai_upscale` (bool): 是否对抖音图片启用自动 AI 升图，默认 `true`。
* `weibo_settings.enable_ai_upscale` (bool): 是否对微博图片启用自动 AI 升图，默认 `true`。
* `twitter_settings.enable_ai_upscale` (bool): 是否对 X 图片启用自动 AI 升图，默认 `true`。
* `ffmpeg_bin_path` (string): `ffmpeg` 可执行文件路径或系统命令。
* `progress_report_interval` (int): 后台日志进度输出间隔（1%-100%），算力较低时建议设为 `10`。

---

## 🛠️ 后续跟进原版（Upstream）更新指南

当原版 `vacacia/astrbot_plugin_link_resolver` 发布新版本需要合并时，**切勿直接覆盖全套代码**。请按照以下规则保留魔改核心文件：

### 1. 绝对不能覆盖的核心自定义模块

* `core/common/media/` 文件夹（包含分类器 `classifier.py`、编码器 `encoder.py`、Upscayl 升图 `upscaler.py` 及进程监控 `process.py`）
* `MOD_DESCRIPTION.md`（魔改变更历史记录）

### 2. 需要合并（Merge）而非覆盖的文件及核心方法

| 文件路径 | 原版功能 | 本魔改版需保留的关键逻辑/方法 |
| --- | --- | --- |
| `main.py` | 插件入口与事件注册 | `_auto_clean_expired_cache` 定时清理任务、`_send_notify` 与 `_recall_notify` 消息撤回逻辑 |
| `core/common/base_mixin.py` | 基础工具类 | `_send_file_via_api` (Base64 上传)、`_ai_upscale_platform_image`、`_convert_to_avif_with_preview` |
| `core/common/commands_mixin.py` | 命令处理 | `cmd_qrcode_login_bilibili` (B站扫码)、`cmd_query_xhs_progress_*` (进度查询) |
| `core/common/config_mixin.py` | 配置刷新 | 读取 `enable_auto_download`、`upscayl` 路径、`ffmpeg` 配置及自定义字体逻辑 |
| `core/*\/handler.py` | 各平台 handler | 各平台图片下载后的 **AI 升图后处理** 以及 **AVIF/JPG 预览转换逻辑** |
| `_conf_schema.json` | 配置文件结构 | 新增的 `upscayl_*`、`ffmpeg_*`、`enable_auto_download` 等配置字段 |

### 3. 合并步骤建议

1. 拉取原版更新分支或下载原版源码。
2. 对比 `core/bilibili/handler.py`、`core/xiaohongshu/handler.py` 等主要平台解析器是否有 API 接口或解析正则的修复。
3. 复制原版的解析修复逻辑，将魔改版中的后处理代码块（`_ai_upscale_platform_image` 与 `_convert_to_avif_with_preview`）重新补在图片下载循环之后。
4. 检查并保留全中文 Emoji 日志输出。
## 📥 安装方法

进入 AstrBot 的 `data/plugins` 目录，执行以下命令克隆本项目：
外部工具依赖：

FFmpeg：需安装系统环境变量中，或在配置项 ffmpeg_bin_path 中指定绝对路径。

Upscayl：若使用旧模型或 AVV3，请安装 Upscayl 官方客户端；默认会自动寻找 `C:/Program Files/Upscayl/...`，也可使用插件 `resources` 中的运行器与模型。

配套资源：将发布包中的 `resources.zip` 解压到插件根目录，最终应存在 `resources/bin`、`resources/models`、`resources/span_models`、`resources/animejanai_models` 和 `resources/licenses`。首次启动时，AnimeJaNai 权重会自动迁移到实例目录的 `models/animejanai`；该目录与 `core`、`venv` 并列，插件和 AstrBot 实例更新都不会覆盖其中的权重。SPAN 与 AVV3 可直接使用；Windows 上 AnimeJaNai 使用内置 DirectML 运行器。

首次启动也会把 SPAN 与 Upscayl 运行器、模型迁移到实例目录的 `ai_upscale`。因此插件 GitHub 更新和 AstrBot 实例更新都不会再导致自动升图丢失资源。

分类器首次启动会在后台从独立 GitHub Release 下载约 `176MB` 的 CLIP 视觉 ONNX、类别原型、许可证和 SHA256 清单到实例目录 `models/classifier`。插件源码固定清单与资源哈希，并使用目录锁、唯一临时文件和原子替换处理插件重载；下载失败时继续使用规则 V2，不会阻断平台解析。

AnimeJaNai 模型由 [the-database/mpv-AnimeJaNai](https://github.com/the-database/mpv-AnimeJaNai) 提供，采用 `CC BY-NC-SA 4.0`：允许公开分享和非商业使用，但必须署名、保留许可证、标注修改，衍生版本需使用相同许可证。完整文本位于 `resources/licenses/AnimeJaNai-LICENSE.txt`。本插件及作者不主张这些模型权重的所有权；若未来用于收费服务、商业机器人或其他变现用途，请先移除该模型或另行取得授权。

```bash
git clone [https://github.com/Astraea35/astrbot_plugin_link_resolver_plus.git](https://github.com/Astraea35/astrbot_plugin_link_resolver_plus.git)
