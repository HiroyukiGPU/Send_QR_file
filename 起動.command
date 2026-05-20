#!/bin/bash
# QR ファイル転送 — 起動スクリプト
# Python 3.10+ がインストールされた任意の Mac で動作します

set -e
cd "$(dirname "$0")"

VENV=".venv"
DEPS="opencv-python qrcode[pil] pillow zxing-cpp"

ensure_venv_pip() {
    if ! "$VENV/bin/python3" -m pip --version >/dev/null 2>&1; then
        echo "🧰  pip をセットアップ中..."
        "$VENV/bin/python3" -m ensurepip --upgrade >/dev/null
    fi
}

# ── Python を探す ──────────────────────────────────────────────
find_python() {
    local candidates=(
        "$VENV/bin/python3"         # 既存 venv（最優先）
        "$(which uv 2>/dev/null) run python3"  # uv (スキップ、後で処理)
        python3
        python
        /opt/homebrew/bin/python3   # Homebrew (Apple Silicon)
        /usr/local/bin/python3      # Homebrew (Intel)
        /usr/bin/python3            # macOS システム
        "$HOME/.pyenv/shims/python3"
        "$HOME/miniconda3/bin/python3"
        "$HOME/miniforge3/bin/python3"
        "$HOME/anaconda3/bin/python3"
    )
    for py in "${candidates[@]}"; do
        # スペースを含むコマンド（uv run）はスキップ
        [[ "$py" == *" "* ]] && continue
        if command -v "$py" &>/dev/null || [[ -x "$py" ]]; then
            local ver
            ver=$("$py" -c "import sys; print(sys.version_info >= (3,10))" 2>/dev/null)
            if [[ "$ver" == "True" ]]; then
                echo "$py"
                return 0
            fi
        fi
    done
    return 1
}

# ── venv セットアップ ──────────────────────────────────────────
setup_venv() {
    local base_py
    # venv 外で Python を探す
    local candidates=(
        python3
        python
        /opt/homebrew/bin/python3
        /usr/local/bin/python3
        /usr/bin/python3
        "$HOME/.pyenv/shims/python3"
        "$HOME/miniconda3/bin/python3"
        "$HOME/miniforge3/bin/python3"
        "$HOME/anaconda3/bin/python3"
    )
    for py in "${candidates[@]}"; do
        if command -v "$py" &>/dev/null || [[ -x "$py" ]]; then
            local ver
            ver=$("$py" -c "import sys; print(sys.version_info >= (3,10))" 2>/dev/null)
            if [[ "$ver" == "True" ]]; then
                base_py="$py"
                break
            fi
        fi
    done

    if [[ -z "$base_py" ]]; then
        echo "❌  Python 3.10 以上が見つかりません"
        echo "   https://www.python.org/downloads/ からインストールしてください"
        echo ""
        read -r -p "Enterキーで終了..."
        exit 1
    fi

    echo "🐍  Python: $("$base_py" --version)"
    echo "📦  仮想環境を作成中: $VENV"
    "$base_py" -m venv "$VENV"

    echo "📥  依存パッケージをインストール中..."
    ensure_venv_pip
    "$VENV/bin/python3" -m pip install --upgrade pip -q
    # shellcheck disable=SC2086
    "$VENV/bin/python3" -m pip install $DEPS -q
    echo "✅  インストール完了"
}

# ── uv が使えるなら優先 ────────────────────────────────────────
if command -v uv &>/dev/null && [[ ! -d "$VENV" ]]; then
    echo "⚡  uv で環境をセットアップ中..."
    uv venv "$VENV" -q
    uv pip install $DEPS --python "$VENV/bin/python3" -q
    echo "✅  インストール完了"
fi

# ── venv が無いか壊れていたら作り直す ─────────────────────────
if [[ ! -x "$VENV/bin/python3" ]]; then
    setup_venv
fi

ensure_venv_pip

# 必要パッケージが揃っているか確認（揃っていなければ追加インストール）
if ! "$VENV/bin/python3" -c "import cv2, qrcode, PIL, zxingcpp" 2>/dev/null; then
    echo "📥  不足パッケージを追加インストール中..."
    # shellcheck disable=SC2086
    "$VENV/bin/python3" -m pip install $DEPS -q
fi

# ── 起動 ──────────────────────────────────────────────────────
echo "🚀  QR ファイル転送を起動します..."
exec "$VENV/bin/python3" main.py
