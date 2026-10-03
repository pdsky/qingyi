"""An in-memory reading companion using the user's selected AI provider."""
import threading
from pathlib import Path
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, Gdk, GLib, Pango
from ai_backend import chat, make_chat_message, Cancelled, MAX_SUMMARY_TEXT


class ChatPanel(Gtk.Box):
    def __init__(self, owner):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.owner = owner
        self.add_css_class('chat-pane')
        self.set_size_request(340, -1)
        self.history = []
        self.serial = 0
        self.cancel_event = None
        self.pending = None
        self.busy = False
        top = Gtk.Box(spacing=8)
        avatar = Gtk.Image.new_from_file(str(Path(__file__).resolve().parent / 'assets/black-cat.svg'))
        avatar.set_pixel_size(28)
        top.append(avatar)
        top.append(Gtk.Label(label='小黑陪你读', xalign=0, hexpand=True, css_classes=['chat-title']))
        clear = Gtk.Button(label='新对话', css_classes=['small-button'])
        clear.connect('clicked', self.clear)
        top.append(clear)
        self.append(top)
        self.model_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=32,
                                     css_classes=['hint'])
        self.append(self.model_label)
        self.scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=220,
                                         hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.messages = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.messages.set_margin_end(3)
        self.scroll.set_child(self.messages)
        self.append(self.scroll)
        self.welcome = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                               margin_top=32, margin_bottom=24)
        mascot = Gtk.Image.new_from_file(str(Path(__file__).resolve().parent / 'assets/black-cat.svg'))
        mascot.set_pixel_size(76)
        self.welcome.append(mascot)
        self.welcome.append(Gtk.Label(label='哪里不懂，问问我呀', css_classes=['welcome-title']))
        self.welcome.append(Gtk.Label(label='解释术语、拆解句子，\n也可以聊聊新的问题。',
                                      justify=Gtk.Justification.CENTER, css_classes=['hint']))
        self.messages.append(self.welcome)
        suggestions = Gtk.Box(spacing=6, homogeneous=True)
        for label, prompt in [('解释术语', '请解释原文中的专业术语，尽量用通俗的语言。'),
                              ('举个例子', '请用一个具体例子解释这段内容。'),
                              ('更简单点', '请用更简单的语言解释，适合初学者理解。')]:
            button = Gtk.Button(label=label, css_classes=['prompt-chip'])
            button.connect('clicked', lambda _, value=prompt: self.set_question(value))
            suggestions.append(button)
        self.append(suggestions)
        self.attach_source = Gtk.CheckButton(label='带上左边原文', active=True)
        self.attach_source.set_tooltip_text('发送问题时附带当前原文；取消勾选后只发送问题和最近的对话。')
        self.append(self.attach_source)
        self.input = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, accepts_tab=False,
                                  css_classes=['chat-input'])
        self.input.set_tooltip_text('写下问题，Ctrl+Enter 发送；Enter 换行。')
        input_scroll = Gtk.ScrolledWindow(min_content_height=82, max_content_height=130)
        input_scroll.add_css_class('chat-input-card')
        input_scroll.set_child(self.input)
        self.append(input_scroll)
        input_hint = Gtk.Label(label='写下你的问题 · Ctrl+Enter 发送', xalign=0, css_classes=['hint'])
        self.append(input_hint)
        controls = Gtk.Box(spacing=8)
        self.spinner = Gtk.Spinner()
        controls.append(self.spinner)
        self.status = Gtk.Label(label='准备好听你的问题啦', xalign=0, hexpand=True,
                                wrap=True, max_width_chars=23, css_classes=['notice'])
        controls.append(self.status)
        self.cancel_button = Gtk.Button(label='取消', visible=False)
        self.cancel_button.connect('clicked', self.cancel)
        controls.append(self.cancel_button)
        self.send_button = Gtk.Button(label='发送  ↗', css_classes=['suggested-action'])
        self.send_button.connect('clicked', self.send)
        controls.append(self.send_button)
        self.append(controls)
        self.append(Gtk.Label(label='发送时携带最近对话 · 使用所选模型的额度', xalign=0,
                               wrap=True, css_classes=['hint']))
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect('key-pressed', self.on_key)
        self.input.add_controller(keys)
        self.update_model_label()

    def question_text(self):
        buf = self.input.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()

    def set_question(self, value):
        if not self.busy:
            self.input.get_buffer().set_text(value)
            self.input.grab_focus()

    def on_key(self, controller, keyval, keycode, state):
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and state & Gdk.ModifierType.CONTROL_MASK:
            self.send()
            return True
        return False

    def model_name(self):
        config = self.owner.model_settings.active()
        provider = {'codex': 'Codex', 'deepseek': 'DeepSeek', 'custom': '自定义 API'}[config['provider']]
        return provider if config['provider'] == 'codex' else provider + ' / ' + config['model']

    def update_model_label(self):
        name = self.model_name()
        self.model_label.set_text('回答模型：' + name)
        self.model_label.set_tooltip_text(name)

    def bubble(self, role, text, model=''):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.add_css_class('chat-bubble-user' if role == 'user' else 'chat-bubble-ai')
        box.set_margin_start(22 if role == 'user' else 0)
        box.set_margin_end(0 if role == 'user' else 12)
        name = Gtk.Label(label='你' if role == 'user' else '小黑 · ' + model, xalign=0,
                          ellipsize=Pango.EllipsizeMode.END, max_width_chars=30, css_classes=['bubble-author'])
        if model:
            name.set_tooltip_text(model)
        box.append(name)
        body = Gtk.Label(label=text, xalign=0, wrap=True, selectable=True,
                          wrap_mode=Pango.WrapMode.WORD_CHAR, max_width_chars=35, css_classes=['chat-text'])
        box.append(body)
        self.messages.append(box)
        self.welcome.set_visible(False)
        GLib.timeout_add(80, self.scroll_bottom)
        return box, body

    def scroll_bottom(self):
        adjustment = self.scroll.get_vadjustment()
        adjustment.set_value(max(adjustment.get_lower(), adjustment.get_upper() - adjustment.get_page_size()))
        return False

    def set_busy(self, busy):
        self.busy = busy
        self.input.set_editable(not busy)
        self.send_button.set_sensitive(not busy)
        self.cancel_button.set_visible(busy)
        if busy:
            self.spinner.start()
        else:
            self.spinner.stop()

    def send(self, *_):
        if self.busy:
            return
        question = self.question_text()
        source = self.owner.source_text() if self.attach_source.get_active() else ''
        if not question:
            self.status.set_text('先写下你的问题吧')
            self.input.grab_focus()
            return
        if len(question) > 5000 or len(source) > MAX_SUMMARY_TEXT:
            self.status.set_text('问题最多 5000 字；附带原文最多 30000 字。')
            return
        self.serial += 1
        serial = self.serial
        self.cancel_event = threading.Event()
        cancel = self.cancel_event
        config = self.owner.model_settings.active()
        target = self.owner.targets[self.owner.language.get_selected()]
        history = list(self.history)
        self.bubble('user', question)
        box, body = self.bubble('assistant', '小黑正在想一想…', self.model_name())
        self.pending = body
        self.set_busy(True)
        self.status.set_text('正在思考，稍等一下')

        def worker():
            try:
                key = '' if config['provider'] == 'codex' else self.owner.model_settings.get_key(config['base_url'])
                result, error = chat(question, source, target, config, key, cancel, history), None
            except Cancelled:
                return
            except Exception as exc:
                result, error = '', str(exc)
            GLib.idle_add(done, result, error)

        def done(result, error):
            if serial != self.serial:
                return False
            self.set_busy(False)
            self.pending = None
            if error:
                body.set_text(error)
                body.add_css_class('error')
                self.status.set_text('这次没连上，可以再试试')
            else:
                body.set_text(result)
                copy = Gtk.Button(label='复制回答', halign=Gtk.Align.START, css_classes=['small-button'])
                copy.connect('clicked', lambda *_: Gdk.Display.get_default().get_clipboard().set_text(result))
                box.append(copy)
                self.history += [make_chat_message(question, source), {'role': 'assistant', 'content': result}]
                self.history = self.history[-16:]
                self.input.get_buffer().set_text('')
                self.status.set_text('可以继续追问哦')
                self.input.grab_focus()
            GLib.timeout_add(80, self.scroll_bottom)
            return False

        thread = threading.Thread(target=worker, daemon=True)
        self.owner.workers = [old for old in self.owner.workers if old.is_alive()]
        self.owner.workers.append(thread)
        thread.start()

    def cancel(self, *_):
        self.serial += 1
        if self.cancel_event:
            self.cancel_event.set()
        if self.pending is not None:
            self.pending.set_text('这次提问已取消。')
            self.pending = None
        self.set_busy(False)
        self.status.set_text('已取消，可以修改问题再问')

    def clear(self, *_):
        self.cancel()
        self.history.clear()
        child = self.messages.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            if child is not self.welcome:
                self.messages.remove(child)
            child = following
        self.welcome.set_visible(True)
        self.input.get_buffer().set_text('')
        self.status.set_text('开启新的小对话啦')
