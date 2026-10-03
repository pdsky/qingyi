"""Translate a captured image in place, without changing the editor's draft."""
import statistics
import threading

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('GdkPixbuf', '2.0')
gi.require_version('Pango', '1.0')
from gi.repository import Gtk, Gdk, GdkPixbuf, Gio, GLib, Pango, Gsk, Graphene

from ai_backend import Cancelled, translate_image_regions
from screenshot import recognize_regions


class ImageLayer(Gtk.Widget):
    def __init__(self, texture, width, height):
        super().__init__()
        self.texture = texture
        self.width, self.height = width, height

    def do_measure(self, orientation, for_size):
        size = self.width if orientation == Gtk.Orientation.HORIZONTAL else self.height
        return size, size, -1, -1

    def do_snapshot(self, snapshot):
        snapshot.append_texture(self.texture, Graphene.Rect().init(0, 0, self.width, self.height))


class ScreenshotWindow(Gtk.ApplicationWindow):
    def __init__(self, owner, image):
        super().__init__(application=owner, title='轻译 · 截图画面翻译')
        self.owner = owner
        self.image = image
        self.texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(image))
        self.width, self.height = self.texture.get_width(), self.texture.get_height()
        if self.width * self.height > 32000000 or max(self.width, self.height) > 16000:
            self.destroy()
            raise ValueError('截图太大，请缩小框选区域。')
        loader = GdkPixbuf.PixbufLoader.new_with_type('png')
        loader.write(image)
        loader.close()
        self.pixels = loader.get_pixbuf()
        self.pixel_data = self.pixels.get_pixels()
        self.regions = []
        self.translations = []
        self.covers = []
        self.serial = 0
        self.cancel_event = None
        self.closed = False
        self.busy = False
        self.zoom = min(1., 900 / self.width, 540 / self.height)
        self.add_css_class('qingyi-window')
        self.set_default_size(max(560, min(1000, self.width + 40)), min(780, max(420, self.height + 190)))
        header = Gtk.HeaderBar()
        header.set_title_widget(Gtk.Label(label='小黑 · 画面翻译'))
        self.set_titlebar(header)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root.set_margin_start(14)
        root.set_margin_end(14)
        root.set_margin_top(12)
        root.set_margin_bottom(12)
        self.set_child(root)
        row = Gtk.Box(spacing=8)
        self.show_translation = Gtk.CheckButton(label='显示译文', active=True)
        self.show_translation.connect('toggled', self.toggle_original)
        row.append(self.show_translation)
        for label, action in [('−', lambda: self.set_zoom(self.zoom / 1.25)),
                              ('+', lambda: self.set_zoom(self.zoom * 1.25)),
                              ('适应窗口', self.fit)]:
            button = Gtk.Button(label=label)
            button.connect('clicked', lambda _, callback=action: callback())
            row.append(button)
        self.zoom_label = Gtk.Label(hexpand=True, xalign=0)
        row.append(self.zoom_label)
        self.save_button = Gtk.Button(label='保存画面')
        self.save_button.set_tooltip_text('保存当前显示的画面为 PNG · Ctrl+S')
        self.save_button.connect('clicked', self.save)
        row.append(self.save_button)
        root.append(row)
        row = Gtk.Box(spacing=8)
        use_ai = owner.ai_translation.get_active() if owner.screenshot_use_ai is None else owner.screenshot_use_ai
        self.use_ai = Gtk.CheckButton(label='用 AI 模型翻译', active=use_ai)
        self.use_ai.set_tooltip_text('勾选后使用所选模型及其额度；不勾选使用 Google')
        self.use_ai.connect('toggled', self.options_changed)
        row.append(self.use_ai)
        self.language = Gtk.DropDown.new_from_strings(['简体中文', 'English', '日本語', '한국어'])
        self.language.set_selected(owner.language.get_selected())
        self.language.connect('notify::selected', self.options_changed)
        row.append(self.language)
        self.translate_button = Gtk.Button(label='翻译画面', css_classes=['suggested-action'])
        self.translate_button.connect('clicked', self.begin)
        row.append(self.translate_button)
        self.cancel_button = Gtk.Button(label='取消', visible=False)
        self.cancel_button.connect('clicked', self.cancel)
        row.append(self.cancel_button)
        self.spinner = Gtk.Spinner(hexpand=True, halign=Gtk.Align.END)
        row.append(self.spinner)
        root.append(row)
        self.model_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=['hint'])
        root.append(self.model_label)
        self.scene = Gtk.Fixed()
        self.scene.set_overflow(Gtk.Overflow.HIDDEN)
        scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroll.add_css_class('card')
        scroll.set_child(self.scene)
        root.append(scroll)
        self.scroll = scroll
        self.status = Gtk.Label(label='译文会覆盖在原来的文字位置', wrap=True, xalign=0, css_classes=['notice'])
        root.append(self.status)
        root.append(Gtk.Label(label='取消勾选「显示译文」可看原图 · 悬停看完整译文 · Esc 关闭',
                              xalign=0, wrap=True, css_classes=['hint']))
        keys = Gtk.EventControllerKey()
        keys.connect('key-pressed', self.on_key)
        self.add_controller(keys)
        self.connect('close-request', self.on_close)
        self.update_model_label()
        self.rebuild_scene()

    def update_model_label(self):
        config = self.owner.model_settings.active()
        provider = {'codex': 'Codex', 'deepseek': 'DeepSeek', 'custom': '自定义 API'}[config['provider']]
        self.model_label.set_text('AI：' + provider + (' / ' + config.get('model', '') if config['provider'] != 'codex' else '') + ' · 图片留在本机，仅发送文字')

    def background(self, region):
        colors = []
        stride, channels = self.pixels.get_rowstride(), self.pixels.get_n_channels()
        for fraction in (.05, .25, .5, .75, .95):
            x = min(self.width - 1, max(0, int(region.x + region.width * fraction)))
            for y in (max(0, region.y - 3), min(self.height - 1, region.y + region.height + 3)):
                start = y * stride + x * channels
                colors.append(tuple(self.pixel_data[start:start + 3]))
        return tuple(int(statistics.median(c[i] for c in colors)) for i in range(3))

    def font_size(self, text, width, height, original_lines):
        layout = self.create_pango_layout(text)
        layout.set_width(max(1, int((width - 4) * Pango.SCALE)))
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        font = Pango.FontDescription.from_string('Sans')
        size = min(36., max(6., height / max(1, original_lines) * .82))
        while size > 5:
            font.set_absolute_size(size * Pango.SCALE)
            layout.set_font_description(font)
            measured_w, measured_h = layout.get_pixel_size()
            if measured_w <= width - 2 and measured_h <= height - 2:
                break
            size -= .5
        return max(5., size)

    def rebuild_scene(self):
        child = self.scene.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.scene.remove(child)
            child = next_child
        self.covers = []
        width, height = max(1, round(self.width * self.zoom)), max(1, round(self.height * self.zoom))
        self.scene.set_size_request(width, height)
        picture = ImageLayer(self.texture, width, height)
        self.scene.put(picture, 0, 0)
        for region, text in zip(self.regions, self.translations):
            x, y = max(0, region.x - 2), max(0, region.y - 2)
            w = min(self.width - x, region.width + 4)
            h = min(self.height - y, region.height + 4)
            color = self.background(region)
            foreground = '#25362b' if sum(color) > 360 else '#ffffff'
            cover = Gtk.Box()
            cover.set_overflow(Gtk.Overflow.HIDDEN)
            cover.set_size_request(max(1, round(w * self.zoom)), max(1, round(h * self.zoom)))
            css = Gtk.CssProvider()
            css.load_from_data(('box { background: rgb(%d,%d,%d); color: %s; }' % (*color, foreground)).encode())
            cover.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            label = Gtk.Label(label=text, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, xalign=0, yalign=0)
            label.set_margin_start(1)
            label.set_margin_end(1)
            label.set_tooltip_text(text)
            label.set_hexpand(True)
            size = self.font_size(text, w, h, region.text.count('\n') + 1)
            attributes = Pango.AttrList()
            attributes.insert(Pango.attr_size_new_absolute(int(size * self.zoom * Pango.SCALE)))
            label.set_attributes(attributes)
            cover.append(label)
            cover.set_visible(self.show_translation.get_active())
            self.scene.put(cover, x * self.zoom, y * self.zoom)
            self.covers.append(cover)
        self.zoom_label.set_text(f'{self.zoom:.0%}')

    def set_zoom(self, zoom):
        self.zoom = min(3., max(.1, zoom))
        self.rebuild_scene()

    def fit(self):
        width = self.scroll.get_width() or 900
        height = self.scroll.get_height() or 540
        self.set_zoom(min(1., (width - 4) / self.width, (height - 4) / self.height))

    def toggle_original(self, *_):
        for cover in self.covers:
            cover.set_visible(self.show_translation.get_active())

    def options_changed(self, *_):
        self.owner.screenshot_use_ai = self.use_ai.get_active()
        self.cancel()
        self.status.set_text('翻译方式已切换 · 点击「翻译画面」；AI 翻译使用所选模型的额度')

    def cancel(self, *_):
        self.serial += 1
        if self.cancel_event:
            self.cancel_event.set()
        self.busy = False
        self.spinner.stop()
        self.cancel_button.set_visible(False)
        self.translate_button.set_sensitive(True)
        self.status.set_text('已取消 · 原图和已有译文保留')

    def begin(self, *_):
        self.cancel()
        serial = self.serial
        self.cancel_event = threading.Event()
        cancel = self.cancel_event
        self.busy = True
        self.translate_button.set_sensitive(False)
        self.cancel_button.set_visible(True)
        self.spinner.start()
        self.status.remove_css_class('error')
        self.status.set_text('正在翻译画面…' if self.regions else '正在定位画面中的文字…')
        self.update_model_label()
        use_ai = self.use_ai.get_active()
        config = self.owner.model_settings.active()
        target = self.owner.targets[self.language.get_selected()]
        cached, image = list(self.regions), self.image
        self.owner.hold()

        def worker():
            regions, translations, error, canceled = cached, [], None, False
            try:
                regions = cached or recognize_regions(image, self.width, self.height, cancel)
                if use_ai:
                    key = '' if config['provider'] == 'codex' else self.owner.model_settings.get_key(config['base_url'])
                    translations = translate_image_regions([r.text for r in regions], target, config, key, cancel)
                else:
                    from qingyi import translate, friendly_error
                    for region in regions:
                        if cancel.is_set():
                            raise Cancelled()
                        try:
                            translations.append(translate(region.text, target))
                        except Exception as exc:
                            raise ValueError(friendly_error(exc).replace('翻译也用所选 AI 模型', '用 AI 模型翻译')) from None
                if cancel.is_set():
                    raise Cancelled()
            except Cancelled:
                canceled = True
            except Exception as exc:
                error = str(exc)
            GLib.idle_add(self.complete, serial, regions, translations, error, canceled)

        thread = threading.Thread(target=worker, daemon=True)
        self.owner.workers = [t for t in self.owner.workers if t.is_alive()]
        self.owner.workers.append(thread)
        thread.start()

    def complete(self, serial, regions, translations, error, canceled):
        self.owner.release()
        if self.closed or serial != self.serial:
            return False
        self.busy = False
        self.spinner.stop()
        self.cancel_button.set_visible(False)
        self.translate_button.set_sensitive(True)
        self.regions = regions
        if canceled:
            self.status.set_text('已取消 · 原图和已有译文保留')
        elif error:
            self.status.set_text(error)
            self.status.add_css_class('error')
        else:
            self.translations = translations
            self.show_translation.set_active(True)
            self.rebuild_scene()
            self.translate_button.set_label('重新翻译')
            self.status.set_text('画面翻译完成 · 可切换原图或保存当前画面')
        return False

    def export_png(self, path):
        snapshot = Gtk.Snapshot()
        snapshot.scale(1 / self.zoom, 1 / self.zoom)
        Gtk.WidgetPaintable.new(self.scene).snapshot(snapshot, self.scene.get_width(), self.scene.get_height())
        node = snapshot.to_node()
        if node is None:
            raise ValueError('画面尚未显示完成，请稍后再保存。')
        renderer = Gsk.Renderer.new_for_surface(self.get_surface())
        try:
            bounds = Graphene.Rect().init(0, 0, self.width, self.height)
            if not renderer.render_texture(node, bounds).save_to_png(str(path)):
                raise ValueError('无法保存图片，请换一个文件夹。')
        finally:
            renderer.unrealize()

    def save(self, *_):
        dialog = Gtk.FileDialog(title='保存截图画面', initial_name='轻译-截图.png')
        def saved(dialog, result):
            try:
                path = dialog.save_finish(result).get_path()
                if not path:
                    raise ValueError('请选择本机文件夹。')
                self.export_png(path)
                self.status.set_text('当前画面已保存')
            except GLib.Error as exc:
                if not exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED):
                    self.status.set_text('保存失败，请重新选择文件夹。')
            except Exception as exc:
                self.status.set_text(str(exc))
        dialog.save(self, None, saved)

    def on_key(self, _, keyval, __, state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        if keyval in (Gdk.KEY_s, Gdk.KEY_S) and state & Gdk.ModifierType.CONTROL_MASK:
            self.save()
            return True
        return False

    def on_close(self, *_):
        self.cancel()
        self.closed = True
        self.image = b''
        self.pixel_data = b''
        if self.owner.screenshot_window is self:
            self.owner.screenshot_window = None
        return False
