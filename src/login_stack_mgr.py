# coding=UTF-8
"""
Copyright (c) 2026 KKeygen

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""


class LoginStackManager:
    _instance = None

    def __init__(self) -> None:
        self._cached_qrcode_data_stack = {}
        self._pending_login_info_stack = {}

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def find_stack_by_common_suffix(cls, stack_dict, game_id):
        stack = stack_dict.get(game_id, [])
        final_key=None
        if stack == []:
            for key in stack_dict.keys():
                common_len = 0
                for a, b in zip(reversed(game_id), reversed(key)):
                    if a == b:
                        common_len += 1
                    else:
                        break
                if common_len >= 3:
                    stack = stack_dict.get(key, [])
                    final_key=key
                    break
        return stack, final_key if final_key else game_id

    def ensure_pending_stack(self, game_id):
        self._pending_login_info_stack.setdefault(game_id, [])

    def push_pending_login_info(self, game_id, process_id, login_info):
        stack,final_key = self.find_stack_by_common_suffix(self._pending_login_info_stack, game_id)
        if process_id:
            stack = [item for item in stack if item.get("process_id") != process_id]
        stack.append({
            "process_id": process_id,
            "login_info": login_info,
        })
        self._pending_login_info_stack[final_key] = stack

    def pop_pending_login_info(self, game_id, process_id=None):
        stack,final_key=self.find_stack_by_common_suffix(self._pending_login_info_stack, game_id)
        item = None
        if process_id:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i].get("process_id") == process_id:
                    item = stack.pop(i)
                    break
        elif stack:
            item = stack.pop()
        self._pending_login_info_stack[final_key] = stack
        return item["login_info"] if item else None

    def push_cached_qrcode_data(self, game_id, process_id, data):
        stack,final_key = self.find_stack_by_common_suffix(self._cached_qrcode_data_stack, game_id)
        if process_id:
            stack = [item for item in stack if item.get("process_id") != process_id]
        stack.append({
            "process_id": process_id,
            "data": data,
        })
        self._cached_qrcode_data_stack[final_key] = stack

    def pop_cached_qrcode_data(self, game_id, process_id=None):
        stack,final_key = self.find_stack_by_common_suffix(self._cached_qrcode_data_stack, game_id)
        item = None
        if process_id:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i].get("process_id") == process_id:
                    item = stack.pop(i)
                    break
        elif stack:
            item = stack.pop()
        self._cached_qrcode_data_stack[final_key] = stack
        return item["data"] if item else None
