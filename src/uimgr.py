# coding=UTF-8
# Copyright (c) 2026 KKeygen. SPDX-License-Identifier: GPL-3.0-or-later
import sys

if sys.platform == 'win32':
    from fever_uimgr import (UIManager, configure_application_icon,
                              configure_windows_app_identity, register_url_scheme)
else:
    from web_uimgr import (UIManager, configure_application_icon,
                           configure_windows_app_identity, register_url_scheme)
