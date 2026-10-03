"""Local screenshot selection and OCR. Images never go to a web service."""
import os
import csv
from dataclasses import dataclass
import io
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


@dataclass(frozen=True)
class TextRegion:
    x: int
    y: int
    width: int
    height: int
    text: str


def clean_text(text):
    text = re.sub(r'(?<=[\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff，。！？；：、])', '', text)
    return re.sub(r'(?<=[，。！？；：、])[ \t]+(?=[\u4e00-\u9fff])', '', text).strip()


def parse_regions(tsv, width, height):
    paragraphs = {}
    for row in csv.DictReader(io.StringIO(tsv), delimiter='\t'):
        if row.get('level') != '5' or not row.get('text', '').strip():
            continue
        try:
            key = tuple(int(row[name]) for name in ('page_num', 'block_num', 'par_num'))
            line = int(row['line_num'])
            x, y, w, h = [int(row[name]) for name in ('left', 'top', 'width', 'height')]
        except (KeyError, TypeError, ValueError):
            continue
        left, top, right, bottom = max(0, x), max(0, y), min(width, x + w), min(height, y + h)
        if right <= left or bottom <= top:
            continue
        paragraph = paragraphs.setdefault(key, {'words': [], 'boxes': []})
        paragraph['words'].append((line, row['text'].strip()))
        paragraph['boxes'].append((left, top, right, bottom))
    regions = []
    for paragraph in paragraphs.values():
        lines = {}
        for line, word in paragraph['words']:
            lines.setdefault(line, []).append(word)
        text = clean_text('\n'.join(' '.join(words) for words in lines.values()))
        boxes = paragraph['boxes']
        x, y = min(b[0] for b in boxes), min(b[1] for b in boxes)
        right, bottom = max(b[2] for b in boxes), max(b[3] for b in boxes)
        regions.append(TextRegion(x, y, right - x, bottom - y, text))
    if not regions:
        raise ScreenshotError('没有找到清晰的文字，请放大页面后重新框选。')
    if len(regions) > 80 or sum(len(region.text) for region in regions) > 5000:
        raise ScreenshotError('截图文字较多，请缩小框选区域后重试。')
    return regions


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
    text = clean_text(text)
    if not text:
        raise ScreenshotError('没有识别到文字。请放大页面后重新框选。')
    return text


def screenshot_text(cancel, progress=None):
    runtime = check_tools(cancel)
    image = capture(cancel)
    if progress:
        progress()
    return recognize(image, cancel, runtime)


def recognize_regions(image, width, height, cancel):
    binary, env = ocr_runtime()
    if not image.startswith(PNG_HEADER):
        raise ScreenshotError('截图不是有效的 PNG 图片。')
    with tempfile.TemporaryDirectory(prefix='qingyi-ocr-', dir=os.environ.get('XDG_RUNTIME_DIR')) as folder:
        path = Path(folder) / 'selection.png'
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as file:
            file.write(image)
        code, output, _ = run_process([binary, str(path), 'stdout', '-l', 'eng+chi_sim',
                                      '--psm', '3', 'tsv'], cancel, 60, env)
    if code:
        raise ScreenshotError('文字定位失败，请重新截取清晰的文字区域。')
    return parse_regions(output.decode('utf-8', 'replace'), width, height)
