#!/bin/bash
# git_hook_template.sh
#
# ローカル PC の git リポジトリに設置する post-push フック。
# git push 後にサーバーの Webhook API を叩いて自動 ingest をトリガーする。
#
# ── インストール方法 ──────────────────────────────────────────────────────
# 1. このファイルを原稿リポジトリの .git/hooks/post-push にコピー
#    cp git_hook_template.sh /path/to/your-novel-repo/.git/hooks/post-push
#
# 2. 実行権限を付与
#    chmod +x /path/to/your-novel-repo/.git/hooks/post-push
#
# 3. 下記の変数を自分の環境に合わせて書き換える
#
# ── または git エイリアスとして登録する方法 ─────────────────────────────
# git config --global alias.push-rag '!git push "$@" && /path/to/git_hook_template.sh'
# → git push-rag origin main で使用可能
#
# ── 注意 ─────────────────────────────────────────────────────────────────
# git に標準の post-push フックは存在しないため、このスクリプトは
# post-push ファイルとして配置するか、git エイリアスとして使用すること。

set -e

# ── 設定（自分の環境に合わせて変更すること） ─────────────────────────────
WEBHOOK_URL="http://YOUR_SERVER_IP:8766/webhook"
WEBHOOK_SECRET="your-webhook-secret-here"   # .env の WEBHOOK_SECRET と一致させる

# ── Webhook 送信 ──────────────────────────────────────────────────────────
echo "🚀 RAG Webhook を送信中... ($WEBHOOK_URL)"

RESPONSE=$(curl -s -o /tmp/rag_webhook_response.json -w "%{http_code}" \
  -X POST \
  -H "X-Webhook-Secret: ${WEBHOOK_SECRET}" \
  -H "Content-Type: application/json" \
  -d '{}' \
  --connect-timeout 10 \
  --max-time 120 \
  "${WEBHOOK_URL}")

if [ "$RESPONSE" = "200" ]; then
  echo "✅ RAG 自動更新成功"
  # 処理結果を表示（jq がある場合）
  if command -v jq &> /dev/null; then
    jq '.message' /tmp/rag_webhook_response.json 2>/dev/null || true
  fi
elif [ "$RESPONSE" = "000" ]; then
  echo "⚠️  Webhook サーバーに接続できませんでした（サーバーが起動しているか確認）"
elif [ "$RESPONSE" = "403" ]; then
  echo "❌ Webhook シークレットが一致しません（WEBHOOK_SECRET を確認）"
else
  echo "⚠️  Webhook レスポンス: HTTP $RESPONSE"
  cat /tmp/rag_webhook_response.json 2>/dev/null || true
fi

rm -f /tmp/rag_webhook_response.json
