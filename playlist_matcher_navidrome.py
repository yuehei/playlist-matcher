import customtkinter as ctk
from tkinter import filedialog, messagebox
import requests
import hashlib
import random
import string
import json
import math
import re
from urllib.parse import urlencode, urljoin, quote_plus
import time
import logging
import html
import threading
import queue
import os
import sys
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
from fuzzywuzzy import fuzz

# --- 全局配置和常量 ---
APP_NAME = "歌单匹配与创建器 (Navidrome)"
LOG_FILE_BASENAME = "playlist_match_create_debug_navidrome"
APP_VERSION = "1.6.0"  # Version updated for new matching logic
NAVIDROME_CLIENT_NAME = "PlaylistMatcherGUI_CTk"
NAVIDROME_API_VERSION = "1.16.1" # Subsonic API Version

# API 和常量
QQ_API_GET_PLAYLIST_URL = "http://i.y.qq.com/qzone/fcg-bin/fcg_ucc_getcdinfo_byids_cp.fcg"
NCM_API_PLAYLIST_DETAIL_URL = "https://music.163.com/api/v3/playlist/detail"
NCM_API_SONG_DETAIL_URL = "https://music.163.com/api/song/detail/"

# 匹配参数
MATCH_THRESHOLD = 9 # 基于模糊匹配算法 (满分15)
NAVIDROME_SCAN_PAGE_SIZE = 500
NAVIDROME_PLAYLIST_ADD_BATCH_SIZE = 200 # 添加到歌单时的批次大小

# --- 缓存设置 ---
CACHE_TIMEOUT_SECONDS = 3600
qq_playlist_cache = {}
ncm_playlist_cache = {}
ncm_track_cache = {}

# --- 日志和队列设置 ---
log_queue = queue.Queue()
unmatched_queue = queue.Queue()
gui_alert_queue = queue.Queue()
logger = logging.getLogger('PlaylistMatchGUI_Navidrome')
logger.setLevel(logging.INFO)
if logger.hasHandlers(): logger.handlers.clear()

# --- 辅助函数 ---
def get_random_string(length=6):
    return ''.join(random.choice(string.ascii_letters + string.digits) for _ in range(length))

def strip_jsonp(jsonp_str):
    match = re.match(r'^[^{]*\(({.*?})\)[^}]*$', jsonp_str.strip())
    return match.group(1) if match else jsonp_str

def _normalize_artists(artist_str: str) -> set:
    """标准化歌手字符串，返回一个集合以便比较。"""
    if not isinstance(artist_str, str): return set()
    s = artist_str.lower()
    # 移除括号和方括号内容
    s = re.sub(r'\s*[\(（].*?[\)）]', '', s)
    s = re.sub(r'\s*[\[【].*?[\]】]', '', s)
    # 标准化合作艺人分隔符
    s = re.sub(r'\s+(feat|ft|with|vs|presents|pres\.|starring)\.?\s+', '/', s)
    s = re.sub(r'\s*&\s*', '/', s)
    # 按多种分隔符拆分
    artists = {artist.strip() for artist in re.split(r'\s*[/•,、]\s*', s) if artist.strip()}
    return artists

def _get_title_lookup_key(title: str) -> str:
    """为标题创建标准化的、用于索引的查找键。"""
    if not isinstance(title, str): return ""
    key = title.lower()
    # 移除括号和方括号内容，这通常是版本信息 (Live, Remix, etc.)
    key = re.sub(r'\s*[\(（【\[].*?[\)）】\]]', '', key).strip()
    return key

# --- 日志记录函数 ---
def log_message(level, message, file_logger=None, to_gui=True):
    if to_gui:
        log_queue.put(f"[{level.upper()}] {message}")
    if file_logger:
        level_lower = level.lower()
        if hasattr(file_logger, level_lower): getattr(file_logger, level_lower)(message)
        else: file_logger.info(f"[{level.upper()}] {message}")

# --- Navidrome API 调用 ---
def _get_auth_params(username, password):
    """Generates Subsonic authentication parameters."""
    salt = get_random_string(6)
    token = hashlib.md5(f"{password}{salt}".encode('utf-8')).hexdigest()
    return {
        "u": username,
        "t": token,
        "s": salt,
        "v": NAVIDROME_API_VERSION,
        "c": NAVIDROME_CLIENT_NAME,
        "f": "json"
    }

def call_navidrome_api(endpoint, params, base_url, username, password, file_logger, method='GET', timeout=30):
    api_url = urljoin(base_url, f"/rest/{endpoint}.view")
    auth_params = _get_auth_params(username, password)
    
    response = None
    try:
        if method.upper() == 'GET':
            full_params = {**auth_params, **params}
            response = requests.get(api_url, params=full_params, timeout=timeout)
        elif method.upper() == 'POST':
            # Auth params go in URL, other params go in POST body
            post_data = params
            response = requests.post(api_url, params=auth_params, data=post_data, timeout=timeout)
        else:
            log_message('error', f"不支持的 HTTP 方法: {method}", file_logger)
            return None

        response.raise_for_status()
        data = response.json()

        if 'subsonic-response' in data and data['subsonic-response']['status'] == 'failed':
            error_info = data['subsonic-response']['error']
            error_msg = f"Navidrome API 错误 ({endpoint}): {error_info['message']} (代码: {error_info['code']})"
            log_message('error', error_msg, file_logger, to_gui=True)
            return None
        
        is_scan_call = (endpoint == 'search2' or endpoint == 'search3')
        if not is_scan_call:
            log_message('info', f"Navidrome API ({endpoint}) 调用成功", file_logger, to_gui=True)
            
        return data.get('subsonic-response')

    except requests.exceptions.HTTPError as e:
        log_message('error', f"Navidrome API ({endpoint}) HTTP 错误 ({e.response.status_code}): {e.response.text[:200]}...", file_logger, to_gui=True)
        return None
    except requests.exceptions.RequestException as e:
        log_message('error', f"请求 Navidrome API ({endpoint}) 失败: {e}", file_logger, to_gui=True)
        return None
    except json.JSONDecodeError:
        log_message('error', f"解析 Navidrome API ({endpoint}) 响应失败。响应文本: {response.text[:200]}...", file_logger, to_gui=True)
        return None
    except Exception as e:
        log_message('error', f"调用 Navidrome API ({endpoint}) 时发生未知错误: {e}", file_logger, to_gui=True)
        return None

# --- Navidrome 服务器操作函数 ---
def get_navidrome_playlists(navidrome_config, file_logger):
    log_message('info', "正在获取 Navidrome 播放列表...", file_logger)
    response_data = call_navidrome_api(
        "getPlaylists", {},
        navidrome_config['url'], navidrome_config['username'], navidrome_config['password'],
        file_logger
    )
    if response_data and 'playlists' in response_data:
        playlists_data = response_data['playlists'].get('playlist', [])
        playlists = playlists_data if isinstance(playlists_data, list) else [playlists_data]
        log_message('info', f"获取到 {len(playlists)} 个 Navidrome 播放列表。", file_logger)
        return [{'id': p.get('id'), 'name': p.get('name')} for p in playlists]
    return []

def delete_navidrome_playlist(playlist_id, navidrome_config, file_logger):
    log_message('warning', f"正在删除 Navidrome 歌单 (ID: {playlist_id})...", file_logger)
    response_data = call_navidrome_api(
        "deletePlaylist", {'id': playlist_id},
        navidrome_config['url'], navidrome_config['username'], navidrome_config['password'],
        file_logger
    )
    return response_data is not None and response_data.get('status') == 'ok'

def create_navidrome_playlist(name, is_public, navidrome_config, file_logger):
    log_message('info', f"正在创建 Navidrome 歌单: '{name}' (公开: {is_public})...", file_logger)
    response_data = call_navidrome_api(
        "createPlaylist", {'name': name},
        navidrome_config['url'], navidrome_config['username'], navidrome_config['password'],
        file_logger
    )
    if response_data and 'playlist' in response_data:
        new_playlist_id = response_data['playlist'].get('id')
        log_message('info', f"成功创建 Navidrome 歌单 '{name}' (ID: {new_playlist_id})", file_logger)

        if is_public:
            log_message('info', f"尝试将歌单 (ID: {new_playlist_id}) 设为公开...", file_logger)
            update_params = {'playlistId': new_playlist_id, 'public': 'true'}
            update_response = call_navidrome_api(
                "updatePlaylist", update_params,
                navidrome_config['url'], navidrome_config['username'], navidrome_config['password'],
                file_logger
            )
            if update_response and update_response.get('status') == 'ok':
                log_message('info', f"成功将歌单 (ID: {new_playlist_id}) 设为公开。", file_logger)
            else:
                log_message('warning', f"将歌单 (ID: {new_playlist_id}) 设为公开失败。", file_logger)
        return new_playlist_id
    else:
        log_message('error', f"创建 Navidrome 歌单 '{name}' 失败。", file_logger)
        return None

def add_songs_to_navidrome_playlist_batched(playlist_id, song_ids, navidrome_config, file_logger, batch_size=NAVIDROME_PLAYLIST_ADD_BATCH_SIZE):
    if not playlist_id or not song_ids:
        log_message('warning', "没有歌曲需要添加到歌单或歌单 ID 无效。", file_logger)
        return True

    num_songs_to_add = len(song_ids)
    log_message('info', f"准备将 {num_songs_to_add} 首歌曲分批次添加到 Navidrome 歌单 (ID: {playlist_id})...", file_logger)
    
    successful_batches = 0
    total_batches = math.ceil(num_songs_to_add / batch_size)

    for i in range(0, num_songs_to_add, batch_size):
        current_batch_songs = song_ids[i:i + batch_size]
        log_message('info', f"  正在处理批次 {successful_batches + 1}/{total_batches} (歌曲数: {len(current_batch_songs)})...", file_logger)
        
        params = {'playlistId': playlist_id, 'songIdToAdd': current_batch_songs}
        response_data = call_navidrome_api(
            "updatePlaylist", params,
            navidrome_config['url'], navidrome_config['username'], navidrome_config['password'],
            file_logger, method='POST'
        )

        if response_data and response_data.get('status') == 'ok':
            log_message('info', f"  批次 {successful_batches + 1} 添加成功。", file_logger)
            successful_batches += 1
        else:
            log_message('error', f"  批次 {successful_batches + 1} 添加失败。", file_logger)
            log_message('error', "  歌单添加过程因批次失败而中断。", file_logger)
            return False
        
        time.sleep(0.5)

    if successful_batches == total_batches:
        log_message('info', f"所有 {total_batches} 个批次均成功添加歌曲到 Navidrome 歌单 (ID: {playlist_id})。", file_logger)
        return True
    else:
        log_message('warning', "部分批次添加失败，请检查日志了解详情。", file_logger)
        return False

# --- 源歌单获取和格式化 ---
def get_qq_playlist_details(playlist_id, file_logger):
    cache_key = f"qq_{playlist_id}"
    current_time = time.time()
    if cache_key in qq_playlist_cache:
        cached_data, timestamp = qq_playlist_cache[cache_key]
        if current_time - timestamp < CACHE_TIMEOUT_SECONDS:
            log_message('info', f"命中 QQ 歌单缓存 (ID: {playlist_id})", file_logger)
            return cached_data
        else:
             log_message('debug', f"QQ 歌单缓存过期 (ID: {playlist_id})", file_logger, to_gui=False)
    params = { 'type': 1, 'utf8': 1, 'disstid': playlist_id, 'loginUin': 0 }
    headers = {'Referer': 'https://y.qq.com/n/yqq/playlist','User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36'}
    log_message('info', f"\n正在从 API 获取 QQ 歌单详情 (ID: {playlist_id})...", file_logger)
    try:
        response = requests.get(QQ_API_GET_PLAYLIST_URL, params=params, headers=headers, timeout=15)
        response.raise_for_status()
        json_str = strip_jsonp(response.text)
        data = json.loads(json_str)
        if not data or 'cdlist' not in data or not data['cdlist']:
            log_message('error',"错误：QQ 音乐 API 未返回有效的歌单列表。", file_logger)
            return None, []
        playlist_data = data['cdlist'][0]
        playlist_name = html.unescape(playlist_data.get('dissname', f"QQ 音乐歌单 {playlist_id}"))
        songs = playlist_data.get('songlist', [])
        log_message('info', f"成功获取 QQ 歌单: '{playlist_name}'，共 {len(songs)} 首歌曲。", file_logger)
        result_tuple = (playlist_name, songs)
        qq_playlist_cache[cache_key] = (result_tuple, current_time)
        return result_tuple
    except Exception as e:
        log_message('error', f"获取 QQ 歌单时出错: {e}", file_logger)
        return None, []

def format_qq_music_item(qq_song_data, file_logger):
    if not qq_song_data: return None
    try:
        artists_data = qq_song_data.get('singer', [])
        artists_string = ""
        if artists_data and isinstance(artists_data, list):
             artists_string = "/".join([singer.get('name', '') for singer in artists_data]).strip('/')
        album_info = qq_song_data.get('album', {})
        return {
            'source_id': str(qq_song_data.get('id') or qq_song_data.get('songid', f'qq未知ID_{get_random_string()}')),
            'songmid': qq_song_data.get('mid') or qq_song_data.get('songmid'),
            'title': html.unescape(qq_song_data.get('title') or qq_song_data.get('songname', '未知歌曲')),
            'artist': html.unescape(artists_string if artists_string else '未知歌手'),
            'album': html.unescape(album_info.get('title') or album_info.get('name', '未知专辑')),
            'duration': qq_song_data.get('interval'),
            'platform': 'QQ Music'
        }
    except Exception as e:
        log_message('warning', f"格式化 QQ 歌曲信息时出错 - {e}, 数据: {qq_song_data}", file_logger)
        return None

def get_ncm_playlist_details(playlist_id, file_logger):
    cache_key = f"ncm_playlist_{playlist_id}"
    current_time = time.time()
    if cache_key in ncm_playlist_cache:
        cached_data, timestamp = ncm_playlist_cache[cache_key]
        if current_time - timestamp < CACHE_TIMEOUT_SECONDS:
            log_message('info', f"命中 NCM 歌单缓存 (ID: {playlist_id})", file_logger)
            return cached_data
        else:
             log_message('debug', f"NCM 歌单缓存过期 (ID: {playlist_id})", file_logger, to_gui=False)
    log_message('info', f"\n正在从 API 获取网易云歌单详情 (ID: {playlist_id})...", file_logger)
    headers = {'Referer': 'https://music.163.com/', 'Origin': 'https://music.163.com/', 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36'}
    params = {'id': playlist_id, 'n': 100000}
    try:
        response = requests.get(NCM_API_PLAYLIST_DETAIL_URL, params=params, headers=headers, timeout=20)
        response.raise_for_status()
        data = response.json()
        playlist_data = data.get('playlist')
        if not playlist_data:
            log_message('error',"错误：网易云 API 未返回有效的歌单数据。", file_logger)
            return None, []
        playlist_name = html.unescape(playlist_data.get('name', f"网易云歌单 {playlist_id}"))
        track_ids = [str(track['id']) for track in playlist_data.get('trackIds', [])]
        log_message('info', f"成功获取网易云歌单: '{playlist_name}'，包含 {len(track_ids)} 个 Track ID。", file_logger)
        result_tuple = (playlist_name, track_ids)
        ncm_playlist_cache[cache_key] = (result_tuple, current_time)
        return result_tuple
    except Exception as e:
        log_message('error', f"获取网易云歌单时出错: {e}", file_logger)
        return None, []

def get_ncm_track_details(track_ids, file_logger):
    if not track_ids: return []
    cache_key_tuple = tuple(sorted(track_ids))
    cache_key = f"ncm_tracks_{hash(cache_key_tuple)}"
    current_time = time.time()
    if cache_key in ncm_track_cache:
        cached_data, timestamp = ncm_track_cache[cache_key]
        if current_time - timestamp < CACHE_TIMEOUT_SECONDS:
            log_message('info', f"命中 NCM 歌曲详情缓存 ({len(track_ids)} IDs)", file_logger)
            return cached_data
        else:
            log_message('debug', f"NCM 歌曲详情缓存过期 ({len(track_ids)} IDs)", file_logger, to_gui=False)
    log_message('info', f"正在从 API 获取 {len(track_ids)} 首网易云歌曲的详情...", file_logger)
    headers = {'Referer': 'https://music.163.com/', 'Origin': 'https://music.163.com/', 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36'}
    songs_details = []
    batch_size = 200
    for i in range(0, len(track_ids), batch_size):
        batch_ids = track_ids[i:i + batch_size]
        ids_param_value = f"[{','.join(batch_ids)}]"
        params = {'ids': ids_param_value}
        log_message('debug', f"获取 NCM 歌曲详情批次 {i // batch_size + 1} (IDs: {len(batch_ids)})", file_logger, to_gui=False)
        response = None
        try:
            current_api_url = NCM_API_SONG_DETAIL_URL.rstrip('/')
            response = requests.get(current_api_url, params=params, headers=headers, timeout=15)
            response.raise_for_status()
            data = response.json()
            if data.get('songs'):
                songs_details.extend(data['songs'])
            else:
                log_message('warning', f"网易云歌曲详情 API 批次 {i // batch_size + 1} 未返回歌曲数据。响应: {str(data)[:200]}", file_logger)
        except Exception as e:
            url_for_log = response.url if response else NCM_API_SONG_DETAIL_URL
            log_message('error', f"获取网易云歌曲详情批次 {i // batch_size + 1} 时出错: {e}, URL: {url_for_log}", file_logger)
        time.sleep(0.2)
    log_message('info', f"成功获取到 {len(songs_details)} 首网易云歌曲的详情。", file_logger)
    ncm_track_cache[cache_key] = (songs_details, current_time)
    return songs_details

def format_ncm_music_item(ncm_song_data, file_logger):
    if not ncm_song_data: return None
    try:
        artists_data = ncm_song_data.get('ar') or ncm_song_data.get('artists', [])
        album_data = ncm_song_data.get('al') or ncm_song_data.get('album', {})
        artists_string = ""
        if artists_data and isinstance(artists_data, list):
            artist_names = [artist.get('name', '').strip() for artist in artists_data if isinstance(artist, dict) and artist.get('name')]
            artists_string = "/".join(filter(None, artist_names))
        log_message('debug', f"解析 NCM 歌曲 '{ncm_song_data.get('name', '')}', 原始 ar/artists: {artists_data}, 解析后 artists_string: '{artists_string}'", file_logger, to_gui=False)
        duration_ms = ncm_song_data.get('dt') or ncm_song_data.get('duration')
        duration_sec = math.ceil(duration_ms / 1000) if isinstance(duration_ms, (int, float)) and duration_ms > 0 else None
        return {
            'source_id': str(ncm_song_data.get('id', f'ncm未知ID_{get_random_string()}')),
            'title': html.unescape(ncm_song_data.get('name', '未知歌曲')),
            'artist': html.unescape(artists_string if artists_string else '未知歌手'),
            'album': html.unescape(album_data.get('name', '未知专辑')),
            'duration': duration_sec,
            'platform': 'NetEase Cloud Music'
        }
    except Exception as e:
        log_message('warning', f"格式化网易云歌曲信息时出错 - {e}, 数据: {ncm_song_data}", file_logger)
        return None

def get_apple_music_playlist_details(playlist_input_url_or_id, file_logger):
    url_to_scrape = ""
    assumed_region = None
    input_str_cleaned = str(playlist_input_url_or_id).strip()
    actual_title_after_load = "" # Initialize

    full_url_pattern_regex = r"https?://music\.apple\.com/([a-z]{2})/playlist(?:/[^/]+)?/(pl\.[a-zA-Z0-9\-_]+)(?:\?[^#]*)?"
    match_full_url = re.search(full_url_pattern_regex, input_str_cleaned, re.IGNORECASE)
    match_id_only = None # Initialize

    if match_full_url:
        url_to_scrape = match_full_url.group(0)
        assumed_region = match_full_url.group(1).lower()
        log_message('info', f"处理 Apple Music 完整 URL: {url_to_scrape} (地区: {assumed_region}, ID: {match_full_url.group(2)})", file_logger)
    else:
        id_pattern_regex = r"^(pl\.[a-zA-Z0-9\-_]+)$"
        match_id_only = re.fullmatch(id_pattern_regex, input_str_cleaned)
        if match_id_only:
            playlist_id = match_id_only.group(1)
            assumed_region = 'cn'
            url_to_scrape = f"https://music.apple.com/{assumed_region}/playlist/{playlist_id}?l=zh-Hans-CN"
            log_message('info', f"处理 Apple Music ID: {playlist_id}. 构建 URL: {url_to_scrape} (默认地区: {assumed_region})", file_logger)
        else:
            log_message('error', f"输入 '{input_str_cleaned}' 不是有效的 Apple Music URL 或 ID。", file_logger)
            return None, []

    if not url_to_scrape:
        log_message('error', "无法确定用于抓取的 Apple Music URL。", file_logger)
        return None, []

    log_message('info', f"准备抓取 Apple Music 歌单: {url_to_scrape}", file_logger)

    base_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    chrome_path = os.path.join(base_dir, "chrome-win64", "chrome.exe")
    chromedriver_path = os.path.join(base_dir, "chromedriver-win64", "chromedriver.exe")

    chrome_options = Options()
    chrome_options.add_argument('--headless')
    chrome_options.add_argument('--disable-gpu'); chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36")

    if assumed_region == 'us': chrome_options.add_argument('--lang=en-US,en;q=0.9')
    elif assumed_region == 'cn': chrome_options.add_argument('--lang=zh-CN,zh;q=0.9')
    else: chrome_options.add_argument('--lang=zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7')

    driver = None
    try:
        if os.path.exists(chromedriver_path) and os.path.exists(chrome_path):
            log_message('info', "使用本地 ChromeDriver 和 Chrome。", file_logger)
            chrome_options.binary_location = chrome_path
            service = Service(executable_path=chromedriver_path)
        else:
            log_message('warning', "本地 Chrome 或 ChromeDriver 未在指定路径找到，尝试使用 webdriver_manager。", file_logger)
            try: service = Service(ChromeDriverManager().install())
            except Exception as e_wdm:
                log_message('error', f"webdriver_manager 初始化失败: {e_wdm}。", file_logger); return None, []

        driver = webdriver.Chrome(service=service, options=chrome_options)
        driver.get(url_to_scrape)
        time.sleep(3)
        actual_url_after_load = driver.current_url
        actual_title_after_load = driver.title
        log_message('info', f"浏览器实际加载的 URL (3秒后): {actual_url_after_load}", file_logger)
        log_message('info', f"浏览器页面标题 (3秒后): {actual_title_after_load}", file_logger)

        playlist_id_for_check = ""
        if match_full_url: playlist_id_for_check = match_full_url.group(2).lower()
        elif match_id_only: playlist_id_for_check = match_id_only.group(1).lower()

        current_title_lower = actual_title_after_load.lower()
        generic_titles_keywords = ["browse", "new music", "radio", "library", "itunes store", "商店", " - apple music"]
        is_generic_title_suspicious = any(keyword in current_title_lower for keyword in generic_titles_keywords) and \
                                     ("playlist" not in current_title_lower and "歌单" not in current_title_lower) and \
                                     (not playlist_id_for_check or playlist_id_for_check not in current_title_lower)
        if is_generic_title_suspicious:
            log_message('warning', f"页面标题 '{actual_title_after_load}' 可疑 (基于通用关键词且缺少 playlist/歌单 指示)。", file_logger)

        is_wrong_page = False
        if ("/new" in actual_url_after_load.lower() or \
            "/browse" in actual_url_after_load.lower() or \
            "/library/" in actual_url_after_load.lower()) and \
           "/playlist/" not in actual_url_after_load.lower():
            is_wrong_page = True

        if is_wrong_page:
            log_message('error', f"检测到重定向到非歌单页面。URL: {actual_url_after_load}", file_logger)
            gui_alert_queue.put(("error", "获取歌单失败", "Apple Music 歌单页面加载失败或重定向到非歌单页面。请检查 URL 或稍后再试。"))
            return None, []

        log_message('info', f"等待页面主要内容加载 (额外 {8} 秒)...", file_logger)
        time.sleep(8)
        soup = BeautifulSoup(driver.page_source, 'html.parser')

        h1_playlist_name = None
        playlist_name_tag = soup.find('h1', {'data-testid': 'playlist-name'})
        if not playlist_name_tag:
            playlist_name_tag = soup.find('h1', class_=re.compile(r'product-header__title', re.IGNORECASE)) or \
                                soup.find('div', class_=re.compile(r'headingsContainer', re.IGNORECASE))
        if playlist_name_tag:
            h1_playlist_name = playlist_name_tag.text.strip()

        default_playlist_id_text = "未知ID"
        if match_full_url: default_playlist_id_text = match_full_url.group(2)
        elif match_id_only: default_playlist_id_text = match_id_only.group(1)
        default_fallback_playlist_name = f"Apple Music 歌单 ({default_playlist_id_text})"

        songs = []
        row_selectors = ['div[role="row"][aria-label]', 'div.songs-list-row', 'div[data-testid^="track-item-"]']
        rows = []
        for selector in row_selectors:
            rows = soup.select(selector)
            if rows:
                log_message('debug', f"Apple Music: 使用选择器 '{selector}' 找到 {len(rows)} 行。", file_logger, to_gui=False)
                break

        if not rows:
            log_message('warning', "Apple Music: 未找到歌曲行。", file_logger)

        for idx, row_element in enumerate(rows):
            title, artist = "", ""
            title_tags = row_element.select('div[data-testid="track-title"], .songs-list-row__song-name, .product-lockup__title, [class*="song-name"], [class*="track-lockup__title"], [data-testid="title"] .typography-body, div[data-encore-id="type"].songs-list__col-title > div > div')
            if title_tags: title = title_tags[0].text.strip()

            artist_tags = row_element.select('div[data-testid="track-artist"], .songs-list-row__by-line, .product-lockup__subtitle, [class*="artist-name"], [class*="track-lockup__subtitle"], [data-testid="click-action"] .typography-body, div[data-encore-id="type"].songs-list__col-artist > div > div, span[data-encore-id="type"].songs-list__col-artist > a')
            if artist_tags:
                artist_elements_in_container = artist_tags[0].find_all('a')
                if artist_elements_in_container:
                    artist = " / ".join([a.text.strip() for a in artist_elements_in_container if a.text.strip()])
                else: artist = artist_tags[0].text.strip()

            if title:
                unique_id = f"apple_{idx}_{hash(title + (artist if artist else get_random_string(3)))}"
                songs.append({
                    'title': html.unescape(title), 'artist': html.unescape(artist if artist else "未知歌手"),
                    'platform': 'Apple Music', 'source_id': unique_id, 'album': '', 'duration': None
                })
            else: log_message('debug', f"Apple Music: 第 {idx+1} 行未解析出标题。", file_logger, to_gui=False)

        final_playlist_name = default_fallback_playlist_name
        if h1_playlist_name:
            final_playlist_name = h1_playlist_name

        if len(songs) > 0 and actual_title_after_load:
            log_message('debug', f"成功解析到 {len(songs)} 首歌曲。将使用浏览器标题 '{actual_title_after_load}' 进行歌单命名。", file_logger, to_gui=False)
            processed_browser_title = actual_title_after_load
            
            pattern_middle_removal = re.compile(r"(\s*-\s*(?:歌单|Playlist))(\s*-\s*Apple Music)", re.IGNORECASE | re.UNICODE)
            substituted_title = pattern_middle_removal.sub(r"\2", processed_browser_title)
            
            if substituted_title != processed_browser_title:
                final_playlist_name = substituted_title
                log_message('info', f"Apple Music 歌单名根据浏览器标题规则处理为: '{final_playlist_name}'", file_logger)
            else:
                final_playlist_name = actual_title_after_load
                log_message('info', f"Apple Music 歌单名采用原始浏览器标题: '{final_playlist_name}' (无特定规则应用)", file_logger)

        log_message('info', f"Apple Music 歌单最终定名: '{final_playlist_name}'，解析到 {len(songs)} 首歌曲。", file_logger)
        return final_playlist_name, songs

    except Exception as e:
        log_message('error', f"获取 Apple Music 歌单时出错: {e}", file_logger)
        return None, []
    finally:
        if driver: driver.quit()

# --- Navidrome 歌曲匹配函数 ---
def format_navidrome_music_item(navidrome_track_data, file_logger):
    if not navidrome_track_data: return None
    try:
        return {
            'id': str(navidrome_track_data.get('id', f'未知NavidromeID_{get_random_string(6)}')),
            'title': html.unescape(navidrome_track_data.get('title', '未知歌曲')),
            'artist': html.unescape(navidrome_track_data.get('artist', '未知歌手')),
            'album': html.unescape(navidrome_track_data.get('album', '未知专辑')),
            'duration_seconds': navidrome_track_data.get('duration'),
            'path': navidrome_track_data.get('path', '未知路径')
        }
    except Exception as e:
        log_message('warning', f"格式化 Navidrome 歌曲信息时出错 - {e}, 数据: {str(navidrome_track_data)[:200]}", file_logger)
        return None

# --- 核心匹配函数 ---
def find_best_match_in_candidates(source_track, candidate_tracks, file_logger, match_mode):
    """
    在给定的 Navidrome 候选者列表中为源歌曲找到最佳匹配。
    """
    if not candidate_tracks:
        return None, -1

    source_title_clean = source_track.get('title', '').strip()
    source_artist_str = source_track.get('artist', '').strip()

    if match_mode == "完全匹配":
        source_artists_normalized = sorted(list(_normalize_artists(source_artist_str)))
        for navidrome_track in candidate_tracks:
            navidrome_title_clean = navidrome_track.get('title', '').strip()
            navidrome_artist_str = navidrome_track.get('artist', '').strip()
            navidrome_artists_normalized = sorted(list(_normalize_artists(navidrome_artist_str)))
            if source_title_clean == navidrome_title_clean and source_artists_normalized == navidrome_artists_normalized:
                log_message('info', f"  => 在候选中找到完全匹配: '{navidrome_track.get('title')}' (ID: {navidrome_track.get('id')})", file_logger, to_gui=False)
                navidrome_track['_source_track_info'] = source_track
                return navidrome_track, 100
        return None, -1
        
    else: # "模糊匹配"
        best_match = None
        best_score = -1
        
        source_title_lower = source_title_clean.lower()
        source_artists_norm_set = _normalize_artists(source_artist_str)

        for i, navidrome_track in enumerate(candidate_tracks):
            navidrome_title_clean = navidrome_track.get('title', '').strip()
            navidrome_artist_str = navidrome_track.get('artist', '').strip()
            navidrome_title_lower = navidrome_title_clean.lower()
            
            title_similarity = fuzz.ratio(source_title_lower, navidrome_title_lower)
            title_match_points = 0
            if title_similarity >= 95: title_match_points = 10
            elif title_similarity >= 88: title_match_points = 8
            elif title_similarity >= 75: title_match_points = 5
            elif title_similarity >= 60: title_match_points = 2

            navidrome_artists_norm_set = _normalize_artists(navidrome_artist_str)
            artist_match_points = 0
            artist_match_type = "无"
            if source_artists_norm_set and navidrome_artists_norm_set:
                if source_artists_norm_set == navidrome_artists_norm_set:
                    artist_match_points = 5
                    artist_match_type = "全匹配"
                elif source_artists_norm_set.issubset(navidrome_artists_norm_set) or navidrome_artists_norm_set.issubset(source_artists_norm_set):
                    artist_match_points = 4
                    artist_match_type = "子集匹配"
                elif source_artists_norm_set.intersection(navidrome_artists_norm_set):
                    artist_match_points = 2
                    artist_match_type = "交集匹配"

            current_score = title_match_points + artist_match_points
            
            log_message('debug', f"""    比较候选者 {i+1}/{len(candidate_tracks)}:
              源: '{source_title_clean}' by '{source_artist_str}' (Norm: {source_artists_norm_set})
              Navidrome: '{navidrome_title_clean}' by '{navidrome_artist_str}' (Norm: {navidrome_artists_norm_set})
              匹配: Title Sim({title_similarity}% -> {title_match_points}pts), Artist ({artist_match_type} -> {artist_match_points}pts)
              总分: {current_score}""", file_logger, to_gui=False)

            if current_score > best_score:
                best_match = navidrome_track
                best_score = current_score
            
            # 如果达到一个很高的分数，可以提前确定，减少计算
            if title_match_points >= 10 and artist_match_points >= 4:
                best_match = navidrome_track
                best_score = current_score
                break
                
        if best_match and best_score >= MATCH_THRESHOLD:
            log_message('info', f"  => 在候选中找到最佳匹配 (分数: {best_score}): '{best_match.get('title')}'", file_logger, to_gui=False)
            best_match['_source_track_info'] = source_track 
            return best_match, best_score
        else:
            return None, best_score

# --- URL/ID 解析函数 ---
def parse_playlist_input(input_str, file_logger):
    input_str = input_str.strip()
    apple_url_pattern = r"(https?://music\.apple\.com/([a-z]{2})/playlist(?:/[^/]+)?/(pl\.[a-zA-Z0-9\-_]+)(?:\?[^#]*)?)"
    apple_url_match = re.search(apple_url_pattern, input_str, re.IGNORECASE)
    if apple_url_match:
        full_url = apple_url_match.group(0)
        region = apple_url_match.group(1)
        playlist_id = apple_url_match.group(2)
        if file_logger: log_message('info', f"识别为 Apple Music 歌单 URL: {full_url} (地区: {region}, ID: {playlist_id})", file_logger)
        return [("apple", full_url)]

    apple_id_pattern = r"^(pl\.[a-zA-Z0-9\-_]+)$"
    apple_id_match = re.fullmatch(apple_id_pattern, input_str)
    if apple_id_match:
        playlist_id = apple_id_match.group(1)
        if file_logger: log_message('info', f"识别为 Apple Music 歌单 ID: {playlist_id}", file_logger)
        return [("apple", playlist_id)]

    ncm_patterns = [
        r"music\.163\.com.*[/#\?]id=(\d+)", r"music\.163\.com/playlist/(\d+)",
        r"y\.music\.163\.com/m/playlist\?id=(\d+)", r"^(\d{8,12})$"
    ]
    for pattern in ncm_patterns:
        match = re.search(pattern, input_str)
        if match:
            playlist_id = next((g for g in match.groups() if g), None) or \
                          (match.group(0) if pattern == r"^(\d{8,12})$" and match.group(0).isdigit() else None)
            if playlist_id and playlist_id.isdigit():
                if file_logger: log_message('info', f"识别为网易云歌单 ID/URL: {playlist_id}", file_logger)
                return [("ncm", playlist_id)]

    qq_patterns = [
        r"i\.y\.qq\.com/n2/m/share/details/taoge\.html\?.*id=(\d+)", r"y\.qq\.com/n/ryqq/playlist/(\d+)",
        r"y\.qq.com/n/yqq/playlist/(\d+)\.html", r"qm\.qq\.com/cgi-bin/qm/qr\?k=.*&type=30&sub_type=13&key=(\d+)",
        r"^(\d{10,15})$"
    ]
    for pattern in qq_patterns:
        match = re.search(pattern, input_str)
        if match:
            playlist_id = next((g for g in match.groups() if g), None) or \
                          (match.group(0) if pattern == r"^(\d{10,15})$" and match.group(0).isdigit() else None)
            if playlist_id and playlist_id.isdigit():
                if file_logger: log_message('info', f"识别为QQ音乐歌单 ID/URL: {playlist_id}", file_logger)
                return [("qq", playlist_id)]

    if input_str.isdigit():
        if 10 <= len(input_str) <= 15 :
            if file_logger: log_message('info', f"识别为可能是 QQ 音乐的纯数字歌单 ID (回退): {input_str}", file_logger)
            return [("qq", input_str)]
        elif 8 <= len(input_str) <= 12:
            if file_logger: log_message('info', f"识别为可能是网易云的纯数字歌单 ID (回退): {input_str}", file_logger)
            return [("ncm", input_str)]

    if file_logger: log_message('warning', f"无法识别的输入格式: {input_str}", file_logger)
    return []

# --- 核心匹配与服务器操作逻辑 ---
def run_matching_process(app_instance, navidrome_config, playlist_input_type, playlist_input_data, output_filepath,
                         create_playlist_on_server, make_playlist_public, match_mode,
                         unmatched_q, navidrome_library_data):
    log_filename = f"{LOG_FILE_BASENAME}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    file_logger_name = f'PlaylistMatchFileLogger_Navidrome_{get_random_string()}'
    file_logger = logging.getLogger(file_logger_name)
    file_logger.propagate = False
    file_logger.setLevel(logging.DEBUG) # Set to debug for detailed matching logs
    if file_logger.hasHandlers():
        for handler in file_logger.handlers[:]:
            handler.close()
            file_logger.removeHandler(handler)
    log_dir = os.path.dirname(log_filename)
    if log_dir and not os.path.exists(log_dir):
        try: os.makedirs(log_dir)
        except OSError as e:
            print(f"Error creating log directory {log_dir}: {e}")
            log_queue.put(f"[ERROR] 创建日志目录失败: {log_dir}")
            app_instance.match_thread_explicitly_finished = True
            log_queue.put("<<<PROCESS_COMPLETE>>>")
            return
    file_handler = None
    try:
        file_handler = logging.FileHandler(log_filename, mode='w', encoding='utf-8')
        file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)
        file_logger.addHandler(file_handler)
        log_message('info', "="*30, file_logger, to_gui=True)
        log_message('info', f"歌单匹配与创建器 (Navidrome) - 版本 {APP_VERSION}", file_logger, to_gui=True)
        log_message('info', f"服务器操作选项: {'启用' if create_playlist_on_server else '禁用'}", file_logger, to_gui=True)
        if create_playlist_on_server:
            log_message('info', f"  - 创建为公开歌单: {'是' if make_playlist_public else '否'}", file_logger, to_gui=True)
        log_message('info', f"匹配模式: {match_mode}", file_logger, to_gui=True)
        log_message('info', "="*30, file_logger, to_gui=True)

        source_playlist_name = None
        source_song_list = []
        playlist_type_for_summary = "未知类型"
        playlist_id_for_summary = "未知"
        total_songs = 0
        matched_tracks = []
        unmatched_source_songs_dict = {}

        if playlist_input_type == "file":
            log_message('info', f"从文件 '{playlist_input_data}' 加载歌单信息...", file_logger, to_gui=True)
            try:
                parsed_name, parsed_matched_ids, parsed_unmatched_songs = app_instance._parse_result_file_for_upload(playlist_input_data, file_logger)
                if not parsed_name or not parsed_matched_ids:
                    log_message('error', f"从结果文件加载歌单信息失败或文件内容无效。", file_logger, to_gui=True)
                    app_instance.match_thread_explicitly_finished = True
                    log_queue.put("<<<PROCESS_COMPLETE>>>")
                    return

                source_playlist_name = parsed_name
                matched_tracks = [{'id': song_id, 'title': f"ID: {song_id}", 'artist': '（来自文件）', 'path': ''} for song_id in parsed_matched_ids]
                unmatched_source_songs_dict = {s['source_id']: s for s in parsed_unmatched_songs}
                total_songs = len(parsed_matched_ids) + len(parsed_unmatched_songs)
                playlist_type_for_summary = "来自匹配结果文件"
                playlist_id_for_summary = os.path.basename(playlist_input_data)
                log_message('info', f"已加载歌单 '{source_playlist_name}'，包含 {len(parsed_matched_ids)} 首匹配歌曲和 {len(parsed_unmatched_songs)} 首未匹配歌曲。", file_logger, to_gui=True)

            except Exception as e:
                log_message('error', f"处理结果文件时发生错误: {e}", file_logger, to_gui=True)
                app_instance.match_thread_explicitly_finished = True
                log_queue.put("<<<PROCESS_COMPLETE>>>")
                return

        else:
            if not navidrome_library_data:
                log_message('error', "Navidrome 媒体库数据未加载，无法进行匹配。请先扫描或加载。", file_logger, to_gui=True)
                app_instance.match_thread_explicitly_finished = True
                log_queue.put("<<<PROCESS_COMPLETE>>>")
                return
            
            # --- 性能优化：建立索引 ---
            log_message('info', "正在为 Navidrome 媒体库建立快速查找索引...", file_logger, to_gui=True)
            navidrome_library_index = {}
            for navidrome_track in navidrome_library_data:
                lookup_key = _get_title_lookup_key(navidrome_track.get('title'))
                if lookup_key:
                    if lookup_key not in navidrome_library_index:
                        navidrome_library_index[lookup_key] = []
                    navidrome_library_index[lookup_key].append(navidrome_track)
            log_message('info', f"索引建立完成，共包含 {len(navidrome_library_index)} 个唯一查找键。", file_logger, to_gui=True)

            # --- 获取源歌单 ---
            playlist_candidates = parse_playlist_input(playlist_input_data, file_logger)
            if not playlist_candidates:
                log_message('error', "无效的歌单输入，处理中止。", file_logger, to_gui=True)
                app_instance.match_thread_explicitly_finished = True
                log_queue.put("<<<PROCESS_COMPLETE>>>")
                return

            candidate_type, candidate_data = playlist_candidates[0]
            original_input_for_summary = candidate_data

            if candidate_type == "qq":
                source_playlist_name_from_api, qq_api_song_list = get_qq_playlist_details(candidate_data, file_logger)
                if source_playlist_name_from_api: source_playlist_name = source_playlist_name_from_api
                if qq_api_song_list:
                    source_song_list = [format_qq_music_item(s, file_logger) for s in qq_api_song_list if s]
                playlist_type_for_summary = "QQ Music"
                playlist_id_for_summary = candidate_data
            elif candidate_type == "ncm":
                source_playlist_name_from_api, ncm_track_ids = get_ncm_playlist_details(candidate_data, file_logger)
                if source_playlist_name_from_api: source_playlist_name = source_playlist_name_from_api
                if ncm_track_ids:
                    ncm_api_song_list = get_ncm_track_details(ncm_track_ids, file_logger)
                    source_song_list = [format_ncm_music_item(s, file_logger) for s in ncm_api_song_list if s]
                playlist_type_for_summary = "NetEase Cloud Music"
                playlist_id_for_summary = candidate_data
            elif candidate_type == "apple":
                source_playlist_name_from_api, apple_api_song_list = get_apple_music_playlist_details(candidate_data, file_logger)
                if source_playlist_name_from_api: source_playlist_name = source_playlist_name_from_api
                if apple_api_song_list:
                    source_song_list = apple_api_song_list
                playlist_type_for_summary = "Apple Music"
                playlist_id_for_summary = original_input_for_summary
                if isinstance(original_input_for_summary, str) and ('/' in original_input_for_summary and 'pl.' in original_input_for_summary):
                     id_match_display = re.search(r"(pl\.[a-zA-Z0-9\-_]+)", original_input_for_summary)
                     if id_match_display: playlist_id_for_summary = id_match_display.group(1)

            if not source_playlist_name:
                 source_playlist_name = f"导入的歌单_{get_random_string(4)}"
                 log_message('warning', f"未能从源平台获取歌单名称，将使用默认名称: {source_playlist_name}", file_logger, to_gui=True)
            if not source_song_list:
                log_message('info', f"\n无法处理歌单 '{source_playlist_name or playlist_input_data}' 或歌单为空，处理中止。", file_logger, to_gui=True)
                app_instance.match_thread_explicitly_finished = True
                log_queue.put("<<<PROCESS_COMPLETE>>>")
                return

            source_song_list_valid = [s for s in source_song_list if s and s.get('title')]
            total_songs = len(source_song_list_valid)
            log_message('info', f"\n开始使用索引进行快速匹配 {total_songs} 首源歌曲...", file_logger, to_gui=True)
            if match_mode == "模糊匹配":
                log_message('info', f"模糊匹配阈值分数: {MATCH_THRESHOLD} (满分15)", file_logger, to_gui=True)

            unmatched_source_songs_dict = {s['source_id']: s for s in source_song_list_valid}
            for idx, source_track in enumerate(source_song_list_valid):
                log_message('info', f"\n匹配中 ({idx+1}/{total_songs}): {source_track['platform']} 歌曲 '{source_track['title']}' by '{source_track['artist']}'", file_logger, to_gui=True)
                
                # --- 性能优化：使用索引查找候选者 ---
                lookup_key = _get_title_lookup_key(source_track.get('title'))
                candidate_tracks = navidrome_library_index.get(lookup_key, [])
                
                if not candidate_tracks:
                    log_message('info', f"  => 未在 Navidrome 索引中找到标题为 '{lookup_key}' 的候选歌曲。", file_logger, to_gui=False)
                    continue

                log_message('info', f"  => 在索引中找到 {len(candidate_tracks)} 首候选歌曲，开始精确比较...", file_logger, to_gui=False)
                
                best_match_navidrome, match_score = find_best_match_in_candidates(source_track, candidate_tracks, file_logger, match_mode)

                if best_match_navidrome:
                    matched_tracks.append(best_match_navidrome)
                    if source_track['source_id'] in unmatched_source_songs_dict:
                        del unmatched_source_songs_dict[source_track['source_id']]
                else:
                    log_message('info', f"  => 在 {len(candidate_tracks)} 个候选中未找到足够好的匹配 (最高分: {match_score})", file_logger, to_gui=False)

                log_message('info', "-"*30, file_logger, to_gui=False)

        # --- 服务器操作和总结报告 ---
        unmatched_songs_list = list(unmatched_source_songs_dict.values())
        matched_count = len(matched_tracks)
        unmatched_count = len(unmatched_songs_list)
        server_op_status = "未执行 (选项未启用或无匹配歌曲)"

        if create_playlist_on_server and matched_count > 0:
            log_message('info', "开始执行 Navidrome 服务器歌单操作...", file_logger, to_gui=True)
            server_op_status = "失败"
            try:
                playlists_before_create = get_navidrome_playlists(navidrome_config, file_logger)
                existing_playlist_id = None
                for p in playlists_before_create:
                    if p.get('name') == source_playlist_name:
                        existing_playlist_id = p.get('id')
                        log_message('warning', f"发现同名 Navidrome 歌单 '{source_playlist_name}' (ID: {existing_playlist_id})。正在删除...", file_logger, to_gui=True)
                        if delete_navidrome_playlist(existing_playlist_id, navidrome_config, file_logger):
                            log_message('info', f"成功删除旧歌单 (ID: {existing_playlist_id})。", file_logger, to_gui=True)
                        else:
                            log_message('error', f"删除旧歌单 (ID: {existing_playlist_id}) 失败。", file_logger, to_gui=True)
                            gui_alert_queue.put(("error", "歌单删除失败", f"无法删除现有歌单 '{source_playlist_name}'。请检查用户权限或手动删除。"))
                            raise Exception("Deletion failed, stopping playlist creation.")
                        break

                new_playlist_id = create_navidrome_playlist(source_playlist_name, make_playlist_public, navidrome_config, file_logger)
                if new_playlist_id:
                    unique_ids_to_add = list(set([track['id'] for track in matched_tracks]))
                    num_unique_songs_to_add = len(unique_ids_to_add)
                    log_message('info', f"去重后，准备向歌单添加 {num_unique_songs_to_add} 个唯一的 Navidrome 歌曲ID。", file_logger, to_gui=True)
                    if num_unique_songs_to_add > 0:
                        if add_songs_to_navidrome_playlist_batched(new_playlist_id, unique_ids_to_add, navidrome_config, file_logger):
                            server_op_status = f"成功 (创建/更新歌单 ID: {new_playlist_id}, 添加 {num_unique_songs_to_add} 首歌)"
                        else:
                            server_op_status = f"失败 (歌曲添加到歌单 {new_playlist_id} 时出错)"
                    else:
                        server_op_status = f"成功 (创建/更新歌单 ID: {new_playlist_id}, 无新歌添加)"
                else:
                    server_op_status = "失败 (无法创建新歌单)"
            except Exception as e:
                 log_message('error', f"执行服务器歌单操作时出错: {e}", file_logger, to_gui=True)
                 server_op_status = f"失败 ({e})"
            log_message('info', "服务器歌单操作结束。", file_logger, to_gui=True)

        log_message('info', "\n" + "="*30, file_logger, to_gui=True)
        log_message('info', "歌曲匹配/上传完成。", file_logger, to_gui=True)
        log_message('info', "="*30, file_logger, to_gui=True)

        summary = f"""
{'='*30}
最终总结：
{'='*30}
源歌单名称: '{source_playlist_name}' ({playlist_type_for_summary.upper()} ID: {playlist_id_for_summary or '未知'})
歌单总曲数: {total_songs}
成功匹配数 (映射关系): {matched_count}
未匹配数: {unmatched_count}
"""
        if total_songs > 0: summary += f"匹配率: {matched_count / total_songs:.2%}\n"
        else: summary += "匹配率: N/A (源歌单为空)\n"
        summary += f"服务器歌单操作: {server_op_status}\n"

        unmatched_list_str = ""
        if unmatched_songs_list:
            unmatched_list_str += f"\n未匹配成功的 {playlist_type_for_summary.upper()} 歌曲列表 ({unmatched_count} 首):\n"
            unmatched_q.put("--- 未匹配歌曲 ---")
            for i, track in enumerate(unmatched_songs_list):
                track_title = track.get('title', '未知标题')
                track_artist = track.get('artist', '未知歌手')
                track_id = track.get('source_id', '未知ID')
                unmatched_list_str += f"  {i+1}. '{track_title}' by '{track_artist}' (ID: {track_id})\n"
                unmatched_q.put(f"{track_title} - {track_artist}")
        else:
            unmatched_q.put("--- 未匹配歌曲 ---")
            unmatched_q.put("(无)")

        matched_list_str = ""
        if matched_tracks:
            matched_list_str += f"\n成功匹配的 Navidrome 歌曲列表 (共 {matched_count} 条映射关系):\n"
            for i, track_match in enumerate(matched_tracks):
                 source_info = track_match.get('_source_track_info', {})
                 lib_title = track_match.get('title', '未知标题')
                 lib_artist_str = track_match.get('artist', "未知歌手")
                 lib_id = track_match.get('id', '未知ID')
                 lib_path = track_match.get('path', '未知路径')
                 source_platform = source_info.get('platform', '?')
                 source_title = source_info.get('title', '未知标题')
                 source_artist = source_info.get('artist', '未知歌手')
                 source_id = source_info.get('source_id', '未知ID')
                 if playlist_input_type == "file":
                     matched_list_str += (f"  {i+1}. Navidrome: '{lib_title}' by '{lib_artist_str}' (ID: {lib_id}, Path: {lib_path})\n")
                 else:
                     matched_list_str += (f"  {i+1}. Navidrome: '{lib_title}' by '{lib_artist_str}' (ID: {lib_id}, Path: {lib_path}) "
                                     f"<-- {source_platform}: '{source_title}' by '{source_artist}' (ID: {source_id})\n")
        try:
            output_dir = os.path.dirname(output_filepath)
            if output_dir and not os.path.exists(output_dir): os.makedirs(output_dir)
            with open(output_filepath, 'w', encoding='utf-8') as f:
                f.write(summary)
                if unmatched_list_str: f.write(unmatched_list_str)
                if matched_list_str: f.write(matched_list_str)
            log_message('info', f"匹配结果已保存到: {output_filepath}", file_logger, to_gui=True)
        except Exception as e:
            log_message('error', f"保存结果文件失败: {e}", file_logger, to_gui=True)
        log_message('info', "\n处理完成。详细日志已写入 " + log_filename, file_logger, to_gui=True)
        app_instance.match_thread_explicitly_finished = True
    except Exception as e:
        log_message('error', f"匹配过程中发生未处理的错误: {e}", file_logger, to_gui=True)
        if file_logger: file_logger.exception("Unhandled exception during matching:")
        app_instance.match_thread_explicitly_finished = True
    finally:
        log_queue.put("<<<PROCESS_COMPLETE>>>")
        if file_logger and file_handler:
             try:
                 file_logger.removeHandler(file_handler)
                 file_handler.close()
             except Exception as e_close_log:
                 print(f"Error closing main process file logger: {e_close_log}")

# --- GUI 类 (CustomTkinter) ---
class PlaylistMatcherApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} - v{APP_VERSION}")
        self.geometry("1280x768")
        try:
            if hasattr(sys, "_MEIPASS"): icon_path = os.path.join(sys._MEIPASS, "icon.ico")
            else: icon_path = "icon.ico"
            if os.path.exists(icon_path): self.iconbitmap(icon_path)
            else: print(f"图标文件未找到: {icon_path}")
        except Exception as e: print(f"设置窗口图标失败: {e}")
        
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.grid_columnconfigure(0, weight=1, minsize=450)
        self.grid_columnconfigure(1, weight=2)
        self.grid_rowconfigure(0, weight=1)

        self.scan_thread = None
        self.load_thread = None
        self.match_thread = None
        self.connection_thread = None
        self.match_thread_explicitly_finished = False

        left_frame = ctk.CTkFrame(self)
        left_frame.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")
        left_frame.grid_columnconfigure(0, weight=1)

        right_frame = ctk.CTkFrame(self)
        right_frame.grid(row=0, column=1, padx=(0, 10), pady=10, sticky="nsew")
        right_frame.grid_columnconfigure(0, weight=1)
        right_frame.grid_rowconfigure(0, weight=1)
        right_frame.grid_rowconfigure(1, weight=1)

        settings_frame = ctk.CTkFrame(left_frame)
        settings_frame.grid(row=0, column=0, padx=10, pady=(0, 5), sticky="ew")
        settings_frame.grid_columnconfigure(1, weight=1)
        settings_label = ctk.CTkLabel(settings_frame, text="Navidrome 服务器设置", font=ctk.CTkFont(weight="bold"))
        settings_label.grid(row=0, column=0, columnspan=3, pady=(5, 5), sticky="w", padx=10)
        ctk.CTkLabel(settings_frame, text="服务器地址:").grid(row=1, column=0, sticky="w", padx=10, pady=2)
        self.url_entry = ctk.CTkEntry(settings_frame, placeholder_text="http://your-navidrome-server:4533")
        self.url_entry.grid(row=1, column=1, columnspan=2, sticky="ew", padx=10, pady=2)
        ctk.CTkLabel(settings_frame, text="用户名:").grid(row=2, column=0, sticky="w", padx=10, pady=2)
        self.user_entry = ctk.CTkEntry(settings_frame)
        self.user_entry.grid(row=2, column=1, sticky="ew", padx=10, pady=2)
        ctk.CTkLabel(settings_frame, text="密码:").grid(row=3, column=0, sticky="w", padx=10, pady=2)
        self.pass_entry = ctk.CTkEntry(settings_frame, show="*")
        self.pass_entry.grid(row=3, column=1, sticky="ew", padx=10, pady=2)
        self.connect_button = ctk.CTkButton(settings_frame, text="连接到 Navidrome", command=self.connect_to_navidrome_thread)
        self.connect_button.grid(row=2, column=2, rowspan=2, padx=(5,10), pady=2, sticky="ns")
        self.server_status_var = ctk.StringVar(value="状态: 未连接")
        self.server_status_label = ctk.CTkLabel(settings_frame, textvariable=self.server_status_var)
        self.server_status_label.grid(row=4, column=0, columnspan=3, sticky="w", padx=10, pady=(2,5))
        self.server_connected = False
        self.server_version = "N/A"

        data_frame = ctk.CTkFrame(left_frame)
        data_frame.grid(row=1, column=0, padx=10, pady=5, sticky="ew")
        data_frame.grid_columnconfigure(0, weight=1)
        data_label = ctk.CTkLabel(data_frame, text="Navidrome 媒体库数据", font=ctk.CTkFont(weight="bold"))
        data_label.grid(row=0, column=0, columnspan=3, pady=(5,10), sticky="w", padx=10)
        self.scan_button = ctk.CTkButton(data_frame, text="扫描 Navidrome 全库", command=self.start_scan_thread)
        self.scan_button.grid(row=1, column=0, columnspan=3, padx=10, pady=5, sticky="ew")
        self.scan_file_var = ctk.StringVar()
        self.scan_file_entry = ctk.CTkEntry(data_frame, textvariable=self.scan_file_var, placeholder_text="或选择已导出的 Navidrome 库 JSON 文件", state='readonly')
        self.scan_file_entry.grid(row=2, column=0, columnspan=2, padx=10, pady=5, sticky="ew")
        self.browse_scan_button = ctk.CTkButton(data_frame, text="浏览...", width=80, command=self.browse_scan_file)
        self.browse_scan_button.grid(row=2, column=2, padx=(0,10), pady=5, sticky="ew")
        self.load_scan_button = ctk.CTkButton(data_frame, text="从文件加载数据", command=self.load_scan_from_file_thread)
        self.load_scan_button.grid(row=3, column=0, padx=10, pady=5, sticky="ew")
        self.export_scan_button = ctk.CTkButton(data_frame, text="导出当前数据到文件", command=self.export_scan_to_file, state="disabled")
        self.export_scan_button.grid(row=3, column=1, columnspan=2, padx=10, pady=5, sticky="ew")

        input_frame = ctk.CTkFrame(left_frame)
        input_frame.grid(row=2, column=0, padx=10, pady=5, sticky="ew")
        input_frame.grid_columnconfigure(0, weight=1)
        input_label = ctk.CTkLabel(input_frame, text="歌单输入源", font=ctk.CTkFont(weight="bold"))
        input_label.grid(row=0, column=0, columnspan=3, pady=(5, 5), sticky="w", padx=10)
        self.playlist_input_source_var = ctk.StringVar(value="url_id")
        self.url_radio = ctk.CTkRadioButton(input_frame, text="从 URL 或 ID 获取歌单", variable=self.playlist_input_source_var, value="url_id", command=self._on_source_selection_change)
        self.url_radio.grid(row=1, column=0, columnspan=3, sticky="w", padx=10, pady=5)
        self.playlist_entry = ctk.CTkEntry(input_frame, placeholder_text="输入歌单 URL 或 ID (QQ/网易云/Apple Music)")
        self.playlist_entry.grid(row=2, column=0, columnspan=3, sticky="ew", padx=10, pady=(0,5))
        self.file_radio = ctk.CTkRadioButton(input_frame, text="从匹配结果文件上传歌单 (跳过匹配)", variable=self.playlist_input_source_var, value="file", command=self._on_source_selection_change)
        self.file_radio.grid(row=3, column=0, columnspan=3, sticky="w", padx=10, pady=5)
        self.result_file_path_var = ctk.StringVar()
        self.result_file_entry = ctk.CTkEntry(input_frame, textvariable=self.result_file_path_var, placeholder_text="选择之前导出的匹配结果文件 (.txt)", state='readonly')
        self.result_file_entry.grid(row=4, column=0, columnspan=2, padx=10, pady=(0,5), sticky="ew")
        self.browse_result_file_button = ctk.CTkButton(input_frame, text="浏览...", width=80, command=self.browse_result_file)
        self.browse_result_file_button.grid(row=4, column=2, padx=(0,10), pady=(0,5), sticky="ew")

        output_op_frame = ctk.CTkFrame(left_frame)
        output_op_frame.grid(row=3, column=0, padx=10, pady=5, sticky="ew")
        output_op_frame.grid_columnconfigure(1, weight=1)
        output_op_label = ctk.CTkLabel(output_op_frame, text="输出与服务器操作", font=ctk.CTkFont(weight="bold"))
        output_op_label.grid(row=0, column=0, columnspan=3, pady=(5, 5), sticky="w", padx=10)
        
        ctk.CTkLabel(output_op_frame, text="匹配模式:").grid(row=1, column=0, sticky="w", padx=10, pady=5)
        self.match_mode_var = ctk.StringVar(value="模糊匹配")
        self.match_mode_seg_button = ctk.CTkSegmentedButton(output_op_frame, values=["模糊匹配", "完全匹配"], variable=self.match_mode_var)
        self.match_mode_seg_button.grid(row=1, column=1, columnspan=2, padx=10, pady=5, sticky="ew")
        
        ctk.CTkLabel(output_op_frame, text="结果保存:").grid(row=2, column=0, sticky="w", padx=10, pady=2)
        self.output_path_var = ctk.StringVar()
        self.output_entry = ctk.CTkEntry(output_op_frame, textvariable=self.output_path_var, state='readonly')
        self.output_entry.grid(row=2, column=1, sticky="ew", padx=(10, 5), pady=2)
        self.browse_button = ctk.CTkButton(output_op_frame, text="浏览...", width=80, command=self.browse_output_file)
        self.browse_button.grid(row=2, column=2, padx=(0, 10), pady=2)
        self.create_playlist_var = ctk.BooleanVar(value=True)
        self.create_playlist_check = ctk.CTkCheckBox(output_op_frame, text="在 Navidrome 上创建/更新同名歌单 (若存在则覆盖)", variable=self.create_playlist_var, command=self.update_button_states)
        self.create_playlist_check.grid(row=3, column=0, columnspan=3, padx=10, pady=(10,0), sticky="w")
        self.make_public_var = ctk.BooleanVar(value=False)
        self.make_public_check = ctk.CTkCheckBox(output_op_frame, text="将创建的歌单设为公开", variable=self.make_public_var)
        self.make_public_check.grid(row=4, column=0, columnspan=3, padx=10, pady=(10,0), sticky="w")
        
        self.start_button = ctk.CTkButton(left_frame, text="开始匹配与创建", command=self.start_matching_thread, height=30, state="disabled")
        self.start_button.grid(row=4, column=0, padx=10, pady=10, sticky="ew")

        log_frame = ctk.CTkFrame(right_frame)
        log_frame.grid(row=0, column=0, padx=10, pady=(0, 5), sticky="nsew")
        log_frame.grid_rowconfigure(1, weight=1)
        log_frame.grid_columnconfigure(0, weight=1)
        log_label = ctk.CTkLabel(log_frame, text="日志和状态", font=ctk.CTkFont(weight="bold"))
        log_label.grid(row=0, column=0, pady=(5, 5), sticky="w", padx=10)
        self.log_area = ctk.CTkTextbox(log_frame, wrap='word', state='disabled', activate_scrollbars=True)
        self.log_area.grid(row=1, column=0, padx=10, pady=(0, 5), sticky="nsew")

        unmatched_frame = ctk.CTkFrame(right_frame)
        unmatched_frame.grid(row=1, column=0, padx=10, pady=(5, 0), sticky="nsew")
        unmatched_frame.grid_rowconfigure(1, weight=1)
        unmatched_frame.grid_columnconfigure(0, weight=1)
        unmatched_label = ctk.CTkLabel(unmatched_frame, text="未匹配歌曲", font=ctk.CTkFont(weight="bold"))
        unmatched_label.grid(row=0, column=0, pady=(5, 5), sticky="w", padx=10)
        self.unmatched_area = ctk.CTkTextbox(unmatched_frame, wrap='word', state='disabled', activate_scrollbars=True)
        self.unmatched_area.grid(row=1, column=0, padx=10, pady=(0, 5), sticky="nsew")
        
        self.library_data = []
        self._on_source_selection_change()
        self.update_button_states()
        self.update_log_area()
        self.update_unmatched_area()
        self.process_gui_alerts()

    def process_gui_alerts(self):
        try:
            while True:
                alert_type, title, message = gui_alert_queue.get_nowait()
                if alert_type == "error": messagebox.showerror(title, message)
                elif alert_type == "warning": messagebox.showwarning(title, message)
                elif alert_type == "info": messagebox.showinfo(title, message)
        except queue.Empty: pass
        self.after(200, self.process_gui_alerts)

    def connect_to_navidrome_thread(self):
        self.server_status_var.set("状态: 连接中...")
        self.update_idletasks()
        temp_connect_logger = self._get_temp_file_logger("navidrome_connect")
        url = self.url_entry.get().strip().rstrip('/')
        user = self.user_entry.get().strip()
        password = self.pass_entry.get()
        if not (url and user and password):
            gui_alert_queue.put(("error", "错误", "请填写 Navidrome 服务器地址、用户名和密码！"))
            self.server_status_var.set("状态: 连接失败 (信息不全)")
            log_queue.put("<<<PROCESS_COMPLETE_FOR_BUTTON_REACTIVATION_ONLY>>>")
            return
        self.update_button_states()
        self.connection_thread = threading.Thread(target=self._connect_to_navidrome_worker, args=(url, user, password, temp_connect_logger), daemon=True)
        self.connection_thread.start()

    def _connect_to_navidrome_worker(self, base_url, username, password, file_logger):
        try:
            ping_response = call_navidrome_api("ping", {}, base_url, username, password, file_logger)
            if ping_response and ping_response.get('status') == 'ok':
                self.server_connected = True
                self.server_version = ping_response.get('serverVersion', 'N/A')
                log_message('info', f"Navidrome 连接成功: {username}@{base_url}", file_logger, to_gui=True)
                self.server_status_var.set(f"状态: 已连接到 Navidrome (v{self.server_version})")
            else:
                self.server_connected = False
                self.server_status_var.set("状态: 连接失败")
                log_message('error', "Navidrome 连接失败。请检查服务器信息和凭据。", file_logger, to_gui=True)
                gui_alert_queue.put(("error", "连接失败", "Navidrome 认证失败。请检查服务器信息和凭据。"))
        except Exception as e:
            self.server_connected = False
            self.server_status_var.set("状态: 连接错误")
            log_message('error', f"连接 Navidrome 时发生意外错误: {e}", file_logger, to_gui=True)
            gui_alert_queue.put(("error", "连接错误", f"连接 Navidrome 时发生错误: {e}"))
        finally:
            log_queue.put("<<<PROCESS_COMPLETE>>>")

    def _on_source_selection_change(self):
        selected_source = self.playlist_input_source_var.get()
        if selected_source == "url_id":
            self.playlist_entry.configure(state='normal')
            self.result_file_entry.configure(state='disabled')
            self.browse_result_file_button.configure(state='disabled')
        else:
            self.playlist_entry.configure(state='disabled')
            self.result_file_entry.configure(state='readonly')
            self.browse_result_file_button.configure(state='normal')
        self.update_button_states()

    def update_button_states(self):
        data_loaded = bool(self.library_data)
        scan_active = self.scan_thread and self.scan_thread.is_alive()
        load_active = self.load_thread and self.load_thread.is_alive()
        match_active = self.match_thread and self.match_thread.is_alive()
        connect_active = self.connection_thread and self.connection_thread.is_alive()
        any_thread_active = scan_active or load_active or match_active or connect_active

        self.connect_button.configure(state="disabled" if any_thread_active else "normal")
        self.scan_button.configure(state="normal" if self.server_connected and not any_thread_active and self.playlist_input_source_var.get() == "url_id" else "disabled")
        self.load_scan_button.configure(state="normal" if not any_thread_active else "disabled")
        self.browse_scan_button.configure(state="normal" if not any_thread_active else "disabled")
        self.export_scan_button.configure(state="normal" if data_loaded and not any_thread_active else "disabled")

        can_start_matching_base = not any_thread_active
        if self.playlist_input_source_var.get() == "url_id":
            can_start_matching = can_start_matching_base and data_loaded
        else:
            can_start_matching = can_start_matching_base and bool(self.result_file_path_var.get())

        if self.create_playlist_var.get():
            can_start_matching = can_start_matching and self.server_connected
            self.make_public_check.configure(state="normal" if not any_thread_active else "disabled")
        else:
            self.make_public_check.configure(state="disabled")

        self.start_button.configure(state="normal" if can_start_matching else "disabled")
        self.create_playlist_check.configure(state="normal" if not any_thread_active else "disabled")

    def browse_output_file(self):
        filepath = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text files", "*.txt"), ("All files", "*.*")], title="选择结果保存位置", initialfile=f"Navidrome匹配结果_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
        if filepath: self.output_path_var.set(filepath)

    def browse_scan_file(self):
        filepath = filedialog.askopenfilename(defaultextension=".json", filetypes=[("JSON files", "*.json"), ("All files", "*.*")], title="选择已导出的 Navidrome 库数据文件")
        if filepath: self.scan_file_var.set(filepath)

    def browse_result_file(self):
        filepath = filedialog.askopenfilename(defaultextension=".txt", filetypes=[("Text files", "*.txt"), ("All files", "*.*")], title="选择匹配结果文件")
        if filepath: self.result_file_path_var.set(filepath)
        self.update_button_states()

    def start_scan_thread(self):
        if not self.server_connected:
            gui_alert_queue.put(("error", "错误", "请先连接到 Navidrome 服务器。"))
            return
        navidrome_config = {
            'url': self.url_entry.get().strip().rstrip('/'),
            'username': self.user_entry.get().strip(),
            'password': self.pass_entry.get()
        }
        log_queue.put("[INFO] 开始 Navidrome 全库扫描...")
        self.update_button_states()
        self.scan_thread = threading.Thread(target=self._execute_scan, args=(navidrome_config,), daemon=True)
        self.scan_thread.start()

    def _execute_scan(self, navidrome_config):
        temp_file_logger = self._get_temp_file_logger("navidrome_scan")
        try:
            scanned_songs = []
            offset = 0
            
            while True:
                params = {'query': '', 'songCount': NAVIDROME_SCAN_PAGE_SIZE, 'songOffset': offset, 'artistCount': 0, 'albumCount': 0}
                response_data = call_navidrome_api(
                    "search3", 
                    params, 
                    navidrome_config['url'], 
                    navidrome_config['username'], 
                    navidrome_config['password'], 
                    temp_file_logger
                )
                
                if response_data and 'searchResult3' in response_data:
                    items = response_data['searchResult3'].get('song', [])
                    if not isinstance(items, list): items = [items]
                    if not items: break

                    for item_data in items:
                        formatted_item = format_navidrome_music_item(item_data, temp_file_logger)
                        if formatted_item: scanned_songs.append(formatted_item)

                    log_message('info', f"已扫描 {len(scanned_songs)} 首 Navidrome 歌曲...", temp_file_logger, to_gui=True)

                    if len(items) < NAVIDROME_SCAN_PAGE_SIZE: break
                    offset += NAVIDROME_SCAN_PAGE_SIZE
                    time.sleep(0.2)
                else:
                    log_message('error', "扫描 Navidrome 库时出错或未返回项目。", temp_file_logger, to_gui=True)
                    break
            
            self.library_data = scanned_songs
            log_message('info', f"Navidrome 全库扫描完成。共找到 {len(self.library_data)} 首歌曲。", temp_file_logger, to_gui=True)
            if not self.library_data:
                gui_alert_queue.put(("warning", "扫描结果", "Navidrome 库扫描未找到任何音频项目。"))
            else:
                gui_alert_queue.put(("info", "扫描完成", f"Navidrome 全库扫描完成。共找到 {len(self.library_data)} 首歌曲。"))

        except Exception as e:
            log_message('error', f"Navidrome 全库扫描失败: {e}", temp_file_logger, to_gui=True)
            if temp_file_logger:
                 import traceback
                 temp_file_logger.error(traceback.format_exc())
            gui_alert_queue.put(("error", "扫描失败", f"Navidrome 全库扫描失败: {e}"))
            self.library_data = []
        finally:
            log_queue.put("<<<PROCESS_COMPLETE>>>")

    def load_scan_from_file_thread(self):
        filepath = self.scan_file_var.get()
        if not filepath:
            gui_alert_queue.put(("error", "错误", "请先选择要加载的 Navidrome 库数据文件。"))
            return
        log_queue.put(f"[INFO] 开始从文件 {os.path.basename(filepath)} 加载 Navidrome 库数据...")
        self.update_button_states()
        self.load_thread = threading.Thread(target=self._execute_load_scan, args=(filepath,), daemon=True)
        self.load_thread.start()

    def _execute_load_scan(self, filepath):
        try:
            with open(filepath, 'r', encoding='utf-8') as f: loaded_data = json.load(f)
            if not isinstance(loaded_data, list) or (loaded_data and not isinstance(loaded_data[0], dict)):
                 raise ValueError("文件内容格式不正确。")
            self.library_data = loaded_data
            log_message('info', f"成功从文件加载 {len(self.library_data)} 条歌曲信息。", None, to_gui=True)
            gui_alert_queue.put(("info", "加载完成", f"成功加载 {len(self.library_data)} 条歌曲信息。"))
        except Exception as e:
            log_message('error', f"从文件加载数据失败: {e}", None, to_gui=True)
            gui_alert_queue.put(("error", "加载失败", f"错误: {e}"))
            self.library_data = []
        finally:
            log_queue.put("<<<PROCESS_COMPLETE>>>")

    def export_scan_to_file(self):
        if not self.library_data:
            messagebox.showwarning("无数据", "没有 Navidrome 库数据可供导出。")
            return
        filepath = filedialog.asksaveasfilename(
            defaultextension=".json", filetypes=[("JSON files", "*.json")],
            title="选择 Navidrome 库数据保存位置",
            initialfile=f"navidrome_library_scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        if not filepath: return
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(self.library_data, f, ensure_ascii=False, indent=2)
            log_message('info', f"库数据已成功导出到: {filepath}", None, to_gui=True)
            messagebox.showinfo("导出成功", f"Navidrome 库数据已导出到:\n{filepath}")
        except Exception as e:
            log_message('error', f"导出数据失败: {e}", None, to_gui=True)
            messagebox.showerror("导出失败", f"导出文件失败: {e}")

    def update_log_area(self):
        try:
            while True:
                msg = log_queue.get_nowait()
                if msg == "<<<PROCESS_COMPLETE>>>":
                    self.update_button_states()
                    if hasattr(self, 'match_thread_explicitly_finished') and self.match_thread_explicitly_finished:
                        gui_alert_queue.put(("info", "完成", "歌单匹配与创建过程已完成！"))
                        self.match_thread_explicitly_finished = False
                    continue
                elif msg == "<<<PROCESS_COMPLETE_FOR_BUTTON_REACTIVATION_ONLY>>>":
                     self.update_button_states()
                     continue
                self.log_area.configure(state='normal')
                self.log_area.insert('end', msg + '\n')
                self.log_area.configure(state='disabled')
                self.log_area.see('end')
                self.update()
        except queue.Empty:
            pass
        finally:
            self.after(100, self.update_log_area)

    def update_unmatched_area(self):
        try:
            while True:
                msg = unmatched_queue.get_nowait()
                self.unmatched_area.configure(state='normal')
                if msg == "--- 未匹配歌曲 ---":
                    self.unmatched_area.insert('end', msg + '\n', ('header',))
                    self.unmatched_area.tag_config('header', font=ctk.CTkFont(weight="bold"))
                else:
                    self.unmatched_area.insert('end', msg + '\n')
                self.unmatched_area.configure(state='disabled')
                self.unmatched_area.see('end')
                self.update()
        except queue.Empty:
            pass
        finally:
            self.after(100, self.update_unmatched_area)

    def _get_temp_file_logger(self, process_name):
        try:
            logger_instance_name = f'TempFileLogger_{process_name}_{get_random_string(6)}'
            temp_logger = logging.getLogger(logger_instance_name)
            temp_logger.propagate = False
            temp_logger.setLevel(logging.INFO)
            if temp_logger.hasHandlers():
                for handler in temp_logger.handlers[:]:
                    handler.close()
                    temp_logger.removeHandler(handler)
            log_filename_temp = f"{LOG_FILE_BASENAME}_{process_name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
            file_handler_temp = logging.FileHandler(log_filename_temp, mode='w', encoding='utf-8')
            file_formatter_temp = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
            file_handler_temp.setFormatter(file_formatter_temp)
            temp_logger.addHandler(file_handler_temp)
            log_message('info', f"临时操作日志文件已创建: {log_filename_temp}", None, to_gui=True)
            return temp_logger
        except Exception as e:
            log_message('error', f"创建临时日志文件处理器失败: {e}", None, to_gui=True)
            return None

    def _parse_result_file_for_upload(self, filepath, file_logger):
        playlist_name = None
        matched_song_ids = []
        unmatched_songs = []
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            name_match = re.search(r"源歌单名称: '(.*?)'", content)
            if name_match:
                playlist_name = name_match.group(1).strip()
            
            # Updated regex to be more robust for Navidrome files
            matched_section_match = re.search(r"成功匹配的 Navidrome 歌曲列表.*?:\n(.*?)(?=\n未匹配成功的|\Z)", content, re.DOTALL)
            if matched_section_match:
                for line in matched_section_match.group(1).strip().split('\n'):
                    id_match = re.search(r"\(ID: ([\w\d\-]+),", line)
                    if id_match:
                        matched_song_ids.append(id_match.group(1))

            unmatched_section_match = re.search(r"未匹配成功的 .*? 歌曲列表 .*?:\n(.*?)(?=\n\n成功匹配的|\Z)", content, re.DOTALL)
            if unmatched_section_match:
                for line in unmatched_section_match.group(1).strip().split('\n'):
                    unmatched_match = re.search(r"^\s*\d+\.\s*'(.*?)'\s*by\s*'(.*?)'\s*\(ID:\s*(.*?)\)$", line)
                    if unmatched_match:
                        unmatched_songs.append({'title': unmatched_match.group(1), 'artist': unmatched_match.group(2), 'source_id': unmatched_match.group(3)})
            return playlist_name, matched_song_ids, unmatched_songs
        except Exception as e:
            log_message('error', f"解析结果文件时发生错误: {e}", file_logger)
            return None, [], []

    def start_matching_thread(self):
        playlist_input_type = self.playlist_input_source_var.get()
        playlist_input_data = ""
        if playlist_input_type == "url_id":
            playlist_input_data = self.playlist_entry.get().strip()
            if not playlist_input_data:
                gui_alert_queue.put(("error", "错误", "请输入歌单 URL 或 ID！"))
                return
            if not self.library_data:
                gui_alert_queue.put(("error", "错误", "Navidrome 媒体库数据未加载。"))
                return
        else:
            playlist_input_data = self.result_file_path_var.get()
            if not playlist_input_data:
                gui_alert_queue.put(("error", "错误", "请选择匹配结果文件！"))
                return
        
        output_path = self.output_path_var.get()
        if not output_path:
            output_path = f"Navidrome匹配结果_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            self.output_path_var.set(output_path)
            log_message('info', f"结果输出文件位置未指定，将默认保存到: {output_path}", None, to_gui=True)

        create_playlist_flag = self.create_playlist_var.get()
        if create_playlist_flag and not self.server_connected:
            gui_alert_queue.put(("error", "错误", "已选择在服务器上创建歌单，但当前未连接。"))
            return

        if create_playlist_flag:
             confirm_text = "您已选择在 Navidrome 上创建/更新歌单。\n如果服务器上存在同名歌单，它将被删除并替换。\n"
             if self.make_public_var.get():
                 confirm_text += "\n同时，新歌单将被尝试设为公开。\n"
             confirm_text += "\n确定要继续吗？"
             if not messagebox.askyesno("确认操作", confirm_text, icon='warning'):
                 return

        self.log_area.configure(state='normal'); self.log_area.delete("1.0", 'end'); self.log_area.configure(state='disabled')
        self.unmatched_area.configure(state='normal'); self.unmatched_area.delete("1.0", 'end'); self.unmatched_area.configure(state='disabled')
        
        log_queue.put("开始匹配与创建过程...")
        self.update_button_states()
        
        navidrome_config = {
            'url': self.url_entry.get().strip().rstrip('/'),
            'username': self.user_entry.get().strip(),
            'password': self.pass_entry.get()
        }
        
        match_mode = self.match_mode_var.get()

        self.match_thread_explicitly_finished = False
        self.match_thread = threading.Thread(
            target=run_matching_process,
            args=(self, navidrome_config, playlist_input_type, playlist_input_data, output_path,
                  create_playlist_flag, self.make_public_var.get(), match_mode,
                  unmatched_queue, self.library_data),
            daemon=True
        )
        self.match_thread.start()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', stream=sys.stdout)
    if hasattr(sys, '_MEIPASS'):
        ctk.set_appearance_mode("System")
    app = PlaylistMatcherApp()
    app.mainloop()