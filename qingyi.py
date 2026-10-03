#!/usr/bin/python3
"""轻译: an on-demand GTK4 selection translator for GNOME desktops."""
import json
from pathlib import Path
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from ai_backend import ModelSettings, summarize, translate_with_model, Cancelled, MAX_SUMMARY_TEXT
from preferences import ModelWindow
from chat_panel import ChatPanel
from screenshot import screenshot_text

MAX_TEXT = 5000
APP_ID = 'io.local.Qingyi'


def chunks(text, limit=1000):
    """Split at sentence boundaries without losing spaces or newlines."""
    while len(text) > limit:
        cut = max(text.rfind('\n', 0, limit), text.rfind('. ', 0, limit),
                  text.rfind('。', 0, limit), text.rfind(' ', 0, limit))
        cut = cut + 1 if cut >= limit // 2 else limit
        yield text[:cut]
        text = text[cut:]
    if text:
        yield text


def translate(text, target='zh-CN'):
    text = text.strip()
    if not text:
        raise ValueError('请先选中、复制或输入要翻译的文字。')
    if len(text) > MAX_TEXT:
        raise ValueError(f'一次最多翻译 {MAX_TEXT} 字，请缩短选中文字。')
    if target not in ('zh-CN', 'en', 'ja', 'ko'):
        raise ValueError('不支持该目标语言。')
    output = []
    for part in chunks(text):
        query = urllib.parse.urlencode(dict(client='gtx', sl='auto', tl=target, dt='t', q=part))
        req = urllib.request.Request('https://translate.googleapis.com/translate_a/single?' + query,
                                     headers={'User-Agent': 'Qingyi/1.0'})
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.load(response)
        if not isinstance(data, list) or not data or not isinstance(data[0], list):
            raise ValueError('翻译服务返回异常，请稍后重试。')
        result = ''.join(row[0] for row in data[0] if row and isinstance(row[0], str))
        if not result:
            raise ValueError('没有收到译文，请稍后重试。')
        output.append(result)
    return '\n'.join(output)


def friendly_error(exc):
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 429:
            return 'Google 翻译限流。可勾选「翻译也用所选 AI 模型」后重试，或稍后再试。'
        return f'翻译服务暂时不可用（HTTP {exc.code}），请稍后重试。'
    if isinstance(exc, (urllib.error.URLError, TimeoutError, socket.timeout)):
        return '连接翻译服务失败，请检查网络后重试，或点击「网页翻译」。'
    if isinstance(exc, (json.JSONDecodeError, IndexError, TypeError)):
        return '翻译服务返回异常，请稍后重试。'
    return str(exc)


def main():
    import gi
    gi.require_version('Gtk', '4.0')
    from gi.repository import Gtk, Gdk, Gio, GLib, Pango

    class Qingyi(Gtk.Application):
        def __init__(self):
            super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
            self.win = None
            self.serial = 0
            self.result = ''
            self.targets = ['zh-CN', 'en', 'ja', 'ko']
            self.reader = None
            self.request_id = 0
            self.model_settings = ModelSettings()
            self.settings_window = None
            self.cancel_event = None
            self.mode = 'translate'
            self.workers = []
            self.chat = None
            self.screenshot_busy = False

        def do_shutdown(self):
            if self.cancel_event:
                self.cancel_event.set()
            if self.chat and self.chat.cancel_event:
                self.chat.cancel_event.set()
            deadline = time.monotonic() + 3
            for worker in self.workers:
                if worker.is_alive():
                    worker.join(timeout=max(0, deadline - time.monotonic()))
            Gtk.Application.do_shutdown(self)

        def do_startup(self):
            Gtk.Application.do_startup(self)
            css = Gtk.CssProvider()
            css.load_from_data(b'''
                window.qingyi-window { background: #faf8f2; color: #39443d; }
                headerbar { background: #faf8f2; color: #39443d; box-shadow: none; }
                .workspace { padding: 20px; }
                .brand { font-size: 28px; font-weight: 800; color: #34443a; }
                .hint { color: #68766d; font-size: 12px; }
                .tagline { color: #778478; font-size: 13px; }
                .section { font-weight: 600; color: #526556; }
                .card { background: #fffefb; border: 1px solid #dce6d9; border-radius: 16px; }
                textview, textview text { background: transparent; color: #39443d; caret-color: #527b62; }
                textview text selection { background: #cee4d0; color: #243b2c; }
                .source { padding: 14px; font-size: 14px; }
                .result { padding: 16px; font-size: 15px; }
                .notice { font-size: 12px; color: #627968; }
                .error { color: #a74152; }
                button { border-radius: 11px; background: #fffdf8; color: #526356; border: 1px solid #d9e3d7; box-shadow: none; }
                button:hover { background: #edf4e9; }
                button.suggested-action { background: #64866c; border-color: #64866c; color: #ffffff; }
                button.suggested-action:hover { background: #52765b; }
                button:disabled { opacity: .55; }
                button:focus-visible { outline: 2px solid #8aac8b; outline-offset: 2px; }
                entry { background: #fffefb; color: #39443d; border-radius: 10px; border-color: #d9e3d7; }
                dropdown button { background: #fffefb; }
                checkbutton check:checked { background: #64866c; border-color: #64866c; color: #ffffff; }
                .model-button { background: #edf3e7; color: #55725b; }
                .chat-pane { background: #f0f5ec; border: 1px solid #dde8d7; border-radius: 22px; padding: 18px; }
                .chat-title { font-size: 18px; font-weight: 700; color: #425c49; }
                .welcome-title { font-size: 16px; font-weight: 600; color: #4f6756; }
                .prompt-chip { background: #f8fbf4; font-size: 12px; padding: 7px 5px; }
                .small-button { font-size: 11px; padding: 5px 8px; min-height: 18px; }
                .chat-bubble-user { background: #f7eae1; border: 1px solid #eddcd0; border-radius: 15px 15px 4px 15px; padding: 12px; }
                .chat-bubble-ai { background: #fffefa; border: 1px solid #dfe8d9; border-radius: 15px 15px 15px 4px; padding: 12px; }
                .bubble-author { font-size: 11px; font-weight: 600; color: #738271; }
                .chat-text { font-size: 13px; color: #39443d; }
                .chat-input-card { background: #fffefb; border: 1px solid #cfddc9; border-radius: 14px; }
                .chat-input { font-size: 13px; padding: 12px; }
            ''')
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        def do_activate(self):
            self.show_window()

        def do_command_line(self, command_line):
            args = command_line.get_arguments()[1:]
            if args and args[0] == '--quit':
                self.quit()
            elif args and args[0] == '--settings':
                self.show_window()
                self.open_settings()
                if len(args) > 1 and args[1] in ('deepseek', 'custom'):
                    self.settings_window.provider.set_selected(1 if args[1] == 'deepseek' else 2)
            elif args and args[0] == '--chat':
                self.show_window()
                self.chat.input.grab_focus()
            elif args and args[0] == '--screenshot':
                self.begin_screenshot()
            elif args and args[0] in ('--selection', '--clipboard'):
                self.read_selection(args[0] == '--clipboard')
            elif args and args[0] == '--text':
                self.show_window()
                self.set_source(' '.join(args[1:]))
                self.begin_translation()
            elif args and args[0] == '--load-draft' and len(args) == 2:
                self.show_window()
                try:
                    draft = json.loads(Path(args[1]).read_text())
                    self.set_source(str(draft.get('source', '')))
                    result = draft.get('result', '')
                    if isinstance(result, str) and result:
                        self.result = result
                        self.output.get_buffer().set_text(result)
                        self.copy_button.set_sensitive(True)
                    self.status.set_text('已恢复原文 · 点击「总结」生成概述与要点')
                except (OSError, ValueError, TypeError):
                    self.status.set_text('无法读取草稿，请重新粘贴原文。')
            else:
                self.show_window()
            return 0

        def show_window(self):
            if self.win is None:
                self.build_window()
            self.win.present()

        def build_window(self):
            self.win = Gtk.ApplicationWindow(application=self, title='轻译 · 小黑陪你读')
            self.win.add_css_class('qingyi-window')
            self.win.set_default_size(1060, 740)
            self.win.connect('close-request', self.on_close)
            header = Gtk.HeaderBar()
            header.set_title_widget(Gtk.Label(label='轻译'))
            self.win.set_titlebar(header)
            shell = Gtk.Box(spacing=20)
            shell.add_css_class('workspace')
            self.win.set_child(shell)
            root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=True)
            root.set_size_request(470, -1)
            shell.append(root)
            top = Gtk.Box(spacing=12)
            mascot = Gtk.Image.new_from_file(str(Path(__file__).resolve().parent / 'assets/black-cat.svg'))
            mascot.set_pixel_size(58)
            top.append(mascot)
            titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
            brand = Gtk.Label(label='轻译', xalign=0)
            brand.add_css_class('brand')
            titles.append(brand)
            titles.append(Gtk.Label(label='慢慢读，轻轻问。', xalign=0, css_classes=['tagline']))
            top.append(titles)
            self.language = Gtk.DropDown.new_from_strings(['简体中文', 'English', '日本語', '한국어'])
            self.language.set_valign(Gtk.Align.CENTER)
            self.language.connect('notify::selected', self.on_language)
            top.append(self.language)
            root.append(top)
            hint = Gtk.Label(label='划词 Alt+Q  ·  复制 Alt+Shift+Q  ·  截图 Alt+S', xalign=0)
            hint.add_css_class('hint')
            root.append(hint)
            self.model_button = Gtk.Button(halign=Gtk.Align.START, css_classes=['model-button'])
            self.model_button.connect('clicked', self.open_settings)
            root.append(self.model_button)
            self.update_model_label()
            self.ai_translation = Gtk.CheckButton(label='翻译也用所选 AI 模型')
            self.ai_translation.set_tooltip_text('勾选后，翻译与截图翻译使用所选模型及其额度；不勾选使用 Google')
            self.ai_translation.connect('toggled', self.translation_engine_changed)
            root.append(self.ai_translation)
            root.append(Gtk.Label(label='原文 · 自动识别语言', xalign=0, css_classes=['section']))
            self.source = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
            self.source.add_css_class('source')
            self.source.set_accepts_tab(False)
            src_scroll = Gtk.ScrolledWindow(min_content_height=95, max_content_height=180)
            src_scroll.add_css_class('card')
            src_scroll.set_child(self.source)
            root.append(src_scroll)
            controls = Gtk.Box(spacing=8)
            self.translate_button = Gtk.Button(label='翻译', css_classes=['suggested-action'])
            self.translate_button.set_tooltip_text('翻译原文 · Ctrl+Enter')
            self.translate_button.connect('clicked', lambda *_: self.begin_translation())
            controls.append(self.translate_button)
            self.summary_button = Gtk.Button(label='总结', css_classes=['suggested-action'])
            self.summary_button.set_tooltip_text('用所选模型生成概述和要点 · Ctrl+Shift+Enter')
            self.summary_button.connect('clicked', lambda *_: self.begin_summary())
            controls.append(self.summary_button)
            self.screenshot_button = Gtk.Button(label='截图翻译')
            self.screenshot_button.set_tooltip_text('框选屏幕文字，自动识别并翻译 · Alt+S · Esc 取消框选')
            self.screenshot_button.connect('clicked', lambda *_: self.begin_screenshot())
            controls.append(self.screenshot_button)
            paste = Gtk.Button(label='粘贴')
            paste.connect('clicked', lambda *_: self.read_selection(True))
            controls.append(paste)
            web = Gtk.Button(label='网页翻译')
            web.connect('clicked', self.open_web)
            controls.append(web)
            self.spinner = Gtk.Spinner(hexpand=True, halign=Gtk.Align.END)
            controls.append(self.spinner)
            root.append(controls)
            out_top = Gtk.Box(spacing=8)
            self.output_title = Gtk.Label(label='译文', xalign=0, hexpand=True, css_classes=['section'])
            out_top.append(self.output_title)
            self.cancel_button = Gtk.Button(label='取消', visible=False)
            self.cancel_button.connect('clicked', self.cancel_job)
            out_top.append(self.cancel_button)
            self.copy_button = Gtk.Button(label='复制译文', sensitive=False)
            self.copy_button.connect('clicked', self.copy_result)
            out_top.append(self.copy_button)
            root.append(out_top)
            self.output = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
            self.output.add_css_class('result')
            out_scroll = Gtk.ScrolledWindow(min_content_height=190, vexpand=True)
            out_scroll.add_css_class('card')
            out_scroll.set_child(self.output)
            root.append(out_scroll)
            self.status = Gtk.Label(label='选中文字后按 Alt+Q，也可以直接输入原文。', xalign=0, wrap=True)
            self.status.add_css_class('notice')
            root.append(self.status)
            privacy = Gtk.Label(label='截图本机识别 · 仅发送文字 · 不保存历史',
                                xalign=0, wrap=True)
            privacy.add_css_class('hint')
            root.append(privacy)
            self.chat = ChatPanel(self)
            shell.append(self.chat)
            keys = Gtk.EventControllerKey()
            keys.connect('key-pressed', self.on_key)
            self.win.add_controller(keys)
            self.source.get_buffer().connect('changed', self.source_edited)

        def source_edited(self, *_):
            if self.cancel_button.get_visible():
                self.cancel_job()
            self.result = ''
            self.output.get_buffer().set_text('')
            self.copy_button.set_sensitive(False)
            self.status.set_text('原文已更新 · 可点击翻译或总结')

        def on_close(self, *_):
            # Keep one small resident process so repeated shortcuts open quickly.
            self.serial += 1
            if self.cancel_event:
                self.cancel_event.set()
            if self.chat and self.chat.busy:
                self.chat.cancel()
            self.win.set_visible(False)
            self.spinner.stop()
            self.translate_button.set_sensitive(True)
            self.summary_button.set_sensitive(True)
            self.cancel_button.set_visible(False)
            return True

        def on_key(self, controller, keyval, keycode, state):
            if keyval in (Gdk.KEY_q, Gdk.KEY_Q) and state & Gdk.ModifierType.CONTROL_MASK and state & Gdk.ModifierType.SHIFT_MASK:
                self.quit()
                return True
            if keyval == Gdk.KEY_Escape:
                self.win.close()
                return True
            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and state & Gdk.ModifierType.CONTROL_MASK:
                if state & Gdk.ModifierType.SHIFT_MASK:
                    self.begin_summary()
                else:
                    self.begin_translation()
                return True
            return False

        def set_source(self, text):
            self.source.get_buffer().set_text(text)

        def source_text(self):
            buf = self.source.get_buffer()
            return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()

        def read_selection(self, use_clipboard):
            self.request_id += 1
            request_id = self.request_id
            if self.reader:
                self.reader.cancel()
            self.reader = Gio.Cancellable()
            reader = self.reader
            # Read BEFORE focusing the popup: selecting elsewhere must survive activation.
            display = Gdk.Display.get_default()
            clipboard = display.get_clipboard() if use_clipboard else display.get_primary_clipboard()
            self.hold()
            completed = False

            def finish(text):
                nonlocal completed
                if completed:
                    return
                completed = True
                try:
                    if request_id != self.request_id:
                        return
                    self.show_window()
                    if text and text.strip():
                        self.set_source(text.strip())
                        self.begin_translation()
                    else:
                        self.status.set_text('没有读到文字。请先选中；若应用不支持划词，请 Ctrl+C 后按 Alt+Shift+Q。')
                finally:
                    self.release()

            def ready(cb, result):
                try:
                    value = cb.read_text_finish(result)
                except GLib.Error:
                    value = None
                finish(value)

            def timeout():
                if not completed:
                    reader.cancel()
                    finish(None)
                return False

            clipboard.read_text_async(reader, ready)
            GLib.timeout_add(2000, timeout)

        def begin_translation(self):
            self.begin_action('translate')

        def begin_summary(self):
            self.begin_action('summary')

        def begin_screenshot(self):
            if self.screenshot_busy:
                return
            if self.win is None:
                self.build_window()
            self.request_id += 1
            if self.reader:
                self.reader.cancel()
            self.cancel_job()
            self.cancel_event = threading.Event()
            cancel = self.cancel_event
            serial = self.serial
            self.screenshot_busy = True
            self.screenshot_button.set_sensitive(False)
            self.status.set_text('拖动框选要翻译的文字 · 松开鼠标开始识别 · Esc 取消')
            # Hide without close-request: the chat and previous draft survive.
            self.win.set_visible(False)
            self.hold()

            def progress():
                GLib.idle_add(self.screenshot_progress, serial)

            def worker():
                text, error, canceled = '', None, False
                try:
                    if cancel.wait(.3):
                        raise Cancelled()
                    text = screenshot_text(cancel, progress)
                except Cancelled:
                    canceled = True
                except Exception as exc:
                    error = str(exc)
                GLib.idle_add(self.screenshot_complete, serial, text, error, canceled)

            thread = threading.Thread(target=worker, daemon=True)
            self.workers = [old for old in self.workers if old.is_alive()]
            self.workers.append(thread)
            thread.start()

        def screenshot_progress(self, serial):
            if serial == self.serial:
                self.show_window()
                self.status.set_text('正在识别截图文字…')
                self.status.remove_css_class('error')
                self.spinner.start()
                self.cancel_button.set_visible(True)
            return False

        def screenshot_complete(self, serial, text, error, canceled):
            self.screenshot_busy = False
            self.screenshot_button.set_sensitive(True)
            self.release()
            if serial != self.serial:
                return False
            self.show_window()
            self.spinner.stop()
            self.cancel_button.set_visible(False)
            if canceled:
                self.status.set_text('已取消截图 · 原文和结果仍保留')
            elif error:
                self.status.set_text(error)
                self.status.add_css_class('error')
            else:
                self.set_source(text)
                if len(text) > MAX_TEXT:
                    self.status.set_text(f'已识别 {len(text)} 字 · 超过翻译上限 {MAX_TEXT} 字，可缩短原文或点击总结')
                else:
                    self.begin_translation()
            return False

        def begin_action(self, mode):
            text = self.source_text()
            use_ai = mode == 'translate' and self.ai_translation.get_active()
            if self.cancel_event:
                self.cancel_event.set()
            self.cancel_event = threading.Event()
            cancel = self.cancel_event
            self.serial += 1
            serial = self.serial
            self.mode = mode
            limit = MAX_SUMMARY_TEXT if mode == 'summary' else MAX_TEXT
            self.output_title.set_text('总结 · 概述与要点' if mode == 'summary' else ('译文 · AI 模型' if use_ai else '译文'))
            self.copy_button.set_label('复制总结' if mode == 'summary' else '复制译文')
            if not text or len(text) > limit:
                self.result = ''
                self.output.get_buffer().set_text('')
                self.copy_button.set_sensitive(False)
                self.status.set_text('请先输入原文。' if not text else f'一次最多{"总结" if mode == "summary" else "翻译"} {limit} 字。')
                self.spinner.stop()
                self.translate_button.set_sensitive(True)
                self.summary_button.set_sensitive(True)
                self.cancel_button.set_visible(False)
                return
            self.result = ''
            self.output.get_buffer().set_text('')
            self.copy_button.set_sensitive(False)
            self.status.remove_css_class('error')
            self.status.set_text('正在总结…' if mode == 'summary' else ('正在用所选模型翻译…' if use_ai else '正在翻译…'))
            self.translate_button.set_sensitive(mode != 'translate')
            self.summary_button.set_sensitive(mode != 'summary')
            self.cancel_button.set_visible(True)
            self.spinner.start()
            target = self.targets[self.language.get_selected()]
            config = self.model_settings.active()

            def worker():
                try:
                    if mode == 'summary' or use_ai:
                        key = '' if config['provider'] == 'codex' else self.model_settings.get_key(config['base_url'])
                        action = summarize if mode == 'summary' else translate_with_model
                        translated = action(text, target, config, key, cancel)
                    else:
                        translated = translate(text, target)
                    error = None
                except Cancelled:
                    return
                except Exception as exc:
                    translated, error = '', str(exc) if mode == 'summary' or use_ai else friendly_error(exc)
                GLib.idle_add(self.complete, serial, translated, error)

            thread = threading.Thread(target=worker, daemon=True)
            self.workers = [old for old in self.workers if old.is_alive()]
            self.workers.append(thread)
            thread.start()

        def complete(self, serial, translated, error):
            if serial != self.serial:
                return False
            self.spinner.stop()
            self.translate_button.set_sensitive(True)
            self.summary_button.set_sensitive(True)
            self.cancel_button.set_visible(False)
            if error:
                self.status.set_text(error)
                self.status.add_css_class('error')
            else:
                self.result = translated
                self.output.get_buffer().set_text(translated)
                self.copy_button.set_sensitive(True)
                self.status.set_text(('总结完成' if self.mode == 'summary' else '翻译完成') + ' · Esc 收起窗口')
            return False

        def on_language(self, *_):
            if self.source_text():
                if self.mode == 'summary' or self.ai_translation.get_active():
                    self.cancel_job()
                    self.result = ''
                    self.output.get_buffer().set_text('')
                    self.copy_button.set_sensitive(False)
                    self.status.set_text('输出语言已切换 · 点击「总结」或「翻译」重新生成')
                else:
                    self.begin_translation()

        def copy_result(self, *_):
            if self.result:
                Gdk.Display.get_default().get_clipboard().set_text(self.result)
                self.status.set_text('总结已复制。' if self.mode == 'summary' else '译文已复制。')

        def translation_engine_changed(self, *_):
            if self.mode == 'translate' and not self.screenshot_busy:
                self.cancel_job()
            self.status.set_text('已切换翻译方式 · 点击「翻译」或重新截图；AI 翻译使用所选模型的额度')

        def cancel_job(self, *_):
            self.serial += 1
            if self.cancel_event:
                self.cancel_event.set()
            self.spinner.stop()
            self.translate_button.set_sensitive(True)
            self.summary_button.set_sensitive(True)
            self.cancel_button.set_visible(False)
            self.status.remove_css_class('error')
            self.status.set_text('已取消，可以修改原文后重新操作。')

        def update_model_label(self):
            config = self.model_settings.active()
            provider = {'codex': 'Codex', 'deepseek': 'DeepSeek', 'custom': '自定义 API'}[config['provider']]
            label = provider if config['provider'] == 'codex' else provider + ' / ' + config['model']
            self.model_button.set_label('AI 模型：' + label + '   ⚙')
            child = self.model_button.get_child()
            child.set_ellipsize(Pango.EllipsizeMode.END)
            child.set_max_width_chars(44)
            self.model_button.set_tooltip_text('模型设置 · ' + label)
            if self.chat:
                self.chat.update_model_label()

        def open_settings(self, *_):
            if self.settings_window is None:
                self.settings_window = ModelWindow(self)
            self.settings_window.present()

        def model_changed(self):
            if self.mode == 'summary' or self.ai_translation.get_active():
                self.cancel_job()
                self.result = ''
                self.output.get_buffer().set_text('')
                self.copy_button.set_sensitive(False)
            if self.chat and self.chat.busy:
                self.chat.cancel()
            self.update_model_label()
            self.status.set_text('模型设置已保存 · 总结和提问将使用新模型')

        def open_web(self, *_):
            text = self.source_text()
            if not text:
                self.status.set_text('请先输入原文。')
                return
            params = urllib.parse.urlencode(dict(sl='auto', tl=self.targets[self.language.get_selected()],
                                               text=text[:MAX_TEXT], op='translate'))
            Gio.AppInfo.launch_default_for_uri('https://translate.google.com/?' + params, None)

    return Qingyi().run(sys.argv)


if __name__ == '__main__':
    raise SystemExit(main())
