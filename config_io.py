"""验证并原子替换配置文件，供网页和配置向导共用。"""

import shutil
from types import ModuleType
from importlib.util import cache_from_source
from pathlib import Path
from runpy import run_path
from tempfile import NamedTemporaryFile


def loopback_host(host=None):
    """旧配置指定外部地址时，回退到本机 IPv4 回环地址。"""
    return host if host in ('127.0.0.1', '::1', 'localhost') else '127.0.0.1'


def check_frequency_minutes(settings):
    """读取实际检查间隔，缺省时使用每小时整点检查。"""
    try:
        frequency = int(getattr(settings, 'CHECK_FREQUENCY_MINUTES', None))
    except (TypeError, ValueError, OverflowError):
        return 60
    return frequency if frequency > 0 else 60


def setup_required(settings):
    required = {
        'BUPT_USERNAME': '你的学号',
        'BUPT_PASSWORD': '你的密码',
        'APARTMENT_ID': '你的公寓楼ID',
        'FLOOR_ID': '你的楼层',
        'ROOM_NUMBER': '你的房间号',
    }
    for key, placeholder in required.items():
        value = getattr(settings, key, None)
        if value is None or str(value).strip() in ('', placeholder):
            return True
    return str(getattr(settings, 'AREA_ID', None)) not in ('1', '2')


def initial_config(path):
    """为首次启动加载内存配置，完成浏览器引导后再写入 config.py。"""
    defaults = run_path(str(path.with_name('config.py.example')))
    settings = ModuleType('config')
    settings.__file__ = str(path)
    vars(settings).update((key, value) for key, value in defaults.items() if key.isupper())
    settings.EMAIL_USERNAME = ''
    settings.EMAIL_PASSWORD = ''
    settings.ALERT_EMAILS = []
    return settings


def write_config(path, content):
    path = Path(path).resolve()
    values = {}
    exec(compile(content, str(path), 'exec'), values)

    temporary = NamedTemporaryFile(
        mode='w', encoding='utf-8', suffix='.tmp', prefix='config-',
        dir=path.parent, delete=False,
    )
    temporary_path = Path(temporary.name)
    try:
        with temporary:
            temporary.write(content)
        if path.exists():
            shutil.copy2(path, path.with_suffix('.py.backup'))
        # 同一秒内保存同样大小的配置时，也要让下次启动读取新值。
        Path(cache_from_source(str(path))).unlink(missing_ok=True)
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)

    return {name: value for name, value in values.items() if name.isupper()}
