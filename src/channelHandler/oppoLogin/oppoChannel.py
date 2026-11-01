# coding=UTF-8
# Copyright (c) 2026 KKeygen. SPDX-License-Identifier: GPL-3.0-or-later
import sys

if sys.platform == 'win32':
    from channelHandler.oppoLogin.oppoChannel_windows import *
else:
    from channelHandler.oppoLogin.oppoChannel_legacy import *
