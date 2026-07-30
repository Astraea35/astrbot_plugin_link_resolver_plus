================================================================================
  LinkResolver 魔改功能记录
  基于原版 v1.0.10，融合个人魔改 v1.0.9 的全部自定义功能
  最后更新: 2026-07-27 (第2版)
================================================================================

┌─────────────────────────────────────────────────────────────────────────────┐
│  🎨 魔改功能清单                                                           │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  1. 🤖 动漫分类器 (ONNX)                                                    │
│     文件: core/xiaohongshu/handler.py - AnimePhotoClassifier 类              │
│     模型: anime_classifier.onnx（插件根目录）                                │
│     说明: ONNX 推理 + CV 兜底，判断二次元/照片                               │
│     二次元用 digital-art-4x 模型升图，照片用 ultrasharp-4x                   │
│                                                                             │
│  2. 🎨 AI 升图 (Upscayl)                                                    │
│     文件: core/xiaohongshu/handler.py - _upscayl_image 方法                 │
│     说明: 低分辨率(<1080px)或模糊(<80分)自动触发双重生图+TAA                 │
│     配置: xhs_settings.* (enable_ai_upscale/upscayl_*)                      │
│                                                                             │
│  3. 🗜️ AVIF 压缩 (全平台)                                                   │
│     文件: main.py - _ffmpeg_compress_av1 / _convert_to_avif_with_preview    │
│     说明: FFmpeg libaom-av1 CRF18 高质量压缩，带7天缓存                      │
│     全平台可用: general_settings.enable_ffmpeg_compress                       │
│                                                                             │
│  4. 🖼️ JPG 全图预览 (全平台)                                                │
│     文件: main.py - _generate_jpg_preview                                   │
│     说明: AVIF 文件同时生成 JPG 预览(最长边1920px)供QQ内联显示              │
│     JPG 发 Image 组件，AVIF 发 File 组件(文件API上传)                       │
│                                                                             │
│  5. 🧹 7天缓存自动清理                                                      │
│     文件: main.py - _auto_clean_expired_cache                               │
│     说明: 每12小时后台自动清理小红书缓存中超过7天的文件                      │
│                                                                             │
│  6. 📢 处理通知+撤回                                                        │
│     文件: main.py - _send_notify / _recall_notify                           │
│     说明: B站/抖音/小红书/微博/X 处理时发送"正在解析"提示，完成后自动撤回   │
│                                                                             │
│  7. 📊 进度查询命令                                                         │
│     文件: main.py - cmd_query_xhs_progress_1/2/3                           │
│     命令: /小红书进度 / 解析进度 / 生图进度                                 │
│     说明: 实时查询当前解析/AI升图/转码进度                                  │
│                                                                             │
│  8. 📎 文件 API 上传                                                        │
│     文件: xiaohongshu/handler.py - _send_file_via_api                       │
│     说明: AVIF 等大文件通过 OneBot API (upload_group/private_file) 上传     │
│                                                                             │
│  9. 🖼️ 图文合并发送                                                        │
│     文件: main.py - xhs_image_merge_send 配置                               │
│     说明: 小红书图片笔记可选合并转发发送                                    │
│                                                                             │
│  10. 📝 全中文日志                                                          │
│     文件: 全部 handler + main.py                                            │
│     说明: 所有 logger 输出均为中文+emoji，零英文残留                         │
│                                                                             │
│                                                                             │
│                                                                             │
│  注: Twitter handler 中的 AVIF 代码块之前被错误放在图片下载循环之前(无效)，  │
│      已修复为下载后处理，同时添加 AI 升图逻辑。                              │
│                                                                             ││  12. 🎨 AI 升图 (全平台)                                                    │
│     文件: main.py - _ai_upscale_platform_image (共享辅助方法)                │
│     文件: douyin/handler.py / weibo/handler.py / twitter/handler.py         │
│     说明: 抖音/微博/X 图片也支持 AI 升图检测(分辨率→模糊度→升图)            │
│     配置: 各平台独立开关 + 阈值                                              │
│     抖音: douyin_settings.enable_ai_upscale / low_quality_threshold         │
│     微博: weibo_settings.enable_ai_upscale / low_quality_threshold          │
│     X:   twitter_settings.enable_ai_upscale / low_quality_threshold         │
│                                                                             ││  11. 🔗 多平台支持 (原版 v1.10 继承)                                        │
│     支持: B站 / 抖音 / 小红书 / 微博 / X/Twitter                            │
│     表情: 多表情回应系统(随机/顺序)                                         │
│     摘要: 文字摘要/渲染卡片可切换                                           │
│     群过滤: 黑名单/白名单模式                                               │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────┐
│  ⚠️ 下次更新原版时需注意                                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  保留以下文件/代码：                                                        │
│  - anime_classifier.onnx（模型文件）                                         │
│  - AnimePhotoClassifier 类（ONNX+CV分类器）                                  │
│  - _upscayl_image 方法                                                      │
│  - _send_file_via_api 方法                                                  │
│  - _ffmpeg_compress_av1 + _generate_jpg_preview + _convert_to_avif_with_preview │
│  - _post_process_xhs_image 方法（使用_convert_to_avif_with_preview）         │
│  - _auto_clean_expired_cache 方法 + 异步任务启动                            │
│  - _send_notify / _recall_notify 方法                                       │
│  - cmd_query_xhs_progress_1/2/3 命令                                         │
│  - _do_query_xhs_progress 方法                                              │
│  - xhs_image_merge_send 配置 + 处理逻辑                                     │
│  - 各平台 handler 中的 AVIF 后处理代码块                                    │
│  - 所有中文 logger 输出                                                     │
│                                                                             │
│  需要同步的配置字段：                                                       │
│  - xhs_settings.* (enable_ai_upscale/upscayl_*/enable_ffmpeg_compress等)    │
│  - general_settings.enable_ffmpeg_compress                                  │
│  - general_settings.ffmpeg_bin_path                                         │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘

## v1.0.10-mod (2026-07-27 第2次更新)

### 🆕 B站扫码登录
- 新增命令 扫码登录B站
- 生成 B站 扫码二维码图片，扫码并确认后自动保存 Cookie 到文件
- 重启插件后 Cookie 生效

### ⚙️ B站自动下载开关
- 配置项：ili_settings.enable_auto_download（默认开启）
- 关闭后 B站 链接不再自动下载视频（普通文本/卡片消息均跳过）
- 用于避免与视频总结类插件冲突

### ℹ️ 微博扫码登录
- ❌ 微博没有开放扫码登录 API，仅支持 Cookie 粘贴（已支持）

### 其他
- JSON 卡片中的 B站 链接也会受 enable_auto_download 控制
## 2026-07-27 修复：B站自动下载开关 + 微博 Cookie 管理

### B站自动下载开关（真正生效）
- 配置项：ili_settings.enable_auto_download（默认开启）
- 关闭后：
  - 文本消息中的 B站 链接 → 直接跳过
  - JSON 卡片中的 B站 链接 → 直接跳过
  - 日志显示：⏭️ B站自动下载已关闭，跳过处理视频链接
- 用途：避免与视频总结插件冲突

### 微博 Cookie 管理
- 扫码登录微博 → 引导手动设置方式（接口已废弃）
- 设置微博Cookie <cookie> → 手动设置并持久化保存到配置文件
- 设置后自动生效，无需重启

### 2026-07-27 (第3次): 生图/转码进度日志
- 新增后台日志进度输出：AI 升图和 AVIF 转码期间，后台日志实时显示完成百分比
- 可配置进度输出间隔：general_settings.progress_report_interval，默认每1%输出一次
- GPU较弱用户可以改为10%以减少日志量

## 📦 备份归档

**完整项目备份（保留）**：
- 路径：`C:\Users\Administrator\.astrbot_launcher\instances\81380def-5792-474b-80ae-f8994ce85134\core\data\plugins\astrbot_plugin_link_resolver_backup_20260727_163311`
- 说明：原版 v1.0.0 → v1.0.10 首次更新时创建的完整备份，包含旧版 main.py、handler.py 以及全部配置
- 用途：回滚参考 / 下次更新原版时对照差异

**关于插件内备份文件**：
- 所有 `.bak` / `_bak*.py` / `_check*.py` / `_fix*.py` 等临时文件已清理
- 仅保留当前正在运行的代码文件
- 如需历史版本对照，请参考上方备份目录
