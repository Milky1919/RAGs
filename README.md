# 自律型ラノベ執筆 RAG システム

Claude Code と連携してライトノベル執筆を支援するサーバー型 RAG システムです。

```
git push → Webhook → git pull → 自動 ingest → Qdrant 更新
Claude Code ← MCP（HTTP/SSE）← rag_search / rag_ingest / rag_status
```

---

## アーキテクチャ

| サービス | ポート | 役割 |
|---|---|---|
| Qdrant | 6333 / 6334 | ベクターDB |
| MCP サーバー | 8765 | Claude Code 連携（HTTP/SSE） |
| Webhook API | 8766 | git push トリガーで自動 ingest |
| Streamlit Web UI | 8767 | 確認済みフラグ付与・管理画面 |

---

## セットアップ手順

### 1. GitHub リポジトリのセットアップ

原稿を管理する GitHub リポジトリを用意し、以下の構成にしてください:

```
your-novel-repo/
└── rag_data/
    ├── settings/    ← キャラクター設定・世界観・用語集
    ├── chapters/    ← 完成した本編章ファイル
    └── plot/        ← あらすじ・プロット
```

次に GitHub の **Personal Access Token** を発行します:
1. GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens
2. 対象リポジトリに `Contents: Read` 権限を付与
3. トークン文字列をメモしておく

---

### 2. .env ファイルの作成

サーバーの RAG プロジェクトディレクトリで:

```bash
cp .env.example .env
```

`.env` を編集して各値を設定してください:

```env
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxx
GITHUB_REPO=your-username/your-novel-repo
WEBHOOK_SECRET=任意のランダム文字列（32文字以上推奨）
OLLAMA_HOST=http://host.docker.internal:11434
QDRANT_HOST=qdrant
QDRANT_PORT=6333
```

---

### 3. Dockge へのスタック登録・起動

1. サーバーの Dockge UI（通常 `http://サーバーIP:5001`）を開く
2. 「New Stack」をクリック
3. スタック名を `novel-rag` に設定
4. このプロジェクトのディレクトリを Dockge のスタックとして登録
5. `.env` が同じディレクトリにあることを確認して「Deploy」

起動確認:
- Qdrant UI: `http://サーバーIP:6333/dashboard`
- Web UI: `http://サーバーIP:8767`

---

### 4. ローカル PC への git hook インストール

原稿リポジトリのローカルコピーに移動して:

```bash
# フックをコピー（git_hook_template.sh を原稿リポジトリに配置した場合）
cp git_hook_template.sh /path/to/your-novel-repo/.git/hooks/post-push

# 実行権限を付与
chmod +x /path/to/your-novel-repo/.git/hooks/post-push
```

フック内の以下の値を書き換えてください:

```bash
WEBHOOK_URL="http://YOUR_SERVER_IP:8766/webhook"
WEBHOOK_SECRET="your-webhook-secret-here"   # .env の WEBHOOK_SECRET と一致させること
```

> **git に標準の post-push フックはありません。**
> または、以下のように git エイリアスとして登録する方法もあります:
> ```bash
> git config alias.push-rag '!git push "$@" && /path/to/git_hook_template.sh'
> # → git push-rag origin main で使用可能
> ```

---

### 5. Claude Code MCP 設定方法

`~/.claude/claude_desktop_config.json`（Windows: `%APPDATA%\Claude\claude_desktop_config.json`）に追加:

```json
{
  "mcpServers": {
    "novel-rag": {
      "type": "sse",
      "url": "http://SERVER_IP:8765/sse"
    }
  }
}
```

Claude Code を再起動後、以下のように使用できます:

```
RAGシステムの状態を教えて（rag_status）
「氷室玲が泣くシーン」を検索して（rag_search）
このファイルを登録して（rag_ingest）
```

> **⚠️ rag_ingest には絶対パスを渡してください。**
> MCP サーバーはコンテナ内で動作するため、ファイルパスは `/app/rag_data/...` 形式で渡してください。

---

### 6. rag_data へのファイル配置ルール

| ディレクトリ | カテゴリ | 用途 |
|---|---|---|
| `rag_data/settings/` | settings | キャラクター設定・世界観・用語集 |
| `rag_data/chapters/` | chapter | 完成した本編章ファイル |
| `rag_data/plot/` | plot | あらすじ・タイムライン・プロット |

**ファイル命名例:**
```
rag_data/chapters/1巻第1章_出会い.md
rag_data/settings/キャラクター設定_氷室玲.md
rag_data/plot/2巻プロット.txt
```

巻数・章番号はファイル名から自動抽出されます:
- 巻数: `1巻`, `vol01`, `v1` など
- 章番号: `第1章`, `1章`, `ch01` など

---

### 7. 確認済みフラグ付与

1. Web UI（`http://サーバーIP:8767`）を開く
2. 「ファイル管理」画面を開く
3. 確認したいファイルのチェックボックスをオンにする
4. 「確認済みにする」ボタンをクリック

> **一度「確認済み」にしたファイルは、AI（MCP/Webhook 経由）からの上書きが拒否されます。**
> 変更する場合は Web UI から再アップロードしてください。

---

### 8. Cloudflare Tunnel での Web UI 公開（概要）

サーバーの Web UI をインターネットからアクセスしたい場合:

```bash
# Cloudflare Tunnel のインストール（サーバー上で実行）
curl -L --output cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
chmod +x cloudflared

# トンネル経由で公開
./cloudflared tunnel --url http://localhost:8767
```

Cloudflare アカウントがある場合は Named Tunnel を使うと永続的な URL が取得できます。

---

## トラブルシューティング

| エラー | 対処法 |
|---|---|
| `Qdrantコンテナが起動しているか確認してください` | `docker-compose up -d` でコンテナを起動する |
| `Ollamaが起動しているか確認してください` | Ollama ホストが稼働しているか確認する |
| `シークレットが一致しません` | `.env` の `WEBHOOK_SECRET` と git hook の設定が一致しているか確認 |
| `確認済みのため上書きできません` | Web UI からファイルを再アップロードする |
| Streamlit が表示されない | `docker logs novel_rag_service` でエラーを確認する |
# RAGs
