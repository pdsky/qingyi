"""Local screenshot selection and OCR. Images never go to a web service."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

from ai_backend import Cancelled

PNG_HEADER = b'\x89PNG\r\n\x1a\n'


class ScreenshotError(ValueError):
    pass


def run_process(args, cancel, timeout, env=None, input_data=None):
    if cancel.is_set():
        raise Cancelled()
    child = subprocess.Popen(args, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    deadline = time.monotonic() + timeout
    try:
        first = True
        while True:
            if cancel.is_set():
                raise Cancelled()
            if time.monotonic() > deadline:
                raise ScreenshotError('截图或文字识别超时，请重新框选一小块文字。')
            try:
                stdout, stderr = child.communicate(input_data if first else None, timeout=.15)
                if cancel.is_set():
                    raise Cancelled()
                return child.returncode, stdout, stderr
            except subprocess.TimeoutExpired:
                first = False
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()


def ocr_runtime():
    """Prefer our verified user installation, otherwise use system Tesseract."""
    runtime = Path.home() / '.local/share/qingyi/ocr'
    binary = runtime / 'usr/bin/tesseract'
    env = os.environ.copy()
    if binary.is_file():
        library_dirs = [str(p) for p in (runtime / 'usr/lib').glob('*-linux-gnu') if p.is_dir()]
        if library_dirs:
            env['LD_LIBRARY_PATH'] = ':'.join(library_dirs)
        data_dirs = list((runtime / 'usr/share/tesseract-ocr').glob('*/tessdata'))
        if data_dirs:
            env['TESSDATA_PREFIX'] = str(data_dirs[0])
        return str(binary), env
    binary = shutil.which('tesseract')
    if not binary:
        raise ScreenshotError('尚未安装文字识别。请运行 /usr/bin/python3 install.py --with-ocr。')
    return binary, env


def check_tools(cancel):
    if not shutil.which('flameshot'):
        raise ScreenshotError('截图需要 Flameshot，请先安装：sudo apt install flameshot。')
    binary, env = ocr_runtime()
    code, languages, _ = run_process([binary, '--list-langs'], cancel, 10, env)
    available = set(languages.decode('utf-8', 'replace').splitlines()[1:])
    if code or not {'eng', 'chi_sim'} <= available:
        raise ScreenshotError('缺少中英文识别数据。请运行 /usr/bin/python3 install.py --with-ocr。')
    return binary, env


def capture(cancel):
    env = os.environ.copy()
    if env.get('XDG_SESSION_TYPE') == 'wayland':
        env['QT_QPA_PLATFORM'] = 'wayland'
    code, image, error = run_process(['flameshot', 'gui', '--raw', '--accept-on-select'],
                                     cancel, 180, env)
    diagnostic = error.decode('utf-8', 'replace').lower()
    if not image and ('abort' in diagnostic or 'cancel' in diagnostic or code == 0):
        raise Cancelled()
    if code or not image.startswith(PNG_HEADER):
        raise ScreenshotError('没有取得截图。若系统询问截图权限，请允许 Flameshot；然后重新截图。')
    if len(image) > 30 * 1024 * 1024:
        raise ScreenshotError('截图太大，请只框选要翻译的文字区域。')
    return image


def recognize(image, cancel, runtime=None):
    binary, env = runtime or ocr_runtime()
    if not image.startswith(PNG_HEADER):
        raise ScreenshotError('截图不是有效的 PNG 图片。')
    # Tesseract gets only the selected crop. The private file is removed on
    # success, failure and cancellation, before any translation request.
    runtime_dir = os.environ.get('XDG_RUNTIME_DIR')
    with tempfile.TemporaryDirectory(prefix='qingyi-ocr-', dir=runtime_dir) as folder:
        path = Path(folder) / 'selection.png'
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as file:
            file.write(image)
        code, output, _ = run_process([binary, str(path), 'stdout', '-l', 'eng+chi_sim',
                                      '--psm', '3'], cancel, 60, env)
    if code:
        raise ScreenshotError('文字识别失败，请重新截取清晰的文字区域。')
    text = output.decode('utf-8', 'replace').strip()
    text = re.sub(r'(?<=[\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff，。！？；：、])', '', text)
    text = re.sub(r'(?<=[，。！？；：、])[ \t]+(?=[\u4e00-\u9fff])', '', text)
    if not text:
        raise ScreenshotError('没有识别到文字。请放大页面后重新框选。')
    return text


def screenshot_text(cancel, progress=None):
    runtime = check_tools(cancel)
    image = capture(cancel)
    if progress:
        progress()
    return recognize(image, cancel, runtime)
