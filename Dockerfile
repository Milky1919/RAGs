# ── ベースイメージ ─────────────────────────────────────────────────────────
FROM python:3.11-slim

# ── システムパッケージ（git, supervisor） ────────────────────────────────
RUN apt-get update && apt-get install -y \
    git \
    supervisor \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ── 作業ディレクトリ ────────────────────────────────────────────────────
WORKDIR /app

# ── Python 依存ライブラリ（レイヤーキャッシュ活用のため先にコピー） ──
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── アプリケーションコードをコピー ────────────────────────────────────
COPY . .

# ── 必要ディレクトリの作成 ──────────────────────────────────────────────
RUN mkdir -p \
    /app/rag_data/settings \
    /app/rag_data/chapters \
    /app/rag_data/plot \
    /app/repo \
    /var/log/supervisor

# ── supervisord 設定をコピー ─────────────────────────────────────────────
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# ── 起動コマンド ──────────────────────────────────────────────────────────
CMD ["supervisord", "-n", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
