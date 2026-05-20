#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["opencv-python", "qrcode[pil]", "pillow"]
# ///
"""
QR ファイル転送 — 双方向 Stop-and-Wait プロトコル

送信側: チャンクQRを表示 → 受信側のACK QRをスキャン → 次チャンクへ
受信側: チャンクQRをスキャン → ACK QRを表示 → 送信側がスキャン → 繰り返し
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import cv2
import qrcode
from PIL import Image, ImageTk
import json
import base64
import math
import uuid
import threading
import time
import os

# ── 定数 ────────────────────────────────────────────────────────
CHUNK_SIZE = 600   # 1チャンクあたりのバイト数
QR_PX      = 340   # QR表示サイズ（ピクセル）
CAM_W      = 340   # カメラ表示幅
CAM_H      = 255   # カメラ表示高さ (4:3)

BG     = '#0f0f0f'
BG2    = '#1c1c1c'
BG3    = '#272727'
FG     = '#e8e8e8'
FG2    = '#666666'
BLUE   = '#2563eb'
GREEN  = '#16a34a'
YELLOW = '#ca8a04'
RED    = '#dc2626'


# ── QRコード生成 ─────────────────────────────────────────────────

def gen_qr(text: str) -> ImageTk.PhotoImage:
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=6,
        border=2,
    )
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white').convert('RGB')
    img = img.resize((QR_PX, QR_PX), Image.NEAREST)
    return ImageTk.PhotoImage(img)


def fmt_bytes(n: int) -> str:
    if n < 1024:     return f'{n} B'
    if n < 1 << 20:  return f'{n / 1024:.1f} KB'
    return f'{n / (1 << 20):.1f} MB'


# ── アプリ本体 ───────────────────────────────────────────────────

class QRApp(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title('QR ファイル転送')
        self.configure(bg=BG)
        self.resizable(False, False)
        self.minsize(760, 560)

        # カメラ
        self._cam_idx     = 0
        self.cam_running  = False
        self._last_qr     = ''
        self._last_qr_t   = 0.0

        # 送信ステート
        self.s_chunks:  list[str]    = []
        self.s_idx      = 0
        self.s_session  = ''
        self.s_done     = False

        # 受信ステート
        self.r_map:     dict[int, dict] = {}
        self.r_session  = ''
        self.r_total    = 0
        self.r_name     = 'file'
        self.r_size     = 0
        self.r_done     = False

        self._mode = 'send'
        self._build_ui()
        self._start_camera()
        self.protocol('WM_DELETE_WINDOW', self._quit)

    # ── UIビルド ─────────────────────────────────────────────────

    def _build_ui(self):
        # ヘッダー
        hdr = tk.Frame(self, bg='#111111')
        hdr.pack(fill='x')

        tk.Label(hdr, text='▣  QR ファイル転送',
                 font=('Helvetica', 14, 'bold'),
                 bg='#111111', fg=FG, padx=14, pady=10).pack(side='left')

        # カメラ選択
        cam_fr = tk.Frame(hdr, bg='#111111', padx=6)
        cam_fr.pack(side='right')

        tk.Label(cam_fr, text='カメラ:', bg='#111111', fg=FG2,
                 font=('Helvetica', 10)).pack(side='left')

        self._cam_var   = tk.StringVar(value='検索中…')
        self._cam_combo = ttk.Combobox(
            cam_fr, textvariable=self._cam_var,
            state='disabled', width=14, font=('Helvetica', 10))
        self._cam_combo.pack(side='left', padx=(4, 0), pady=8)
        self._cam_combo.bind('<<ComboboxSelected>>', self._on_cam_select)

        bf = tk.Frame(hdr, bg='#111111', padx=10)
        bf.pack(side='right')

        self._bsend = tk.Button(bf, text='📤 送信',
            command=lambda: self._switch('send'),
            bg=BLUE, fg='white', relief='flat',
            padx=14, pady=7, font=('Helvetica', 11, 'bold'), cursor='hand2')
        self._bsend.pack(side='left', padx=3, pady=8)

        self._brecv = tk.Button(bf, text='📥 受信',
            command=lambda: self._switch('recv'),
            bg=BG3, fg=FG2, relief='flat',
            padx=14, pady=7, font=('Helvetica', 11), cursor='hand2')
        self._brecv.pack(side='left', padx=3, pady=8)

        self._sf = self._build_send_ui()
        self._rf = self._build_recv_ui()
        self._sf.pack(fill='both', expand=True)

        # バックグラウンドでカメラ一覧を検索
        threading.Thread(target=self._enumerate_cams, daemon=True).start()

    def _qr_panel(self, parent, title: str):
        """QRコード表示パネルを作成して (frame, canvas, chunk_label) を返す"""
        outer = tk.Frame(parent, bg=BG2, padx=10, pady=10)
        tk.Label(outer, text=title, bg=BG2, fg=FG2,
                 font=('Helvetica', 9)).pack()
        c = tk.Canvas(outer, width=QR_PX, height=QR_PX,
                      bg='white', highlightthickness=0)
        c.pack(pady=(6, 4))
        lbl = tk.Label(outer, text='— / —',
                       bg=BG2, fg=FG, font=('Helvetica', 18, 'bold'))
        lbl.pack()
        tk.Label(outer, text='チャンク', bg=BG2, fg='#444',
                 font=('Helvetica', 8)).pack(pady=(1, 8))
        return outer, c, lbl

    def _cam_panel(self, parent, title: str):
        """カメラ表示パネルを作成して (frame, canvas, status_label) を返す"""
        outer = tk.Frame(parent, bg=BG2, padx=10, pady=10)
        tk.Label(outer, text=title, bg=BG2, fg=FG2,
                 font=('Helvetica', 9)).pack()
        c = tk.Canvas(outer, width=CAM_W, height=CAM_H,
                      bg='#111111', highlightthickness=0)
        c.pack(pady=(6, 4))
        st = tk.Label(outer, text='',
                      bg=BG2, fg=FG2,
                      font=('Helvetica', 9), wraplength=320)
        st.pack()
        return outer, c, st

    def _build_send_ui(self):
        f = tk.Frame(self, bg=BG, padx=14, pady=12)

        # ファイル選択行
        fr = tk.Frame(f, bg=BG)
        fr.pack(fill='x', pady=(0, 10))
        tk.Button(fr, text='📁 ファイルを選択',
                  command=self._pick_file,
                  bg='#374151', fg=FG, relief='flat', padx=12, pady=7,
                  font=('Helvetica', 10), cursor='hand2').pack(side='left')
        self._s_filelbl = tk.Label(fr, text='ファイル未選択',
                                    bg=BG, fg=FG2, font=('Helvetica', 10))
        self._s_filelbl.pack(side='left', padx=10)

        panels = tk.Frame(f, bg=BG)
        panels.pack()

        # 左: 送信QR
        lp, self._sq_c, self._s_seq_lbl = self._qr_panel(
            panels, '送信QR — 受信側に見せる')
        lp.grid(row=0, column=0, padx=(0, 8))

        # 右: カメラ（ACKスキャン）
        rp, self._s_cam_c, self._s_st = self._cam_panel(
            panels, 'ACKスキャン — 受信側の画面を撮影')
        self._s_st.config(text='ファイルを選択するとQRが表示されます')
        rp.grid(row=0, column=1, padx=(8, 0))

        # プログレス
        pg = tk.Frame(f, bg=BG)
        pg.pack(fill='x', pady=(10, 0))
        self._s_prog = ttk.Progressbar(pg, maximum=100, length=700)
        self._s_prog.pack(fill='x')

        return f

    def _build_recv_ui(self):
        f = tk.Frame(self, bg=BG, padx=14, pady=12)

        self._r_infolbl = tk.Label(
            f, text='送信側のQRコードをスキャン待ち…',
            bg=BG, fg=FG2, font=('Helvetica', 10))
        self._r_infolbl.pack(fill='x', pady=(0, 10))

        panels = tk.Frame(f, bg=BG)
        panels.pack()

        # 左: ACK QR
        lp, self._rq_c, self._r_seq_lbl = self._qr_panel(
            panels, 'ACK QR — 送信側に見せる')
        lp.grid(row=0, column=0, padx=(0, 8))

        # 右: カメラ（チャンクスキャン）
        rp, self._r_cam_c, self._r_st = self._cam_panel(
            panels, 'チャンクスキャン — 送信側の画面を撮影')
        self._r_st.config(text='送信側の画面にカメラを向けてください')
        rp.grid(row=0, column=1, padx=(8, 0))

        # プログレス + 保存ボタン
        pg = tk.Frame(f, bg=BG)
        pg.pack(fill='x', pady=(10, 0))
        self._r_prog = ttk.Progressbar(pg, maximum=100, length=700)
        self._r_prog.pack(fill='x')
        self._r_save_btn = tk.Button(
            pg, text='💾 ファイルを保存', command=self._save_file,
            bg=GREEN, fg='white', relief='flat', padx=14, pady=8,
            font=('Helvetica', 10, 'bold'), cursor='hand2')
        # 完了時に pack

        return f

    def _switch(self, mode: str):
        if mode == self._mode:
            return
        self._mode = mode
        if mode == 'send':
            self._rf.pack_forget()
            self._sf.pack(fill='both', expand=True)
            self._bsend.configure(bg=BLUE, fg='white',
                                  font=('Helvetica', 11, 'bold'))
            self._brecv.configure(bg=BG3, fg=FG2,
                                  font=('Helvetica', 11))
        else:
            self._sf.pack_forget()
            self._rf.pack(fill='both', expand=True)
            self._brecv.configure(bg=GREEN, fg='white',
                                  font=('Helvetica', 11, 'bold'))
            self._bsend.configure(bg=BG3, fg=FG2,
                                  font=('Helvetica', 11))

    # ── カメラ ───────────────────────────────────────────────────

    def _enumerate_cams(self):
        """利用可能なカメラを 0〜7 番で探してコンボボックスを更新する"""
        found: list[int] = []
        for i in range(8):
            cap = cv2.VideoCapture(i)
            if cap.isOpened():
                found.append(i)
                cap.release()
            elif found:
                break  # 既に1台以上見つかった後に失敗 → それ以上はない

        labels = [f'カメラ {i}' for i in found] if found else ['カメラなし']

        def _update():
            self._cam_combo['values'] = labels
            self._cam_combo['state']  = 'readonly' if found else 'disabled'
            self._cam_var.set(labels[0])
            if found:
                self._cam_idx = found[0]
            self._start_camera()

        self.after(0, _update)

    def _on_cam_select(self, _event=None):
        """コンボボックスで別のカメラが選ばれたら切り替える"""
        label = self._cam_var.get()
        try:
            new_idx = int(label.split()[-1])
        except ValueError:
            return
        if new_idx == self._cam_idx:
            return

        # 旧ループを止めてから新しいインデックスで再起動
        self.cam_running = False
        self._cam_idx    = new_idx
        self._last_qr    = ''
        self._last_qr_t  = 0.0

        def _delayed_restart():
            time.sleep(0.15)          # 旧スレッドが cap.release() するのを待つ
            self.after(0, self._start_camera)

        threading.Thread(target=_delayed_restart, daemon=True).start()

    def _start_camera(self):
        self.cam_running = True
        threading.Thread(target=self._cam_loop, daemon=True).start()

    def _cam_loop(self):
        idx = self._cam_idx
        cap = cv2.VideoCapture(idx)
        if not cap.isOpened():
            self.after(0, lambda: messagebox.showerror(
                'カメラエラー',
                f'カメラ {idx} を開けません\n別のカメラを選択してください'))
            return

        detector = cv2.QRCodeDetector()

        while self.cam_running:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue

            # QR検出
            try:
                data, _, _ = detector.detectAndDecode(frame)
                if data:
                    t = time.time()
                    if data != self._last_qr or t - self._last_qr_t > 1.2:
                        self._last_qr   = data
                        self._last_qr_t = t
                        self.after(0, lambda d=data: self._on_qr(d))
            except Exception:
                pass

            # カメラ映像をPIL Imageに変換（スレッドセーフ）
            small    = cv2.resize(frame, (CAM_W, CAM_H))
            pil_img  = Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
            # UIスレッドで描画
            self.after(0, lambda im=pil_img: self._draw_cam(im))

            time.sleep(0.04)  # ~25fps

        cap.release()

    def _draw_cam(self, pil_img: Image.Image):
        # 現在のモードに応じたキャンバスを選択
        canvas = self._s_cam_c if self._mode == 'send' else self._r_cam_c
        photo  = ImageTk.PhotoImage(pil_img)
        canvas.delete('all')
        canvas.create_image(0, 0, anchor='nw', image=photo)
        canvas._img = photo  # GC防止

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
            p: dict = {
                'i': sid, 's': i, 't': total,
                'd': base64.b64encode(ch).decode(),
            }
            if i == 0:
                p['n'] = name
                p['z'] = size
            self.s_chunks.append(json.dumps(p, separators=(',', ':')))

        self.s_session = sid
        self.s_idx     = 0
        self.s_done    = False

        self._s_filelbl.config(
            text=f'{name}  ({fmt_bytes(size)},  {total} チャンク)', fg=FG)

        self._show_chunk(0)

    def _show_chunk(self, idx: int):
        n = len(self.s_chunks)
        self.s_idx = idx

        photo = gen_qr(self.s_chunks[idx])
        self._sq_c.delete('all')
        self._sq_c.create_image(0, 0, anchor='nw', image=photo)
        self._sq_c._img = photo

        self._s_seq_lbl.config(text=f'{idx + 1} / {n}', fg=FG)
        self._s_prog['value'] = (idx + 1) / n * 100
        self._s_st.config(
            text=f'チャンク {idx + 1}/{n} を表示中\n'
                 f'← 受信側のACK QRをスキャンしたら次へ進みます',
            fg=YELLOW)

    def _handle_ack(self, data: str):
        """受信側のACK QRを解析して次のチャンクへ進む"""
        try:
            obj = json.loads(data)
        except Exception:
            return

        # セッションIDチェック
        if obj.get('a') != self.s_session:
            return

        acked: int = obj.get('s', -1)

        # 現在のチャンクのACKだけを受け付ける
        if acked != self.s_idx:
            return

        nxt = self.s_idx + 1
        if nxt >= len(self.s_chunks):
            # 全チャンク完了
            self.s_done = True
            self._s_seq_lbl.config(text='完了 ✓', fg=GREEN)
            self._s_st.config(text='✅ 全チャンクの送信が完了しました！', fg=GREEN)
            self._s_prog['value'] = 100
        else:
            self._show_chunk(nxt)

    # ── 受信ロジック ──────────────────────────────────────────────

    def _handle_chunk(self, data: str):
        """送信側のチャンクQRを解析してACKを表示する"""
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

        # 異なるセッションは無視
        if self.r_session and self.r_session != sid:
            return

        # 初回受信でセッション初期化
        if not self.r_session:
            self.r_session = sid
            self.r_total   = total
            self.r_name    = obj.get('n', 'file')
            self.r_size    = obj.get('z', 0)
            self._r_infolbl.config(
                text=f'受信中: {self.r_name}  '
                     f'({fmt_bytes(self.r_size)}, {total} チャンク)',
                fg=FG)

        # チャンクを保存（重複時は上書きしない）
        self.r_map.setdefault(seq, obj)

        # ACK QRを表示（重複でも毎回更新して送信側が確実にスキャンできるようにする）
        ack_str = json.dumps({'a': sid, 's': seq}, separators=(',', ':'))
        photo = gen_qr(ack_str)
        self._rq_c.delete('all')
        self._rq_c.create_image(0, 0, anchor='nw', image=photo)
        self._rq_c._img = photo

        received = len(self.r_map)
        self._r_seq_lbl.config(text=f'{received} / {total}', fg=FG)
        self._r_prog['value'] = received / total * 100
        self._r_st.config(
            text=f'チャンク {seq + 1}/{total} を受信 ✓\n'
                 f'← ACK QRを送信側にスキャンさせてください',
            fg=GREEN)

        if received >= total and not self.r_done:
            self.r_done = True
            self._r_infolbl.config(
                text=f'✅ 受信完了！  {self.r_name}  ({fmt_bytes(self.r_size)})',
                fg=GREEN)
            self._r_save_btn.pack(pady=8)

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
                for i in range(self.r_total)
            )
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
