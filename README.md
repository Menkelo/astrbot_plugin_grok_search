# Grok 搜索 (astrbot_plugin_grok_search)

一个 AstrBot 插件：通过 [chenyme/grok2api](https://github.com/chenyme/grok2api) 的服务端搜索工具，为聊天平台提供**联网搜索**与 **X（推特）搜索**，回复末尾自动附带参考链接。

从 [astrbot_plugin_zssm_core](https://github.com/Menkelo/astrbot_plugin_zssm_core) v1.8.0 的搜索功能独立拆分而来。

---

## 功能

- **联网搜索**（`web_search` 工具）：`/search 今天北京天气`
- **X（推特）搜索**（`x_search` 工具）：`/xsearch 马斯克最新动态`，支持配置日期范围
- 回复末尾自动附带从响应引用（`url_citation`）中提取的「参考链接」列表（标题 + URL，去重）
- 回复格式自动降级为 QQ 等纯文本客户端友好的排版（剥离 markdown 标记、链接转裸 URL）
- 支持回复一条消息后再发指令，被回复的文本会作为搜索上下文
- 请求失败自动重试、超时保护、耗时显示

## 指令

| 指令 | 别名 | 说明 |
|------|------|------|
| `/search <问题>` | `/搜索`、`/联网搜索` | 联网搜索 |
| `/xsearch <问题>` | `/x搜索`、`/推特搜索`、`/搜推特` | X（推特）搜索 |

示例：

```
/search 今天有什么科技新闻
/搜索 一下上海天气
/xsearch 马斯克最新推文
/推特搜索 世界杯
```

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `grok2api_base_url` | string | (空) | grok2api 服务地址，如 `http://127.0.0.1:8000`（可含/不含 `/v1`，自动补全）。 |
| `grok2api_api_key` | string | (空) | grok2api 后台创建的客户端 API Key（Bearer 方式携带）。 |
| `grok2api_model` | string | `grok-4` | grok2api 中配置的公开模型名。 |
| `grok2api_x_from_date` | string | (空) | X 搜索起始日期（YYYY-MM-DD），留空不限。 |
| `grok2api_x_to_date` | string | (空) | X 搜索结束日期（YYYY-MM-DD），留空不限。 |
| `llm_timeout_sec` | int | `90` | 搜索请求超时（秒）。 |
| `llm_retry_times` | int | `2` | 失败重试次数（含首次）。 |
| `show_cost` | bool | `true` | 回复末尾显示耗时。 |

## 依赖

- 需要 Python 3.10+ 与 `aiohttp`（安装插件时自动安装）。
- 需要自行部署 [grok2api](https://github.com/chenyme/grok2api)，并在其中创建客户端 API Key。

## 已知限制

- **X 搜索需要 grok2api 内的账号类型支持**：Console/Build 类账号支持 `x_search`；Web 类（grok.com 网页逆向）账号仅支持联网搜索，使用 `/xsearch` 会收到上游报错。
- 引用链接依赖上游返回的 `annotations` 字段，个别模型/账号可能不带引用。

## 致谢

- [chenyme/grok2api](https://github.com/chenyme/grok2api) - Grok 逆向 API 项目
- [astrbot_plugin_zssm_core](https://github.com/Menkelo/astrbot_plugin_zssm_core) - 本插件由此拆分
