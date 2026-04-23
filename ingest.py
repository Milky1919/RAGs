# -*- coding: utf-8 -*-
"""
ingest.py - ラノベ執筆RAGシステム コアインジェストモジュール

チャンク分割・埋め込み生成・Qdrant登録の共通ロジック。
webhook_server.py と mcp_server.py から呼び出される。

【確認済みファイルの保護ルール】
- status: "checked" のファイルは、allow_overwrite_checked=False の場合に上書き拒否
- Web UI からのアップロードは allow_overwrite_checked=True で呼び出す
- AI（MCP/Webhook 経由）は allow_overwrite_checked=False で呼び出す
"""

import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
    PointIdsList,
    Filter,
    FieldCondition,
    MatchValue,
)

# ── 環境変数から接続先を取得（コンテナ内 / ローカル両対応） ────────────
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://host.docker.internal:11434")
OLLAMA_URL = f"{OLLAMA_HOST}/api/embed"
OLLAMA_MODEL = "snowflake-arctic-embed2"

QDRANT_HOST = os.environ.get("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))
QDRANT_URL = f"http://{QDRANT_HOST}:{QDRANT_PORT}"

COLLECTION_NAME = "novel_rag"
VECTOR_DIM = 1024       # snowflake-arctic-embed2 の出力次元
CHUNK_SIZE = 600        # チャンク基準サイズ（文字数）
OVERLAP = 80            # 前後オーバーラップ（文字数）


# ── Qdrant クライアント ────────────────────────────────────────────────────
def get_qdrant_client() -> QdrantClient:
    """Qdrant クライアントを返す。接続失敗時は RuntimeError を送出。"""
    try:
        client = QdrantClient(url=QDRANT_URL, timeout=30)
        client.get_collections()  # 接続テスト
        return client
    except Exception as e:
        raise RuntimeError(
            "Qdrantコンテナが起動しているか確認してください。"
            f"（接続先: {QDRANT_URL}）"
        ) from e


def ensure_collection(client: QdrantClient) -> None:
    """コレクションが存在しなければ作成する。"""
    existing = [c.name for c in client.get_collections().collections]
    if COLLECTION_NAME not in existing:
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
        )


# ── ファイル保護チェック ────────────────────────────────────────────────────
def is_file_checked(client: QdrantClient, source_file: str) -> bool:
    """
    指定ファイルのチャンクに status: checked のものが1件でもあれば True を返す。
    確認済みファイルへの AI 上書きを防止するために使用する。
    """
    checked_filter = Filter(
        must=[
            FieldCondition(key="source_file", match=MatchValue(value=source_file)),
            FieldCondition(key="status", match=MatchValue(value="checked")),
        ]
    )
    result, _ = client.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=checked_filter,
        limit=1,
        with_payload=False,
        with_vectors=False,
    )
    return len(result) > 0


# ── ステータス更新（Web UI 用） ───────────────────────────────────────────
def update_file_status(client: QdrantClient, source_file: str, new_status: str) -> int:
    """
    指定ファイルの全チャンクの status を new_status に更新する。
    Web UI からの「確認済み」操作に使用する。
    戻り値: 更新したチャンク数
    """
    if new_status != "checked":
        raise ValueError(f"update_file_status は 'checked' への昇格のみ許可されています（要求値: {new_status!r}）")

    # 全チャンクの ID を収集
    file_filter = Filter(
        must=[FieldCondition(key="source_file", match=MatchValue(value=source_file))]
    )
    ids: list = []
    offset = None
    while True:
        result, next_offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=file_filter,
            limit=100,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        ids.extend([p.id for p in result])
        if next_offset is None:
            break
        offset = next_offset

    if not ids:
        return 0

    # ペイロードを一括更新
    client.set_payload(
        collection_name=COLLECTION_NAME,
        payload={"status": new_status},
        points=PointIdsList(points=ids),
    )
    return len(ids)


# ── 既存チャンク削除 ─────────────────────────────────────────────────────────
def delete_chunks(client: QdrantClient, source_file: str) -> int:
    """
    指定ファイルの既存チャンクをすべて削除する。
    戻り値: 削除したチャンク数
    """
    file_filter = Filter(
        must=[FieldCondition(key="source_file", match=MatchValue(value=source_file))]
    )
    ids: list = []
    offset = None
    while True:
        result, next_offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=file_filter,
            limit=100,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        ids.extend([p.id for p in result])
        if next_offset is None:
            break
        offset = next_offset

    if ids:
        client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=PointIdsList(points=ids),
        )
    return len(ids)


# ── カテゴリ・巻・章の自動判定 ────────────────────────────────────────────
def detect_category(file_path: Path) -> str:
    """
    rag_data/ 配下のディレクトリ名からカテゴリを自動判定する。
    settings/ → "settings", chapters/ → "chapter", plot/ → "plot", その他 → "other"
    """
    parts = file_path.resolve().parts
    for i, part in enumerate(parts):
        if part == "rag_data" and i + 1 < len(parts):
            mapping = {"settings": "settings", "chapters": "chapter", "plot": "plot"}
            return mapping.get(parts[i + 1], "other")
    return "other"


def extract_volume(filename: str) -> str | None:
    """ファイル名から巻数を抽出する（1巻, vol01, v1 等に対応）。"""
    for pat in [r"(\d+)巻", r"[Vv][Oo][Ll]0*(\d+)", r"(?<![a-zA-Z0-9])v0*(\d+)(?![a-zA-Z0-9])"]:
        m = re.search(pat, filename)
        if m:
            return m.group(1)
    return None


def extract_chapter(filename: str) -> str | None:
    """ファイル名から章番号を抽出する（第1章, 1章, ch01 等に対応）。"""
    for pat in [r"第(\d+)章", r"(\d+)章", r"[Cc][Hh]0*(\d+)"]:
        m = re.search(pat, filename)
        if m:
            return m.group(1)
    return None


# ── チャンク分割（ラノベ特化） ────────────────────────────────────────────
def split_into_chunks(text: str) -> list[str]:
    """
    ラノベ向けチャンク分割。

    ルール:
    1. 空行（段落区切り）を優先して分割する
    2. 基準サイズ (CHUNK_SIZE) を超えたら次の段落区切りで分割
    3. 「」で始まる台詞の途中では分割しない（未閉じ「 を検出）
    4. 前後 OVERLAP 文字のオーバーラップを持たせる
    """
    # 空行で段落に分割
    paragraphs = re.split(r"\n\s*\n", text.strip())
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    chunks: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    def is_in_dialogue(parts: list[str]) -> bool:
        """積み上げた段落群が未閉じの台詞（「 > 」）で終わっているか判定。"""
        combined = "\n\n".join(parts)
        return combined.count("「") > combined.count("」")

    for para in paragraphs:
        tentative_len = current_len + len(para) + (2 if current_parts else 0)

        if tentative_len <= CHUNK_SIZE:
            current_parts.append(para)
            current_len = tentative_len
        else:
            # 台詞途中なら延長、そうでなければチャンク確定
            if current_parts and is_in_dialogue(current_parts):
                current_parts.append(para)
                current_len = tentative_len
            else:
                if current_parts:
                    chunks.append("\n\n".join(current_parts))
                current_parts = [para]
                current_len = len(para)

    if current_parts:
        chunks.append("\n\n".join(current_parts))

    # 前後オーバーラップを付与
    overlapped: list[str] = []
    for i, chunk in enumerate(chunks):
        prefix = chunks[i - 1][-OVERLAP:] if i > 0 else ""
        suffix = chunks[i + 1][:OVERLAP] if i < len(chunks) - 1 else ""
        body = chunk
        if prefix:
            body = prefix + "\n" + body
        if suffix:
            body = body + "\n" + suffix
        overlapped.append(body)

    return overlapped


# ── Ollama 埋め込み生成 ────────────────────────────────────────────────────
def _call_ollama_embed(input_data) -> list:
    """
    Ollama /api/embed エンドポイントを呼び出す内部関数。
    input_data は str（単一）または list[str]（バッチ）を受け付ける。
    """
    payload = {"model": OLLAMA_MODEL, "input": input_data}
    try:
        resp = requests.post(OLLAMA_URL, json=payload, timeout=300)
        resp.raise_for_status()
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(
            "Ollamaが起動しているか、host.docker.internalに接続できるか確認してください。"
            f"（接続先: {OLLAMA_URL}）"
        ) from e
    except requests.exceptions.HTTPError as e:
        raise RuntimeError(f"Ollama APIエラー: {e}") from e

    data = resp.json()
    embeddings = data.get("embeddings") or data.get("embedding")
    if not embeddings:
        raise ValueError(f"Ollamaから埋め込みが返されませんでした: {data}")
    return embeddings


def get_embedding(text: str) -> list[float]:
    """単一テキストの埋め込みベクトルを生成する（検索クエリ用）。"""
    embeddings = _call_ollama_embed(text)
    if isinstance(embeddings[0], list):
        return embeddings[0]
    return embeddings


def get_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """複数テキストの埋め込みを1回の API コールで一括生成する（高速）。"""
    return _call_ollama_embed(texts)


# ── ファイル統計取得（Web UI / rag_status 用） ─────────────────────────────
def get_file_stats(client: QdrantClient) -> list[dict]:
    """
    Qdrant 内の全ファイルの統計情報を取得する。
    戻り値: [{source_file, source_type, status, category, chunk_count, created_at}, ...]
    """
    files: dict[str, dict] = {}
    offset = None
    while True:
        result, next_offset = client.scroll(
            collection_name=COLLECTION_NAME,
            limit=100,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for point in result:
            p = point.payload or {}
            fname = p.get("source_file", "unknown")
            if fname not in files:
                files[fname] = {
                    "source_file": fname,
                    "source_type": p.get("source_type", "unknown"),
                    "status": p.get("status", "unknown"),
                    "category": p.get("category", "other"),
                    "volume": p.get("volume"),
                    "chapter": p.get("chapter"),
                    "chunk_count": 0,
                    "created_at": p.get("created_at", ""),
                }
            files[fname]["chunk_count"] += 1
        if next_offset is None:
            break
        offset = next_offset
    return list(files.values())


# ── メインインジェスト関数 ────────────────────────────────────────────────
def ingest_file(
    file_path: str | Path,
    source_type: str,
    allow_overwrite_checked: bool = False,
) -> dict:
    """
    ファイルを読み込み、チャンク分割して Qdrant に登録する。

    Args:
        file_path: 登録するファイルのパス（絶対パスまたは /app からの相対パス）
        source_type: "ai" または "human"
        allow_overwrite_checked: True の場合、確認済みファイルも上書き可（Web UI 用）

    Returns:
        {"success": bool, "message": str, "chunks": int, "deleted": int}
    """
    path = Path(file_path)
    if not path.is_absolute():
        path = Path("/app") / path

    if not path.exists():
        return {"success": False, "message": f"ファイルが見つかりません: {file_path}", "chunks": 0, "deleted": 0}
    if path.suffix not in (".md", ".txt"):
        return {"success": False, "message": f"対応していない形式です（.md/.txt のみ）: {file_path}", "chunks": 0, "deleted": 0}

    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {"success": False, "message": f"ファイルが空です: {file_path}", "chunks": 0, "deleted": 0}

    client = get_qdrant_client()
    ensure_collection(client)

    source_file = path.name

    # ── 確認済みファイルへの上書き保護 ──────────────────────────────────
    if not allow_overwrite_checked and is_file_checked(client, source_file):
        return {
            "success": False,
            "message": (
                f"'{source_file}' は確認済み（status: checked）のため上書きできません。"
                "Web UI から変更してください。"
            ),
            "chunks": 0,
            "deleted": 0,
        }

    # ── 既存チャンク削除（再登録時の重複防止） ───────────────────────────
    deleted = delete_chunks(client, source_file)

    # ── メタデータ構築 ────────────────────────────────────────────────────
    category = detect_category(path)
    volume = extract_volume(path.stem)
    chapter = extract_chapter(path.stem)
    created_at = datetime.now(timezone.utc).isoformat()

    # ── チャンク分割 → バッチ埋め込み → 登録 ────────────────────────────
    chunks = split_into_chunks(text)
    vectors = get_embeddings_batch(chunks)

    points = [
        PointStruct(
            id=str(uuid.uuid4()),
            vector=vector,
            payload={
                "source_file": source_file,
                "source_type": source_type,
                "status": "unchecked",   # 新規登録は常に unchecked
                "category": category,
                "volume": volume,
                "chapter": chapter,
                "created_at": created_at,
                "chunk_index": idx,
                "text": chunk,
            },
        )
        for idx, (chunk, vector) in enumerate(zip(chunks, vectors))
    ]

    client.upsert(collection_name=COLLECTION_NAME, points=points)

    return {
        "success": True,
        "message": f"'{source_file}' を {len(points)} チャンク登録しました。",
        "chunks": len(points),
        "deleted": deleted,
        "category": category,
        "volume": volume,
        "chapter": chapter,
    }


# ── 検索関数（mcp_server / web_ui から共通利用） ─────────────────────────
def search(
    query: str,
    top_k: int = 5,
    category: str | None = None,
    volume: str | None = None,
    source_type: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """
    Qdrant に類似検索をかける。

    Returns:
        [{"score": float, "payload": dict}, ...]
    """
    client = get_qdrant_client()
    vector = get_embedding(query)

    conditions = []
    if category:
        conditions.append(FieldCondition(key="category", match=MatchValue(value=category)))
    if volume:
        conditions.append(FieldCondition(key="volume", match=MatchValue(value=volume)))
    if source_type:
        conditions.append(FieldCondition(key="source_type", match=MatchValue(value=source_type)))
    if status:
        conditions.append(FieldCondition(key="status", match=MatchValue(value=status)))

    query_filter = Filter(must=conditions) if conditions else None
    top_k = min(max(1, top_k), 15)

    results = client.query_points(
        collection_name=COLLECTION_NAME,
        query=vector,
        limit=top_k,
        query_filter=query_filter,
        with_payload=True,
    ).points

    return [{"score": r.score, "payload": r.payload} for r in results]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="ファイルをRAGシステムに登録する")
    parser.add_argument("--file", required=True, help="登録するファイルパス")
    parser.add_argument("--source-type", required=True, choices=["ai", "human"], help="ソースタイプ")
    parser.add_argument("--allow-overwrite-checked", action="store_true", help="確認済みファイルの上書きを許可する")
    args = parser.parse_args()

    result = ingest_file(
        file_path=args.file,
        source_type=args.source_type,
        allow_overwrite_checked=args.allow_overwrite_checked,
    )

    if result["success"]:
        print(f"登録完了: {result['message']}")
        print(f"登録チャンク数: {result['chunks']} / 削除チャンク数: {result['deleted']}")
    else:
        print(f"エラー: {result['message']}", file=sys.stderr)
        sys.exit(1)
