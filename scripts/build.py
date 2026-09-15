#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI Daily News Builder
Python標準ライブラリのみで動作するニュース収集・要約・静的サイト生成スクリプト
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
        # 末尾のZを+00:00に置換
        iso_str = date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    return None

def fetch_feed(feed_info, category_id, category_name):
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

    # RSS 2.0 (<item>) または Atom (<entry>) の探索
    items = root.findall(".//item")
    if not items:
        items = root.findall(".//entry")

    now_utc = datetime.now(timezone.utc)
    cutoff_time = now_utc - timedelta(hours=96) # 過去96時間（4日以内）

    for item in items:
        # タイトル取得
        title_el = item.find("title")
        title = clean_text(title_el.text if title_el is not None else "")
        if not title:
            continue

        # リンク取得
        link = ""
        link_el = item.find("link")
        if link_el is not None:
            link = link_el.get("href") or link_el.text or ""
            link = link.strip()
        if not link:
            # guidがURLの場合のフォールバック
            guid_el = item.find("guid")
            if guid_el is not None and guid_el.text and guid_el.text.startswith("http"):
                link = guid_el.text.strip()
        if not link:
            continue

        # 日時取得
        pub_dt = None
        for date_tag in ["pubDate", "published", "updated", "date"]:
            dt_el = item.find(date_tag)
            if dt_el is not None and dt_el.text:
                pub_dt = parse_date(dt_el.text)
                if pub_dt:
                    break

        if pub_dt and pub_dt < cutoff_time:
            continue

        # 概要・本文スニペット取得
        desc = ""
        for desc_tag in ["description", "summary", "content"]:
            d_el = item.find(desc_tag)
            if d_el is not None and d_el.text:
                desc = clean_text(d_el.text)
                if desc:
                    break

        # キーワードフィルタ判定（設定されている場合）
        if keywords:
            target_text = f"{title} {desc}".lower()
            if not any(kw in target_text for kw in keywords):
                continue

        # 重複チェック用ハッシュ
        url_hash = hashlib.md5(link.encode("utf-8")).hexdigest()

        articles.append({
            "id": url_hash,
            "title": title,
            "link": link,
            "published_at": pub_dt.isoformat() if pub_dt else now_utc.isoformat(),
            "published_dt": pub_dt or now_utc,
            "source": feed_name,
            "category_id": category_id,
            "category_name": category_name,
            "snippet": desc[:250] if desc else ""
        })

    return articles

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
    # 30日以上前の記事履歴をクリーンアップ
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    cleaned = {k: v for k, v in seen_dict.items() if v >= cutoff}
    with open(seen_path, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2, ensure_ascii=False)

def collect_all_articles(config):
    """全フィードから並列に記事を収集"""
    all_articles = []
    feed_tasks = []

    print("[1/5] Collecting feeds concurrently...")
    with ThreadPoolExecutor(max_workers=10) as executor:
        for cat in config.get("categories", []):
            cat_id = cat["id"]
            cat_name = cat["name"]
            for feed in cat["feeds"]:
                future = executor.submit(fetch_feed, feed, cat_id, cat_name)
                feed_tasks.append(future)

        for future in as_completed(feed_tasks):
            articles = future.result()
            all_articles.extend(articles)

    print(f"  -> Total raw articles collected: {len(all_articles)}")
    return all_articles

def filter_and_deduplicate(all_articles, seen_dict):
    """既読除外、URL重複除外、カテゴリごとのソートと件数制限"""
    print("[2/5] Filtering duplicates and previously seen articles...")
    unique_by_link = {}
    
    # 新しい順にソート
    all_articles.sort(key=lambda x: x["published_dt"], reverse=True)

    for art in all_articles:
        link = art["link"]
        art_id = art["id"]
        # すでに前日以前に配信済みの記事は除外
        if art_id in seen_dict:
            continue
        if link not in unique_by_link:
            unique_by_link[link] = art

    selected_articles = list(unique_by_link.values())

    # カテゴリごとに最大6件、全体で最大24件に厳選（毎朝3分で読める量・Gemini無料枠1回分）
    categorized = {}
    for art in selected_articles:
        cid = art["category_id"]
        categorized.setdefault(cid, []).append(art)

    final_articles = []
    for cid, arts in categorized.items():
        final_articles.extend(arts[:6])

    final_articles = final_articles[:24]
    print(f"  -> Selected fresh articles for summary: {len(final_articles)}")
    return final_articles

def summarize_with_gemini(articles):
    """
    Gemini API（無料枠）で全記事を1リクエストで一括要約
    APIキー未設定やエラー時は、見出しと元スニペットで安全にフォールバック
    """
    print("[3/5] Summarizing articles with Gemini API...")
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()

    if not api_key:
        print("  [INFO] GEMINI_API_KEY is not set. Falling back to headlines mode.")
        for art in articles:
            art["title_ja"] = art["title"]
            art["summary"] = art["snippet"] if art["snippet"] else "元記事リンクから詳細をご確認ください。"
            art["tags"] = [art["source"]]
        return articles

    if not articles:
        return articles

    # プロンプト作成
    articles_input = []
    for idx, art in enumerate(articles, 1):
        articles_input.append({
            "index": idx,
            "source": art["source"],
            "original_title": art["title"],
            "snippet": art["snippet"]
        })

    prompt = f"""あなたは敏腕テックジャーナリスト・AIリサーチャーです。
通勤中・スキマ時間のビジネスパーソンやエンジニアが、スマホでサクッと最新動向を把握できるよう、以下のAIニュース記事リストを日本語でわかりやすく要約してください。

【出力要件】
1. 必ず指定のJSON配列形式のみで返してください。
2. 各要素には以下のフィールドを含めてください：
   - "index": 入力のindex番号（整数）
   - "title_ja": 日本語の見出し。30〜45文字程度で何が起きたか一目でわかる魅力的なタイトル
   - "summary": 2〜3文（80〜120文字程度）の簡潔な要約。「何が発表されたか」「なぜ重要か・どう役立つか」がすぐ伝わる内容
   - "tags": 関連するキーワードタグ2〜3個の配列（例: ["OpenAI", "LLM", "API"]）

【記事リスト】
{json.dumps(articles_input, ensure_ascii=False, indent=2)}
"""

    api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={api_url_safe(api_key)}"
    payload = {
        "contents": [
            {
                "parts": [{"text": prompt}]
            }
        ],
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
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        raw_content = data["candidates"][0]["content"]["parts"][0]["text"]
        summary_list = json.loads(raw_content)

        summary_map = {item["index"]: item for item in summary_list if "index" in item}
        for idx, art in enumerate(articles, 1):
            if idx in summary_map:
                s_item = summary_map[idx]
                art["title_ja"] = s_item.get("title_ja", art["title"])
                art["summary"] = s_item.get("summary", art["snippet"])
                art["tags"] = s_item.get("tags", [art["source"]])
            else:
                art["title_ja"] = art["title"]
                art["summary"] = art["snippet"]
                art["tags"] = [art["source"]]

        print("  -> Gemini batch summary succeeded!")
    except Exception as e:
        print(f"  [WARN] Gemini API call failed ({e}). Falling back to headline mode.")
        for art in articles:
            art["title_ja"] = art["title"]
            art["summary"] = art["snippet"] if art["snippet"] else "元記事リンクより詳細をご確認ください。"
            art["tags"] = [art["source"]]

    return articles

def api_url_safe(key):
    return urllib.parse.quote(key, safe="")

def generate_html(categories, articles, output_path="docs/index.html"):
    """モダンなPWA対応モバイル最適化HTMLを生成"""
    print("[4/5] Generating responsive PWA HTML...")
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M")
    total_count = len(articles)

    # カテゴリごとに分類
    cat_articles = {}
    for art in articles:
        cat_articles.setdefault(art["category_id"], []).append(art)

    # HTML構築
    html_content = f"""<!DOCTYPE html>
<html lang="ja">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
  <title>AI Daily ニュース</title>
  
  <!-- PWA & Mobile Meta Tags -->
  <meta name="theme-color" content="#0f172a">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="AI Daily">
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
      --purple: #a855f7;
      --read-opacity: 0.65;
    }}
    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      -webkit-tap-highlight-color: transparent;
    }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background-color: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding-bottom: 80px;
      min-height: 100vh;
    }}
    /* ヘッダー */
    header {{
      position: sticky;
      top: 0;
      z-index: 100;
      background: rgba(11, 15, 25, 0.85);
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
    }}
    h1 {{
      font-size: 18px;
      font-weight: 700;
      letter-spacing: -0.3px;
    }}
    .updated-text {{
      font-size: 11px;
      color: var(--text-muted);
    }}
    .mark-read-btn {{
      background: rgba(255, 255, 255, 0.08);
      border: 1px solid var(--card-border);
      color: var(--text-muted);
      padding: 6px 12px;
      border-radius: 20px;
      font-size: 12px;
      cursor: pointer;
      transition: all 0.2s;
    }}
    .mark-read-btn:active {{
      background: rgba(255, 255, 255, 0.16);
      transform: scale(0.96);
    }}

    /* カテゴリタブバー */
    .tabs-wrap {{
      overflow-x: auto;
      white-space: nowrap;
      padding: 10px 16px 4px 16px;
      display: flex;
      gap: 8px;
      scrollbar-width: none;
    }}
    .tabs-wrap::-webkit-scrollbar {{
      display: none;
    }}
    .tab-item {{
      display: inline-block;
      padding: 6px 14px;
      background: #1e293b;
      color: var(--text-muted);
      border-radius: 16px;
      font-size: 13px;
      font-weight: 500;
      text-decoration: none;
      transition: all 0.2s;
    }}
    .tab-item.active {{
      background: var(--accent);
      color: #0f172a;
      font-weight: 700;
    }}

    /* メインコンテンツ */
    main {{
      max-width: 680px;
      margin: 0 auto;
      padding: 16px;
    }}
    .category-section {{
      margin-bottom: 28px;
    }}
    .category-header {{
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 12px;
      padding-bottom: 6px;
      border-bottom: 1px solid var(--card-border);
    }}
    .category-title {{
      font-size: 16px;
      font-weight: 700;
      color: var(--accent);
    }}
    .category-count {{
      font-size: 12px;
      color: var(--text-muted);
      background: rgba(255, 255, 255, 0.06);
      padding: 2px 8px;
      border-radius: 10px;
    }}

    /* 記事カード */
    .article-card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 14px;
      padding: 16px;
      margin-bottom: 14px;
      transition: transform 0.15s, opacity 0.2s, border-color 0.2s;
      position: relative;
    }}
    .article-card:active {{
      transform: scale(0.99);
    }}
    .article-card.is-read {{
      opacity: var(--read-opacity);
      border-color: transparent;
    }}
    .meta-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
      font-size: 11px;
      color: var(--text-muted);
    }}
    .source-tag {{
      background: var(--accent-glow);
      color: var(--accent);
      padding: 2px 8px;
      border-radius: 6px;
      font-weight: 600;
    }}
    .card-title {{
      font-size: 16px;
      font-weight: 700;
      line-height: 1.4;
      margin-bottom: 10px;
      color: #fff;
    }}
    .card-summary {{
      font-size: 13.5px;
      line-height: 1.6;
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
      padding: 2px 6px;
      border-radius: 4px;
    }}
    .card-footer {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-top: 10px;
      border-top: 1px solid rgba(255, 255, 255, 0.05);
    }}
    .read-toggle-btn {{
      background: transparent;
      border: none;
      color: var(--text-muted);
      font-size: 12px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 4px;
      padding: 4px 0;
    }}
    .origin-link {{
      color: var(--accent);
      text-decoration: none;
      font-size: 13px;
      font-weight: 600;
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }}

    /* フッター */
    footer {{
      text-align: center;
      padding: 24px 16px;
      font-size: 12px;
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
          <h1>AI Daily</h1>
          <div class="updated-text">更新: {now_jst} ({total_count}件)</div>
        </div>
      </div>
      <button class="mark-read-btn" id="markAllBtn">全件既読</button>
    </div>
  </header>

  <div class="tabs-wrap">
    <a href="#all" class="tab-item active" onclick="switchTab(event, 'all')">すべて ({total_count})</a>
"""

    for cat in categories:
        cid = cat["id"]
        cname = cat["name"]
        count = len(cat_articles.get(cid, []))
        if count > 0:
            html_content += f'    <a href="#{cid}" class="tab-item" onclick="switchTab(event, \'{cid}\')">{cname} ({count})</a>\n'

    html_content += """  </div>

  <main id="mainContent">
"""

    for cat in categories:
        cid = cat["id"]
        cname = cat["name"]
        items = cat_articles.get(cid, [])
        if not items:
            continue

        html_content += f"""
    <section class="category-section" id="section-{cid}">
      <div class="category-header">
        <span class="category-title">{cname}</span>
        <span class="category-count">{len(items)}件</span>
      </div>
"""
        for art in items:
            art_id = art["id"]
            title_ja = html.escape(art.get("title_ja", art["title"]))
            summary = html.escape(art.get("summary", ""))
            source = html.escape(art["source"])
            link = html.escape(art["link"])
            tags_html = "".join([f'<span class="tag-badge">#{html.escape(t)}</span>' for t in art.get("tags", [])])

            html_content += f"""
      <article class="article-card" id="card-{art_id}" data-id="{art_id}">
        <div class="meta-row">
          <span class="source-tag">{source}</span>
          <span>{art['published_dt'].strftime('%m/%d %H:%M')}</span>
        </div>
        <h2 class="card-title">{title_ja}</h2>
        <p class="card-summary">{summary}</p>
        <div class="tags-row">{tags_html}</div>
        <div class="card-footer">
          <button class="read-toggle-btn" onclick="toggleRead('{art_id}')">
            <span class="read-icon">✓</span> <span class="read-text">既読にする</span>
          </button>
          <a href="{link}" target="_blank" rel="noopener noreferrer" class="origin-link" onclick="markRead('{art_id}')">元記事を読む →</a>
        </div>
      </article>
"""
        html_content += "    </section>\n"

    html_content += """  </main>

  <footer>
    <p>AI Daily - 朝のスキマ時間で追いつく最新AIニュースまとめ</p>
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

    // 既読管理 (localStorage)
    const STORAGE_KEY = 'ai_daily_read_ids';
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

    function applyReadState() {
      const readIds = new Set(getReadIds());
      document.querySelectorAll('.article-card').forEach(card => {
        const id = card.getAttribute('data-id');
        const isRead = readIds.has(id);
        if (isRead) {
          card.classList.add('is-read');
          const btnText = card.querySelector('.read-text');
          if (btnText) btnText.textContent = '既読解除';
        } else {
          card.classList.remove('is-read');
          const btnText = card.querySelector('.read-text');
          if (btnText) btnText.textContent = '既読にする';
        }
      });
    }

    function toggleRead(id) {
      let readIds = getReadIds();
      if (readIds.includes(id)) {
        readIds = readIds.filter(x => x !== id);
      } else {
        readIds.push(id);
      }
      saveReadIds(readIds);
      applyReadState();
    }

    function markRead(id) {
      let readIds = getReadIds();
      if (!readIds.includes(id)) {
        readIds.push(id);
        saveReadIds(readIds);
        applyReadState();
      }
    }

    document.getElementById('markAllBtn').addEventListener('click', () => {
      const allCards = document.querySelectorAll('.article-card');
      const allIds = Array.from(allCards).map(c => c.getAttribute('data-id'));
      saveReadIds(allIds);
      applyReadState();
    });

    // タブ切り替え
    function switchTab(event, targetCat) {
      event.preventDefault();
      document.querySelectorAll('.tab-item').forEach(el => el.classList.remove('active'));
      event.currentTarget.classList.add('active');

      const sections = document.querySelectorAll('.category-section');
      if (targetCat === 'all') {
        sections.forEach(sec => sec.style.display = 'block');
      } else {
        sections.forEach(sec => {
          if (sec.id === 'section-' + targetCat) {
            sec.style.display = 'block';
          } else {
            sec.style.display = 'none';
          }
        });
      }
    }

    // 初期化
    applyReadState();
  </script>
</body>
</html>
"""

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"  -> Generated: {output_path} ({len(html_content)} bytes)")

def main():
    print("=== AI Daily News Build Started ===")
    config = load_feeds_config()
    seen_dict = load_seen_articles()

    # 1. RSS収集
    raw_articles = collect_all_articles(config)

    # 2. 重複・既読除外
    articles = filter_and_deduplicate(raw_articles, seen_dict)

    # 3. Gemini要約
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
