#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
最新AI系ニュースまとめ Builder
Python標準ライブラリのみで動作するニュース収集・自動翻訳・要約・PWA生成スクリプト
"""

import os
import sys
import json
import re
import html
import hashlib
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

JST = timezone(timedelta(hours=9))

def clean_text(raw_html):
    """HTMLタグを除去し、テキストのみを抽出・正規化"""
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

def is_japanese(text):
    """日本語文字（ひらがな・カタカナ・漢字）が含まれるか判定"""
    return bool(re.search(r'[\u3040-\u30ff\u4e00-\u9fff]', text or ""))

def translate_to_ja(text):
    """
    Python標準ライブラリのみでGoogle翻訳API(無料エンドポイント)を利用して日本語に翻訳
    Gemini APIキー未設定時でも海外ニュースを確実に日本語化する
    """
    if not text or is_japanese(text):
        return text
    try:
        # 長すぎる文章はカット
        truncated = text[:400]
        url = "https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl=ja&dt=t&q=" + urllib.parse.quote(truncated)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=6) as res:
            data = json.loads(res.read().decode("utf-8"))
            translated = "".join([part[0] for part in data[0] if part[0]])
            return translated if translated else text
    except Exception:
        return text

def parse_date(date_str):
    """様々なフォーマットの日時文字列をUTC datetimeに変換"""
    if not date_str:
        return None
    date_str = date_str.strip()
    # RFC 2822 (RSS 2.0)
    try:
        dt = parsedate_to_datetime(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    # ISO 8601 (Atom)
    try:
        iso_str = date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    return None

def fetch_feed(feed_info, category_id, category_name, category_short):
    """単一のRSS/Atomフィードを取得・解析"""
    url = feed_info["url"]
    feed_name = feed_info["name"]
    keywords = [k.lower() for k in feed_info.get("filter", [])]
    articles = []

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"
        }
    )

    try:
        with urllib.request.urlopen(req, timeout=12) as response:
            xml_bytes = response.read()
    except Exception as e:
        print(f"  [WARN] Failed to fetch feed '{feed_name}' ({url}): {e}")
        return articles

    try:
        root = ET.fromstring(xml_bytes)
    except Exception as e:
        print(f"  [WARN] XML parse error for '{feed_name}': {e}")
        return articles

    # XML名前空間の除去
    for elem in root.iter():
        if "}" in elem.tag:
            elem.tag = elem.tag.split("}", 1)[1]

    items = root.findall(".//item")
    if not items:
        items = root.findall(".//entry")

    now_utc = datetime.now(timezone.utc)
    cutoff_time = now_utc - timedelta(hours=96)

    for item in items:
        title_el = item.find("title")
        title = clean_text(title_el.text if title_el is not None else "")
        if not title:
            continue

        link = ""
        link_el = item.find("link")
        if link_el is not None:
            link = link_el.get("href") or link_el.text or ""
            link = link.strip()
        if not link:
            guid_el = item.find("guid")
            if guid_el is not None and guid_el.text and guid_el.text.startswith("http"):
                link = guid_el.text.strip()
        if not link:
            continue

        pub_dt = None
        for date_tag in ["pubDate", "published", "updated", "date"]:
            dt_el = item.find(date_tag)
            if dt_el is not None and dt_el.text:
                pub_dt = parse_date(dt_el.text)
                if pub_dt:
                    break

        if pub_dt and pub_dt < cutoff_time:
            continue

        desc = ""
        for desc_tag in ["description", "summary", "content"]:
            d_el = item.find(desc_tag)
            if d_el is not None and d_el.text:
                desc = clean_text(d_el.text)
                if desc:
                    break

        if keywords:
            target_text = f"{title} {desc}".lower()
            if not any(kw in target_text for kw in keywords):
                continue

        url_hash = hashlib.md5(link.encode("utf-8")).hexdigest()
        feed_slug = re.sub(r'[^a-zA-Z0-9_-]', '_', feed_name)

        articles.append({
            "id": url_hash,
            "title": title,
            "link": link,
            "published_at": pub_dt.isoformat() if pub_dt else now_utc.isoformat(),
            "published_dt": pub_dt or now_utc,
            "source": feed_name,
            "source_slug": feed_slug,
            "category_id": category_id,
            "category_name": category_name,
            "category_short": category_short,
            "snippet": desc[:300] if desc else "",
            "score": 3 # デフォルト
        })

    return articles

def get_category_short(cat_id):
    mapping = {
        "company": "企業",
        "global": "海外",
        "japan": "国内",
        "dev": "開発",
        "research": "論文"
    }
    return mapping.get(cat_id, "一般")

def load_feeds_config(config_path="feeds.json"):
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)

def load_seen_articles(seen_path="data/seen.json"):
    if os.path.exists(seen_path):
        try:
            with open(seen_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_seen_articles(seen_dict, seen_path="data/seen.json"):
    os.makedirs(os.path.dirname(seen_path), exist_ok=True)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    cleaned = {k: v for k, v in seen_dict.items() if v >= cutoff}
    with open(seen_path, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2, ensure_ascii=False)

def collect_all_articles(config):
    all_articles = []
    feed_tasks = []

    print("[1/5] Collecting feeds concurrently...")
    with ThreadPoolExecutor(max_workers=10) as executor:
        for cat in config.get("categories", []):
            cat_id = cat["id"]
            cat_name = cat["name"]
            cat_short = get_category_short(cat_id)
            for feed in cat["feeds"]:
                future = executor.submit(fetch_feed, feed, cat_id, cat_name, cat_short)
                feed_tasks.append(future)

        for future in as_completed(feed_tasks):
            articles = future.result()
            all_articles.extend(articles)

    print(f"  -> Total raw articles collected: {len(all_articles)}")
    return all_articles

def filter_and_deduplicate(all_articles, seen_dict):
    print("[2/5] Filtering duplicates and previously seen articles...")
    unique_by_link = {}
    all_articles.sort(key=lambda x: x["published_dt"], reverse=True)

    for art in all_articles:
        link = art["link"]
        art_id = art["id"]
        if art_id in seen_dict:
            continue
        if link not in unique_by_link:
            unique_by_link[link] = art

    selected_articles = list(unique_by_link.values())

    # 各フィードから最大4件、全体で最大30件
    by_source = {}
    for art in selected_articles:
        by_source.setdefault(art["source"], []).append(art)

    final_articles = []
    for src, arts in by_source.items():
        final_articles.extend(arts[:4])

    # 最新順に並べて最大28件
    final_articles.sort(key=lambda x: x["published_dt"], reverse=True)
    final_articles = final_articles[:28]
    print(f"  -> Selected fresh articles: {len(final_articles)}")
    return final_articles

def calculate_auto_score(art):
    """Geminiキー未設定時の自動注目度採点（1〜5）"""
    score = 3
    source_lower = art["source"].lower()
    title_lower = art["title"].lower()

    # 主要公式ソース
    if any(k in source_lower for k in ["openai", "google", "anthropic"]):
        score += 1
    if any(k in source_lower for k in ["mit", "pivot", "arxiv"]):
        score += 0.5

    # 注目キーワード
    hot_keywords = [
        "gpt-5", "gpt-4", "claude 3", "claude", "gemini 2", "gemini", "o1", "o3",
        "breakthrough", "release", "announce", "agent", "reasoning", "benchmark",
        "発表", "新機能", "最新モデル", "革命", "進化", "速報", "公開"
    ]
    if any(kw in title_lower for kw in hot_keywords):
        score += 1

    return min(5, max(1, int(round(score))))

def translate_article_worker(art):
    """単一記事の英語タイトル・スニペットを日本語に翻訳"""
    t = art["title"]
    s = art["snippet"]
    if not is_japanese(t):
        art["title_ja"] = translate_to_ja(t)
    else:
        art["title_ja"] = t

    if s and not is_japanese(s):
        art["summary"] = translate_to_ja(s)
    elif s:
        art["summary"] = s
    else:
        art["summary"] = "元記事リンクより詳細をご確認ください。"

    art["score"] = calculate_auto_score(art)
    art["tags"] = [art["category_short"], art["source"].split()[0]]
    return art

def summarize_with_gemini(articles):
    """Gemini APIで一括要約＆翻訳＆注目度スコアリング。未設定時は高速並列自動翻訳へフォールバック"""
    print("[3/5] Summarizing, scoring, and translating articles...")
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()

    if not api_key:
        print("  [INFO] GEMINI_API_KEY is not set. Using fast parallel Japanese translation & scoring fallback.")
        with ThreadPoolExecutor(max_workers=8) as executor:
            articles = list(executor.map(translate_article_worker, articles))
        return articles

    if not articles:
        return articles

    articles_input = []
    for idx, art in enumerate(articles, 1):
        articles_input.append({
            "index": idx,
            "source": art["source"],
            "original_title": art["title"],
            "snippet": art["snippet"]
        })

    prompt = f"""あなたは敏腕テックジャーナリスト・AIリサーチャーです。
毎朝スマホでサクッと読める「最新AI系ニュースまとめ」のために、以下のAIニュース記事リストを処理してください。

【厳格な指示】
1. **海外記事（英語）は必ず自然で分かりやすい日本語に翻訳・要約してください。英語のまま出力しないでください。**
2. 各記事に**「注目度スコア（1〜5の整数）」**を付与してください：
   - 5: 業界激震・新フラッグシップモデル発表・重大ブレイクスルー
   - 4: 主要アップデート・実用性の高いツール・注目研究
   - 3: 通常のアップデート・解説・業界動向
3. 必ず以下のJSON配列フォーマットのみで返してください：
[
  {{
    "index": 1,
    "score": 5,
    "title_ja": "日本語の見出し（30〜45文字で何が起きたか一目でわかるタイトル）",
    "summary": "日本語の要約（80〜120文字程度。何が発表され、なぜ注目なのかを簡潔に解説）",
    "tags": ["タグ1", "タグ2"]
  }}
]

【記事リスト】
{json.dumps(articles_input, ensure_ascii=False, indent=2)}
"""

    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={urllib.parse.quote(api_key, safe='')}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json"
        }
    }

    try:
        req = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=35) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        raw_content = data["candidates"][0]["content"]["parts"][0]["text"]
        summary_list = json.loads(raw_content)

        summary_map = {item["index"]: item for item in summary_list if "index" in item}
        for idx, art in enumerate(articles, 1):
            if idx in summary_map:
                s_item = summary_map[idx]
                art["title_ja"] = s_item.get("title_ja", art["title"])
                art["summary"] = s_item.get("summary", art["snippet"])
                art["tags"] = s_item.get("tags", [art["category_short"], art["source"]])
                art["score"] = int(s_item.get("score", calculate_auto_score(art)))
            else:
                art = translate_article_worker(art)

        print("  -> Gemini batch summary & scoring succeeded!")
    except Exception as e:
        print(f"  [WARN] Gemini API call failed ({e}). Falling back to automatic translation mode.")
        with ThreadPoolExecutor(max_workers=8) as executor:
            articles = list(executor.map(translate_article_worker, articles))

    return articles

def get_star_string(score):
    """スコアを星表現に変換（★★★★★）"""
    score = max(1, min(5, int(score)))
    return "★" * score + "☆" * (5 - score)

def generate_html(categories, articles, output_path="docs/index.html"):
    """モダンなPWA対応モバイル最適化HTMLを生成"""
    print("[4/5] Generating responsive PWA HTML...")
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    total_count = len(articles)

    # サイト（フィード）ごとに分類
    site_dict = {}
    for art in articles:
        src = art["source"]
        site_dict.setdefault(src, {
            "name": src,
            "category_name": art["category_name"],
            "category_short": art["category_short"],
            "slug": art["source_slug"],
            "articles": []
        })
        site_dict[src]["articles"].append(art)

    # 本日の注目ニュース上位10件をピックアップ（スコア降順、日付降順）
    sorted_by_score = sorted(articles, key=lambda x: (x.get("score", 3), x["published_dt"]), reverse=True)
    top_picks = sorted_by_score[:10]
    top_pick_ids = set(a["id"] for a in top_picks)
    top_pick_count = len(top_picks)

    # HTML構築
    html_content = f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
  <title>最新AI系ニュースまとめ</title>
  
  <!-- PWA & Mobile Meta Tags -->
  <meta name="theme-color" content="#0b0f19">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="AIニュース">
  <link rel="manifest" href="./manifest.webmanifest">
  <link rel="apple-touch-icon" href="./icon-180.png">
  <link rel="icon" type="image/png" href="./icon-192.png">

  <style>
    :root {{
      --bg: #0b0f19;
      --card-bg: #151c2c;
      --card-border: #1e293b;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --accent: #38bdf8;
      --accent-glow: rgba(56, 189, 248, 0.15);
      --gold: #fbbf24;
      --gold-bg: rgba(251, 191, 36, 0.12);
      --purple: #c084fc;
      --purple-bg: rgba(192, 132, 252, 0.12);
      --read-opacity: 0.65;
    }}
    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      -webkit-tap-highlight-color: transparent;
    }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Hiragino Kaku Gothic ProN", "Yu Gothic", "Segoe UI", Roboto, sans-serif;
      background-color: var(--bg);
      color: var(--text);
      line-height: 1.55;
      padding-bottom: 90px;
      min-height: 100vh;
    }}
    /* ヘッダー */
    header {{
      position: sticky;
      top: 0;
      z-index: 100;
      background: rgba(11, 15, 25, 0.9);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      border-bottom: 1px solid var(--card-border);
      padding: 12px 16px;
      padding-top: max(12px, env(safe-area-inset-top));
    }}
    .header-top {{
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .brand {{
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .logo-badge {{
      width: 32px;
      height: 32px;
      border-radius: 8px;
      background: linear-gradient(135deg, #0ea5e9, #8b5cf6);
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 800;
      font-size: 15px;
      color: #fff;
      box-shadow: 0 2px 8px rgba(14, 165, 233, 0.35);
    }}
    h1 {{
      font-size: 16px;
      font-weight: 700;
      letter-spacing: -0.2px;
    }}
    .updated-text {{
      font-size: 11px;
      color: var(--text-muted);
    }}
    .header-actions {{
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .mark-read-btn {{
      background: rgba(255, 255, 255, 0.08);
      border: 1px solid var(--card-border);
      color: var(--text-muted);
      padding: 5px 12px;
      border-radius: 20px;
      font-size: 12px;
      cursor: pointer;
      transition: all 0.2s;
    }}
    .mark-read-btn:active {{
      background: rgba(255, 255, 255, 0.2);
      transform: scale(0.96);
    }}

    /* タブスクロールバー */
    .tabs-wrap {{
      overflow-x: auto;
      white-space: nowrap;
      padding: 10px 16px 8px 16px;
      display: flex;
      gap: 8px;
      scrollbar-width: none;
      -webkit-overflow-scrolling: touch;
      border-bottom: 1px solid rgba(255, 255, 255, 0.04);
    }}
    .tabs-wrap::-webkit-scrollbar {{
      display: none;
    }}
    .tab-item {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 7px 14px;
      background: #192237;
      color: var(--text-muted);
      border: 1px solid rgba(255, 255, 255, 0.06);
      border-radius: 20px;
      font-size: 13px;
      font-weight: 600;
      text-decoration: none;
      transition: all 0.2s;
      flex-shrink: 0;
    }}
    .tab-item.active {{
      background: var(--accent);
      color: #0b0f19;
      border-color: var(--accent);
      box-shadow: 0 2px 10px rgba(56, 189, 248, 0.35);
    }}
    .tab-item.tab-featured {{
      color: #fde047;
      border-color: rgba(253, 224, 71, 0.3);
      background: rgba(250, 204, 21, 0.1);
    }}
    .tab-item.tab-featured.active {{
      background: #eab308;
      color: #0b0f19;
      border-color: #eab308;
      box-shadow: 0 2px 12px rgba(234, 179, 8, 0.4);
    }}
    .tab-badge {{
      font-size: 10.5px;
      padding: 1px 6px;
      border-radius: 10px;
      background: rgba(0, 0, 0, 0.25);
    }}
    .tab-item.active .tab-badge {{
      background: rgba(0, 0, 0, 0.2);
    }}
    .cat-label {{
      font-size: 10px;
      opacity: 0.85;
      font-weight: 700;
    }}

    /* メインコンテンツ */
    main {{
      max-width: 680px;
      margin: 0 auto;
      padding: 16px;
    }}
    .view-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 14px;
      padding: 4px 2px;
    }}
    .view-title {{
      font-size: 15px;
      font-weight: 700;
      color: var(--text);
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .view-desc {{
      font-size: 12px;
      color: var(--text-muted);
    }}

    /* 記事カード */
    .article-card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 16px;
      margin-bottom: 14px;
      transition: transform 0.15s, opacity 0.25s, max-height 0.3s;
      position: relative;
      overflow: hidden;
    }}
    .article-card:active {{
      transform: scale(0.99);
    }}
    .article-card.is-read {{
      opacity: var(--read-opacity);
      border-color: rgba(255, 255, 255, 0.05);
      background: #111726;
    }}
    .article-card.featured-pick {{
      border-left: 3px solid var(--gold);
    }}

    .meta-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
      font-size: 11.5px;
      color: var(--text-muted);
      flex-wrap: wrap;
      gap: 6px;
    }}
    .source-group {{
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .category-tag {{
      background: var(--purple-bg);
      color: var(--purple);
      padding: 2px 7px;
      border-radius: 6px;
      font-size: 10.5px;
      font-weight: 700;
    }}
    .source-tag {{
      background: var(--accent-glow);
      color: var(--accent);
      padding: 2px 8px;
      border-radius: 6px;
      font-weight: 600;
      font-size: 11px;
    }}
    .stars-badge {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      background: var(--gold-bg);
      color: var(--gold);
      padding: 2px 8px;
      border-radius: 10px;
      font-weight: 700;
      font-size: 11.5px;
      letter-spacing: 1px;
    }}

    .card-title {{
      font-size: 16px;
      font-weight: 700;
      line-height: 1.45;
      margin-bottom: 10px;
      color: #fff;
    }}
    .card-summary {{
      font-size: 13.5px;
      line-height: 1.65;
      color: #cbd5e1;
      margin-bottom: 12px;
      word-break: break-word;
    }}
    .tags-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-bottom: 12px;
    }}
    .tag-badge {{
      font-size: 11px;
      color: #93c5fd;
      background: rgba(59, 130, 246, 0.12);
      padding: 2px 7px;
      border-radius: 6px;
    }}
    .card-footer {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-top: 10px;
      border-top: 1px solid rgba(255, 255, 255, 0.06);
    }}
    .read-toggle-btn {{
      background: rgba(255, 255, 255, 0.06);
      border: 1px solid var(--card-border);
      color: var(--text-muted);
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 6px 12px;
      border-radius: 18px;
      transition: all 0.2s;
    }}
    .read-toggle-btn:active {{
      background: rgba(255, 255, 255, 0.15);
      transform: scale(0.96);
    }}
    .article-card.is-read .read-toggle-btn {{
      color: #38bdf8;
      background: rgba(56, 189, 248, 0.1);
      border-color: rgba(56, 189, 248, 0.3);
    }}
    .origin-link {{
      color: var(--accent);
      text-decoration: none;
      font-size: 13px;
      font-weight: 600;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 4px 8px;
    }}

    /* 空状態メッセージ */
    .empty-state {{
      text-align: center;
      padding: 50px 20px;
      color: var(--text-muted);
    }}
    .empty-icon {{
      font-size: 42px;
      margin-bottom: 12px;
    }}
    .empty-title {{
      font-size: 16px;
      font-weight: 700;
      color: var(--text);
      margin-bottom: 6px;
    }}

    /* フッター */
    footer {{
      text-align: center;
      padding: 24px 16px;
      font-size: 11.5px;
      color: var(--text-muted);
      border-top: 1px solid var(--card-border);
    }}
  </style>
</head>
<body>

  <header>
    <div class="header-top">
      <div class="brand">
        <div class="logo-badge">AI</div>
        <div>
          <h1>最新AI系ニュースまとめ</h1>
          <div class="updated-text">更新: {now_jst} (全{total_count}件)</div>
        </div>
      </div>
      <div class="header-actions">
        <button class="mark-read-btn" id="markAllBtn">未読を全件既読</button>
      </div>
    </div>
  </header>

  <!-- タブナビゲーション -->
  <div class="tabs-wrap" id="tabsWrap">
    <a href="#featured" class="tab-item tab-featured" onclick="switchTab(event, 'featured')">
      <span>★ 注目</span>
      <span class="tab-badge" id="badge-featured">{top_pick_count}</span>
    </a>
    <a href="#unread" class="tab-item active" onclick="switchTab(event, 'unread')">
      <span>未読</span>
      <span class="tab-badge" id="badge-unread">{total_count}</span>
    </a>
"""

    # サイトごとのタブ（何に関するサイトかわかるようにカテゴリ短縮名を表示）
    for src, sdata in site_dict.items():
        slug = sdata["slug"]
        short_cat = sdata["category_short"]
        scount = len(sdata["articles"])
        html_content += f"""    <a href="#site-{slug}" class="tab-item" onclick="switchTab(event, 'site-{slug}')">
      <span>{html.escape(src)}</span>
      <span class="cat-label">[{short_cat}]</span>
      <span class="tab-badge">{scount}</span>
    </a>\n"""

    html_content += f"""    <a href="#read" class="tab-item" onclick="switchTab(event, 'read')">
      <span>既読</span>
      <span class="tab-badge" id="badge-read">0</span>
    </a>
  </div>

  <main id="mainContent">
    <div class="view-header">
      <div class="view-title" id="viewTitle">未読ニュース</div>
      <div class="view-desc" id="viewDesc">最新のAIトレンド一覧</div>
    </div>

    <div id="articlesList">
"""

    # 全記事カードを出力（JSでタブ状態に応じて表示・非表示を制御）
    for idx, art in enumerate(articles):
        art_id = art["id"]
        title_ja = html.escape(art.get("title_ja", art["title"]))
        summary = html.escape(art.get("summary", ""))
        source = html.escape(art["source"])
        source_slug = art["source_slug"]
        category_name = html.escape(art["category_name"])
        category_short = html.escape(art["category_short"])
        link = html.escape(art["link"])
        score = art.get("score", 3)
        star_str = get_star_string(score)
        is_top = art_id in top_pick_ids
        featured_class = "featured-pick" if is_top else ""
        tags_html = "".join([f'<span class="tag-badge">#{html.escape(t)}</span>' for t in art.get("tags", [])])

        html_content += f"""
      <article class="article-card {featured_class}" id="card-{art_id}" data-id="{art_id}" data-source="site-{source_slug}" data-score="{score}" data-featured="{'true' if is_top else 'false'}">
        <div class="meta-row">
          <div class="source-group">
            <span class="category-tag">{category_short}</span>
            <span class="source-tag">{source}</span>
            <span>{art['published_dt'].strftime('%m/%d %H:%M')}</span>
          </div>
          <div class="stars-badge" title="注目度 {score}/5">
            <span>{star_str}</span>
          </div>
        </div>
        <h2 class="card-title">{title_ja}</h2>
        <p class="card-summary">{summary}</p>
        <div class="tags-row">{tags_html}</div>
        <div class="card-footer">
          <button class="read-toggle-btn" onclick="toggleRead('{art_id}', event)">
            <span class="read-icon">✓</span> <span class="read-text">既読にする</span>
          </button>
          <a href="{link}" target="_blank" rel="noopener noreferrer" class="origin-link" onclick="markRead('{art_id}')">元記事を読む →</a>
        </div>
      </article>
"""

    html_content += """    </div>

    <!-- 空状態プレースホルダー -->
    <div class="empty-state" id="emptyState" style="display: none;">
      <div class="empty-icon" id="emptyIcon">🎉</div>
      <div class="empty-title" id="emptyTitle">すべて読み終わりました！</div>
      <p id="emptyText">本日のニュースはすべて既読です。また明朝の更新をお楽しみに！</p>
    </div>
  </main>

  <footer>
    <p>最新AI系ニュースまとめ - 朝のスキマ時間で追いつくAI動向</p>
    <p style="margin-top: 4px; opacity: 0.8;">Serverless & Powered by GitHub Actions & Gemini API</p>
  </footer>

  <script>
    // PWA Service Worker 登録
    if ('serviceWorker' in navigator) {
      window.addEventListener('load', () => {
        navigator.serviceWorker.register('./sw.js').catch(err => {
          console.log('SW registration failed:', err);
        });
      });
    }

    // 状態管理
    const STORAGE_KEY = 'ai_daily_read_ids';
    let currentTab = 'unread';

    function getReadIds() {
      try {
        return JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
      } catch (e) {
        return [];
      }
    }

    function saveReadIds(ids) {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(ids));
    }

    // カウントと表示の更新
    function updateUI() {
      const readIds = new Set(getReadIds());
      const allCards = Array.from(document.querySelectorAll('.article-card'));
      const totalArticles = allCards.length;
      const readCount = allCards.filter(c => readIds.has(c.getAttribute('data-id'))).length;
      const unreadCount = totalArticles - readCount;

      // バッジ更新
      const unreadBadge = document.getElementById('badge-unread');
      if (unreadBadge) unreadBadge.textContent = unreadCount;
      const readBadge = document.getElementById('badge-read');
      if (readBadge) readBadge.textContent = readCount;

      // カード表示制御
      let visibleCount = 0;
      allCards.forEach(card => {
        const id = card.getAttribute('data-id');
        const isRead = readIds.has(id);
        const cardSource = card.getAttribute('data-source');
        const isFeatured = card.getAttribute('data-featured') === 'true';

        // 既読スタイルの付与
        if (isRead) {
          card.classList.add('is-read');
          const btnText = card.querySelector('.read-text');
          if (btnText) btnText.textContent = '既読解除';
        } else {
          card.classList.remove('is-read');
          const btnText = card.querySelector('.read-text');
          if (btnText) btnText.textContent = '既読にする';
        }

        // タブに応じた表示・非表示
        let shouldShow = false;
        if (currentTab === 'unread') {
          shouldShow = !isRead;
        } else if (currentTab === 'read') {
          shouldShow = isRead;
        } else if (currentTab === 'featured') {
          shouldShow = isFeatured;
        } else if (currentTab.startsWith('site-')) {
          shouldShow = (cardSource === currentTab);
        }

        if (shouldShow) {
          card.style.display = 'block';
          visibleCount++;
        } else {
          card.style.display = 'none';
        }
      });

      // 注目タブの場合はスコア順にDOMをソートして表示
      if (currentTab === 'featured') {
        const container = document.getElementById('articlesList');
        const featuredCards = allCards.filter(c => c.getAttribute('data-featured') === 'true');
        featuredCards.sort((a, b) => {
          return parseInt(b.getAttribute('data-score') || 0) - parseInt(a.getAttribute('data-score') || 0);
        });
        featuredCards.forEach(c => container.appendChild(c));
      }

      // 空状態の表示制御
      const emptyState = document.getElementById('emptyState');
      const emptyIcon = document.getElementById('emptyIcon');
      const emptyTitle = document.getElementById('emptyTitle');
      const emptyText = document.getElementById('emptyText');

      if (visibleCount === 0) {
        emptyState.style.display = 'block';
        if (currentTab === 'unread') {
          emptyIcon.textContent = '🎉';
          emptyTitle.textContent = 'すべて読み終わりました！';
          emptyText.textContent = '未読のニュースはありません。今日も良い1日を！';
        } else if (currentTab === 'read') {
          emptyIcon.textContent = '📖';
          emptyTitle.textContent = 'まだ既読の記事はありません';
          emptyText.textContent = '記事の「既読にする」ボタンを押すとここにストックされます。';
        } else {
          emptyIcon.textContent = '📭';
          emptyTitle.textContent = '記事がありません';
          emptyText.textContent = '現在表示できる記事がありません。';
        }
      } else {
        emptyState.style.display = 'none';
      }
    }

    // 既読トグル
    function toggleRead(id, event) {
      if (event) event.stopPropagation();
      let readIds = getReadIds();
      if (readIds.includes(id)) {
        readIds = readIds.filter(x => x !== id);
      } else {
        readIds.push(id);
      }
      saveReadIds(readIds);
      updateUI();
    }

    function markRead(id) {
      let readIds = getReadIds();
      if (!readIds.includes(id)) {
        readIds.push(id);
        saveReadIds(readIds);
        updateUI();
      }
    }

    // 未読を全件既読にする
    document.getElementById('markAllBtn').addEventListener('click', () => {
      const allCards = document.querySelectorAll('.article-card');
      const allIds = Array.from(allCards).map(c => c.getAttribute('data-id'));
      saveReadIds(allIds);
      updateUI();
    });

    // タブ切り替え
    function switchTab(event, targetTab) {
      event.preventDefault();
      currentTab = targetTab;

      document.querySelectorAll('.tab-item').forEach(el => el.classList.remove('active'));
      event.currentTarget.classList.add('active');

      const viewTitle = document.getElementById('viewTitle');
      const viewDesc = document.getElementById('viewDesc');

      if (targetTab === 'featured') {
        viewTitle.textContent = '★ 本日の注目ニュース TOP10';
        viewDesc.textContent = '重要度・注目度が高い順に表示';
      } else if (targetTab === 'unread') {
        viewTitle.textContent = '未読ニュース';
        viewDesc.textContent = '最新のAI動向一覧';
      } else if (targetTab === 'read') {
        viewTitle.textContent = '既読ニュース';
        viewDesc.textContent = '読み終わった記事のアーカイブ';
      } else {
        const tabText = event.currentTarget.querySelector('span').textContent;
        viewTitle.textContent = tabText;
        viewDesc.textContent = 'このサイトの新着記事';
      }

      updateUI();
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }

    // 初期化
    updateUI();
  </script>
</body>
</html>
"""

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"  -> Generated: {output_path} ({len(html_content)} bytes)")

def main():
    print("=== 最新AI系ニュースまとめ Build Started ===")
    config = load_feeds_config()
    seen_dict = load_seen_articles()

    # 1. RSS収集
    raw_articles = collect_all_articles(config)

    # 2. 重複・既読除外
    articles = filter_and_deduplicate(raw_articles, seen_dict)

    # 3. 日本語化・スコアリング・Gemini要約
    summarized_articles = summarize_with_gemini(articles)

    # 4. HTML生成
    generate_html(config["categories"], summarized_articles)

    # 5. 今回取得した記事IDを既読履歴に記録
    now_iso = datetime.now(timezone.utc).isoformat()
    for art in summarized_articles:
        seen_dict[art["id"]] = now_iso
    save_seen_articles(seen_dict)

    print("=== Build Completed Successfully! ===")

if __name__ == "__main__":
    main()
