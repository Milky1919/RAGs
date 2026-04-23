# -*- coding: utf-8 -*-
"""
search.py - ラノベ執筆RAGシステム 検索スクリプト

Qdrant から類似検索を行い、メタデータ付きで結果を出力する。
単体テスト・動作確認用。

使い方:
    python search.py --query "検索したい文章" --top-k 5
    python search.py --query "検索したい文章" --category chapter --top-k 5
    python search.py --query "検索したい文章" --volume 1
"""

import sys
import argparse

# Windows での文字化け防止
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import os
import requests
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue, models

# ── 定数 ──────────────────────────────────────────────────────────────────
_qdrant_host = os.environ.get("QDRANT_HOST", "localhost")
_qdrant_port = os.environ.get("QDRANT_PORT", "6333")
QDRANT_URL = f"http://{_qdrant_host}:{_qdrant_port}"
_ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_URL = f"{_ollama_host}/api/embed"
OLLAMA_MODEL = "snowflake-arctic-embed2"
COLLECTION_NAME = "novel_rag"
TOP_K_MAX = 15


# ── Qdrant クライアント ───────────────────────────────────────────────────
def get_qdrant_client() -> QdrantClient:
    """Qdrant クライアントを返す。接続エラー時はメッセージを出して例外送出。"""
    try:
        client = QdrantClient(url=QDRANT_URL, timeout=30)
        client.get_collections()
        return client
    except Exception:
        print(
            "❌ Qdrant に接続できません。Dockerコンテナが起動しているか確認してください。",
            file=sys.stderr,
        )
        raise


# ── 埋め込み生成 ─────────────────────────────────────────────────────────
def get_embedding(text: str) -> list[float]:
    """
    Ollama REST API で埋め込みベクトルを生成する。
    接続エラー時はわかりやすいメッセージを出力して例外を再送出する。
    """
    payload = {"model": OLLAMA_MODEL, "input": text}
    try:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=120)
        resp.raise_for_status()
    except requests.exceptions.ConnectionError:
        print(
            "❌ Ollama に接続できません。"
            "Ollamaが起動しているか、http://100.119.108.16:11434 に接続できるか確認してください。",
            file=sys.stderr,
        )
        raise
    except requests.exceptions.HTTPError as e:
        print(f"❌ Ollama APIエラー: {e}", file=sys.stderr)
        raise

    data = resp.json()
    embeddings = data.get("embeddings") or data.get("embedding")
    if not embeddings:
        raise ValueError(f"Ollamaから埋め込みが返されませんでした: {data}")

    if isinstance(embeddings[0], list):
        return embeddings[0]
    return embeddings


# ── フィルター構築 ────────────────────────────────────────────────────────
def build_filter(category: str | None, volume: str | None) -> Filter | None:
    """カテゴリ・巻数フィルターを構築する。指定がなければ None を返す。"""
    conditions = []
    if category:
        conditions.append(
            FieldCondition(key="category", match=MatchValue(value=category))
        )
    if volume:
        conditions.append(
            FieldCondition(key="volume", match=MatchValue(value=volume))
        )
    if conditions:
        return Filter(must=conditions)
    return None


# ── 検索メイン ───────────────────────────────────────────────────────────
def search(
    query: str,
    top_k: int = 5,
    category: str | None = None,
    volume: str | None = None,
) -> list[dict]:
    """
    Qdrant に類似検索をかけ、結果リストを返す。
    各要素は {"score": float, "payload": dict} の形式。
    """
    client = get_qdrant_client()
    vector = get_embedding(query)
    query_filter = build_filter(category, volume)

    results = client.query_points(
        collection_name=COLLECTION_NAME,
        query=vector,
        limit=min(top_k, TOP_K_MAX),
        query_filter=query_filter,
        with_payload=True,
    ).points

    return [{"score": r.score, "payload": r.payload} for r in results]


# ── 結果表示 ─────────────────────────────────────────────────────────────
def print_results(results: list[dict]) -> None:
    """検索結果を指定フォーマットで標準出力に表示する。"""
    if not results:
        print("🔍 検索結果が見つかりませんでした。")
        return

    for r in results:
        p = r["payload"]
        source_file = p.get("source_file", "不明")
        volume = p.get("volume") or "N/A"
        chapter = p.get("chapter") or "N/A"
        category = p.get("category", "不明")
        status = p.get("status", "不明")
        text = p.get("text", "")
        score = r["score"]

        print(
            f"[参照元: {source_file} / Vol: {volume} / Ch: {chapter} "
            f"/ Category: {category} / Status: {status}]"
        )
        print(text)
        print(f"スコア: {score:.4f}")
        print("---")


# ── CLI エントリポイント ─────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="ラノベRAG 検索スクリプト - Qdrant から類似検索する"
    )
    parser.add_argument("--query", type=str, required=True, help="検索クエリ文字列")
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help=f"取得件数（デフォルト5、最大{TOP_K_MAX}）",
    )
    parser.add_argument(
        "--category",
        type=str,
        choices=["chapter", "settings", "plot"],
        default=None,
        help="カテゴリフィルター（chapter / settings / plot）",
    )
    parser.add_argument(
        "--volume",
        type=str,
        default=None,
        help="巻数フィルター（例: 1, 2）",
    )
    args = parser.parse_args()

    top_k = min(max(1, args.top_k), TOP_K_MAX)
    results = search(args.query, top_k=top_k, category=args.category, volume=args.volume)
    print_results(results)


if __name__ == "__main__":
    main()
