# -*- coding: utf-8 -*-
"""
mcp_server.py - ラノベ執筆RAGシステム MCPサーバー（HTTP/SSE方式）

Claude Code がリモートから接続するための MCP サーバー。
HTTP/SSE トランスポートで動作し、ポート 8765 でリッスンする。

Claude Code 設定例（claude_desktop_config.json）:
    {
      "mcpServers": {
        "novel-rag": {
          "type": "sse",
          "url": "http://SERVER_IP:8765/sse"
        }
      }
    }

提供するツール:
    - rag_search  : 類似検索
    - rag_ingest  : ファイル登録（確認済みファイルへの上書きは拒否）
    - rag_status  : コレクション状態確認
"""

import sys
import os

# Windows での文字化け防止（コンテナ内でも念のため設定）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

from mcp.server.fastmcp import FastMCP

# コアロジックをインポート
import ingest as core

# ── MCP サーバー初期化（host/port はコンストラクタで指定） ───────────────
_mcp_port = int(os.environ.get("MCP_PORT", "8765"))
mcp = FastMCP("novel-rag-server", host="0.0.0.0", port=_mcp_port)


# ── ツール定義 ────────────────────────────────────────────────────────────

@mcp.tool()
def rag_search(
    query: str,
    top_k: int = 5,
    category: str | None = None,
    volume: str | None = None,
    source_type: str | None = None,
    status: str | None = None,
) -> str:
    """
    RAG 類似検索ツール。

    Args:
        query:       検索クエリ文字列
        top_k:       取得件数（デフォルト5、最大15）
        category:    カテゴリフィルター（chapter / settings / plot）
        volume:      巻数フィルター（例: "1", "2"）
        source_type: 生成元フィルター（ai / human）
        status:      ステータスフィルター（checked / unchecked）

    Returns:
        メタデータヘッダー付きの検索結果テキスト
    """
    try:
        results = core.search(
            query=query,
            top_k=top_k,
            category=category,
            volume=volume,
            source_type=source_type,
            status=status,
        )
    except RuntimeError as e:
        return f"❌ エラー: {e}"

    if not results:
        return "検索結果が見つかりませんでした。"

    lines = []
    for r in results:
        p = r["payload"]
        lines.append(
            f"[参照元: {p.get('source_file', '不明')} / "
            f"Vol: {p.get('volume') or 'N/A'} / "
            f"Ch: {p.get('chapter') or 'N/A'} / "
            f"Category: {p.get('category', '不明')} / "
            f"Source: {p.get('source_type', '不明')} / "
            f"Status: {p.get('status', '不明')}]"
        )
        lines.append(p.get("text", ""))
        lines.append(f"スコア: {r['score']:.4f}")
        lines.append("---")

    return "\n".join(lines)


@mcp.tool()
def rag_ingest(file_path: str, source_type: str = "ai") -> str:
    """
    ファイル登録ツール。

    確認済み（status: checked）ファイルへの上書きは拒否してエラーを返す。
    AI が生成したファイルは source_type="ai" で登録すること。

    Args:
        file_path:   登録するファイルのパス
                     （例: "rag_data/chapters/1巻第1章.md" または絶対パス）
        source_type: "ai" または "human"（デフォルト "ai"）

    Returns:
        処理結果メッセージ
    """
    if source_type not in ("ai", "human"):
        return f"❌ source_type は 'ai' または 'human' を指定してください。（受け取った値: {source_type}）"

    result = core.ingest_file(
        file_path=file_path,
        source_type=source_type,
        allow_overwrite_checked=False,  # MCP 経由は確認済みファイルへの上書き禁止
    )

    if not result["success"]:
        return f"❌ {result['message']}"

    parts = [result["message"]]
    if result["deleted"] > 0:
        parts.append(f"（既存 {result['deleted']} チャンクを削除して再登録）")
    if result.get("category"):
        parts.append(f"カテゴリ: {result['category']}")
    if result.get("volume"):
        parts.append(f"巻: {result['volume']}")
    if result.get("chapter"):
        parts.append(f"章: {result['chapter']}")

    return " ".join(parts)


@mcp.tool()
def rag_status() -> str:
    """
    コレクション状態確認ツール。

    Returns:
        総チャンク数・カテゴリ別内訳・source_type別内訳・status別内訳のテキスト
    """
    try:
        client = core.get_qdrant_client()
    except RuntimeError as e:
        return f"❌ エラー: {e}"

    try:
        info = client.get_collection(core.COLLECTION_NAME)
    except Exception:
        return (
            f"コレクション '{core.COLLECTION_NAME}' が存在しません。"
            "ファイルを登録してください。"
        )

    total = info.indexed_vectors_count or info.points_count or 0

    # カテゴリ別集計
    from qdrant_client.models import Filter, FieldCondition, MatchValue
    lines = [
        f"📚 コレクション: {core.COLLECTION_NAME}",
        f"総チャンク数: {total}",
        "",
        "【カテゴリ別】",
    ]
    for cat in ["chapter", "settings", "plot", "other"]:
        count = client.count(
            collection_name=core.COLLECTION_NAME,
            count_filter=Filter(must=[FieldCondition(key="category", match=MatchValue(value=cat))]),
            exact=True,
        ).count
        lines.append(f"  {cat}: {count} チャンク")

    lines.append("")
    lines.append("【source_type別】")
    for stype in ["ai", "human"]:
        count = client.count(
            collection_name=core.COLLECTION_NAME,
            count_filter=Filter(must=[FieldCondition(key="source_type", match=MatchValue(value=stype))]),
            exact=True,
        ).count
        lines.append(f"  {stype}: {count} チャンク")

    lines.append("")
    lines.append("【status別】")
    for st in ["checked", "unchecked"]:
        count = client.count(
            collection_name=core.COLLECTION_NAME,
            count_filter=Filter(must=[FieldCondition(key="status", match=MatchValue(value=st))]),
            exact=True,
        ).count
        lines.append(f"  {st}: {count} チャンク")

    return "\n".join(lines)


# ── エントリポイント ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"🚀 MCP サーバー起動中... http://0.0.0.0:{_mcp_port}/sse", flush=True)
    mcp.run(transport="sse")
