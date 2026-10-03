"""Install distribution OCR packages in Qingyi's user directory, without sudo."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def install_ocr(app_dir):
    if not shutil.which('apt-get') or not shutil.which('dpkg-deb'):
        raise RuntimeError('此免管理员安装方式需要 Ubuntu / Debian 的 apt-get 和 dpkg-deb。')
    deps = subprocess.check_output(['apt-cache', 'depends', 'libtesseract5'], text=True)
    leptonica = next((line.split()[-1] for line in deps.splitlines()
                      if 'Depends:' in line and line.split()[-1].startswith('liblept')), None)
    if not leptonica:
        raise RuntimeError('系统软件源没有 OCR 依赖，请更新 apt 索引后重试。')
    packages = ['tesseract-ocr', 'libtesseract5', leptonica,
                'tesseract-ocr-eng', 'tesseract-ocr-chi-sim', 'tesseract-ocr-osd']
    app_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.ocr-setup-', dir=app_dir) as folder:
        folder = Path(folder)
        subprocess.run(['apt-get', 'download', *packages], cwd=folder, check=True, timeout=180)
        runtime = folder / 'runtime'
        runtime.mkdir()
        for package in folder.glob('*.deb'):
            subprocess.run(['dpkg-deb', '-x', str(package), str(runtime)], check=True, timeout=30)
        env = os.environ.copy()
        env['LD_LIBRARY_PATH'] = ':'.join(str(p) for p in (runtime / 'usr/lib').glob('*-linux-gnu'))
        data = list((runtime / 'usr/share/tesseract-ocr').glob('*/tessdata'))
        if not data:
            raise RuntimeError('没有找到文字识别语言数据。')
        env['TESSDATA_PREFIX'] = str(data[0])
        result = subprocess.run([str(runtime / 'usr/bin/tesseract'), '--list-langs'],
                                env=env, capture_output=True, text=True, timeout=10)
        if result.returncode or not {'eng', 'chi_sim'} <= set(result.stdout.splitlines()[1:]):
            raise RuntimeError('OCR 缺少系统库，请安装：sudo apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim。')
        destination = app_dir / 'ocr'
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(str(runtime), str(destination))
    print('已安装本机中英文文字识别，不需要上传截图。')


if __name__ == '__main__':
    install_ocr(Path.home() / '.local/share/qingyi')
