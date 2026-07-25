# -*- coding: utf-8 -*-
"""
Flask Web 前端：单用户本机使用。
- 全局 AppState 保存连接信息（仅内存）、媒体库数据、当前后台任务。
- 后台工作（扫描/匹配）在线程中运行，通过 core 的模块级队列推进度；
  /api/events 以 SSE 把队列内容实时推给浏览器，哨兵 <<<PROCESS_COMPLETE>>>
  被翻译成 done 事件（同时以任务线程存活状态作为 job_running 的权威来源）。
"""
import json
import logging
import os
import queue
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, request, send_file

import core

app = Flask(__name__)


class AppState:
    """单用户全局状态，等价于原 GUI 的 PlaylistMatcherApp 实例属性。"""

    def __init__(self):
        self.navidrome_config = None   # {'url','username','password'} 仅内存
        self.server_connected = False
        self.server_version = "N/A"
        self.library_data = []
        self.job_thread = None         # 当前后台任务线程（扫描或匹配）
        self.job_kind = None           # 'scan' | 'match'
        self.last_result_file = None   # 最近一次匹配结果 txt 的绝对路径


state = AppState()


def job_running():
    return state.job_thread is not None and state.job_thread.is_alive()


def error_response(message, status=400):
    return jsonify({'ok': False, 'error': message}), status


# --- 页面与静态 ---
@app.route('/')
def index():
    return render_template('index.html', app_name=core.APP_NAME, version=core.APP_VERSION)


@app.route('/favicon.ico')
def favicon():
    icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'icon.ico')
    if os.path.exists(icon):
        return send_file(icon, mimetype='image/vnd.microsoft.icon')
    return ('', 404)


# --- 状态查询 ---
@app.route('/api/status')
def api_status():
    return jsonify({
        'connected': state.server_connected,
        'server_version': state.server_version,
        'library_count': len(state.library_data),
        'job_running': job_running(),
        'job_kind': state.job_kind if job_running() else None,
        'has_result': bool(state.last_result_file and os.path.exists(state.last_result_file)),
        'version': core.APP_VERSION,
    })


# --- Navidrome 连接 ---
@app.route('/api/connect', methods=['POST'])
def api_connect():
    if job_running():
        return error_response('有任务正在运行，请稍后再试。', 409)
    data = request.get_json(silent=True) or {}
    url = (data.get('url') or '').strip().rstrip('/')
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    if not (url and username and password):
        core.log_queue.put("[ERROR] 请填写 Navidrome 服务器地址、用户名和密码！")
        return error_response('请填写 Navidrome 服务器地址、用户名和密码！')

    file_logger = core.get_temp_file_logger("navidrome_connect")
    try:
        ping_response = core.call_navidrome_api("ping", {}, url, username, password, file_logger, timeout=10)
    except Exception as e:
        state.server_connected = False
        return error_response(f'连接 Navidrome 时发生错误: {e}', 502)

    if ping_response and ping_response.get('status') == 'ok':
        state.navidrome_config = {'url': url, 'username': username, 'password': password}
        state.server_connected = True
        state.server_version = ping_response.get('serverVersion', 'N/A')
        core.log_message('info', f"Navidrome 连接成功: {username}@{url}", file_logger, to_gui=True)
        return jsonify({'ok': True, 'server_version': state.server_version})

    state.server_connected = False
    return error_response('Navidrome 认证失败。请检查服务器信息和凭据。', 401)


# --- 全库扫描（后台线程）---
def _scan_worker(navidrome_config):
    temp_file_logger = core.get_temp_file_logger("navidrome_scan")
    try:
        scanned_songs = core.scan_navidrome_library(navidrome_config, temp_file_logger)
        state.library_data = scanned_songs
        core.log_message('info', f"Navidrome 全库扫描完成。共找到 {len(state.library_data)} 首歌曲。", temp_file_logger, to_gui=True)
        if not state.library_data:
            core.gui_alert_queue.put(("warning", "扫描结果", "Navidrome 库扫描未找到任何音频项目。"))
        else:
            core.gui_alert_queue.put(("info", "扫描完成", f"Navidrome 全库扫描完成。共找到 {len(state.library_data)} 首歌曲。"))
    except Exception as e:
        core.log_message('error', f"Navidrome 全库扫描失败: {e}", temp_file_logger, to_gui=True)
        if temp_file_logger:
            temp_file_logger.error(traceback.format_exc())
        core.gui_alert_queue.put(("error", "扫描失败", f"Navidrome 全库扫描失败: {e}"))
        state.library_data = []
    finally:
        core.log_queue.put(core.PROCESS_COMPLETE_SENTINEL)


@app.route('/api/scan', methods=['POST'])
def api_scan():
    if not state.server_connected or not state.navidrome_config:
        return error_response('请先连接到 Navidrome 服务器。')
    if job_running():
        return error_response('有任务正在运行，请稍后再试。', 409)
    core.log_queue.put("[INFO] 开始 Navidrome 全库扫描...")
    t = threading.Thread(target=_scan_worker, args=(state.navidrome_config,), daemon=True)
    state.job_thread = t
    state.job_kind = 'scan'
    t.start()
    return jsonify({'ok': True})


# --- 媒体库数据 导入/导出 ---
@app.route('/api/library/load', methods=['POST'])
def api_library_load():
    if job_running():
        return error_response('有任务正在运行，请稍后再试。', 409)
    upload = request.files.get('file')
    if not upload or not upload.filename:
        return error_response('请先选择要加载的 Navidrome 库数据文件。')
    try:
        loaded_data = json.load(upload.stream)
        if not isinstance(loaded_data, list) or (loaded_data and not isinstance(loaded_data[0], dict)):
            raise ValueError("文件内容格式不正确。")
        state.library_data = loaded_data
        core.log_queue.put(f"[INFO] 成功从文件加载 {len(state.library_data)} 条歌曲信息。")
        return jsonify({'ok': True, 'count': len(state.library_data)})
    except Exception as e:
        core.log_queue.put(f"[ERROR] 从文件加载数据失败: {e}")
        return error_response(f'从文件加载数据失败: {e}')


@app.route('/api/library/export')
def api_library_export():
    if not state.library_data:
        return error_response('没有 Navidrome 库数据可供导出。')
    payload = json.dumps(state.library_data, ensure_ascii=False, indent=2)
    filename = f"navidrome_library_scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return Response(
        payload,
        mimetype='application/json; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


# --- 匹配与创建（后台线程）---
@app.route('/api/match', methods=['POST'])
def api_match():
    if job_running():
        return error_response('有任务正在运行，请稍后再试。', 409)

    input_type = request.form.get('input_type', 'url_id')
    match_mode = request.form.get('match_mode', '模糊匹配')
    if match_mode not in ('模糊匹配', '完全匹配'):
        match_mode = '模糊匹配'
    create_on_server = request.form.get('create_on_server') == 'true'
    make_public = request.form.get('make_public') == 'true'

    if input_type == 'url_id':
        playlist_input = (request.form.get('playlist_input') or '').strip()
        if not playlist_input:
            return error_response('请输入歌单 URL 或 ID！')
        if not state.library_data:
            return error_response('Navidrome 媒体库数据未加载。')
    else:
        upload = request.files.get('result_file')
        if not upload or not upload.filename:
            return error_response('请选择匹配结果文件！')
        # 上传的结果文件保存到临时路径，由工作线程按原契约解析（文件由 OS 清理）
        tmp = tempfile.NamedTemporaryFile(mode='wb', suffix='.txt', delete=False, prefix='uploaded_result_')
        upload.save(tmp)
        tmp.close()
        playlist_input = tmp.name

    if create_on_server and not state.server_connected:
        return error_response('已选择在服务器上创建歌单，但当前未连接。')

    navidrome_config = state.navidrome_config or {'url': '', 'username': '', 'password': ''}
    output_path = f"Navidrome匹配结果_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    state.last_result_file = os.path.abspath(output_path)

    core.log_queue.put("开始匹配与创建过程...")
    t = threading.Thread(
        target=core.run_matching_process,
        args=(navidrome_config, input_type, playlist_input, output_path,
              create_on_server, make_public, match_mode, state.library_data),
        daemon=True,
    )
    state.job_thread = t
    state.job_kind = 'match'
    t.start()
    return jsonify({'ok': True})


@app.route('/api/result/download')
def api_result_download():
    path = state.last_result_file
    if not path or not os.path.exists(path):
        return error_response('暂无可下载的结果文件。', 404)
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


# --- SSE 事件流 ---
def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _event_stream():
    last_heartbeat = time.time()
    while True:
        emitted = False
        try:
            while True:
                msg = core.log_queue.get_nowait()
                if msg == core.PROCESS_COMPLETE_SENTINEL:
                    yield _sse('done', {'job_kind': state.job_kind})
                else:
                    yield _sse('log', {'message': msg})
                emitted = True
        except queue.Empty:
            pass
        try:
            while True:
                msg = core.unmatched_queue.get_nowait()
                yield _sse('unmatched', {'message': msg})
                emitted = True
        except queue.Empty:
            pass
        try:
            while True:
                alert_type, title, message = core.gui_alert_queue.get_nowait()
                yield _sse('alert', {'type': alert_type, 'title': title, 'message': message})
                emitted = True
        except queue.Empty:
            pass

        now = time.time()
        if now - last_heartbeat >= 15:
            yield ": heartbeat\n\n"
            last_heartbeat = now
        if not emitted:
            time.sleep(0.2)


@app.route('/api/events')
def api_events():
    return Response(
        _event_stream(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', stream=sys.stdout)
    port = int(os.environ.get('PORT', '5000'))
    url = f"http://127.0.0.1:{port}"
    print(f"启动 {core.APP_NAME} v{core.APP_VERSION}，访问 {url}")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    # 仅绑定回环地址：本机单人使用，且内存中持有 Navidrome 凭据
    app.run(host='127.0.0.1', port=port, threaded=True, debug=False)
