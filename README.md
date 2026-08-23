# Zssm(Grok) — astrbot_plugin_grok_search

[astrbot_plugin_zssm_core](https://github.com/Menkelo/astrbot_plugin_zssm_core) 的 **grok2api 版**：完整的 zssm 体验（解释文本 / 图片 / QQ 群文件 / 合并转发），LLM 调用全部直连 [chenyme/grok2api](https://github.com/chenyme/grok2api)，并支持通过其服务端工具进行**联网搜索**、**X（推特）搜索**与**组合搜索**。

> v2.0.0 起本插件从"纯搜索插件"升级为完整 zssm 替代，不再依赖 AstrBot Provider。

---

## 功能

- **文本解释**：`zssm 什么是量子纠缠` —— 详细解释，字数不限
- **图片解释（视觉）**：`zssm [图片]` 或回复图片后发 `zssm` —— 远程图片（QQ CDN 等）会先在插件侧下载并转为 data URI 再发送，避免服务端抓取失败；下载失败的图片自动跳过，不影响本次回答
- **QQ 群文件解释**：回复群文件后发 `zssm`，文本类读取内容预览，PDF 转 Markdown（需 PyMuPDF，可选）
- **合并转发解释**：回复聊天记录后发 `zssm`，展开全部节点整段解释
- **联网搜索**：`zssm 搜索今天的天气`（`web_search` 服务端工具）；开启配置 `search_include_x` 后自动同时开启 X 搜索
- **X（推特）搜索**：`zssm x搜索马斯克最新动态`（`x_search` 服务端工具）
- **组合搜索**：`zssm 全搜今天大事` —— 一次请求同时开启联网 + X 搜索，综合两边信息作答
- **带图搜索**：`zssm 搜索一下x上面有没有 [图片]` —— 搜索指令可附带图片，模型先识别图中人物/物品再进行搜索
- 回复格式自动降级为 QQ 等纯文本客户端友好排版，保留 `**关键词**` / `**详细阐述**` 小节标题
- 配置页模型选择：填好地址与密钥后重载插件，`grok2api_model` 自动变为下拉列表（来自 `/v1/models`）

## 触发方式（与 zssm_core 一致）

- **指令**：`/zssm`（别名：`知识说明`、`解释`）
- **关键词**：消息文本中包含 `zssm` 时自动触发，可通过配置项 `enable_keyword_zssm` 关闭
- **搜索指令**：在 `zssm` 后跟 `搜索/联网/查一下…`（联网）、`x搜索/搜推特/推特搜索…`（X）、`全搜/全网搜索/混合搜索…`（组合）

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `enable_keyword_zssm` | bool | `true` | 是否启用“zssm”关键词自动触发。 |
| `grok2api_base_url` | string | (空) | grok2api 服务地址，如 `http://127.0.0.1:8000`（可含/不含 `/v1`，自动补全）。 |
| `grok2api_api_key` | string | (空) | grok2api 后台创建的客户端 API Key（Bearer 方式携带）。 |
| `grok2api_model` | string | `grok-4` | grok2api 中配置的公开模型名；解释图片需选择支持视觉的模型。配置好地址与密钥并**重载插件**后，此栏自动变为下拉列表（读取自 grok2api `/v1/models`）。 |
| `search_include_x` | bool | `false` | 开启后普通「zssm 搜索xxx」同时开启联网 + X 搜索（等同“全搜”）。 |
| `llm_timeout_sec` | int | `90` | LLM 调用超时（秒）。 |
| `llm_retry_times` | int | `2` | 失败重试次数（含首次）。 |
| `show_cost` | bool | `true` | 回复末尾显示耗时。 |
| `file_preview_max_size_kb` | int | `100` | 群文件内容预览最大文件大小（KB）。 |
| `file_preview_exts` | string | `txt,md,log,…` | 群文件内容预览的文本扩展名（逗号分隔）。 |

## 依赖

- **必需**：`aiohttp`（安装插件时自动安装）
- **可选**：`PyMuPDF`（PDF 转 Markdown，效果更好）/ `PyPDF2`（备选 PDF 解析）
- 需要自行部署 [grok2api](https://github.com/chenyme/grok2api)，并在其中创建客户端 API Key

## 已知限制

- **X 搜索需要 grok2api 内的账号类型支持**：Console/Build 类账号支持 `x_search`；Web 类（grok.com 网页逆向）账号仅支持联网搜索，`x搜索`/`全搜` 会收到上游报错。
- 图片解释依赖所选模型的视觉能力；本地/远程图片统一经 Pillow 解码校验并重编码（带透明保 PNG、其余转 JPEG，超大等比缩小，单张上限 8MB、远程下载超时 15 秒），损坏图片自动跳过，不会再触发上游 `invalid_image` 报错。
- 与 zssm_core 同时安装时会通过 `zssm_handled` 标记互相去重（先到先得），建议只启用其一。

## 致谢

- [薄暝](https://github.com/xiaoxi68) - 原始插件 `astrbot_zssm_explain` 的开发者
- [chenyme/grok2api](https://github.com/chenyme/grok2api) - Grok 逆向 API 项目
