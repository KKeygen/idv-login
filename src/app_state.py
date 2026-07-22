# coding=UTF-8
"""Typed module-level singletons for cross-module shared objects.

Instead of ``genv.set("PROXY_MGR", mgr)`` / ``genv.get("PROXY_MGR")``,
use ``import app_state; app_state.proxy_mgr = mgr`` /
``app_state.proxy_mgr``.  This lets static-analysis tools (pyflakes,
mypy, pylint …) resolve types and detect dead code.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable
from typing import Callable, List
from PyQt6.QtWidgets import QApplication, QWidget, QLabel, QVBoxLayout, QMainWindow, QPushButton
from PyQt6.QtCore import Qt, QTimer, QPropertyAnimation, QPoint, QEasingCurve, QObject, pyqtSignal
from PyQt6.QtGui import QFont, QScreen, QGuiApplication, QCursor, QPainter, QColor, QPen, QPainterPath
if TYPE_CHECKING:
    from channelmgr import ChannelManager
    from cloudRes import CloudRes
    from mitm_proxy import MitmProxyManager
    from uimgr import UIManager
    from PyQt6.QtWidgets import QApplication

channels_helper: ChannelManager | None = None
fake_device: dict | None = None
cloud_res: CloudRes | None = None
app: QApplication | None = None
ui_mgr: UIManager | None = None
proxy_mgr: MitmProxyManager | None = None
fever_bridge = None
fever_bridge_target_game_ids: set[str] = set()

# 用于跨线程调度的辅助对象
_main_thread_invoker = None



_active_toasts: List['ModernToast'] = [] # 关键：防止Toast对象被垃圾回收导致闪退

def _ensure_invoker():
    """确保主线程调度器已初始化（必须在主线程调用）。"""
    global _main_thread_invoker
    if _main_thread_invoker is None and app is not None:
        from PyQt6.QtCore import QObject, pyqtSignal

        class MainThreadInvoker(QObject):
            invoke_signal = pyqtSignal(object)

            def __init__(self):
                super().__init__()
                self.invoke_signal.connect(self._do_invoke)

            def _do_invoke(self, func):
                try:
                    func()
                except Exception:
                    import traceback
                    traceback.print_exc()

        _main_thread_invoker = MainThreadInvoker()


def run_on_main_thread(func: Callable[[], None]) -> None:
    """在 Qt 主线程中执行 func。

    可以从任何线程安全调用。如果没有 Qt 应用运行，则直接执行。
    """
    global _main_thread_invoker
    if _main_thread_invoker is not None:
        _main_thread_invoker.invoke_signal.emit(func)
    elif app is not None:
        # invoker 未初始化，但 app 存在 - 尝试直接用 QTimer
        # 注意：这只在主线程中调用时有效
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, func)
    else:
        func()



class ModernToast(QWidget):
    def __init__(self, text, duration=2500):
        super().__init__()
        
        # 基本属性
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | 
            Qt.WindowType.WindowStaysOnTopHint | 
            Qt.WindowType.Tool |
            Qt.WindowType.WindowTransparentForInput
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowOpacity(0.0)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(text)
        
        # 文字样式（背景由 paintEvent 绘制）
        self.label.setStyleSheet("""
            QLabel {
                background: transparent;
                color: rgba(255, 255, 255, 255);
                padding: 15px 40px;
                font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
                font-size: 18px;                
                font-weight: bold;
            }
        """)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.label)
        
        self.duration = duration
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide_toast)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.height() / 2
        path = QPainterPath()
        path.addRoundedRect(0.5, 0.5, self.width() - 1, self.height() - 1, r, r)
        painter.fillPath(path, QColor(46, 204, 113, 100))
        painter.setPen(QPen(QColor(255, 255, 255, 40), 1))
        painter.drawPath(path)

    def show_toast(self):
        # 动态定位：获取鼠标所在的活动屏幕
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        screen_geometry = screen.geometry()
        
        self.adjustSize()
        
        x = screen_geometry.x() + (screen_geometry.width() - self.width()) // 2
        y = screen_geometry.y() + int(screen_geometry.height() * 0.8) - (self.height() // 2)
        
        self.move(x, y)
        self.show()
        
        # 淡入：文字会随窗口不透明度达到1.0而变实色
        self.fade_anim = QPropertyAnimation(self, b"windowOpacity")
        self.fade_anim.setDuration(300)
        self.fade_anim.setStartValue(0.0)
        self.fade_anim.setEndValue(1.0)
        self.fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.fade_anim.start()
        
        self.timer.start(self.duration)

    def hide_toast(self):
        # 最后一起变透明淡出
        self.fade_out = QPropertyAnimation(self, b"windowOpacity")
        self.fade_out.setDuration(800)
        self.fade_out.setStartValue(1.0)
        self.fade_out.setEndValue(0.0)
        self.fade_out.setEasingCurve(QEasingCurve.Type.InCubic)
        self.fade_out.finished.connect(self._on_close)
        self.fade_out.start()

    def _on_close(self):
        self.close()
        if self in _active_toasts:
            _active_toasts.remove(self) # 允许内存回收

# --- 3. 统一全局调用接口 ---

def toast(text: str, duration: int = 2500):
    """
    可以在任何线程安全调用的全局函数。
    """
    def _create():
        t = ModernToast(text, duration)
        _active_toasts.append(t) # 保持引用防止被GC
        t.show_toast()
    
    run_on_main_thread(_create)
