"""browse_bridge —— 把 bilibili_ai_bot 真实看过的视频，按时机塞进对话。

设计要点（和项目书 §7.6 对齐）：
1. 只读 bilibili_ai_bot 的 sqlite（memories 表 memory_type='video'），不改对方插件、不写对方的库。
2. 注入走 extra_user_content_parts + mark_as_temp()，绝不碰 system_prompt —— 前缀缓存不受影响。
3. 自己在 plugin_data 下维护一张 mentions 表，记录"哪条视频、什么时候说过、说了几次"，
   用来做去重与冷却；对方的库保持只读。
4. 节流三件套：每日上限、两次间隔、同一条视频 N 天内不重复。
5. 话题擦边才提（bigram 重叠），无交集时默认不提（inject_probability_on_miss=0）。
6. 0.1.2 新增「被问到自己」的意图识别：问"今天看了什么/最近刷到啥"时，
   不再要求话题词重叠，直接挑最新那条注入 —— 否则最该答的场景反而答不出来。
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register
from astrbot.core.agent.message import TextPart
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_NAME = "astrbot_plugin_browse_bridge"
DEFAULT_BILI_DB = (
    "/AstrBot/data/plugin_data/astrbot_plugin_bilibili_ai_bot/bilibot.sqlite3"
)

TEMPLATE = """<刚刷到的>
下面这条视频是你自己闲逛时刷到的，不是别人发给你的。
标题：{title}
UP主：{up}
分区：{tname}
你当时的想法：{review}
你的评分：{score}/10

如果这会儿聊天场合合适，可以顺口提一句（比如“我昨天刷到个视频”“刚看到个东西”这种）。
- 别贴链接，别说“推荐你也去看”
- 别背标题，对方问起是什么再说
- 一条消息最多带到一次，不提也行
</刚刷到的>"""

# 对方在问"你自己的事"——这类时候不提视频就是答非所问。
# 命中任意一条，就跳过 bigram 重叠要求，直接挑最新的那条。
INTENT_PATTERNS = (
    r"(今天|刚刚|刚才|昨晚|昨天|最近|这两天|这几天).{0,8}(看|刷|逛|干|做|忙)(了|到|过)?(什么|啥|嘛|点啥)",
    r"(看|刷)(了|到|过)?(什么|啥)(好看|好玩|有意思)?",
    r"你.{0,6}(在看|看了|刷到|刷了|逛到|忙什么|干嘛|在干什么)",
    r"有什么(好看|好玩|有意思)的",
    r"(在|忙)(什么|啥)呢",
    r"(最近|这两天).{0,4}(在|忙|看)",
)


def _bigrams(text: str) -> set[str]:
    """粗粒度中文/英文 2-gram，够用来判断"话题擦边"。"""
    cleaned = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]+", "", text or "")
    if len(cleaned) < 2:
        return set()
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def _wants_self_report(text: str) -> bool:
    tail = (text or "")[-200:]
    return any(re.search(p, tail) for p in INTENT_PATTERNS)


@register(
    PLUGIN_NAME,
    "Contt99",
    "把 bilibili_ai_bot 真实看过的视频带进对话（只读对方数据库）",
    "0.1.2",
)
class BrowseBridge(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.conf: dict[str, Any] = dict(config or {})
        self.data_dir = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "bridge.db"
        self._init_db()
        logger.info(
            f"[browse_bridge] 就绪 v0.1.2；bili_db={self.conf.get('bili_db_path', DEFAULT_BILI_DB)}"
        )

    # ---------- 自己的记录库 ----------

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=5)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self) -> None:
        with self._conn() as con:
            con.execute(
                """create table if not exists mentions(
                    memory_id integer primary key,
                    mention_count integer not null default 0,
                    last_at real not null
                )"""
            )
            con.execute(
                "create table if not exists counters(k text primary key, v real not null)"
            )
            con.execute(
                "create table if not exists kv(k text primary key, v text not null)"
            )

    def _get(self, table: str, key: str) -> float | str | None:
        with self._conn() as con:
            row = con.execute(f"select v from {table} where k=?", (key,)).fetchone()
        return row["v"] if row else None

    def _put(self, table: str, key: str, value: float | str) -> None:
        with self._conn() as con:
            con.execute(
                f"insert into {table}(k, v) values(?, ?) "
                f"on conflict(k) do update set v=excluded.v",
                (key, value),
            )

    # ---------- 只读对方数据库 ----------

    def _bili_conn(self) -> sqlite3.Connection | None:
        path = self.conf.get("bili_db_path") or DEFAULT_BILI_DB
        if not Path(path).exists():
            return None
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3)
            con.row_factory = sqlite3.Row
            return con
        except sqlite3.Error as exc:
            logger.debug(f"[browse_bridge] 打开 bili 库失败：{exc}")
            return None

    def _candidates(self) -> list[dict[str, Any]]:
        con = self._bili_conn()
        if con is None:
            return []
        try:
            max_age = int(self.conf.get("max_age_hours", 48))
            since = time.time() - max_age * 3600
            rows = con.execute(
                "select id, text, meta, importance, value_score, created_at "
                "from memories where memory_type='video' and created_at > ? "
                "order by created_at desc limit 60",
                (since,),
            ).fetchall()
        except sqlite3.Error as exc:
            logger.debug(f"[browse_bridge] 查询 memories 失败：{exc}")
            return []
        finally:
            con.close()

        out: list[dict[str, Any]] = []
        min_score = float(self.conf.get("min_score", 5))
        for row in rows:
            meta_raw = row["meta"]
            try:
                meta = json.loads(meta_raw) if meta_raw else {}
            except (TypeError, ValueError):
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            # 1.7.3 起 memories.meta 外面包了一层 {"legacy": {...实际字段...}}
            inner = meta.get("legacy")
            if isinstance(inner, dict):
                meta = inner
            score = meta.get("score")
            try:
                score = float(score) if score is not None else None
            except (TypeError, ValueError):
                score = None
            if score is not None and score < min_score:
                continue
            out.append(
                {
                    "id": int(row["id"]),
                    "text": row["text"] or "",
                    "title": meta.get("video_title") or meta.get("title") or "",
                    "up": meta.get("owner_name") or "",
                    "tname": meta.get("tname") or "",
                    "review": meta.get("review") or "",
                    "mood": meta.get("mood") or "",
                    "score": score,
                    "created_at": float(row["created_at"] or 0),
                }
            )
        return out

    def _recently_mentioned(self, ids: list[int]) -> set[int]:
        if not ids:
            return set()
        reuse_days = int(self.conf.get("reuse_days", 7))
        marks = ",".join("?" * len(ids))
        with self._conn() as con:
            rows = con.execute(
                f"select memory_id, last_at from mentions where memory_id in ({marks})",
                ids,
            ).fetchall()
        if reuse_days <= 0:
            return {int(r["memory_id"]) for r in rows}
        cutoff = time.time() - reuse_days * 86400
        return {int(r["memory_id"]) for r in rows if float(r["last_at"]) > cutoff}

    # ---------- 节流 ----------

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def _daily_count(self) -> int:
        raw = self._get("counters", f"day:{self._today()}")
        return int(raw or 0)

    def _bump_daily(self) -> None:
        self._put("counters", f"day:{self._today()}", self._daily_count() + 1)

    def _last_at(self) -> float:
        return float(self._get("counters", "last_inject_at") or 0)

    # ---------- 主逻辑 ----------

    def _pick(self, prompt_tail: str, intent: bool) -> dict[str, Any] | None:
        items = self._candidates()
        if not items:
            return None
        skip = self._recently_mentioned([i["id"] for i in items])
        items = [i for i in items if i["id"] not in skip]
        if not items:
            return None

        # 被问到自己（今天看了什么 / 最近刷到啥）→ 别再等词面重叠，挑最新的
        if intent:
            return max(items, key=lambda i: i["created_at"])

        user_bg = _bigrams(prompt_tail)
        scored: list[tuple[int, dict[str, Any]]] = []
        for item in items:
            text = f"{item['title']} {item['tname']} {item['review']} {item['text']}"
            overlap = len(user_bg & _bigrams(text))
            if overlap >= 2:
                scored.append((overlap, item))

        if scored:
            scored.sort(key=lambda x: (x[0], x[1]["created_at"]), reverse=True)
            return scored[0][1]

        miss_p = float(self.conf.get("inject_probability_on_miss", 0.0) or 0.0)
        if miss_p > 0 and random.random() < miss_p:
            return max(items, key=lambda i: i["created_at"])
        return None

    @filter.on_llm_request(priority=900)
    async def inject(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.conf.get("enable", True):
            return

        now = time.time()
        cooldown = int(self.conf.get("cooldown_minutes", 90)) * 60
        if now - self._last_at() < cooldown:
            return
        if self._daily_count() >= int(self.conf.get("daily_limit", 2)):
            return

        prompt = getattr(req, "prompt", "") or ""
        tail = prompt[-400:]
        intent = _wants_self_report(tail)
        item = self._pick(tail, intent)
        if item is None:
            return

        prob = (
            float(self.conf.get("intent_probability", 0.9))
            if intent
            else float(self.conf.get("probability", 0.5))
        )
        if random.random() > prob:
            return

        block = TEMPLATE.format(
            title=item["title"] or "（没记标题）",
            up=item["up"] or "不知道谁",
            tname=item["tname"] or "日常",
            review=item["review"] or (item["text"][:120] or "看着还行"),
            score=item["score"] if item["score"] is not None else "没打分",
        )
        req.extra_user_content_parts.append(TextPart(text=block).mark_as_temp())

        with self._conn() as con:
            con.execute(
                "insert into mentions(memory_id, mention_count, last_at) values(?, 1, ?) "
                "on conflict(memory_id) do update set "
                "mention_count = mention_count + 1, last_at = excluded.last_at",
                (item["id"], now),
            )
        self._put("counters", "last_inject_at", now)
        self._bump_daily()
        logger.info(
            f"[browse_bridge] 注入 1 条{'（意图命中）' if intent else ''}："
            f"mem#{item['id']} 《{item['title'][:30]}》 score={item['score']} "
            f"今日第 {self._daily_count()} 次"
        )
