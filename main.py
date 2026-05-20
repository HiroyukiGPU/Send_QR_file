#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["opencv-python", "qrcode[pil]", "pillow", "zxing-cpp"]
# ///
"""
QR ファイル転送 — 双方向 Stop-and-Wait プロトコル
"""

import tkinter as tk
from tkinter import filedialog, messagebox
import cv2
import qrcode
import zxingcpp
from PIL import Image, ImageTk
import json
import base64
import math
import uuid
import threading
import time
import os
import sys

# ── 定数 ────────────────────────────────────────────────────────
CHUNK_SIZE = 280
QR_PX      = 520
CAM_W      = 280
CAM_H      = 210

# 完全ライトモード配色
# macOS が fg を黒に上書きしても「黒文字×白背景」で必ず可視
PANEL = '#ffffff'   # パネル背景（白）
WIN   = '#f0f4f8'   # ウィンドウ背景（薄青グレー）
HDR   = '#dbeafe'   # ヘッダー背景（薄青）
TX    = '#1e293b'   # 本文テキスト（濃紺）
TX2   = '#475569'   # サブテキスト
CAM_B = '#1e293b'   # カメラ画像ボーダー
BLUE  = '#2563eb'
GREEN = '#16a34a'
AMBER = '#b45309'
RED   = '#dc2626'


# ── ヘルパー: Button を Label として使う ──────────────────────────
# macOS Aqua テーマは tk.Label の fg を黒（Light Mode）または白（Dark Mode）
# に強制上書きする場合がある。tk.Button は bg/fg が必ず描画される。
# ただし bg は明るい色、fg は暗い色を使えば上書きされても読める。
def lbl(parent, text, bg=PANEL, fg=TX, font=('Helvetica', 10), **kw):
    kw.setdefault('padx', 0)
    kw.setdefault('pady', 0)
    return tk.Button(
        parent, text=text, bg=bg, fg=fg,
        activebackground=bg, activeforeground=fg,
        relief='flat', bd=0,
        highlightthickness=0, cursor='arrow',
        takefocus=0, font=font, **kw)


def gen_qr(text: str) -> ImageTk.PhotoImage:
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10, border=4)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white').convert('RGB')
    if img.width > QR_PX or img.height > QR_PX:
        img = img.resize((QR_PX, QR_PX), Image.NEAREST)
    else:
        canvas = Image.new('RGB', (QR_PX, QR_PX), 'white')
        offset = ((QR_PX - img.width) // 2, (QR_PX - img.height) // 2)
        canvas.paste(img, offset)
        img = canvas
    return ImageTk.PhotoImage(img)


def fmt_bytes(n: int) -> str:
    if n < 1024:    return f'{n} B'
    if n < 1 << 20: return f'{n/1024:.1f} KB'
    return f'{n/(1<<20):.1f} MB'


def decode_qr_robust(frame) -> str:
    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    # Try several views of the same frame to tolerate blur, glare, and
    # a QR that occupies only the center of the captured image.
    variants = [frame, gray]

    if w >= 600 and h >= 600:
        cx0, cx1 = w // 6, w * 5 // 6
        cy0, cy1 = h // 6, h * 5 // 6
        center = frame[cy0:cy1, cx0:cx1]
        center_gray = gray[cy0:cy1, cx0:cx1]
        variants.extend([center, center_gray])
    else:
        center_gray = gray

    variants.append(cv2.resize(frame, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_CUBIC))
    variants.append(cv2.resize(center_gray, None, fx=1.8, fy=1.8, interpolation=cv2.INTER_CUBIC))

    blur = cv2.GaussianBlur(center_gray, (5, 5), 0)
    variants.append(cv2.adaptiveThreshold(
        blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 3))
    _, otsu = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    variants.append(otsu)

    for candidate in variants:
        try:
            results = zxingcpp.read_barcodes(candidate)
        except Exception:
            continue
        for result in results:
            text = getattr(result, 'text', '')
            fmt = str(getattr(result, 'format', ''))
            if text and 'QR' in fmt.upper():
                return text
    return ''


class QRApp(tk.Tk):

    def __init__(self):
        super().__init__()
        # macOS がシステムカラーで上書きするのを防ぐ
        try:
            self.tk_setPalette(
                background=WIN, foreground=TX,
                activeBackground=WIN, activeForeground=TX,
                highlightBackground=WIN, highlightColor=BLUE,
                selectBackground=BLUE, selectForeground='white',
                troughColor='#e2e8f0')
        except Exception:
            pass
        self.title('QR ファイル転送')
        self.configure(bg=WIN)
        self.geometry('980x760')
        self.resizable(False, False)

        # プレースホルダー画像
        self._ph_qr  = ImageTk.PhotoImage(Image.new('RGB', (QR_PX, QR_PX), 'white'))
        self._ph_cam = ImageTk.PhotoImage(Image.new('RGB', (CAM_W, CAM_H), '#c8d4e0'))

        # カメラ
        self._cam_idx        = 0
        self.cam_running     = False
        self._last_qr        = ''
        self._last_qr_t      = 0.0
        self._pending_frame  = None
        self._pending_qr     = None
        self._cam_error      = None
        self._cam_status     = None

        # 送信ステート
        self.s_chunks:   list[str]       = []
        self.s_idx       = 0
        self.s_session   = ''
        self.s_done      = False

        # 受信ステート
        self.r_map:      dict[int, dict] = {}
        self.r_session   = ''
        self.r_total     = 0
        self.r_name      = 'file'
        self.r_size      = 0
        self.r_done      = False

        self._mode = 'send'
        self._build_ui()
        self.update_idletasks()
        self.configure(bg=WIN)
        self.after(33, self._ui_tick)
        if sys.platform == 'darwin':
            self._init_mac_camera()
        else:
            threading.Thread(target=self._enumerate_cams, daemon=True).start()
        self.protocol('WM_DELETE_WINDOW', self._quit)

    def _open_camera(self, idx: int):
        if sys.platform == 'darwin':
            return cv2.VideoCapture(idx, cv2.CAP_AVFOUNDATION)
        return cv2.VideoCapture(idx)

    def _open_camera_candidates(self, idx: int):
        if sys.platform == 'darwin':
            return [
                ('AVFoundation', cv2.VideoCapture(idx, cv2.CAP_AVFOUNDATION)),
                ('Default', cv2.VideoCapture(idx)),
            ]
        return [('Default', cv2.VideoCapture(idx))]

    def _set_cam_status(self, text: str, color=TX2):
        for widget in (self._s_st, self._r_st):
            widget.config(text=text, fg=color, bg=PANEL,
                          activeforeground=color, activebackground=PANEL)

    def _queue_cam_status(self, text: str, color=TX2):
        self._cam_status = (text, color)

    def _ui_tick(self):
        if self._cam_status is not None:
            text, color = self._cam_status
            self._cam_status = None
            self._set_cam_status(text, color)
        if self._pending_frame is not None:
            self._flush_cam_frame()
        if self._pending_qr is not None:
            data = self._pending_qr
            self._pending_qr = None
            self._on_qr(data)
        if self._cam_error is not None:
            title, message = self._cam_error
            self._cam_error = None
            messagebox.showerror(title, message)
        self.after(33, self._ui_tick)

    def _init_mac_camera(self):
        menu = self._cam_menu['menu']
        menu.delete(0, 'end')
        menu.add_command(label='カメラ 0',
                         command=lambda: self._select_cam('カメラ 0'))
        self._cam_var.set('カメラ 0')
        self._cam_menu.config(state='normal')
        self._cam_idx = 0
        self.after(50, self._start_camera)

    # ── UIビルド ─────────────────────────────────────────────────

    def _build_ui(self):
        # ── ヘッダー（ライト背景） ──────────────────────────────
        hdr = tk.Frame(self, bg=HDR, bd=0)
        hdr.pack(side='top', fill='x')

        # 区切り線（下線）
        tk.Frame(self, bg='#bfdbfe', height=1).pack(side='top', fill='x')

        # タイトル
        lbl(hdr, '▣  QR ファイル転送', HDR, TX,
            font=('Helvetica', 13, 'bold'), padx=14, pady=10).pack(side='left')

        # モード切替ボタン
        bf = tk.Frame(hdr, bg=HDR)
        bf.pack(side='right', padx=8)
        self._bsend = tk.Button(
            bf, text='送信', command=lambda: self._switch('send'),
            bg=BLUE, fg='white',
            activebackground='#1d4ed8', activeforeground='white',
            relief='flat', padx=14, pady=6,
            font=('Helvetica', 11, 'bold'), cursor='hand2')
        self._bsend.pack(side='left', padx=3, pady=8)
        self._brecv = tk.Button(
            bf, text='受信', command=lambda: self._switch('recv'),
            bg='#e2e8f0', fg=TX,
            activebackground='#cbd5e1', activeforeground=TX,
            relief='flat', padx=14, pady=6,
            font=('Helvetica', 11), cursor='hand2')
        self._brecv.pack(side='left', padx=3, pady=8)

        # カメラ選択（ライト背景）
        cf = tk.Frame(hdr, bg=HDR, padx=6)
        cf.pack(side='right')
        lbl(cf, 'カメラ:', HDR, TX2,
            font=('Helvetica', 10)).pack(side='left')
        initial_cam_label = 'カメラ 0' if sys.platform == 'darwin' else '検索中…'
        self._cam_var = tk.StringVar(value=initial_cam_label)
        self._cam_menu = tk.OptionMenu(cf, self._cam_var, initial_cam_label)
        self._cam_menu.config(
            bg=PANEL, fg=TX,
            activebackground='#e2e8f0', activeforeground=TX,
            relief='flat', highlightthickness=0,
            font=('Helvetica', 10), state='disabled', bd=0)
        self._cam_menu['menu'].config(bg=PANEL, fg=TX)
        self._cam_menu.pack(side='left', padx=(4, 0), pady=8)

        # ── コンテンツ ─────────────────────────────────────────
        self._content = tk.Frame(self, bg=WIN)
        self._content.pack(side='top', fill='both', expand=True)

        self._sf = self._build_send_ui()
        self._rf = self._build_recv_ui()
        self._sf.pack(fill='both', expand=True)

    # ── 送信 UI ───────────────────────────────────────────────────

    def _build_send_ui(self):
        f = tk.Frame(self._content, bg=WIN)

        # ファイル選択行
        fr = tk.Frame(f, bg=WIN)
        fr.pack(fill='x', padx=16, pady=(12, 8))
        self._btn_file = tk.Button(
            fr, text='ファイルを選択', command=self._pick_file,
            bg='#475569', fg='white',
            activebackground='#64748b', activeforeground='white',
            relief='flat', padx=12, pady=6,
            font=('Helvetica', 10), cursor='hand2')
        self._btn_file.pack(side='left')
        self._s_filelbl = lbl(fr, 'ファイル未選択', WIN, TX2,
                               font=('Helvetica', 10), padx=10)
        self._s_filelbl.pack(side='left')

        # 2パネル行
        row = tk.Frame(f, bg=WIN)
        row.pack(padx=10, pady=(4, 0))

        # 左: QRパネル（白背景）
        lp = tk.Frame(row, bg=PANEL, bd=1, relief='solid',
                      highlightbackground='#cbd5e1', highlightthickness=0)
        lp.pack(side='left', padx=(6, 10))
        lbl(lp, '送信QR  （受信側に見せる）', PANEL, TX2,
            font=('Helvetica', 9), pady=6).pack()
        self._sq_c = tk.Label(lp, image=self._ph_qr, bg='white', bd=0,
                              highlightthickness=0)
        self._sq_c._img = self._ph_qr
        self._sq_c.pack(padx=12, pady=8)
        self._s_seq_lbl = lbl(lp, '— / —', PANEL, TX,
                               font=('Helvetica', 15, 'bold'), pady=2)
        self._s_seq_lbl.pack()
        lbl(lp, 'チャンク', PANEL, TX2,
            font=('Helvetica', 8), pady=4).pack()

        # 右: カメラパネル（白背景で枠付き）
        rp = tk.Frame(row, bg=PANEL, bd=1, relief='solid',
                      highlightbackground='#cbd5e1', highlightthickness=0)
        rp.pack(side='left', padx=(0, 6))
        lbl(rp, 'ACKスキャン  （受信側の画面を撮影）', PANEL, TX2,
            font=('Helvetica', 9), pady=6).pack()
        self._s_cam_c = tk.Label(rp, image=self._ph_cam, bg='#c8d4e0', bd=1,
                                 relief='solid', highlightthickness=0)
        self._s_cam_c._img = self._ph_cam
        self._s_cam_c.pack(padx=8, pady=8)
        self._s_st = lbl(rp, 'ファイルを選択するとQRが表示されます',
                          PANEL, TX2,
                          font=('Helvetica', 9), pady=6, wraplength=300)
        self._s_st.pack()

        # プログレスバー
        self._s_prog = tk.Canvas(f, bg='#e2e8f0', height=6, highlightthickness=0)
        self._s_prog.pack(fill='x', padx=16, pady=(8, 12))
        self._s_prog_fill = self._s_prog.create_rectangle(
            0, 0, 0, 6, fill=BLUE, width=0)

        return f

    # ── 受信 UI ───────────────────────────────────────────────────

    def _build_recv_ui(self):
        f = tk.Frame(self._content, bg=WIN)

        self._r_infolbl = lbl(f, '送信側のQRコードをスキャン待ち…',
                               WIN, TX2, font=('Helvetica', 10), pady=2)
        self._r_infolbl.pack(fill='x', padx=16, pady=(12, 8))

        save_row = tk.Frame(f, bg=WIN)
        save_row.pack(fill='x', padx=16, pady=(0, 8))

        self._r_save_btn = tk.Button(
            save_row, text='ファイルを保存', command=self._save_file,
            bg=GREEN, fg='white',
            activebackground='#15803d', activeforeground='white',
            relief='flat', padx=14, pady=8,
            font=('Helvetica', 10, 'bold'), cursor='hand2',
            state='disabled')
        self._r_save_btn.pack(side='left')

        self._r_save_hint = lbl(
            save_row, '受信完了後に保存できます',
            WIN, TX2, font=('Helvetica', 9), padx=10)
        self._r_save_hint.pack(side='left')

        row = tk.Frame(f, bg=WIN)
        row.pack(padx=10, pady=(4, 0))

        # 左: ACK QRパネル（白背景）
        lp = tk.Frame(row, bg=PANEL, bd=1, relief='solid',
                      highlightbackground='#cbd5e1', highlightthickness=0)
        lp.pack(side='left', padx=(6, 10))
        lbl(lp, 'ACK QR  （送信側に見せる）', PANEL, TX2,
            font=('Helvetica', 9), pady=6).pack()
        self._rq_c = tk.Label(lp, image=self._ph_qr, bg='white', bd=0,
                              highlightthickness=0)
        self._rq_c._img = self._ph_qr
        self._rq_c.pack(padx=12, pady=8)
        self._r_seq_lbl = lbl(lp, '— / —', PANEL, TX,
                               font=('Helvetica', 15, 'bold'), pady=2)
        self._r_seq_lbl.pack()
        lbl(lp, 'チャンク', PANEL, TX2,
            font=('Helvetica', 8), pady=4).pack()

        # 右: カメラパネル（白背景）
        rp = tk.Frame(row, bg=PANEL, bd=1, relief='solid',
                      highlightbackground='#cbd5e1', highlightthickness=0)
        rp.pack(side='left', padx=(0, 6))
        lbl(rp, 'チャンクスキャン  （送信側の画面を撮影）', PANEL, TX2,
            font=('Helvetica', 9), pady=6).pack()
        self._r_cam_c = tk.Label(rp, image=self._ph_cam, bg='#c8d4e0', bd=1,
                                 relief='solid', highlightthickness=0)
        self._r_cam_c._img = self._ph_cam
        self._r_cam_c.pack(padx=8, pady=8)
        self._r_st = lbl(rp, '送信側の画面にカメラを向けてください',
                          PANEL, TX2,
                          font=('Helvetica', 9), pady=6, wraplength=300)
        self._r_st.pack()

        # プログレスバー + 保存
        self._r_prog = tk.Canvas(f, bg='#e2e8f0', height=6, highlightthickness=0)
        self._r_prog.pack(fill='x', padx=16, pady=(8, 8))
        self._r_prog_fill = self._r_prog.create_rectangle(
            0, 0, 0, 6, fill=GREEN, width=0)

        return f

    def _prog_update(self, canvas, bar_id, pct: float):
        canvas.update_idletasks()
        w = canvas.winfo_width()
        canvas.coords(bar_id, 0, 0, int(w * pct / 100), 6)

    # ── モード切替 ────────────────────────────────────────────────

    def _switch(self, mode: str):
        if mode == self._mode:
            return
        self._mode = mode
        if mode == 'send':
            self._rf.pack_forget()
            self._sf.pack(fill='both', expand=True)
            self._bsend.configure(bg=BLUE, fg='white',
                                  activebackground='#1d4ed8', activeforeground='white',
                                  font=('Helvetica', 11, 'bold'))
            self._brecv.configure(bg='#e2e8f0', fg=TX,
                                  activebackground='#cbd5e1', activeforeground=TX,
                                  font=('Helvetica', 11))
        else:
            self._sf.pack_forget()
            self._rf.pack(fill='both', expand=True)
            self._brecv.configure(bg=GREEN, fg='white',
                                  activebackground='#15803d', activeforeground='white',
                                  font=('Helvetica', 11, 'bold'))
            self._bsend.configure(bg='#e2e8f0', fg=TX,
                                  activebackground='#cbd5e1', activeforeground=TX,
                                  font=('Helvetica', 11))

    # ── カメラ ───────────────────────────────────────────────────

    def _enumerate_cams(self):
        found: list[int] = []
        max_probe = 8
        for i in range(max_probe):
            cap = self._open_camera(i)
            opened = cap.isOpened()
            cap.release()
            if opened:
                found.append(i)
            elif found:
                break
        labels = [f'カメラ {i}' for i in found] if found else ['カメラなし']

        def _update():
            menu = self._cam_menu['menu']
            menu.delete(0, 'end')
            for lbl_text in labels:
                menu.add_command(label=lbl_text,
                                 command=lambda t=lbl_text: self._select_cam(t))
            self._cam_var.set(labels[0])
            self._cam_menu.config(state='normal' if found else 'disabled')
            if found:
                self._cam_idx = found[0]
                self._start_camera()

        self.after(0, _update)

    def _select_cam(self, label: str):
        try:
            new_idx = int(label.split()[-1])
        except ValueError:
            return
        if new_idx == self._cam_idx:
            return
        self._cam_var.set(label)
        self.cam_running = False
        self._cam_idx    = new_idx
        self._last_qr    = ''
        self._last_qr_t  = 0.0
        def _delayed():
            time.sleep(0.15)
            self.after(0, self._start_camera)
        threading.Thread(target=_delayed, daemon=True).start()

    def _start_camera(self):
        self.cam_running     = True
        self._pending_frame  = None
        self._pending_qr     = None
        self._cam_error      = None
        self._queue_cam_status(f'カメラ {self._cam_idx} を起動中…', BLUE)
        threading.Thread(target=self._cam_loop, daemon=True).start()

    def _cam_loop(self):
        idx = self._cam_idx
        cap = None
        backend_name = ''
        for candidate_name, candidate_cap in self._open_camera_candidates(idx):
            if not candidate_cap.isOpened():
                candidate_cap.release()
                continue
            ok = False
            for _ in range(20):
                frame_ok, _ = candidate_cap.read()
                if frame_ok:
                    ok = True
                    break
                time.sleep(0.05)
            if ok:
                cap = candidate_cap
                backend_name = candidate_name
                break
            candidate_cap.release()
        if cap is None:
            self._cam_error = (
                'カメラエラー',
                f'カメラ {idx} を開けましたが映像を取得できません\n'
                'FaceTime / Zoom / ブラウザなどを閉じて再試行してください'
            )
            self._queue_cam_status(f'カメラ {idx} の映像を取得できません', RED)
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        self._queue_cam_status(f'カメラ {idx} 接続中 ({backend_name})', GREEN)
        fail_count = 0
        while self.cam_running:
            ok, frame = cap.read()
            if not ok:
                fail_count += 1
                if fail_count >= 30:
                    self._queue_cam_status(
                        'カメラが切断されました。\n別のカメラを選択してください。',
                        RED)
                    break
                time.sleep(0.1)
                continue
            fail_count = 0
            try:
                data = decode_qr_robust(frame)
                if data:
                    t = time.time()
                    if data != self._last_qr or t - self._last_qr_t > 1.2:
                        self._last_qr   = data
                        self._last_qr_t = t
                        self._pending_qr = data
            except Exception:
                pass
            small = cv2.resize(frame, (CAM_W, CAM_H))
            self._pending_frame = Image.fromarray(
                cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
            time.sleep(0.04)
        cap.release()

    def _flush_cam_frame(self):
        frame = self._pending_frame
        if frame is None:
            return
        self._pending_frame = None
        lbl_w = self._s_cam_c if self._mode == 'send' else self._r_cam_c
        photo = ImageTk.PhotoImage(frame)
        lbl_w.config(image=photo)
        lbl_w._img = photo

    def _on_cam_disconnect(self):
        self._set_cam_status('カメラが切断されました。\n別のカメラを選択してください。',
                             RED)

    def _on_qr(self, data: str):
        if self._mode == 'send':
            self._handle_ack(data)
        else:
            self._handle_chunk(data)

    # ── 送信ロジック ──────────────────────────────────────────────

    def _pick_file(self):
        path = filedialog.askopenfilename(title='転送するファイルを選択')
        if not path:
            return
        try:
            with open(path, 'rb') as fh:
                raw = fh.read()
        except Exception as e:
            messagebox.showerror('読み込みエラー', str(e))
            return
        if len(raw) > 5 << 20:
            messagebox.showerror('エラー', 'ファイルサイズは5MB以下にしてください')
            return
        self._init_send(path, raw)

    def _init_send(self, path: str, raw: bytes):
        name  = os.path.basename(path)
        size  = len(raw)
        total = max(1, math.ceil(size / CHUNK_SIZE))
        sid   = str(uuid.uuid4())[:8].upper()
        self.s_chunks = []
        for i in range(total):
            ch = raw[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE]
            p: dict = {'i': sid, 's': i, 't': total,
                       'd': base64.b64encode(ch).decode()}
            if i == 0:
                p['n'] = name
                p['z'] = size
            self.s_chunks.append(json.dumps(p, separators=(',', ':')))
        self.s_session = sid
        self.s_idx     = 0
        self.s_done    = False
        self._s_filelbl.config(
            text=f'{name}  ({fmt_bytes(size)},  {total} チャンク)',
            fg=TX, bg=WIN,
            activeforeground=TX, activebackground=WIN)
        self._show_chunk(0)

    def _show_chunk(self, idx: int):
        n = len(self.s_chunks)
        self.s_idx = idx
        photo = gen_qr(self.s_chunks[idx])
        self._sq_c.config(image=photo)
        self._sq_c._img = photo
        self._s_seq_lbl.config(text=f'{idx + 1} / {n}', fg=TX,
                                activeforeground=TX)
        self._prog_update(self._s_prog, self._s_prog_fill, (idx + 1) / n * 100)
        self._s_st.config(
            text=f'チャンク {idx + 1}/{n} を表示中\n受信側のACK QRをスキャンしたら次へ',
            fg=AMBER, bg=PANEL,
            activeforeground=AMBER, activebackground=PANEL)

    def _handle_ack(self, data: str):
        try:
            obj = json.loads(data)
        except Exception:
            return
        if obj.get('a') != self.s_session:
            return
        if obj.get('s', -1) != self.s_idx:
            return
        nxt = self.s_idx + 1
        if nxt >= len(self.s_chunks):
            self.s_done = True
            self._s_seq_lbl.config(text='完了', fg=GREEN, activeforeground=GREEN)
            self._s_st.config(text='全チャンクの送信が完了しました！',
                               fg=GREEN, bg=PANEL,
                               activeforeground=GREEN, activebackground=PANEL)
            self._prog_update(self._s_prog, self._s_prog_fill, 100)
        else:
            self._show_chunk(nxt)

    # ── 受信ロジック ──────────────────────────────────────────────

    def _handle_chunk(self, data: str):
        try:
            obj = json.loads(data)
        except Exception:
            return
        if 'i' not in obj or 'd' not in obj:
            return
        sid: str   = obj['i']
        seq: int   = obj.get('s', -1)
        total: int = obj.get('t', 0)
        if seq < 0 or total <= 0:
            return
        if self.r_session and self.r_session != sid:
            return
        if not self.r_session:
            self.r_session = sid
            self.r_total   = total
            self.r_name    = obj.get('n', 'file')
            self.r_size    = obj.get('z', 0)
            self._r_infolbl.config(
                text=f'受信中: {self.r_name}  '
                     f'({fmt_bytes(self.r_size)}, {total} チャンク)',
                fg=TX, bg=WIN,
                activeforeground=TX, activebackground=WIN)
        self.r_map.setdefault(seq, obj)
        ack_str = json.dumps({'a': sid, 's': seq}, separators=(',', ':'))
        photo = gen_qr(ack_str)
        self._rq_c.config(image=photo)
        self._rq_c._img = photo
        received = len(self.r_map)
        self._r_seq_lbl.config(text=f'{received} / {total}', fg=TX,
                                activeforeground=TX)
        self._prog_update(self._r_prog, self._r_prog_fill, received / total * 100)
        self._r_st.config(
            text=f'チャンク {seq + 1}/{total} を受信\nACK QRを送信側にスキャンさせてください',
            fg=GREEN, bg=PANEL,
            activeforeground=GREEN, activebackground=PANEL)
        if received >= total and not self.r_done:
            self.r_done = True
            self._r_infolbl.config(
                text=f'受信完了！  {self.r_name}  ({fmt_bytes(self.r_size)})',
                fg=GREEN, bg=WIN,
                activeforeground=GREEN, activebackground=WIN)
            self._r_save_btn.config(state='normal')
            self._r_save_hint.config(
                text='保存ボタンを押して保存先を選択してください',
                fg=GREEN, bg=WIN,
                activeforeground=GREEN, activebackground=WIN)

    def _save_file(self):
        if not self.r_done:
            return
        path = filedialog.asksaveasfilename(
            initialfile=self.r_name, title='保存先を選択')
        if not path:
            return
        try:
            data = b''.join(
                base64.b64decode(self.r_map[i]['d'])
                for i in range(self.r_total))
            with open(path, 'wb') as fh:
                fh.write(data)
            messagebox.showinfo('保存完了', f'保存しました:\n{path}')
        except Exception as e:
            messagebox.showerror('保存エラー', str(e))

    # ── 終了 ─────────────────────────────────────────────────────

    def _quit(self):
        self.cam_running = False
        time.sleep(0.1)
        self.destroy()


if __name__ == '__main__':
    QRApp().mainloop()
