import io
import threading
import time
import weakref
from urllib.parse import urljoin

from PyQt6.QtCore import QByteArray, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QAction, QBrush, QColor, QDesktopServices, QFont, QImage,
    QPalette, QTextCharFormat, QTextCursor, QTextDocument, QTextFormat,
    QTextFrameFormat, QTextLength, QTextOption, QTextTable,
)
from PyQt6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton,
    QSizePolicy, QTextBrowser, QToolButton, QVBoxLayout, QWidget,
)


def web_url(value):
    url = QUrl(value) if isinstance(value, (str, QUrl)) else QUrl()
    return (url if url.isValid() and url.scheme().lower() in ('https', 'http')
            and url.host() and not url.userInfo() else QUrl())


def _download_image(url, cancelled, max_bytes, max_pixels):
    import requests
    from PIL import Image, ImageOps, UnidentifiedImageError

    deadline = time.monotonic() + 20
    data = bytearray()
    with requests.Session() as session:
        session.trust_env = False
        for redirect_count in range(6):
            if cancelled.is_set() or time.monotonic() >= deadline:
                return None
            session.cookies.clear()
            with session.get(url, stream=True, allow_redirects=False, timeout=(4, 4)) as response:
                if response.is_redirect:
                    target = urljoin(url, response.headers.get('Location', ''))
                    if (redirect_count == 5 or not web_url(target).isValid()
                            or (QUrl(url).scheme() == 'https' and QUrl(target).scheme() != 'https')):
                        return None
                    url = target
                    continue
                response.raise_for_status()
                if int(response.headers.get('Content-Length', '0') or 0) > max_bytes:
                    return None
                read = getattr(response.raw, 'read1', response.raw.read)
                while not cancelled.is_set() and time.monotonic() < deadline:
                    chunk = read(16 * 1024, decode_content=True)
                    if not chunk:
                        break
                    if len(data) + len(chunk) > max_bytes:
                        return None
                    data.extend(chunk)
                break
    if cancelled.is_set() or time.monotonic() >= deadline:
        return None
    try:
        source = Image.open(io.BytesIO(data), formats=['PNG', 'JPEG', 'GIF', 'WEBP', 'BMP'])
    except UnidentifiedImageError:
        return None
    with source:
        if source.width * source.height > max_pixels:
            return None
        source.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        rgba = ImageOps.exif_transpose(source).convert('RGBA')
        pixels = rgba.tobytes()
        return QImage(pixels, rgba.width, rgba.height, rgba.width * 4,
                      QImage.Format.Format_RGBA8888).copy()


class ReleaseNotesBrowser(QTextBrowser):
    imageStatusChanged = pyqtSignal(str)
    _imageCompleted = pyqtSignal(str, object)
    MAX_IMAGE_BYTES = 8 * 1024 * 1024
    MAX_IMAGE_PIXELS = 16_000_000
    MAX_IMAGES = 16

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenLinks(False)
        self.setOpenExternalLinks(False)
        self.anchorClicked.connect(self._open_link)
        self.setAccessibleName('更新日志')
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        option = self.document().defaultTextOption()
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.document().setDefaultTextOption(option)
        self.document().setDocumentMargin(24)
        self._images = {}
        self._pending = {}
        self._failed = set()
        self._closed = False
        self._cancelled = threading.Event()
        self._imageCompleted.connect(self._image_ready)
        self._placeholder = QImage(24, 24, QImage.Format.Format_ARGB32_Premultiplied)
        self._placeholder.fill(QColor('#8b95a5'))

    def set_notes(self, html, markdown):
        self.document().setDefaultStyleSheet('p { white-space: pre-wrap; }')
        if isinstance(html, str) and html.strip():
            self.setHtml(html)
        elif isinstance(markdown, str) and markdown.strip():
            self.document().setMarkdown(markdown, QTextDocument.MarkdownFeature.MarkdownDialectGitHub)
        else:
            self.setPlainText('此版本暂未提供更新日志。你仍可前往发布页面查看并下载更新。')
        self.style_document()
        self.moveCursor(QTextCursor.MoveOperation.Start)
        self.verticalScrollBar().setValue(0)
        self.horizontalScrollBar().setValue(0)

    def style_document(self):
        palette = self.palette()
        text = palette.color(QPalette.ColorRole.Text)
        border = palette.color(QPalette.ColorRole.Mid)
        alternate = palette.color(QPalette.ColorRole.AlternateBase)
        link = palette.color(QPalette.ColorRole.Link)
        document = self.document()
        font = self.font()
        font.setPointSizeF(max(10.5, font.pointSizeF()))
        document.setDefaultFont(font)
        root_format = document.rootFrame().frameFormat()
        root_format.clearBackground()
        document.rootFrame().setFrameFormat(root_format)
        block = document.begin()
        while block.isValid():
            cursor = QTextCursor(block)
            fmt = block.blockFormat()
            fmt.clearBackground()
            level = fmt.headingLevel()
            fmt.setTopMargin(20 if level else 5)
            fmt.setBottomMargin(10 if level else 7)
            fmt.setLineHeight(145, 1)
            cursor.setBlockFormat(fmt)
            if level:
                cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
                char = QTextCharFormat()
                char.setFontPointSize((22, 17, 14, 12, 11, 10.5)[min(level, 6) - 1])
                char.setFontWeight(QFont.Weight.DemiBold)
                cursor.mergeCharFormat(char)
            iterator = block.begin()
            while not iterator.atEnd():
                fragment = iterator.fragment()
                if fragment.isValid():
                    char = fragment.charFormat()
                    cursor.setPosition(fragment.position())
                    cursor.setPosition(fragment.position() + fragment.length(), QTextCursor.MoveMode.KeepAnchor)
                    char.clearBackground()
                    char.setForeground(link if char.isAnchor() else text)
                    if char.isImageFormat():
                        char = char.toImageFormat()
                        char.setMaximumWidth(QTextLength(QTextLength.Type.PercentageLength, 100))
                        char.clearProperty(QTextFormat.Property.ImageHeight)
                        fmt.setLineHeight(100, 1)
                        cursor.setBlockFormat(fmt)
                    cursor.setCharFormat(char)
                iterator += 1
            block = block.next()

        def style_tables(frame):
            for child in frame.childFrames():
                if isinstance(child, QTextTable):
                    fmt = child.format()
                    fmt.setBorder(1)
                    fmt.setBorderBrush(QBrush(border))
                    fmt.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
                    fmt.setBorderCollapse(True)
                    fmt.setCellPadding(9)
                    if child.columns() > 4:
                        fmt.setWidth(QTextLength(QTextLength.Type.FixedLength, child.columns() * 120))
                    child.setFormat(fmt)
                    for row in range(child.rows()):
                        for column in range(child.columns()):
                            cell = child.cellAt(row, column)
                            cell_fmt = cell.format()
                            cell_fmt.setBackground(QBrush(alternate if row % 2 == 0 else palette.base()))
                            cell.setFormat(cell_fmt)
                style_tables(child)
        style_tables(document.rootFrame())

    def _open_link(self, url):
        if url.isRelative() and not url.path() and url.hasFragment():
            self.scrollToAnchor(url.fragment())
        elif web_url(url).isValid():
            if not QDesktopServices.openUrl(url):
                self.imageStatusChanged.emit('无法打开系统浏览器，请稍后重试。')
        else:
            self.imageStatusChanged.emit('仅支持打开 HTTP / HTTPS 网页链接。')

    def loadResource(self, resource_type, name):
        if resource_type != QTextDocument.ResourceType.ImageResource.value:
            return QByteArray()
        key = name.toString()
        if key in self._images:
            return self._images[key]
        if key not in self._failed and key not in self._pending and not self._closed:
            if web_url(name).isValid() and len(self._images) + len(self._pending) < self.MAX_IMAGES:
                self._pending[key] = None
                QTimer.singleShot(0, self._start_downloads)
            else:
                self._failed.add(key)
                QTimer.singleShot(0, self._image_status)
        return self._placeholder

    def _start_downloads(self):
        if self._closed:
            return
        active = sum(worker is not None for worker in self._pending.values())
        for key, worker in list(self._pending.items()):
            if active >= 2:
                break
            if worker is not None:
                continue
            receiver = weakref.ref(self)
            cancelled = self._cancelled
            max_bytes, max_pixels = self.MAX_IMAGE_BYTES, self.MAX_IMAGE_PIXELS

            def run(url=key):
                try:
                    result = _download_image(url, cancelled, max_bytes, max_pixels)
                except Exception:
                    result = None
                target = receiver()
                if target is not None and not cancelled.is_set():
                    try:
                        target._imageCompleted.emit(url, result)
                    except RuntimeError:
                        pass

            worker = threading.Thread(target=run, name='update-note-image', daemon=True)
            self._pending[key] = worker
            worker.start()
            active += 1
        self._image_status()

    def _image_ready(self, key, image):
        self._pending.pop(key, None)
        if self._closed:
            return
        if image is None:
            self._failed.add(key)
        else:
            self._images[key] = image
            document = self.document()
            document.addResource(QTextDocument.ResourceType.ImageResource.value, QUrl(key), image)
            document.markContentsDirty(0, document.characterCount())
            self.viewport().update()
        self._start_downloads()

    def _image_status(self):
        if self._closed:
            return
        if self._pending:
            self.imageStatusChanged.emit('正在加载日志图片…')
        elif self._failed:
            self.imageStatusChanged.emit('部分图片未能加载，可在发布页面查看；不影响更新。')
        else:
            self.imageStatusChanged.emit('')

    def stop_loading(self):
        self._closed = True
        self._cancelled.set()
        self._pending.clear()


class UpdateDialog(QDialog):
    def __init__(self, current_version, new_version, *, html='', markdown='',
                 download_url='', critical=False, parent=None):
        super().__init__(parent)
        self.ignore_version = False
        self.setWindowTitle('发现新版本 · idv-login')
        self.setSizeGripEnabled(True)
        self.setObjectName('updateDialog')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 18)
        layout.setSpacing(18)
        body = QHBoxLayout()
        body.setSpacing(24)
        summary = QWidget()
        summary.setFixedWidth(194)
        side = QVBoxLayout(summary)
        side.setContentsMargins(0, 4, 0, 0)
        side.setSpacing(12)

        def label(text, name='', parent_layout=side):
            widget = QLabel(str(text or '未知版本'))
            widget.setTextFormat(Qt.TextFormat.PlainText)
            widget.setWordWrap(True)
            widget.setObjectName(name)
            parent_layout.addWidget(widget)
            return widget

        label('IDV-LOGIN', 'eyebrow')
        label('有新版本可用', 'headline')
        label('最新版本', 'muted')
        label(new_version, 'version')
        label(f'当前版本\n{current_version}', 'muted')
        side.addSpacing(12)
        label('安全更新' if critical else '保持最新，体验更好', 'badge')
        label('本次包含安全相关更新，建议及时安装。' if critical
              else '查看本次改进，准备好后前往发布页面下载。', 'muted')
        side.addStretch(1)
        label('更新将在系统浏览器中打开，\n不会在此自动安装。', 'muted')
        body.addWidget(summary)
        notes_panel = QFrame()
        notes_panel.setObjectName('notesPanel')
        notes_layout = QVBoxLayout(notes_panel)
        notes_layout.setContentsMargins(0, 0, 0, 0)
        self.notes = ReleaseNotesBrowser()
        notes_layout.addWidget(self.notes)
        body.addWidget(notes_panel, 1)
        layout.addLayout(body, 1)
        footer = QHBoxLayout()
        self.status = QLabel()
        self.status.setObjectName('muted')
        self.status.setWordWrap(True)
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        footer.addWidget(self.status, 1)
        self.later_button = QToolButton()
        self.later_button.setText('下次提醒我')
        self.later_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.later_button.clicked.connect(self.reject)
        if not critical:
            self.later_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
            menu = QMenu(self.later_button)
            ignore = QAction('忽略此版本', menu)
            ignore.triggered.connect(self._ignore)
            menu.addAction(ignore)
            self.later_button.setMenu(menu)
        self.update_button = QPushButton('现在更新  ↗')
        self.update_button.setObjectName('primary')
        self.update_button.setDefault(True)
        self.update_button.clicked.connect(self.accept)
        self.update_button.setEnabled(web_url(download_url).isValid())
        if not self.update_button.isEnabled():
            self.status.setText('暂未提供有效下载地址，请稍后再试。')
            self.update_button.setToolTip('云端配置中没有有效的 HTTP / HTTPS 下载地址')
        footer.addWidget(self.later_button)
        footer.addWidget(self.update_button)
        layout.addLayout(footer)
        self.notes.imageStatusChanged.connect(self._show_image_status)
        self.finished.connect(self.notes.stop_loading)
        self._apply_theme()
        self.notes.set_notes(html, markdown)
        QApplication.instance().paletteChanged.connect(self._refresh_theme)
        available = self.screen().availableGeometry()
        self.setMinimumSize(min(640, available.width()), min(420, available.height()))
        self.resize(min(980, int(available.width() * .88)), min(680, int(available.height() * .85)))

    def _ignore(self):
        self.ignore_version = True
        self.reject()

    def _show_image_status(self, text):
        if self.update_button.isEnabled():
            self.status.setText(text)

    def _apply_theme(self):
        dark = QApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128
        bg, panel, text, muted, border, accent, badge = (
            ('#191d26', '#232936', '#e8edf6', '#a6b1c3', '#3a4455', '#86adff', '#2c3a52') if dark else
            ('#f4f6fb', '#ffffff', '#202b3d', '#66758b', '#dce3ee', '#275fc7', '#e7eefc'))
        palette = self.notes.palette()
        for role, color in ((QPalette.ColorRole.Base, panel), (QPalette.ColorRole.Text, text),
                            (QPalette.ColorRole.AlternateBase, bg), (QPalette.ColorRole.Mid, border),
                            (QPalette.ColorRole.Link, accent)):
            palette.setColor(role, QColor(color))
        self.notes.setPalette(palette)
        self.setStyleSheet(f'''
            QDialog#updateDialog {{ background: {bg}; color: {text}; }}
            QLabel {{ color: {text}; font-size: 13px; }}
            QLabel#eyebrow {{ color: {accent}; font-size: 12px; font-weight: 700; }}
            QLabel#headline {{ font-size: 24px; font-weight: 700; }}
            QLabel#version {{ font-size: 21px; font-weight: 600; }}
            QLabel#muted {{ color: {muted}; font-size: 12px; }}
            QLabel#badge {{ color: {accent}; background: {badge}; padding: 9px; border-radius: 6px; }}
            QFrame#notesPanel {{ background: {panel}; border: 1px solid {border}; border-radius: 10px; }}
            QTextBrowser {{ background: transparent; color: {text}; border: 0; }}
            QPushButton, QToolButton {{ color: {text}; background: {panel}; border: 1px solid {border};
                border-radius: 6px; padding: 9px 14px; font-size: 13px; }}
            QPushButton:hover, QToolButton:hover {{ border-color: {accent}; }}
            QPushButton#primary {{ background: {accent}; color: {'#152039' if dark else '#ffffff'};
                border-color: {accent}; font-weight: 600; }}
            QPushButton#primary:disabled {{ background: {border}; color: {muted}; border-color: {border}; }}
            QToolButton::menu-button {{ width: 18px; border-left: 1px solid {border}; }}
            QToolButton[popupMode="1"] {{ padding-right: 28px; }}
            QScrollBar:vertical {{ background: {panel}; width: 12px; margin: 4px 2px; }}
            QScrollBar:horizontal {{ background: {panel}; height: 12px; margin: 2px 4px; }}
            QScrollBar::handle {{ background: {border}; border-radius: 4px; min-width: 24px; min-height: 24px; }}
            QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
            QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
        ''')

    def _refresh_theme(self):
        self._apply_theme()
        self.notes.style_document()
