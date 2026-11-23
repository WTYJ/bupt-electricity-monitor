"""读取本机日志尾部，并管理轮转文件。"""

import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import RLock


LOG_PATH = Path('electric_monitor.log')
BACKUP_COUNT = 3
LOG_PATTERN = re.compile(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) - (\w+) - (.+)')
_log_lock = RLock()


def rotating_log_handler():
    handler = RotatingFileHandler(LOG_PATH, maxBytes=2 * 1024 * 1024, backupCount=BACKUP_COUNT, encoding='utf-8')
    handler.lock = _log_lock
    return handler


def _archives():
    return (LOG_PATH.with_name(f'{LOG_PATH.name}.{number}') for number in range(1, BACKUP_COUNT + 1))


def _reverse_lines(path, chunk_size=8192):
    try:
        stream = path.open('rb')
    except FileNotFoundError:
        return
    with stream:
        position = stream.seek(0, 2)
        tail = b''
        while position:
            size = min(position, chunk_size)
            position -= size
            stream.seek(position)
            tail, *lines = (stream.read(size) + tail).split(b'\n')
            yield from reversed(lines)
        if tail:
            yield tail


def recent_logs(limit, level):
    logs = []
    with _log_lock:
        for path in (LOG_PATH, *_archives()):
            for line in _reverse_lines(path):
                # 旧文件可能混有 GBK 行，保留可解码内容。
                match = LOG_PATTERN.match(line.decode('utf-8', errors='replace').strip())
                if match is None or (level != 'all' and match[2] != level):
                    continue
                timestamp, log_level, message = match.groups()
                logs.append({'timestamp': timestamp, 'level': log_level, 'message': message})
                if len(logs) >= limit:
                    return logs
    return logs


def clear_logs():
    with _log_lock:
        if LOG_PATH.exists():
            LOG_PATH.write_bytes(b'')
        for path in _archives():
            path.unlink(missing_ok=True)
