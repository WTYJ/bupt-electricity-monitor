"""读取本机版本并检查项目的 GitHub 正式发布。"""

import logging
import re
from pathlib import Path
from threading import Thread

import requests


REPOSITORY = 'WTYJ/bupt-electricity-monitor'
RELEASES_URL = f'https://github.com/{REPOSITORY}/releases'
LATEST_RELEASE_API = f'https://api.github.com/repos/{REPOSITORY}/releases/latest'
VERSION_FILE = Path(__file__).with_name('VERSION')


class SoftwareUpdates:
    """在服务进程内保留最近一次可比较的版本结果。"""

    def __init__(self):
        self.current_version = read_version()
        self.latest_result = {}
        self.startup_thread = None

    def check(self):
        result = check_for_update(self.current_version)
        if 'update_available' in result:
            self.latest_result = result
        return result

    def check_on_launch(self):
        result = self.check()
        logging.info('软件更新：%s', result['message'])
        return result

    def start_check(self):
        self.startup_thread = Thread(target=self.check_on_launch, daemon=True)
        self.startup_thread.start()

    def available_version(self):
        if self.startup_thread is not None:
            self.startup_thread.join(timeout=10)
        result = self.latest_result
        return result.get('latest_version') if result.get('update_available') else None


def _parse_version(tag):
    match = re.fullmatch(r'v?([0-9]+)\.([0-9]+)\.([0-9]+)', tag or '')
    return tuple(map(int, match.groups())) if match else None


def read_version():
    try:
        version = VERSION_FILE.read_text(encoding='ascii').strip()
        return version if _parse_version(version) is not None else None
    except (OSError, ValueError):
        return None


def check_for_update(current_version):
    try:
        response = requests.get(
            LATEST_RELEASE_API,
            headers={'Accept': 'application/vnd.github+json'},
            timeout=(3, 5),
        )
        if response.status_code == 404:
            return {'message': 'GitHub 未找到可用的正式发布。'}
        if response.status_code == 429 or (
            response.status_code == 403 and response.headers.get('X-RateLimit-Remaining') == '0'
        ):
            return {'message': 'GitHub 暂时限制了查询，请稍后重试。'}
        response.raise_for_status()
        release = response.json()
        latest_version = release.get('tag_name') if isinstance(release, dict) else None
        latest = _parse_version(latest_version) if isinstance(latest_version, str) else None
        if latest is None:
            raise ValueError('未找到可比较的正式版本号')
    except (requests.RequestException, ValueError) as exc:
        logging.warning('检查软件更新失败: %s', exc)
        return {'message': '暂时无法检查 GitHub 更新，请稍后重试。'}

    current = _parse_version(current_version)
    if current is None:
        message = f'GitHub 最新正式版本为 {latest_version}，当前版本未知。'
    elif latest > current:
        message = f'发现新版本 {latest_version}。'
    elif latest == current:
        message = '已是最新正式版本。'
    else:
        message = f'当前版本高于 GitHub 最新正式版本 {latest_version}。'

    return {
        'message': message,
        'latest_version': latest_version,
        'update_available': current is not None and latest > current,
        'release_url': f'{RELEASES_URL}/tag/{latest_version}',
    }
