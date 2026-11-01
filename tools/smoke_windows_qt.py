import importlib.util
from pathlib import Path
import sys

from PyQt6 import QtCore, QtGui, QtWidgets

app = QtWidgets.QApplication([sys.argv[0]])
if sys.platform != 'win32' or app.platformName() != 'windows':
    raise RuntimeError('This packaging check requires the Windows QPA plugin')
for name in ('QtWebEngineCore', 'QtWebEngineWidgets', 'QtNetwork', 'QtQml', 'QtQuick'):
    if importlib.util.find_spec('PyQt6.' + name) is not None:
        raise RuntimeError('Unused Windows Qt module was packaged: ' + name)
icon = QtGui.QIcon(str(Path(sys.argv[1]).resolve()))
if icon.pixmap(32, 32).isNull():
    raise RuntimeError('The ICO plugin failed to decode the application icon')
svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><rect width="16" height="16" fill="red"/></svg>'
if QtGui.QImage.fromData(svg, 'SVG').isNull():
    raise RuntimeError('The bounded SVG image path lacks its decoder')
window = QtWidgets.QMainWindow()
content = QtWidgets.QWidget(window)
layout = QtWidgets.QVBoxLayout(content)
layout.addWidget(QtWidgets.QLabel('Windows native Widgets packaging check'))
layout.addWidget(QtWidgets.QPushButton('Native button'))
table = QtWidgets.QTableWidget(1, 2)
table.setItem(0, 0, QtWidgets.QTableWidgetItem('native'))
layout.addWidget(table)
window.setCentralWidget(content)
window.resize(500, 300)
window.show()
QtCore.QTimer.singleShot(250, app.quit)
app.exec()
if window.grab().isNull():
    raise RuntimeError('Native window could not render')
window.close()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dist' / 'src'))
import webview2_com_interfaces
from webview2_com_loader import load_loader
load_loader()
if (Path(sys.path[0]) / 'native/webview2/idv_webview2.dll').exists():
    raise RuntimeError('Retired custom WebView2 DLL was packaged')
print('Windows Widgets packaging check passed: native QPA/style, ICO, SVG, raster controls; comtypes and Microsoft loader loaded; no custom WebView2 DLL')
