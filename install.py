#!/usr/bin/python3
"""Install/uninstall only Qingyi's own user files and GNOME shortcut entries."""
import shutil
import subprocess
import sys
from pathlib import Path

import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gio, Gtk

home = Path.home()
app_dir = home / '.local/share/qingyi'
desktop = home / '.local/share/applications/io.local.Qingyi.desktop'
launcher = home / '.local/bin/qingyi'
base = '/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/'
entries = [('qingyi-selection/', '轻译：划词翻译', '<Alt>q', '--selection'),
           ('qingyi-clipboard/', '轻译：复制翻译', '<Alt><Shift>q', '--clipboard'),
           ('qingyi-screenshot/', '轻译：截图翻译', '<Alt>s', '--screenshot')]
media = Gio.Settings.new('org.gnome.settings-daemon.plugins.media-keys')
paths = list(media.get_strv('custom-keybindings'))


def own_settings(suffix):
    return Gio.Settings.new_with_path('org.gnome.settings-daemon.plugins.media-keys.custom-keybinding', base + suffix)


def accel(binding):
    ok, key, mods = Gtk.accelerator_parse(binding)
    return (key, int(mods)) if ok else None


if '--uninstall' in sys.argv:
    if launcher.exists():
        subprocess.run([str(launcher), '--quit'], timeout=5, check=False)
    try:
        from ai_backend import secret_api
        secret, schema = secret_api()
        if secret is not None:
            secret.password_clear_sync(schema, {}, None)
    except Exception:
        print('系统钥匙串暂不可访问；可在“密码和密钥”中删除轻译 API Key。')
    model_config = home / '.config/qingyi/models.json'
    model_config.unlink(missing_ok=True)
    if model_config.parent.is_dir() and not any(model_config.parent.iterdir()):
        model_config.parent.rmdir()
    own = {base + suffix for suffix, *_ in entries}
    media.set_strv('custom-keybindings', [p for p in paths if p not in own])
    for suffix, *_ in entries:
        setting = own_settings(suffix)
        for key in ('name', 'command', 'binding'):
            setting.reset(key)
    Gio.Settings.sync()
    launcher.unlink(missing_ok=True)
    desktop.unlink(missing_ok=True)
    if app_dir.exists():
        shutil.rmtree(app_dir)
    print('轻译已卸载；原有快捷键保持不变。')
    raise SystemExit(0)

# Detect conflicts before writing anything; don't replace the user's shortcuts.
bindings = []
for path in paths:
    if path not in {base + e[0] for e in entries}:
        setting = Gio.Settings.new_with_path('org.gnome.settings-daemon.plugins.media-keys.custom-keybinding', path)
        bindings.append((setting.get_string('binding'), setting.get_string('name')))
schemas = Gio.SettingsSchemaSource.get_default()
for schema_name in ('org.gnome.desktop.wm.keybindings', 'org.gnome.shell.keybindings',
                    'org.gnome.mutter.keybindings', 'org.gnome.mutter.wayland.keybindings',
                    'org.gnome.settings-daemon.plugins.media-keys'):
    schema = schemas.lookup(schema_name, True)
    if not schema:
        continue
    setting = Gio.Settings.new(schema_name)
    for key in schema.list_keys():
        if key == 'custom-keybindings':
            continue
        value = setting.get_value(key)
        if value.get_type_string() == 'as':
            bindings.extend((binding, key) for binding in value.unpack())
for _, name, binding, _ in entries:
    conflict = next((label for existing, label in bindings if accel(existing) == accel(binding)), None)
    if conflict:
        raise SystemExit(f'{name}快捷键冲突：{conflict}；尚未安装。')

app_dir.mkdir(parents=True, exist_ok=True)
launcher.parent.mkdir(parents=True, exist_ok=True)
desktop.parent.mkdir(parents=True, exist_ok=True)
source = Path(__file__).resolve().parent
if '--with-ocr' in sys.argv:
    from setup_ocr import install_ocr
    install_ocr(app_dir)
for name in ('qingyi.py', 'ai_backend.py', 'preferences.py', 'chat_panel.py', 'screenshot.py',
             'setup_ocr.py', 'install.py', 'README.md'):
    if (source / name).resolve() != (app_dir / name).resolve():
        shutil.copy2(source / name, app_dir / name)
if (source / 'assets').resolve() != (app_dir / 'assets').resolve():
    shutil.copytree(source / 'assets', app_dir / 'assets', dirs_exist_ok=True)
# GNOME bridges native Wayland selections to Xwayland. This backend can read
# the selection before the popup takes focus, including on the first launch.
launcher.write_text('#!/bin/sh\nexport GDK_BACKEND=x11,wayland\nexec /usr/bin/python3 "' + str(app_dir / 'qingyi.py') + '" "$@"\n')
launcher.chmod(0o755)
desktop.write_text(f'''[Desktop Entry]
Version=1.0
Type=Application
Name=轻译
Name[en]=Qingyi Translator
Comment=划词与截图翻译、AI 总结与小黑阅读助手
Exec={launcher}
Icon={app_dir / 'assets/black-cat.svg'}
Terminal=false
Categories=Utility;
Keywords=翻译;划词;translate;translation;
StartupNotify=true
StartupWMClass=io.local.Qingyi
Actions=Selection;Clipboard;Screenshot;Quit;

[Desktop Action Selection]
Name=翻译选中文字
Exec={launcher} --selection

[Desktop Action Clipboard]
Name=翻译剪贴板
Exec={launcher} --clipboard

[Desktop Action Screenshot]
Name=截图翻译
Exec={launcher} --screenshot

[Desktop Action Quit]
Name=退出轻译
Exec={launcher} --quit
''')
for suffix, name, binding, arg in entries:
    setting = own_settings(suffix)
    setting.set_string('name', name)
    setting.set_string('command', f'{launcher} {arg}')
    setting.set_string('binding', binding)
    if base + suffix not in paths:
        paths.append(base + suffix)
media.set_strv('custom-keybindings', paths)
Gio.Settings.sync()
update_database = shutil.which('update-desktop-database')
if update_database:
    subprocess.run([update_database, str(desktop.parent)], check=False)
print('已安装轻译。Alt+Q：划词翻译；Alt+Shift+Q：复制翻译；Alt+S：截图翻译。')
