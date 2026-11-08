# browse_bridge · astrbot_plugin_browse_bridge

把她**自己闲逛时刷到的视频**，在聊天里合适的时候带出来。

不是"引用了什么"，是"我昨天刷到个东西"。

## 它解决的缺口

`bilibili_ai_bot` 会自己看视频、自己打分、自己写观后感，存在 `bilibot.sqlite3` 里。但那是**它自己的数据库**——对话时她看不见。

市场上有两类东西，中间是空的：

| | 能自己动 | 能进对话 |
|---|---|---|
| `bili_whale` | ✅ | ❌ |
| `bilibili_eyes` | ❌ | 被动 |
| **browse_bridge** | 只读前者 | ✅ |

## 数据来源

**只读** `bilibili_ai_bot` 的库，一行都不写：

```sql
-- bilibot.sqlite3
select ... from memories where memory_type = 'video'
```

默认路径 `/AstrBot/data/plugin_data/astrbot_plugin_bilibili_ai_bot/bilibot.sqlite3`，可以用 `bili_db_path` 改。

自己的记录（哪条视频、什么时候提过、提过几次）存在自己的 `bridge.db` 里，对方的库全程 `read-only`。

## 什么时候提

两条件，满足其一：

**一、话题擦边。** 当前这条消息和视频标题做 2-gram 重叠，重叠率 ≥ `threshold`（默认 0.12）。没交集就**不提**（`inject_probability_on_miss=0`）。

**二、被问到自己。** 命中这些句式时跳过重叠检查，直接挑最新那条：

```
你今天看什么了 / 最近刷到啥 / 你在干嘛 / 有什么好玩的 / 这两天忙什么
```

第二条是 0.1.2 加的——最该答的场景，恰恰是话题词最不重叠的时候。

## 怎么提

注入走 `req.extra_user_content_parts` + `mark_as_temp()`，**从不碰 `system_prompt`**，前缀缓存不受影响。

提出来的是这个形状：

```
<刚刷到的>
下面这条视频是你自己闲逛时刷到的，不是别人发给你的。
标题：两个鸡蛋包装结构设计
UP主：xxx
分区：生活
你当时的想法：以特写展示双手操作纸质盒子……
你的评分：7.5/10

如果这会儿聊天场合合适，可以顺口提一句（比如"我昨天刷到个视频""刚看到个东西"这种）。
- 别贴链接，别说"推荐你也去看"
- 别背标题，对方问起是什么再说
- 一条消息最多带到一次，不提也行
</刚刷到的>
```

## 节流

三道闸，防止她变成复读机：

| 闸 | 默认 |
|---|---|
| 每天最多提几次 | `daily_limit=2` |
| 两次之间至少隔多久 | `cooldown_minutes=90` |
| 同一条视频多少天内不再提 | `reuse_days=7` |

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `enable_inject` | 开 | 总开关 |
| `bili_db_path` | 自动 | bilibili_ai_bot 的库路径 |
| `threshold` | 0.12 | 2-gram 重叠率门槛 |
| `intent_probability` | 0.9 | 命中"问自己"时直接提的概率 |
| `inject_probability_on_miss` | 0.0 | 没交集时硬提的概率，**建议保持 0** |
| `daily_limit` | 2 | 每日上限 |
| `cooldown_minutes` | 90 | 冷却 |
| `reuse_days` | 7 | 同一条视频的复用窗口 |
| `max_age_hours` | 72 | 多久以前看过的还算"刚刷到" |
| `min_score` | 6.0 | 低于这个分的视频不提 |
| `max_chars_title` | 60 | 标题截断 |

## 安装

放到 `AstrBot/data/plugins/astrbot_plugin_browse_bridge/`，重启。

依赖 `astrbot_plugin_bilibili_ai_bot`（数据源）和 `astrbot_plugin_message_recorder`（可选，用于"上次聊到这条是在哪"）。

## 已知边界

- 对方库的表结构一旦改名，这里会静默拿不到数据（日志里有 warning）
- 只读 `memories`，不看 `watch_log.json`（那个是 cap=200 的滚动缓冲）
- 没有向量检索，2-gram 判断"擦边"很粗——好处是零成本、不额外调模型

## 版本

- `0.1.2` 加"被问到自己"的意图识别，跳过重叠检查
- `0.1.1` 节流三件套、评分门槛
- `0.1.0` 首版：只读 + 注入
