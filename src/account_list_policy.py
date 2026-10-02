"""Local account scope, identity and quota policy (never performs login requests)."""
from copy import deepcopy
import re
import base64
import binascii
import json
from urllib.parse import unquote

from envmgr import genv
from channelHandler.channelUtils import getShortGameId


DEFAULT_NATIVE_SCAN_CHANNELS = ['huawei', 'oppo', 'nearme_vivo', 'xiaomi_app']


def native_scan_channels(game_id):
    import app_state
    cloud = app_state.cloud_res
    configured = cloud.get_by_game_id_and_key(getShortGameId(game_id), 'native_scan_channels') if cloud else None
    return list(DEFAULT_NATIVE_SCAN_CHANNELS if configured is None else configured)


class AccountListPolicy:
    DEFAULT_NATIVE_SCAN_CHANNELS = ['huawei', 'oppo', 'nearme_vivo', 'xiaomi_app']

    def __init__(self, manager):
        self.manager = manager
        self._statuses = {}
        self.aliases = {}

    def _cloud(self):
        import app_state
        return app_state.cloud_res

    def native_scan_channels(self, game_id):
        return native_scan_channels(game_id)

    @staticmethod
    def _manual(record):
        return record.record_source == 'manual'

    def is_cross_game(self, record):
        if self._manual(record):
            return bool(record.crossGames)
        # Base scan records historically defaulted crossGames=True: that flag is
        # not evidence that a captured MPay session works in another game.
        cloud = self._cloud()
        channels = cloud.get_by_game_id_and_key(self.record_game(record), 'cross_game_scan_channels') if cloud else None
        return record.channel_name in (channels or [])

    @staticmethod
    def record_game(record):
        return getShortGameId(getattr(record, 'game_id', '') or record.ext_info.get('from_game_id', '') or record.login_info.get('game_id', ''))

    def eligible_games(self):
        sync = self.manager.db_sync
        if sync is not None and hasattr(sync, 'game_ids'):
            return sorted({getShortGameId(game) for game in sync.game_ids() if game})
        # Before DB discovery is attached, only persisted installations are known.
        return sorted({getShortGameId(game) for game in genv.get('game_installation_settings_v1', {}) if game})

    @staticmethod
    def _positive(value, name):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f'{name}必须是正整数')
        return value

    def _settings(self):
        saved = deepcopy(genv.get('account_list_settings', {}))
        saved.setdefault('global_limit', 7)
        saved.setdefault('games', {})
        return saved

    def get_settings(self, game_id=''):
        settings = self._settings()
        if not game_id:
            return settings
        game = getShortGameId(game_id)
        item = settings['games'].get(game, {})
        return {'global_limit': settings['global_limit'], 'limit': item.get('limit', 5), 'pinned': list(item.get('pinned', []))}

    def _static_sdkuid(self, record):
        # Read only fields used by the adapters' existing UniSDK result builders.
        attrs = vars(record)
        channel = record.channel_name
        if channel == 'xiaomi_app':
            return (attrs.get('oAuthData') or {}).get('uuid')
        if channel == 'oppo':
            return attrs.get('chosen_account_id')
        if channel == 'uc_platform':
            data = attrs.get('ucSession') or {}
            return (data.get('data') or data).get('accountId') or (data.get('data') or data).get('ucid')
        if channel == 'bilibili_sdk':
            data = attrs.get('loginResp') or {}
            return (data.get('data') or data).get('uid')
        if channel == 'honor_sdk':
            return (attrs.get('unionToken') or {}).get('openId')
        if channel == 'nearme_vivo':
            return vars(attrs['activeAccount']).get('subOpenId') if attrs.get('activeAccount') else None
        if channel == '360_assistant':
            return (attrs.get('userInfo') or {}).get('qid')
        session = attrs.get('session')
        data = session if isinstance(session, dict) else vars(session) if session else {}
        field = {'huawei': 'playerId', 'myapp': 'openid', 'myapp_qq': 'openid', 'qihoo': 'qid'}.get(channel)
        return data.get(field) if field else attrs.get('user_id')

    def identity(self, record, game_id):
        game = getShortGameId(game_id)
        if not self._manual(record):
            uid = record.user_info.get('id')
        else:
            sync = self.manager.db_sync
            binding = sync.bindings.get(record.uuid, {}).get(game) if sync else None
            if binding:
                return (record.channel_name, 'mpay', str(binding))
            prepared = sync.writes.get(record.uuid, {}).get(game, {}) if sync else {}
            sdkuid = prepared.get('sdkuid') or self._static_sdkuid(record)
            # Without an exchange binding, SDK UID cannot be treated as MPay UID.
            return (record.channel_name, 'sdk', str(sdkuid)) if sdkuid else ('uuid', record.uuid)
        return (record.channel_name, 'mpay', str(uid)) if uid else ('uuid', record.uuid)

    def _identity_keys(self, record, game):
        keys = {self.identity(record, game)}
        sync = self.manager.db_sync
        if self._manual(record):
            prepared = sync.writes.get(record.uuid, {}).get(game, {}) if sync else {}
            sdkuid = prepared.get('sdkuid') or self._static_sdkuid(record)
            if sdkuid:
                keys.add((record.channel_name, 'sdk', str(sdkuid)))
            return keys
        sources = [record.user_info.get('pc_ext_info', {}).get('extra_unisdk_data'), record.ext_info.get('extra_unisdk_data')]
        for extra in sources:
            if not extra:
                continue
            try:
                extra = json.loads(extra) if isinstance(extra, str) else extra
                sauth = json.loads(base64.b64decode(unquote(extra['SAUTH_JSON'])))
                # This exchange artifact ties SDK UID to this captured MPay user.
                if getShortGameId(sauth.get('gameid', '')) == game and sauth.get('login_channel') == record.channel_name and sauth.get('sdkuid'):
                    keys.add((record.channel_name, 'sdk', str(sauth['sdkuid'])))
            except (ValueError, TypeError, KeyError, binascii.Error):
                # Old captures can lack a usable exchange artifact; the MPay
                # identity remains valid but no SDK equivalence is inferred.
                continue
        return keys

    def _candidates(self, game):
        records = [r for r in self.manager.channels if self.is_cross_game(r) or self.record_game(r) == game]
        records = [r for r in records if self._manual(r) or r.channel_name not in self.native_scan_channels(game)]
        selected_uuid = genv.get('CHANNEL_ACCOUNT_SELECTED', '')
        records.sort(key=lambda r: (not self._manual(r), r.uuid != selected_uuid, -r.last_login_time, r.uuid))
        parent = {}

        def find(key):
            parent.setdefault(key, key)
            if parent[key] != key:
                parent[key] = find(parent[key])
            return parent[key]

        record_keys = []
        for record in records:
            keys = sorted(self._identity_keys(record, game))
            first = find(keys[0])
            for key in keys[1:]:
                parent[find(key)] = first
            record_keys.append((record, keys[0]))
        grouped = {}
        aliases = {}
        for record, key in record_keys:
            winner = grouped.setdefault(find(key), record)
            aliases[record.uuid] = winner.uuid
        self.aliases[game] = aliases
        return list(grouped.values())

    def select(self, game_ids=None):
        games = sorted({getShortGameId(g) for g in (self.eligible_games() if game_ids is None else game_ids) if g})
        candidates = {game: self._candidates(game) for game in games}
        games.sort(key=lambda game: (-max((r.last_login_time for r in candidates[game] if not self.is_cross_game(r)), default=0), game))
        settings = self._settings()
        result = {game: [] for game in games}
        used = set()
        self._statuses = {}
        # Reserve pins across every game before recents consume global capacity.
        queues = {}
        blocked = {game: [] for game in games}
        for game in games:
            config = self.get_settings(game)
            by_uuid = {r.uuid: r for r in candidates[game]}
            pin_ids = list(dict.fromkeys(self.aliases[game].get(uid, uid) for uid in config['pinned']))
            pinned = [by_uuid[uid] for uid in pin_ids if uid in by_uuid]
            recent = sorted((r for r in candidates[game] if r.uuid not in pin_ids and not self.is_cross_game(r)), key=lambda r: (-r.last_login_time, r.uuid))
            cross = sorted((r for r in candidates[game] if r.uuid not in pin_ids and self.is_cross_game(r)), key=lambda r: r.uuid)
            queues[game] = (pinned, recent + cross)
        for phase in (0, 1):
            for game in games:
                limit = self.get_settings(game)['limit']
                for record in queues[game][phase]:
                    if len(result[game]) >= limit or (record.uuid not in used and len(used) >= settings['global_limit']):
                        blocked[game].append(record.uuid)
                        continue
                    used.add(record.uuid)
                    result[game].append(record)
        for game in games:
            self._statuses[game] = {**self.get_settings(game), 'included': [r.uuid for r in result[game]], 'excluded': blocked[game], 'aliases': dict(self.aliases[game]), 'global_used': len(used), 'quota_exhausted': bool(blocked[game]), 'global_quota_exhausted': len(used) >= settings['global_limit'], 'game_quota_exhausted': len(result[game]) >= self.get_settings(game)['limit']}
        return result

    def status(self, game_id):
        game = getShortGameId(game_id)
        self.select()
        return deepcopy(self._statuses.get(game, {**self.get_settings(game), 'included': [], 'quota_exhausted': False}))

    def set_settings(self, game_id, global_limit=None, limit=None, pinned=None):
        game = getShortGameId(game_id)
        settings = self._settings()
        if global_limit is not None:
            settings['global_limit'] = self._positive(global_limit, '全局上限')
        if limit is not None or pinned is not None:
            if not game:
                raise ValueError('设置游戏上限或固定账号时必须指定游戏')
            item = settings['games'].setdefault(game, {'limit': 5, 'pinned': []})
            if limit is not None:
                item['limit'] = self._positive(limit, '游戏上限')
            if pinned is not None:
                if not isinstance(pinned, list) or any(not isinstance(uid, str) for uid in pinned):
                    raise ValueError('固定账号必须是 UUID 列表')
                self._candidates(game)
                valid = self.aliases[game]
                if any(uid not in valid for uid in pinned):
                    raise ValueError('固定账号必须属于该游戏的可选账号')
                item['pinned'] = list(dict.fromkeys(pinned))
        all_pins = set()
        for configured_game, item in settings['games'].items():
            self._candidates(configured_game)
            canonical = {self.aliases[configured_game].get(uid, uid) for uid in item.get('pinned', [])}
            if len(canonical) > item.get('limit', 5):
                raise ValueError(f'该游戏固定账号需要游戏上限至少 {len(canonical)}')
            all_pins.update(canonical)
        if len(all_pins) > settings['global_limit']:
            raise ValueError(f'固定账号需要全局上限至少 {len(all_pins)}')
        genv.set('account_list_settings', settings, True)
        return self.status(game) if game else self.get_settings()

    def account_label(self, record, expired=False):
        channel = record.channel_name
        if channel == 'myapp' and str(record.uuid).removeprefix('idv-').startswith('qq-'):
            channel = 'myapp_qq'
        known = {'huawei': '华为', 'oppo': 'OPPO', 'nearme_vivo': 'vivo', 'xiaomi_app': '小米', 'bilibili_sdk': '哔哩哔哩', 'uc_platform': '九游', '360_assistant': '360', '4399': '4399', 'm4399': '4399', 'honor_sdk': '荣耀', 'myapp_qq': 'QQ', 'myapp': '微信'}
        original_display = self.manager._manual_channel_name(channel, self.record_game(record)) or channel
        display = known.get(channel, re.sub(r'账号$', '', original_display).strip())
        name = str(record.name or record.uuid)
        name = re.sub(r'(?:\s*[（(【\[](?:临时|已过期|扫码|扫码导入|手动|长期保存|短期保存|已失效，在网页重新登录)(?:账号)?[）)】\]])+$', '', name).strip()
        for prefix in sorted({original_display, display}, key=len, reverse=True):
            if name.startswith(prefix):
                name = name[len(prefix):].strip(' -：:')
                break
        label = f'{display} {name}'.strip()
        if not self._manual(record):
            return label + '（临时）'
        return label + ('（已过期）' if expired else '')
