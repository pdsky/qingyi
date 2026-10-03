"""GTK model selector: Codex login or a user-configured compatible API."""
import threading
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import Gtk, GLib
from ai_backend import DEFAULT_PROFILES, fetch_models, secret_api


class ModelWindow(Gtk.Window):
    def __init__(self, owner):
        super().__init__(title='轻译 · 模型设置', transient_for=owner.win, modal=True)
        self.owner = owner
        self.add_css_class('qingyi-window')
        self.set_default_size(510, 460)
        self.set_resizable(False)
        self.generation = 0
        self.filling = False
        self.current = None
        self.models = []
        self.drafts = {name: dict(value) for name, value in owner.model_settings.data['profiles'].items()}
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                       margin_top=20, margin_bottom=20, margin_start=22, margin_end=22)
        self.set_child(root)
        title = Gtk.Label(label='给小黑挑一个模型', xalign=0, css_classes=['chat-title'])
        root.append(title)
        self.provider = Gtk.DropDown.new_from_strings(['Codex（现有登录）', 'DeepSeek', '自定义 API（兼容 OpenAI）'])
        root.append(self.provider)
        self.form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=9)
        root.append(self.form)
        self.form.append(Gtk.Label(label='API 地址', xalign=0, css_classes=['section']))
        self.base_url = Gtk.Entry(placeholder_text='https://api.deepseek.com 或 https://服务商地址/v1')
        self.form.append(self.base_url)
        self.form.append(Gtk.Label(label='API Key', xalign=0, css_classes=['section']))
        self.api_key = Gtk.PasswordEntry(show_peek_icon=True, placeholder_text='输入服务商提供的 API Key')
        self.form.append(self.api_key)
        self.remember = Gtk.CheckButton(label='保存 API Key 到系统钥匙串')
        available = secret_api()[0] is not None
        self.remember.set_sensitive(available)
        self.remember.set_active(available)
        self.form.append(self.remember)
        self.form.append(Gtk.Label(label='模型 ID · 可手动输入', xalign=0, css_classes=['section']))
        row = Gtk.Box(spacing=8)
        self.model = Gtk.Entry(hexpand=True, placeholder_text='先获取模型，或填入服务商提供的模型 ID')
        row.append(self.model)
        self.fetch_button = Gtk.Button(label='获取模型')
        self.fetch_button.connect('clicked', self.fetch)
        row.append(self.fetch_button)
        self.form.append(row)
        self.model_list = Gtk.DropDown.new_from_strings([])
        self.model_list.set_visible(False)
        self.model_list.connect('notify::selected', self.choose_model)
        self.form.append(self.model_list)
        self.note = Gtk.Label(xalign=0, wrap=True, max_width_chars=56, css_classes=['hint'])
        root.append(self.note)
        self.status = Gtk.Label(label='', xalign=0, wrap=True, max_width_chars=56, css_classes=['notice'])
        root.append(self.status)
        controls = Gtk.Box(spacing=8, halign=Gtk.Align.END)
        cancel = Gtk.Button(label='取消')
        cancel.connect('clicked', lambda *_: self.close())
        controls.append(cancel)
        self.save_button = Gtk.Button(label='保存并使用', css_classes=['suggested-action'])
        self.save_button.connect('clicked', self.save)
        controls.append(self.save_button)
        root.append(controls)
        self.provider.connect('notify::selected', self.change_provider)
        self.base_url.connect('changed', self.endpoint_changed)
        self.api_key.connect('changed', self.key_changed)
        self.connect('close-request', self.on_close)
        self.provider.set_selected(['codex', 'deepseek', 'custom'].index(owner.model_settings.data['provider']))
        if self.current is None:
            self.change_provider()

    def on_close(self, *_):
        self.generation += 1
        self.owner.settings_window = None
        return False

    def key_changed(self, *_):
        if not self.filling:
            self.generation += 1
            self.fetch_button.set_sensitive(True)

    def endpoint_changed(self, *_):
        if not self.filling:
            self.generation += 1
            self.api_key.set_text('')
            self.model_list.set_visible(False)
            self.fetch_button.set_sensitive(True)

    def change_provider(self, *_):
        if self.current and self.current != 'codex':
            self.drafts[self.current] = {'base_url': self.base_url.get_text(), 'model': self.model.get_text(),
                                         'remember': self.remember.get_active()}
        self.current = ['codex', 'deepseek', 'custom'][self.provider.get_selected()]
        self.generation += 1
        generation = self.generation
        self.status.set_text('')
        self.fetch_button.set_sensitive(True)
        self.model_list.set_visible(False)
        self.form.set_visible(self.current != 'codex')
        if self.current == 'codex':
            self.note.set_text('使用电脑上已登录的 Codex，不需要 API Key。总结与聊天会使用 Codex 账号额度。')
            return
        profile = self.drafts.get(self.current, DEFAULT_PROFILES[self.current])
        self.filling = True
        self.base_url.set_text(profile['base_url'])
        self.model.set_text(profile['model'])
        self.remember.set_active(self.remember.get_sensitive() and profile.get('remember', True))
        self.api_key.set_text('')
        self.filling = False
        self.note.set_text('总结与聊天会发送给此地址对应的服务商，并按该服务商的规则计费。填写 Key 后获取模型，也可以手动输入模型 ID。')
        base = profile['base_url']
        if not base:
            return

        def load():
            try:
                key = self.owner.model_settings.get_key(base)
            except Exception:
                key = ''
            GLib.idle_add(loaded, key)

        def loaded(key):
            if generation == self.generation and self.get_visible():
                self.filling = True
                self.api_key.set_text(key)
                self.filling = False
            return False

        threading.Thread(target=load, daemon=True).start()

    def choose_model(self, *_):
        index = self.model_list.get_selected()
        if index < len(self.models):
            self.model.set_text(self.models[index])

    def fetch(self, *_):
        self.generation += 1
        generation = self.generation
        base, key = self.base_url.get_text(), self.api_key.get_text()
        self.fetch_button.set_sensitive(False)
        self.status.set_text('正在获取此账号可用的模型…')

        def worker():
            try:
                models, error = fetch_models(base, key), None
            except Exception as exc:
                models, error = [], str(exc)
            GLib.idle_add(done, models, error)

        def done(models, error):
            if generation != self.generation or not self.get_visible():
                return False
            self.fetch_button.set_sensitive(True)
            if error:
                self.status.set_text(error)
            else:
                previous = self.model.get_text()
                self.models = models
                self.model_list.set_model(Gtk.StringList.new(models))
                self.model_list.set_selected(models.index(previous) if previous in models else 0)
                self.choose_model()
                self.model_list.set_visible(True)
                self.status.set_text(f'获取成功，共 {len(models)} 个模型。请选择一个。')
            return False

        threading.Thread(target=worker, daemon=True).start()

    def save(self, *_):
        values = (self.current, self.base_url.get_text(), self.model.get_text(), self.api_key.get_text(), self.remember.get_active())
        self.save_button.set_sensitive(False)
        self.provider.set_sensitive(False)
        self.form.set_sensitive(False)
        self.status.set_text('正在保存设置…')

        def worker():
            try:
                self.owner.model_settings.save(*values)
                error = None
            except Exception as exc:
                error = str(exc)
            GLib.idle_add(done, error)

        def done(error):
            if error:
                if self.get_visible():
                    self.save_button.set_sensitive(True)
                    self.provider.set_sensitive(True)
                    self.form.set_sensitive(True)
                    self.status.set_text(error)
            else:
                self.owner.model_changed()
                self.close()
            return False

        threading.Thread(target=worker, daemon=True).start()
