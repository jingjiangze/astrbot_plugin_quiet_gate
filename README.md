# astrbot_plugin_quiet_gate · 表情暂存

> 表情是情绪，不是信息。先 hold 住，等对方把话说完，再带着情绪回。

## 它解决什么问题

群里常见的对话是这样：

```
A: 😭
A: 我今天面试又挂了
```

普通机器人会对第一条 `😭` 单独回一句「怎么啦？」，显得很急、也很笨；或者干脆把表情当噪音丢掉，第二条就少了情绪背景。

这个插件把顺序倒过来：

1. 只收到表情 → **不回复**，把表情和它表达的情绪**暂存**起来
2. 同一用户 `TTL` 内发来文字 → 把情绪作为背景写进这次的 system prompt，模型「带着情绪」回那句话
3. `TTL` 内没等到文字 → **自动失效**，不追问、不补回

## 特性

- **只拦 LLM，不拦事件流**：用 `on_llm_request` + `stop_event`，所以表情包采集、图搜等其它插件照常工作
- **不臆测情绪**：unicode emoji 走可靠映射；QQ 表情按 id 查表，认不出来的标「情绪不明」，交给模型结合语境判断
- **分用户画像**：按用户累计「表情 → 情绪」次数，样本够了才在提示里补一句「此人的表情历史以 大笑×3、难过×2 为主」
- **超时失效**：TTL 可配，过期的暂存直接丢弃
- **作用域可控**：私聊开关、群白名单/黑名单
- **可热改映射表**：`plugin_data/astrbot_plugin_quiet_gate/emotion_map.json` 随时补充自己的表情映射

## 安装

AstrBot WebUI → 插件市场 → 从仓库安装：

```
https://github.com/jingjiangze/astrbot_plugin_quiet_gate
```

或直接放进 `data/plugins/astrbot_plugin_quiet_gate/` 后重载插件。

要求 AstrBot `>= 4.29.0`。

## 配置

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enable` | bool | true | 总开关 |
| `ttl_seconds` | int | 120 | 暂存有效期（秒） |
| `profile_min_samples` | int | 5 | 画像注入门槛 |
| `max_pending_per_user` | int | 4 | 每用户最多暂存几个表情 |
| `private_enable` | bool | true | 私聊是否生效 |
| `group_whitelist` | list | [] | 只在这些群生效，空=全部 |
| `group_blacklist` | list | [] | 这些群不生效，优先级高于白名单 |
| `unknown_label` | string | 情绪不明 | 未收录的表情标签 |
| `show_hint_to_user` | bool | false | 调试用，回显识别结果 |

## 命令

| 命令 | 权限 | 作用 |
|---|---|---|
| `/表情暂存` | 所有人 | 查看自己当前的暂存状态与情绪 |
| `/表情画像` | 所有人 | 查看自己的表情情绪统计 |
| `/表情画像清除` | 管理员 | 清空全部画像数据 |

## 判定规则

一条消息满足以下任一条件 → 正常处理（不暂存）：

- 含 `@机器人`
- 去掉标点、emoji、`[表情]`/`[图片]` 等占位符后，仍有 ≥1 个文字字符

否则（纯表情 / 纯贴纸 / 纯图片 / 纯标点空白）→ 暂存情绪，跳过本次 LLM。

## 数据文件

都在 `data/plugin_data/astrbot_plugin_quiet_gate/`：

- `pending.json` — 暂存队列（带 TTL，读写时自动清理过期项）
- `profile.json` — 用户画像 `{sender_id: {counts, total, updated}}`
- `emotion_map.json` — 可编辑的情绪映射表，格式：

```json
{
  "qq_face": {"14": "难过", "178": "大笑"},
  "unicode": {"😂": "大笑", "😭": "难过"}
}
```

改完重载插件生效。

## 已知边界

- 情绪映射是**启发式**的，QQ 表情 id 未收录时只能标「情绪不明」；想更准就补 `emotion_map.json`
- 暂存按「会话 + 发送者」隔离，跨群不共享
- 不支持把表情本身当成图片内容去理解（那是视觉模型的活，不是这个插件）

## License

MIT
