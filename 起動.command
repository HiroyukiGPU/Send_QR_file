#!/bin/bash
cd "$(dirname "$0")"

# 依存パッケージが無ければインストール
python3 -c "import cv2, qrcode, PIL" 2>/dev/null || \
    pip install opencv-python "qrcode[pil]" pillow --system -q

python3 main.py
