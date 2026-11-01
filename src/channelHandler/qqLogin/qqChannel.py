# coding=UTF-8
# Copyright (c) 2026 KKeygen. SPDX-License-Identifier: GPL-3.0-or-later
import sys

if sys.platform == 'win32':
    from channelHandler.qqLogin.qqChannel_windows import *
else:
    from channelHandler.qqLogin.qqChannel_legacy import *
