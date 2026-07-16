#!/usr/bin/env python3
"""
============================================================
竞品情报自动化系统 — 主脚本
============================================================
功能：
  1. 自动采集竞品动态（GitHub Releases + Hacker News）
  2. 用 AI 进行摘要和分类
  3. 生成结构化 Markdown 日报 + HTML 可视化报告
  4. 自动推送到 Slack / 飞书 / 企业微信 / Telegram / 邮件

使用方法：
  python competitive_intel.py

首次运行（仅测试采集，不需要 API Key）：
  python competitive_intel.py --collect-only
============================================================
"""

import os
import sys
import json
import hashlib
import logging
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

# 第三方依赖
try:
    import yaml
    import requests
    import feedparser
except ImportError as e:
    print(f"\n[错误] 缺少依赖包，请先运行：")
    print(f"  pip install -r requirements.txt")
    print(f"\n详细错误: {e}\n")
    sys.exit(1)

# ============================================================
# 日志配置
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)

# ============================================================
# 第一部分：配置加载
# ============================================================
class Config:
    """加载和管理配置文件"""

    def __init__(self, config_path="config.yaml"):
        self.path = config_path
        if not os.path.exists(config_path):
            logger.error(f"配置文件不存在: {config_path}")
            sys.exit(1)
        with open(config_path, "r", encoding="utf-8") as f:
            self.data = yaml.safe_load(f)
        logger.info(f"配置已加载: {config_path}")

    def get(self, *keys, default=None):
        """嵌套取值，如 config.get('ai', 'provider')"""
        result = self.data
        for key in keys:
            if isinstance(result, dict) and key in result:
                result = result[key]
            else:
                return default
        return result

    @property
    def competitors(self):
        return self.get("competitors", default=[])

    @property
    def rsshub_base(self):
        return self.get("rsshub", "base_url", default="https://rsshub.app")

    @property
    def hn_enabled(self):
        return self.get("hackernews", "enabled", default=True)

    @property
    def hn_min_points(self):
        return self.get("hackernews", "min_points", default=20)

    @property
    def ai_provider(self):
        return self.get("ai", "provider", default="gemini")

    @property
    def categories(self):
        return self.get("categories", default={})

# ============================================================
# 第二部分：数据采集器
# ============================================================
class Collector:
    """从多个渠道采集竞品动态"""

    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "CompetitiveIntelBot/1.0"
        })

    def collect_all(self):
        """执行全部采集，返回原始数据列表"""
        all_items = []

        # 1. GitHub Releases
        logger.info("=" * 50)
        logger.info("开始采集 GitHub Releases...")
        gh_items = self._collect_github_releases()
        all_items.extend(gh_items)
        logger.info(f"GitHub Releases 采集完成: {len(gh_items)} 条")

        # 2. Hacker News 提及
        if self.config.hn_enabled:
            logger.info("=" * 50)
            logger.info("开始采集 Hacker News 提及...")
            hn_items = self._collect_hackernews()
            all_items.extend(hn_items)
            logger.info(f"Hacker News 采集完成: {len(hn_items)} 条")

        # 去重
        all_items = self._deduplicate(all_items)
        logger.info(f"去重后总计: {len(all_items)} 条")

        return all_items

    def _collect_github_releases(self):
        """从 GitHub Atom Feed 采集竞品 Release"""
        items = []
        max_per_comp = self.config.get("ai", "max_items_per_competitor", default=3)

        for comp in self.config.competitors:
            name = comp.get("name", "")
            github = comp.get("github")
            if not github:
                logger.info(f"  [{name}] 无 GitHub 仓库，跳过")
                continue

            # 尝试 Atom Feed
            atom_url = f"https://github.com/{github}/releases.atom"
            try:
                resp = self.session.get(atom_url, timeout=15)
                if resp.status_code == 404:
                    # 仓库没有 releases，尝试 commits
                    atom_url = f"https://github.com/{github}/commits.atom"
                    resp = self.session.get(atom_url, timeout=15)

                if resp.status_code != 200:
                    logger.warning(f"  [{name}] GitHub 返回 {resp.status_code}")
                    continue

                feed = feedparser.parse(resp.content)
                count = 0
                for entry in feed.entries[:max_per_comp]:
                    # 清理 HTML 标签
                    summary_raw = entry.get("summary", "")
                    summary_clean = self._strip_html(summary_raw)

                    items.append({
                        "competitor": name,
                        "title": entry.get("title", "").strip(),
                        "summary": summary_clean[:2000],
                        "link": entry.get("link", ""),
                        "published": entry.get("published", ""),
                        "source": "GitHub",
                        "content_hash": self._hash(
                            entry.get("title", "") + entry.get("link", "")
                        ),
                    })
                    count += 1
                logger.info(f"  [{name}] 获取 {count} 条 Release")

            except requests.RequestException as e:
                logger.warning(f"  [{name}] GitHub 请求失败: {e}")

        return items

    def _collect_hackernews(self):
        """从 HN Algolia API 采集竞品提及"""
        items = []
        min_points = self.config.hn_min_points
        max_per_comp = self.config.get("ai", "max_items_per_competitor", default=3)

        for comp in self.config.competitors:
            name = comp.get("name", "")
            keywords = comp.get("keywords", [])
            if not keywords:
                continue

            for kw in keywords:
                try:
                    resp = self.session.get(
                        "https://hn.algolia.com/api/v1/search",
                        params={
                            "query": kw,
                            "tags": "story",
                            "hitsPerPage": max_per_comp,
                        },
                        timeout=15,
                    )
                    if resp.status_code != 200:
                        logger.warning(f"  [{name}] HN 返回 {resp.status_code}")
                        continue

                    data = resp.json()
                    count = 0
                    for hit in data.get("hits", []):
                        points = hit.get("points", 0) or 0
                        if points < min_points:
                            continue

                        created_ts = hit.get("created_at_i", 0)
                        published = datetime.fromtimestamp(
                            created_ts, tz=timezone.utc
                        ).isoformat() if created_ts else ""

                        # 清理 HTML 标签和实体
                        raw_text = hit.get("story_text") or ""
                        clean_text = self._strip_html(raw_text) if raw_text else ""

                        items.append({
                            "competitor": name,
                            "title": hit.get("title", "") or hit.get("story_title", ""),
                            "summary": clean_text[:2000] or hit.get("title", ""),
                            "link": f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}",
                            "published": published,
                            "source": "Hacker News",
                            "points": points,
                            "content_hash": self._hash(
                                hit.get("title", "") + hit.get("objectID", "")
                            ),
                        })
                        count += 1

                    if count > 0:
                        logger.info(f"  [{name}] HN 关键词 '{kw}': {count} 条 (≥{min_points} 赞)")

                except requests.RequestException as e:
                    logger.warning(f"  [{name}] HN 请求失败: {e}")

        return items

    def _deduplicate(self, items):
        """根据 content_hash 去重"""
        seen = set()
        unique = []
        for item in items:
            h = item.get("content_hash", "")
            if h and h not in seen:
                seen.add(h)
                unique.append(item)
        return unique

    @staticmethod
    def _strip_html(text):
        """简单去除 HTML 标签和实体"""
        import re
        import html as html_module
        clean = re.sub(r"<[^>]+>", "", text)
        clean = html_module.unescape(clean)  # 解码 &#x2F; &#x27; 等
        clean = re.sub(r"\s+", " ", clean).strip()
        return clean

    @staticmethod
    def _hash(text):
        return hashlib.md5(text.encode("utf-8")).hexdigest()

# ============================================================
# 第三部分：AI 处理器
# ============================================================
class AIProcessor:
    """用 LLM 对竞品新闻进行摘要和分类"""

    SYSTEM_PROMPT = """你是一名 AI Coding 行业的竞品分析专家。请对以下竞品新闻进行摘要和分类。

任务要求：
1. 用 1-2 句话概括核心内容（中文）
2. 从以下类别中选择最匹配的一个：
   feature_update, strategy_change, pricing_change, partnership, personnel_change, funding, other
3. 提取 2-3 个关键词
4. 判断重要性（high / medium / low）：
   - high: 直接影响产品策略或市场格局
   - medium: 值得关注但不紧急
   - low: 常规更新

只输出 JSON，格式如下，不要输出其他任何内容：
{"summary": "一句话摘要", "category": "类别代码", "keywords": ["关键词1", "关键词2"], "importance": "high/medium/low", "competitor": "竞品名称"}
"""

    def __init__(self, config: Config):
        self.config = config
        self.provider = config.ai_provider
        self._client = None

    def process_batch(self, items):
        """批量处理竞品动态"""
        if not items:
            logger.info("没有需要处理的数据")
            return []

        logger.info(f"开始 AI 处理，共 {len(items)} 条，提供商: {self.provider}")
        results = []

        for i, item in enumerate(items, 1):
            logger.info(f"  处理 {i}/{len(items)}: {item.get('title', '')[:40]}...")
            try:
                ai_result = self._call_ai(item)
                item.update(ai_result)
            except Exception as e:
                logger.warning(f"    AI 处理失败: {e}")
                item["summary"] = item.get("title", "")[:60]
                item["category"] = "other"
                item["keywords"] = []
                item["importance"] = "low"
            results.append(item)

        return results

    def _call_ai(self, item):
        """调用 AI API"""
        content = f"标题: {item.get('title', '')}\n内容: {item.get('summary', '')[:1500]}\n来源: {item.get('source', '')}"

        if self.provider == "gemini":
            return self._call_gemini(content, item.get("competitor", ""))
        elif self.provider == "openai":
            return self._call_openai(content, item.get("competitor", ""))
        else:
            raise ValueError(f"未知的 AI 提供商: {self.provider}")

    def _call_gemini(self, content, competitor_name):
        """调用 Google Gemini API（免费层）"""
        try:
            import google.generativeai as genai
        except ImportError:
            raise ImportError("缺少 google-generativeai 包，请运行: pip install google-generativeai")

        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("环境变量 GEMINI_API_KEY 未设置")

        genai.configure(api_key=api_key)
        model_name = self.config.get("ai", "gemini_model", default="gemini-2.0-flash")
        model = genai.GenerativeModel(model_name)

        prompt = f"{self.SYSTEM_PROMPT}\n\n竞品名称: {competitor_name}\n\n新闻内容:\n{content}"
        resp = model.generate_content(prompt)
        text = resp.text.strip()

        return self._parse_json(text)

    def _call_openai(self, content, competitor_name):
        """调用 OpenAI API"""
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("缺少 openai 包，请运行: pip install openai")

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("环境变量 OPENAI_API_KEY 未设置")

        if self._client is None:
            self._client = OpenAI(api_key=api_key)

        model_name = self.config.get("ai", "openai_model", default="gpt-4.1-mini")

        resp = self._client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": f"竞品名称: {competitor_name}\n\n新闻内容:\n{content}"},
            ],
            response_format={"type": "json_object"},
            max_tokens=500,
        )

        text = resp.choices[0].message.content.strip()
        return self._parse_json(text)

    @staticmethod
    def _parse_json(text):
        """解析 AI 返回的 JSON，容错处理"""
        # 去除可能的 markdown 代码块标记
        if "```" in text:
            parts = text.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    text = part
                    break
        text = text.strip()

        data = json.loads(text)
        return {
            "summary": data.get("summary", ""),
            "category": data.get("category", "other"),
            "keywords": data.get("keywords", []),
            "importance": data.get("importance", "medium"),
            "competitor": data.get("competitor", ""),
        }

# ============================================================
# 第四部分：报告生成器
# ============================================================
class Reporter:
    """生成结构化的竞品情报报告"""

    IMPORTANCE_ORDER = {"high": 0, "medium": 1, "low": 2}
    IMPORTANCE_LABEL = {"high": "🔴 高", "medium": "🟡 中", "low": "🟢 低"}

    def __init__(self, config: Config):
        self.config = config
        self.categories = config.categories

    def generate_markdown(self, items):
        """生成 Markdown 格式的报告"""
        date_str = datetime.now().strftime("%Y-%m-%d")
        prefix = self.config.get("report", "title_prefix", default="竞品情报日报")

        # 按类别分组
        groups = {}
        for item in items:
            cat = item.get("category", "other")
            groups.setdefault(cat, []).append(item)

        md = f"# {prefix} - {date_str}\n\n"
        md += f"> 自动采集时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        md += f"> 共收集 **{len(items)}** 条竞品动态\n\n"
        md += "---\n\n"

        if not items:
            md += "今日暂无竞品动态。\n"
            return md

        # 按类别输出
        for cat_code in self.categories:
            cat_items = groups.get(cat_code, [])
            if not cat_items:
                continue

            cat_name = self.categories[cat_code]
            # 按重要性排序
            cat_items.sort(key=lambda x: self.IMPORTANCE_ORDER.get(
                x.get("importance", "low"), 2))

            md += f"## {cat_name}\n\n"
            for item in cat_items:
                imp = item.get("importance", "medium")
                imp_label = self.IMPORTANCE_LABEL.get(imp, "")
                competitor = item.get("competitor", "未知")
                summary = item.get("summary", "")
                keywords = item.get("keywords", [])
                link = item.get("link", "")
                source = item.get("source", "")
                points = item.get("points")

                md += f"### {imp_label} {competitor}\n\n"
                md += f"{summary}\n\n"
                if keywords:
                    kw_str = " ".join(f"`{k}`" for k in keywords)
                    md += f"关键词: {kw_str}\n\n"
                md += f"来源: {source}"
                if points:
                    md += f" | HN 赞数: {points}"
                md += f" | [查看原文]({link})\n\n"
                md += "---\n\n"

        md += f"\n---\n*本报告由竞品情报自动化系统自动生成*\n"
        return md

    def generate_text_for_chat(self, items):
        """生成适合 Slack/飞书/Telegram 的纯文本摘要（精简版）"""
        date_str = datetime.now().strftime("%Y-%m-%d")
        prefix = self.config.get("report", "title_prefix", default="竞品情报日报")

        text = f"📋 {prefix}\n📅 {date_str}\n"
        text += f"📊 共 {len(items)} 条动态\n\n"

        if not items:
            text += "今日暂无竞品动态。"
            return text

        # 按类别分组
        groups = {}
        for item in items:
            cat = item.get("category", "other")
            groups.setdefault(cat, []).append(item)

        for cat_code in self.categories:
            cat_items = groups.get(cat_code, [])
            if not cat_items:
                continue

            cat_name = self.categories[cat_code]
            cat_items.sort(key=lambda x: self.IMPORTANCE_ORDER.get(
                x.get("importance", "low"), 2))

            text += f"\n【{cat_name}】\n"
            for item in cat_items:
                imp = item.get("importance", "medium")
                imp_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(imp, "")
                competitor = item.get("competitor", "未知")
                summary = item.get("summary", "")
                text += f"  {imp_icon} {competitor}: {summary}\n"

        return text.strip()

    def generate_html(self, items):
        """生成漂亮的 HTML 可视化报告"""
        date_str = datetime.now().strftime("%Y-%m-%d")
        time_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        prefix = self.config.get("report", "title_prefix", default="竞品情报日报")

        # 按类别分组
        groups = {}
        for item in items:
            cat = item.get("category", "other")
            groups.setdefault(cat, []).append(item)

        # 统计各竞品动态数量
        competitor_counts = {}
        for item in items:
            comp = item.get("competitor", "未知")
            competitor_counts[comp] = competitor_counts.get(comp, 0) + 1

        # 重要性统计
        imp_counts = {"high": 0, "medium": 0, "low": 0}
        for item in items:
            imp = item.get("importance", "medium")
            imp_counts[imp] = imp_counts.get(imp, 0) + 1

        # 构建 HTML
        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{prefix} - {date_str}</title>
<style>
:root {{
  --bg: #faf8f5;
  --bg2: #ffffff;
  --bg3: #f3efe8;
  --ink: #1a1a2e;
  --muted: #6b7280;
  --rule: #e5e0d8;
  --accent: #2563eb;
  --accent2: #f59e0b;
  --tip: #059669;
  --tip-bg: #ecfdf5;
  --warn: #dc2626;
  --warn-bg: #fef2f2;
  --note-bg: #eff6ff;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Noto Sans CJK SC", sans-serif;
  font-size: 16px;
  line-height: 1.75;
  color: var(--ink);
  background: var(--bg);
}}
.container {{
  max-width: 860px;
  margin: 0 auto;
  padding: 2rem 1.5rem 4rem;
}}
.hero {{
  text-align: center;
  padding: 2.5rem 0 2rem;
  border-bottom: 2px solid var(--accent);
  margin-bottom: 2rem;
}}
.hero .badge {{
  display: inline-block;
  background: var(--note-bg);
  color: var(--accent);
  font-size: 0.8rem;
  font-weight: 700;
  padding: 0.3rem 1rem;
  border-radius: 100px;
  margin-bottom: 0.8rem;
}}
.hero h1 {{
  font-size: 2rem;
  margin-bottom: 0.5rem;
}}
.hero .meta {{
  color: var(--muted);
  font-size: 0.92rem;
  margin-top: 0.5rem;
}}
.stats {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: 1rem;
  margin: 1.5rem 0 2rem;
}}
.stat-card {{
  background: var(--bg2);
  border: 1px solid var(--rule);
  border-radius: 10px;
  padding: 1rem;
  text-align: center;
}}
.stat-card .num {{
  font-size: 1.8rem;
  font-weight: 800;
  color: var(--accent);
}}
.stat-card .label {{
  font-size: 0.82rem;
  color: var(--muted);
}}
.stat-card.high .num {{ color: var(--warn); }}
.stat-card.medium .num {{ color: var(--accent2); }}
.stat-card.low .num {{ color: var(--tip); }}
section {{ margin-bottom: 2rem; }}
section h2 {{
  font-size: 1.3rem;
  margin-bottom: 1rem;
  padding-bottom: 0.5rem;
  border-bottom: 1px solid var(--rule);
}}
.item-card {{
  background: var(--bg2);
  border: 1px solid var(--rule);
  border-left: 4px solid var(--muted);
  border-radius: 8px;
  padding: 1rem 1.2rem;
  margin-bottom: 0.8rem;
  transition: box-shadow 0.15s;
}}
.item-card:hover {{ box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
.item-card.high {{ border-left-color: var(--warn); }}
.item-card.medium {{ border-left-color: var(--accent2); }}
.item-card.low {{ border-left-color: var(--tip); }}
.item-card .item-header {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.4rem;
}}
.item-card .competitor {{
  font-weight: 700;
  font-size: 1.05rem;
}}
.item-card .importance {{
  font-size: 0.78rem;
  font-weight: 700;
  padding: 0.15rem 0.6rem;
  border-radius: 100px;
}}
.item-card .importance.high {{ background: var(--warn-bg); color: var(--warn); }}
.item-card .importance.medium {{ background: #fef3c7; color: #92400e; }}
.item-card .importance.low {{ background: var(--tip-bg); color: var(--tip); }}
.item-card .summary {{
  color: var(--ink);
  margin-bottom: 0.4rem;
}}
.item-card .keywords {{
  margin-bottom: 0.4rem;
}}
.item-card .keyword {{
  display: inline-block;
  background: var(--bg3);
  color: var(--accent);
  font-size: 0.78rem;
  padding: 0.1rem 0.5rem;
  border-radius: 4px;
  margin-right: 0.3rem;
}}
.item-card .meta {{
  font-size: 0.82rem;
  color: var(--muted);
}}
.item-card .meta a {{ color: var(--accent); }}
.competitor-bar {{
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin-bottom: 1.5rem;
}}
.competitor-bar .chip {{
  background: var(--bg3);
  border-radius: 100px;
  padding: 0.3rem 0.8rem;
  font-size: 0.82rem;
  font-weight: 600;
}}
.competitor-bar .chip .count {{
  color: var(--accent);
  font-weight: 800;
}}
footer {{
  margin-top: 3rem;
  padding-top: 1.5rem;
  border-top: 1px solid var(--rule);
  text-align: center;
  color: var(--muted);
  font-size: 0.85rem;
}}
</style>
</head>
<body>
<div class="container">
  <div class="hero">
    <span class="badge">{date_str}</span>
    <h1>{prefix}</h1>
    <div class="meta">自动采集时间: {time_str}</div>
  </div>

  <div class="stats">
    <div class="stat-card"><div class="num">{len(items)}</div><div class="label">总动态数</div></div>
    <div class="stat-card high"><div class="num">{imp_counts['high']}</div><div class="label">高重要性</div></div>
    <div class="stat-card medium"><div class="num">{imp_counts['medium']}</div><div class="label">中重要性</div></div>
    <div class="stat-card low"><div class="num">{imp_counts['low']}</div><div class="label">低重要性</div></div>
  </div>

  <div class="competitor-bar">
"""
        for comp, count in sorted(competitor_counts.items(), key=lambda x: -x[1]):
            html += f'    <span class="chip">{comp} <span class="count">{count}</span></span>\n'

        html += "  </div>\n"

        if not items:
            html += "  <p>今日暂无竞品动态。</p>\n"
        else:
            for cat_code in self.categories:
                cat_items = groups.get(cat_code, [])
                if not cat_items:
                    continue
                cat_name = self.categories[cat_code]
                cat_items.sort(key=lambda x: self.IMPORTANCE_ORDER.get(
                    x.get("importance", "low"), 2))

                html += f'  <section>\n    <h2>{cat_name}</h2>\n'
                for item in cat_items:
                    imp = item.get("importance", "medium")
                    imp_label = {"high": "高", "medium": "中", "low": "低"}.get(imp, "中")
                    competitor = item.get("competitor", "未知")
                    summary = item.get("summary", "")
                    keywords = item.get("keywords", [])
                    link = item.get("link", "")
                    source = item.get("source", "")
                    points = item.get("points")

                    kw_html = " ".join(f'<span class="keyword">{k}</span>' for k in keywords)
                    source_html = f'来源: {source}'
                    if points:
                        source_html += f' | HN 赞数: {points}'
                    source_html += f' | <a href="{link}" target="_blank">查看原文</a>'

                    html += f"""    <div class="item-card {imp}">
      <div class="item-header">
        <span class="competitor">{competitor}</span>
        <span class="importance {imp}">{imp_label}优先级</span>
      </div>
      <div class="summary">{summary}</div>
      <div class="keywords">{kw_html}</div>
      <div class="meta">{source_html}</div>
    </div>
"""
                html += "  </section>\n"

        html += f"""  <footer>
    本报告由竞品情报自动化系统自动生成 | {time_str}
  </footer>
</div>
</body>
</html>"""
        return html

    def save_report(self, markdown_content, html_content=None):
        """保存报告到本地文件（Markdown + HTML）"""
        output_dir = self.config.get("report", "output_dir", default="reports")
        Path(output_dir).mkdir(parents=True, exist_ok=True)

        date_str = datetime.now().strftime("%Y-%m-%d")

        # 保存 Markdown
        md_filename = f"intel-{date_str}.md"
        md_filepath = os.path.join(output_dir, md_filename)
        with open(md_filepath, "w", encoding="utf-8") as f:
            f.write(markdown_content)
        logger.info(f"Markdown 报告已保存: {md_filepath}")

        # 保存 HTML
        html_filepath = None
        if html_content:
            html_filename = f"intel-{date_str}.html"
            html_filepath = os.path.join(output_dir, html_filename)
            with open(html_filepath, "w", encoding="utf-8") as f:
                f.write(html_content)
            logger.info(f"HTML 报告已保存: {html_filepath}")

        return html_filepath or md_filepath

# ============================================================
# 第五部分：通知推送器
# ============================================================
class Notifier:
    """推送到各种消息渠道"""

    def __init__(self, config: Config):
        self.config = config

    def send_all(self, items, markdown_report):
        """根据配置推送到所有启用的渠道"""
        notif_config = self.config.get("notifications", default={})
        text_summary = None  # 延迟生成

        # Slack
        if notif_config.get("slack", {}).get("enabled", False):
            webhook = os.environ.get("SLACK_WEBHOOK", "")
            if webhook:
                if text_summary is None:
                    reporter = Reporter(self.config)
                    text_summary = reporter.generate_text_for_chat(items)
                self._send_slack(webhook, text_summary)
            else:
                logger.warning("Slack 已启用但 SLACK_WEBHOOK 环境变量未设置")

        # 飞书
        if notif_config.get("feishu", {}).get("enabled", False):
            webhook = os.environ.get("FEISHU_WEBHOOK", "")
            if webhook:
                if text_summary is None:
                    reporter = Reporter(self.config)
                    text_summary = reporter.generate_text_for_chat(items)
                self._send_feishu(webhook, text_summary)
            else:
                logger.warning("飞书已启用但 FEISHU_WEBHOOK 环境变量未设置")

        # 企业微信
        if notif_config.get("wechat_work", {}).get("enabled", False):
            webhook = os.environ.get("WECHAT_WORK_WEBHOOK", "")
            if webhook:
                if text_summary is None:
                    reporter = Reporter(self.config)
                    text_summary = reporter.generate_text_for_chat(items)
                self._send_wechat_work(webhook, text_summary)
            else:
                logger.warning("企业微信已启用但 WECHAT_WORK_WEBHOOK 环境变量未设置")

        # Telegram
        if notif_config.get("telegram", {}).get("enabled", False):
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
            if token and chat_id:
                if text_summary is None:
                    reporter = Reporter(self.config)
                    text_summary = reporter.generate_text_for_chat(items)
                self._send_telegram(token, chat_id, text_summary)
            else:
                logger.warning("Telegram 已启用但 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID 未设置")

        # 邮件
        if notif_config.get("email", {}).get("enabled", False):
            api_key = os.environ.get("RESEND_API_KEY", "")
            if api_key:
                from_email = notif_config.get("email", {}).get("from_email", "intel@yourdomain.com")
                to_email = notif_config.get("email", {}).get("to_email", "team@yourdomain.com")
                self._send_email(api_key, from_email, to_email, markdown_report)
            else:
                logger.warning("邮件已启用但 RESEND_API_KEY 环境变量未设置")

    def _send_slack(self, webhook_url, text):
        """推送到 Slack"""
        try:
            resp = requests.post(webhook_url, json={"text": text}, timeout=10)
            if resp.status_code == 200:
                logger.info("✅ Slack 推送成功")
            else:
                logger.warning(f"Slack 推送返回 {resp.status_code}: {resp.text}")
        except Exception as e:
            logger.warning(f"Slack 推送失败: {e}")

    def _send_feishu(self, webhook_url, text):
        """推送到飞书"""
        try:
            payload = {
                "msg_type": "text",
                "content": {"text": text},
            }
            resp = requests.post(webhook_url, json=payload, timeout=10)
            if resp.status_code == 200 and resp.json().get("code", 0) == 0:
                logger.info("✅ 飞书推送成功")
            else:
                logger.warning(f"飞书推送返回: {resp.text}")
        except Exception as e:
            logger.warning(f"飞书推送失败: {e}")

    def _send_wechat_work(self, webhook_url, text):
        """推送到企业微信"""
        try:
            payload = {
                "msgtype": "markdown",
                "markdown": {"content": text},
            }
            resp = requests.post(webhook_url, json=payload, timeout=10)
            if resp.status_code == 200 and resp.json().get("errcode", 0) == 0:
                logger.info("✅ 企业微信推送成功")
            else:
                logger.warning(f"企业微信推送返回: {resp.text}")
        except Exception as e:
            logger.warning(f"企业微信推送失败: {e}")

    def _send_telegram(self, token, chat_id, text):
        """推送到 Telegram"""
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
            }
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                logger.info("✅ Telegram 推送成功")
            else:
                logger.warning(f"Telegram 推送返回 {resp.status_code}: {resp.text}")
        except Exception as e:
            logger.warning(f"Telegram 推送失败: {e}")

    def _send_email(self, api_key, from_email, to_email, html_content):
        """通过 Resend 发送邮件"""
        try:
            import resend
            resend.api_key = api_key
            date_str = datetime.now().strftime("%Y-%m-%d")

            # 简单的 Markdown 转 HTML
            html = html_content.replace("\n", "<br>\n")

            resend.Emails.send({
                "from": from_email,
                "to": to_email,
                "subject": f"竞品情报日报 - {date_str}",
                "html": html,
            })
            logger.info("✅ 邮件推送成功")
        except Exception as e:
            logger.warning(f"邮件推送失败: {e}")

# ============================================================
# 第六部分：主流程
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="竞品情报自动化系统")
    parser.add_argument(
        "--collect-only",
        action="store_true",
        help="仅采集数据，不调用 AI 和推送（用于测试）",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="配置文件路径（默认: config.yaml）",
    )
    args = parser.parse_args()

    # 加载配置
    config = Config(args.config)

    # ===== Step 1: 数据采集 =====
    print("\n" + "=" * 60)
    print("  竞品情报自动化系统")
    print("=" * 60)

    collector = Collector(config)
    items = collector.collect_all()

    if not items:
        print("\n⚠️  没有采集到任何数据。请检查网络连接和配置文件。")
        return

    # 如果只采集，输出结果并退出
    if args.collect_only:
        print("\n" + "=" * 60)
        print("  采集结果（仅采集模式，不调用 AI）")
        print("=" * 60)
        for i, item in enumerate(items, 1):
            print(f"\n--- 第 {i} 条 ---")
            print(f"竞品: {item.get('competitor', '')}")
            print(f"标题: {item.get('title', '')}")
            print(f"来源: {item.get('source', '')}")
            print(f"链接: {item.get('link', '')}")
            if item.get("points"):
                print(f"HN赞数: {item['points']}")
            summary_preview = item.get("summary", "")[:200]
            print(f"摘要预览: {summary_preview}...")

        print(f"\n✅ 采集完成！共 {len(items)} 条数据。")
        print("💡 如需完整运行（AI摘要+推送），请配置 API Key 后运行：")
        print("   python competitive_intel.py")
        return

    # ===== Step 2: AI 摘要与分类 =====
    print("\n" + "=" * 60)
    print("  AI 处理中...")
    print("=" * 60)

    ai_processor = AIProcessor(config)
    items = ai_processor.process_batch(items)

    # ===== Step 3: 生成报告 =====
    print("\n" + "=" * 60)
    print("  生成报告...")
    print("=" * 60)

    reporter = Reporter(config)
    markdown_report = reporter.generate_markdown(items)
    html_report = reporter.generate_html(items)
    filepath = reporter.save_report(markdown_report, html_report)

    # 输出到控制台
    print("\n" + "=" * 60)
    print("  报告预览")
    print("=" * 60)
    print(markdown_report[:3000])
    if len(markdown_report) > 3000:
        print(f"\n... (完整报告已保存到 {filepath})")

    # ===== Step 4: 推送通知 =====
    print("\n" + "=" * 60)
    print("  推送通知...")
    print("=" * 60)

    notifier = Notifier(config)
    notifier.send_all(items, markdown_report)

    # ===== 完成 =====
    print("\n" + "=" * 60)
    print("  ✅ 全部完成！")
    print(f"  报告已保存: {filepath}")
    print("=" * 60)

if __name__ == "__main__":
    main()
