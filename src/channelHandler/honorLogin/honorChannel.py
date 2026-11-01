# coding=UTF-8
# Copyright (c) 2026 KKeygen. SPDX-License-Identifier: GPL-3.0-or-later
import sys

if sys.platform == 'win32':
    from channelHandler.honorLogin.honorChannel_windows import *
else:
    from channelHandler.honorLogin.honorChannel_legacy import *
