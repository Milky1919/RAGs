# -*- coding: utf-8 -*-
"""
web_ui.py - ラノベ執筆RAGシステム Streamlit Web UI

ポート 8767 で動作する管理・確認ダッシュボード。

画面構成:
    - ダッシュボード : 統計情報・最近登録されたファイル一覧
    - ファイル管理   : 一覧表示・アップロード・削除・確認済みフラグ付与
    - 検索テスト     : クエリ入力・フィルター・結果表示
"""

import os
import sys
from pathlib import Path
from datetime import datetime

import streamlit as st

# コアロジックをインポート
import ingest as core

# ── ページ設定 ─────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="ラノベ執筆 RAG ダッシュボード",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="expanded",
)

RAG_DATA_DIR = Path("/app/rag_data")


# ── 共通ユーティリティ ─────────────────────────────────────────────────────
@st.cache_resource(ttl=30)
def _get_client():
    """Qdrant クライアントをキャッシュして返す（30秒キャッシュ）。"""
    return core.get_qdrant_client()


def _get_stats() -> list[dict]:
    """ファイル統計を取得する。"""
    try:
        client = _get_client()
        core.ensure_collection(client)
        return core.get_file_stats(client)
    except RuntimeError as e:
        st.error(str(e))
        return []


# ══════════════════════════════════════════════════════════════════════════════
# ダッシュボード画面
# ══════════════════════════════════════════════════════════════════════════════
def page_dashboard():
    st.title("📚 ラノベ執筆 RAG ダッシュボード")

    stats = _get_stats()
    if not stats:
        st.info("データが登録されていません。ファイル管理画面からアップロードしてください。")
        return

    total = sum(s["chunk_count"] for s in stats)
    checked = sum(s["chunk_count"] for s in stats if s["status"] == "checked")
    ai_chunks = sum(s["chunk_count"] for s in stats if s["source_type"] == "ai")
    human_chunks = sum(s["chunk_count"] for s in stats if s["source_type"] == "human")

    # ── KPI カード ──────────────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("総チャンク数", total)
    c2.metric("確認済み", checked, f"{checked/total*100:.0f}%" if total else "0%")
    c3.metric("AI 生成", ai_chunks)
    c4.metric("人間作成", human_chunks)

    st.divider()

    # ── カテゴリ別内訳 ───────────────────────────────────────────────────────
    st.subheader("カテゴリ別内訳")
    cat_data = {}
    for s in stats:
        cat = s["category"]
        cat_data[cat] = cat_data.get(cat, 0) + s["chunk_count"]

    cols = st.columns(len(cat_data) or 1)
    for i, (cat, cnt) in enumerate(sorted(cat_data.items())):
        cols[i].metric(cat, f"{cnt} チャンク")

    st.divider()

    # ── 最近登録されたファイル ────────────────────────────────────────────
    st.subheader("最近登録されたファイル（上位20件）")
    sorted_stats = sorted(stats, key=lambda x: x["created_at"], reverse=True)[:20]

    rows = []
    for s in sorted_stats:
        rows.append({
            "ファイル名": s["source_file"],
            "カテゴリ": s["category"],
            "Source": s["source_type"],
            "Status": s["status"],
            "チャンク数": s["chunk_count"],
            "登録日時": s["created_at"][:19].replace("T", " ") if s["created_at"] else "",
        })

    if rows:
        import pandas as pd
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ══════════════════════════════════════════════════════════════════════════════
# ファイル管理画面
# ══════════════════════════════════════════════════════════════════════════════
def page_file_management():
    st.title("📂 ファイル管理")

    stats = _get_stats()
    stats_by_file = {s["source_file"]: s for s in stats}

    # ── ファイルアップロード ─────────────────────────────────────────────────
    with st.expander("➕ ファイルをアップロード・登録", expanded=False):
        category_dir = st.selectbox(
            "保存先カテゴリ",
            ["settings", "chapters", "plot"],
            format_func=lambda x: {"settings": "settings（設定資料）", "chapters": "chapters（本編章）", "plot": "plot（プロット）"}[x],
        )
        uploaded = st.file_uploader(
            "ファイルを選択（.md / .txt）",
            type=["md", "txt"],
            accept_multiple_files=True,
        )
        if st.button("アップロード & 登録", type="primary") and uploaded:
            save_dir = RAG_DATA_DIR / category_dir
            save_dir.mkdir(parents=True, exist_ok=True)
            for uf in uploaded:
                save_path = save_dir / uf.name
                save_path.write_bytes(uf.getvalue())
                result = core.ingest_file(
                    file_path=str(save_path),
                    source_type="human",
                    allow_overwrite_checked=True,  # Web UI は上書き許可
                )
                if result["success"]:
                    st.success(f"✅ {uf.name}: {result['message']}")
                else:
                    st.error(f"❌ {uf.name}: {result['message']}")
            _get_client.clear()
            st.rerun()

    st.divider()

    # ── カテゴリ別タブ ───────────────────────────────────────────────────────
    tabs = st.tabs(["📖 chapters", "⚙️ settings", "📋 plot", "🗂️ other"])
    categories = ["chapter", "settings", "plot", "other"]

    for tab, cat in zip(tabs, categories):
        with tab:
            cat_files = [s for s in stats if s["category"] == cat]
            if not cat_files:
                st.info("このカテゴリにファイルはありません。")
                continue

            # ── ファイル一覧テーブル ──────────────────────────────────────
            import pandas as pd
            df_rows = []
            for s in sorted(cat_files, key=lambda x: x["source_file"]):
                df_rows.append({
                    "選択": False,
                    "ファイル名": s["source_file"],
                    "Source": s["source_type"],
                    "Status": s["status"],
                    "チャンク数": s["chunk_count"],
                    "巻": s.get("volume") or "-",
                    "章": s.get("chapter") or "-",
                })

            df = pd.DataFrame(df_rows)
            edited = st.data_editor(
                df,
                column_config={
                    "選択": st.column_config.CheckboxColumn("選択", default=False),
                    "Status": st.column_config.TextColumn("Status", disabled=True),
                },
                disabled=["ファイル名", "Source", "Status", "チャンク数", "巻", "章"],
                use_container_width=True,
                hide_index=True,
                key=f"editor_{cat}",
            )

            selected = edited[edited["選択"] == True]["ファイル名"].tolist()

            # ── 一括確認済みボタン ──────────────────────────────────────
            col1, col2 = st.columns([2, 5])
            with col1:
                if st.button(
                    f"✅ 選択ファイルを「確認済み」にする（{len(selected)}件）",
                    disabled=len(selected) == 0,
                    key=f"confirm_{cat}",
                ):
                    try:
                        client = _get_client()
                        for fname in selected:
                            updated = core.update_file_status(client, fname, "checked")
                            st.success(f"✅ {fname}: {updated} チャンクを確認済みに更新")
                        _get_client.clear()
                        st.rerun()
                    except RuntimeError as e:
                        st.error(str(e))

            with col2:
                if st.button(
                    f"🗑️ 選択ファイルを削除（{len(selected)}件）",
                    disabled=len(selected) == 0,
                    key=f"delete_{cat}",
                    type="secondary",
                ):
                    try:
                        client = _get_client()
                        for fname in selected:
                            info = stats_by_file.get(fname, {})
                            if info.get("status") == "checked":
                                st.warning(f"⚠️ {fname} は確認済みのため削除できません。")
                                continue
                            deleted = core.delete_chunks(client, fname)
                            # ファイル本体も削除
                            for subdir in ["settings", "chapters", "plot"]:
                                fp = RAG_DATA_DIR / subdir / fname
                                if fp.exists():
                                    fp.unlink()
                            st.success(f"🗑️ {fname}: {deleted} チャンクを削除")
                        _get_client.clear()
                        st.rerun()
                    except RuntimeError as e:
                        st.error(str(e))


# ══════════════════════════════════════════════════════════════════════════════
# 検索テスト画面
# ══════════════════════════════════════════════════════════════════════════════
def page_search():
    st.title("🔍 検索テスト")

    with st.form("search_form"):
        query = st.text_area("検索クエリ", placeholder="例: 氷室玲が図書室で泣くシーン", height=80)
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            top_k = st.slider("取得件数", 1, 15, 5)
        with col2:
            category = st.selectbox("カテゴリ", ["（指定なし）", "chapter", "settings", "plot"])
        with col3:
            source_type = st.selectbox("Source", ["（指定なし）", "ai", "human"])
        with col4:
            status = st.selectbox("Status", ["（指定なし）", "checked", "unchecked"])
        submitted = st.form_submit_button("🔍 検索", type="primary")

    if submitted and query.strip():
        with st.spinner("検索中..."):
            try:
                results = core.search(
                    query=query,
                    top_k=top_k,
                    category=None if category == "（指定なし）" else category,
                    source_type=None if source_type == "（指定なし）" else source_type,
                    status=None if status == "（指定なし）" else status,
                )
            except RuntimeError as e:
                st.error(str(e))
                return

        if not results:
            st.info("検索結果が見つかりませんでした。")
            return

        st.success(f"{len(results)} 件見つかりました。")
        for r in results:
            p = r["payload"]
            header = (
                f"**{p.get('source_file', '不明')}** | "
                f"Vol: {p.get('volume') or 'N/A'} | "
                f"Ch: {p.get('chapter') or 'N/A'} | "
                f"Category: {p.get('category', '?')} | "
                f"Source: {p.get('source_type', '?')} | "
                f"Status: {p.get('status', '?')} | "
                f"スコア: {r['score']:.4f}"
            )
            with st.expander(header):
                st.text(p.get("text", ""))


# ══════════════════════════════════════════════════════════════════════════════
# メインルーター
# ══════════════════════════════════════════════════════════════════════════════
def main():
    st.sidebar.title("📚 RAG ナビゲーション")
    page = st.sidebar.radio(
        "画面を選択",
        ["ダッシュボード", "ファイル管理", "検索テスト"],
        index=0,
    )

    st.sidebar.divider()
    st.sidebar.caption(f"Qdrant: {core.QDRANT_URL}")
    st.sidebar.caption(f"Ollama: {core.OLLAMA_HOST}")

    if page == "ダッシュボード":
        page_dashboard()
    elif page == "ファイル管理":
        page_file_management()
    elif page == "検索テスト":
        page_search()


if __name__ == "__main__":
    main()
