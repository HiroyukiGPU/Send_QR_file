from flask import Flask, render_template, request, jsonify
import base64
import json
import math
import uuid

app = Flask(__name__)

CHUNK_SIZE = 600  # bytes per QR code payload

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/send')
def send_page():
    return render_template('send.html')

@app.route('/receive')
def receive_page():
    return render_template('receive.html')

@app.route('/api/prepare', methods=['POST'])
def prepare():
    if 'file' not in request.files:
        return jsonify({'error': 'ファイルがありません'}), 400

    file = request.files['file']
    if not file.filename:
        return jsonify({'error': 'ファイル名がありません'}), 400

    data = file.read()
    filename = file.filename
    filesize = len(data)

    if filesize > 3 * 1024 * 1024:
        return jsonify({'error': 'ファイルサイズは3MB以下にしてください'}), 400

    session_id = str(uuid.uuid4())[:8].upper()
    total = max(1, math.ceil(filesize / CHUNK_SIZE))

    chunks = []
    for i in range(total):
        chunk_data = data[i * CHUNK_SIZE:(i + 1) * CHUNK_SIZE]
        payload = {
            "i": session_id,
            "s": i,
            "t": total,
            "d": base64.b64encode(chunk_data).decode('ascii'),
        }
        if i == 0:
            payload["n"] = filename
            payload["z"] = filesize
        chunks.append(json.dumps(payload, separators=(',', ':')))

    return jsonify({
        "session_id": session_id,
        "total": total,
        "chunks": chunks,
        "filename": filename,
        "filesize": filesize
    })


if __name__ == '__main__':
    import socket
    hostname = socket.gethostname()
    try:
        local_ip = socket.gethostbyname(hostname)
    except Exception:
        local_ip = '127.0.0.1'
    print(f"\n  ローカル:  http://127.0.0.1:5001")
    print(f"  ネットワーク: http://{local_ip}:5001")
    print(f"  (スマホでスキャンするには同じWi-Fiに接続してください)\n")
    app.run(debug=False, port=5001, host='0.0.0.0')
