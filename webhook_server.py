# -*- coding: utf-8 -*-
"""
webhook_server.py - ラノベ執筆RAGシステム Webhook 受信 API

git push トリガーで呼び出される FastAPI サーバー。
ポート 8766 でリッスンし、POST /webhook を受け付ける。

処理フロー:
    1. X-Webhook-Secret ヘッダーでリクエストを検証
    2. git pull を実行して最新化
    3. 変更ファイル一覧を取得
    4. .md/.txt ファイルを source_type="ai" で Qdrant に登録
    5. 処理結果を JSON で返す
"""

import os
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
import uvicorn

# コアロジックをインポート
import ingest as core

# ── 環境変数 ────────────────────────────────────────────────────────────────
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
REPO_DIR = "/app/repo"

app = FastAPI(
    title="Novel RAG Webhook API",
    description="git push トリガーで RAG を自動更新する Webhook サーバー",
    version="1.0.0",
)


# ── Git 操作ユーティリティ ───────────────────────────────────────────────────
def _git_clone_or_pull() -> tuple[bool, str, list[str]]:
    """
    リポジトリが存在しなければ clone、存在すれば pull を実行する。

    Returns:
        (success, message, changed_files)
    """
    repo_path = Path(REPO_DIR)

    if not GITHUB_TOKEN or not GITHUB_REPO:
        return False, "GITHUB_TOKEN または GITHUB_REPO が設定されていません。", []

    git_url = f"https://{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git"

    if not (repo_path / ".git").exists():
        # ── 初回クローン ──
        result = subprocess.run(
            ["git", "clone", git_url, REPO_DIR],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            return False, f"git clone 失敗: {result.stderr}", []
        return True, "初回クローン完了", []

    # ── pull 前の HEAD を記録 ──────────────────────────────────────────────
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_DIR, capture_output=True, text=True,
    ).stdout.strip()

    # ── URL にトークンを埋め込んで pull ──────────────────────────────────
    subprocess.run(
        ["git", "remote", "set-url", "origin", git_url],
        cwd=REPO_DIR, capture_output=True,
    )
    branch_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=REPO_DIR, capture_output=True, text=True,
    )
    branch = branch_result.stdout.strip() or "main"
    pull_result = subprocess.run(
        ["git", "pull", "origin", branch],
        cwd=REPO_DIR, capture_output=True, text=True, timeout=120,
    )
    if pull_result.returncode != 0:
        return False, f"git pull 失敗: {pull_result.stderr}", []

    # ── pull 後の HEAD を取得して差分ファイル一覧を作成 ──────────────────
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_DIR, capture_output=True, text=True,
    ).stdout.strip()

    if before == after:
        return True, "変更なし（最新の状態です）", []

    diff_result = subprocess.run(
        ["git", "diff", "--name-only", before, after],
        cwd=REPO_DIR, capture_output=True, text=True,
    )
    changed = [
        f for f in diff_result.stdout.strip().split("\n")
        if f and Path(f).suffix in (".md", ".txt")
    ]
    return True, f"git pull 完了（{len(changed)} ファイル変更）", changed


# ── Webhook エンドポイント ──────────────────────────────────────────────────
@app.post("/webhook")
async def webhook(
    request: Request,
    x_webhook_secret: str | None = Header(default=None),
) -> JSONResponse:
    """
    git push トリガーで呼び出される Webhook エンドポイント。

    Headers:
        X-Webhook-Secret: WEBHOOK_SECRET 環境変数と一致するシークレット

    Returns:
        {
            "success": bool,
            "message": str,
            "changed_files": [...],
            "ingest_results": [...]
        }
    """
    # ── シークレット検証 ──────────────────────────────────────────────────
    if not WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="WEBHOOK_SECRET が設定されていません。")
    if x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="シークレットが一致しません。")

    # ── git pull ──────────────────────────────────────────────────────────
    pull_ok, pull_msg, changed_files = _git_clone_or_pull()
    if not pull_ok:
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": pull_msg, "changed_files": [], "ingest_results": []},
        )

    if not changed_files:
        return JSONResponse(content={
            "success": True,
            "message": pull_msg,
            "changed_files": [],
            "ingest_results": [],
        })

    # ── 変更ファイルを ingest ─────────────────────────────────────────────
    ingest_results = []
    for rel_path in changed_files:
        abs_path = Path(REPO_DIR) / rel_path

        # rag_data/ 配下のファイルのみ対象
        try:
            # repo/rag_data/... → /app/rag_data/... にコピーして登録
            rag_rel = Path(rel_path)
            parts = rag_rel.parts
            if "rag_data" not in parts:
                continue
            rag_idx = parts.index("rag_data")
            target_path = Path("/app") / Path(*parts[rag_idx:])
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(abs_path.read_bytes())
        except Exception as e:
            ingest_results.append({"file": rel_path, "success": False, "message": str(e)})
            continue

        result = core.ingest_file(
            file_path=str(target_path),
            source_type="ai",
            allow_overwrite_checked=False,
        )
        ingest_results.append({"file": rel_path, **result})

    success_count = sum(1 for r in ingest_results if r.get("success"))
    return JSONResponse(content={
        "success": True,
        "message": f"{pull_msg} / ingest: {success_count}/{len(ingest_results)} 件成功",
        "changed_files": changed_files,
        "ingest_results": ingest_results,
    })


@app.get("/health")
async def health() -> dict:
    """ヘルスチェックエンドポイント。"""
    return {"status": "ok", "service": "webhook_server"}


# ── エントリポイント ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("WEBHOOK_PORT", "8766"))
    print(f"🚀 Webhook サーバー起動中... http://0.0.0.0:{port}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port)
