"""Explicit opt-in account selection and identity policy (never performs login requests)."""
import re

from envmgr import genv
from channelHandler.channelUtils import getShortGameId


DEFAULT_NATIVE_SCAN_CHANNELS = ['huawei', 'oppo', 'nearme_vivo', 'xiaomi_app']


def native_scan_channels(game_id):
    import app_state
    cloud = app_state.cloud_res
    configured = cloud.get_by_game_id_and_key(getShortGameId(game_id), 'native_scan_channels') if cloud else None
    return list(DEFAULT_NATIVE_SCAN_CHANNELS if configured is None else configured)


class AccountListPolicy:
    def __init__(self, manager, game_helper=None):
        import app_state
        self.manager = manager
        self.game_helper = (game_helper or getattr(getattr(manager, "db_sync", None), "game_helper", None)
                            or getattr(app_state.ui_mgr, "game_helper", None))

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

    def games(self):
        """One account list per SDK game, regardless of local installation count."""
        if self.game_helper is None:
            return []
        discovered = {self.record_game(record) for record in self.manager.channels
                      if not self.is_cross_game(record)
                      and (record.user_info.get('login_channel') or record.channel_name) != 'netease'}
        sync = getattr(self.manager, 'db_sync', None)
        if sync:
            discovered.update(sync.game_ids())
        for game_id in sorted(discovered - {''}):
            self.game_helper.get_game(game_id)
        cloud = self._cloud()
        result = []
        for game in list(self.game_helper.games.values()):
            short = getShortGameId(game.game_id)
            catalog = cloud.dynamic_game_catalog.get_game(short) if cloud else None
            name = game.name if game.name and game.name != game.game_id else (catalog or {}).get('name') or short
            result.append({'game_id': game.game_id, 'short_game_id': short, 'name': name})
        return result

    @staticmethod
    def _positive(value, name):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f'{name}必须是正整数')
        return value

    def get_settings(self):
        # Old quotas never count as consent. Upgrades start disabled.
        if self.game_helper is not None:
            self.game_helper.migrate_account_switching()
        saved = genv.get('account_switching_settings', {})
        choices = {}
        for item in self.games():
            choice = self.game_helper.get_existing_game(item['game_id']).account_switching
            choices[item['short_game_id']] = {'enabled': choice.get('enabled', False),
                                              'account_uuids': list(choice.get('account_uuids', []))}
        return {
            'enabled': saved.get('enabled', False),
            'onboarding_completed': saved.get('onboarding_completed', False),
            'auto_import_new': saved.get('auto_import_new', True),
            'limit_enabled': saved.get('limit_enabled', True),
            'recent_limit': saved.get('recent_limit', 5),
            'games': choices,
        }

    def set_settings(self, config):
        if not isinstance(config, dict):
            raise ValueError('游戏内切换设置必须是 JSON 对象')
        settings = self.get_settings()
        for key in ('enabled', 'onboarding_completed', 'auto_import_new', 'limit_enabled'):
            if key in config:
                if not isinstance(config[key], bool):
                    raise ValueError(f'{key}必须是开关值')
                settings[key] = config[key]
        if 'recent_limit' in config:
            settings['recent_limit'] = self._positive(config['recent_limit'], '最近账号数量')
        if 'games' in config:
            if not isinstance(config['games'], dict):
                raise ValueError('请选择有效的游戏')
            available = {item['short_game_id']: item for item in self.games()}
            games = {game: {'enabled': False, 'account_uuids': []} for game in available}
            for game_id, item in config['games'].items():
                if game_id not in available:
                    continue  # A removed game must not be silently re-enabled.
                if not isinstance(item, dict) or not isinstance(item.get('enabled', False), bool):
                    raise ValueError('游戏设置必须包含启用开关')
                chosen = item.get('account_uuids', [])
                if not isinstance(chosen, list) or any(not isinstance(uid, str) for uid in chosen):
                    raise ValueError('请选择有效的账号')
                games[game_id] = {'enabled': item.get('enabled', False),
                                   'account_uuids': list(dict.fromkeys(chosen))}
            settings['games'] = games
        self._save_settings(settings)
        return settings

    def _save_settings(self, settings):
        if self.game_helper is None:
            raise RuntimeError('Game manager is unavailable')
        self.game_helper.save_account_switching(
            {key: value for key, value in settings.items() if key != 'games'}, settings['games'])

    def include_logged_in(self, record):
        """A successful login event, not a timestamp guess, opts future accounts in."""
        settings = self.get_settings()
        if not settings['enabled'] or not settings['auto_import_new']:
            return
        changed = False
        for item in self.games():
            config = settings['games'].get(item['short_game_id'], {})
            if not config.get('enabled'):
                continue
            game = item['short_game_id']
            if self.identity(record, game) is None and not (self._manual(record) and self.is_cross_game(record)):
                continue
            uuid = self.canonical_uuid(record, game)
            if uuid is not None and uuid not in config['account_uuids']:
                config['account_uuids'].append(uuid)
                changed = True
        if changed:
            self._save_settings(settings)

    @staticmethod
    def selection_reason(record, game):
        """Advisory for default selection, derived locally; never an eligibility gate."""
        try:
            if record.is_login_expired(game):
                return ('登录已过期，请重新扫码' if record.record_source == 'scan' else
                        '登录已过期，请在账号管理中重新登录')
            if record.record_source == 'scan':
                return '' if record.user_info.get('token') else '请重新扫码登录'
            if record.cached_unisdk(game) is not None:
                return ''
            attrs = vars(record)
            if record.channel_name == 'oppo':
                login = attrs.get('loginResp') or {}
                if not (login.get('accountToken') or {}).get('idToken'):
                    return '请在账号管理中重新登录渠道账号'
                accounts = ((attrs.get('oppo_open_account') or {}).get('gamesdk_latest_role') or {}).get('accounts') or []
                chosen = attrs.get('chosen_account_id') or ''
                if len(accounts) > 1 and not any(str(item.get('account_id') or '') == chosen for item in accounts):
                    return '未固定小号，请在账号管理中确认选择'
            elif record.channel_name == 'nearme_vivo':
                active = attrs.get('activeAccount')
                if not getattr(active, 'openToken', None):
                    if not attrs.get('cookies'):
                        return '请在账号管理中重新登录渠道账号'
                    accounts = getattr(attrs.get('session'), 'subAccounts', [])
                    if len(accounts) > 1 and not any(item.subOpenId == attrs.get('chosenAccount') for item in accounts):
                        return '请在账号管理中重新选择小号'
            elif record.channel_name == 'huawei' and not attrs.get('serviceToken'):
                return '请在账号管理中重新登录渠道账号'
            elif record.channel_name == 'xiaomi_app':
                auth = attrs.get('oAuthData') or {}
                if not auth.get('uuid') or not auth.get('st'):
                    return '请在账号管理中重新登录渠道账号'
        except (AttributeError, KeyError, TypeError, ValueError):
            return '登录数据需要确认，请在账号管理中重新登录'
        return ''  # No packet yet can still be acquired silently; do not mark it invalid.

    def catalog(self):
        config = self.get_settings()
        sync = getattr(self.manager, 'db_sync', None)
        counts = {}
        result = []
        for item in self.games():
            game = item['short_game_id']
            accounts = sorted(self._candidates(game), key=lambda record: (-record.last_login_time, record.uuid))
            if game not in counts:
                counts[game] = sync.native_account_count(game) if sync else None
            result.append({**item, 'tool_account_count': len(accounts),
                           'native_account_count': counts[game],
                           'accounts': [{'uuid': record.uuid, 'name': record.name,
                                         'selection_reason': self.selection_reason(record, game),
                                         'last_login_time': record.last_login_time} for record in accounts]})
        return {'config': config, 'games': result}

    @staticmethod
    def identity(record, game_id):
        """Deduplicate only by the Channel's actual SAUTH identity for this game."""
        try:
            return record.sdk_identity(game_id)
        except ValueError as error:
            record.report_login_failure(game_id, 'selection.identity', '已跳过无效身份，不参与去重', error, detail=str(error))
            return None

    def _candidates(self, game):
        records = [r for r in self.manager.channels if self.is_cross_game(r) or self.record_game(r) == game]
        records = [r for r in records if (r.user_info.get('login_channel') or r.channel_name) != 'netease']
        records = [r for r in records if self._manual(r) or r.channel_name not in self.native_scan_channels(game)]
        records.sort(key=lambda r: (not self._manual(r), -r.last_login_time, r.uuid))
        result, seen = [], set()
        for record in records:
            identity = self.identity(record, game)
            # Keep old Channels visible for repair through the original login.
            # The DB writer reports and skips missing SDK identity individually.
            if identity is None or identity not in seen:
                result.append(record)
                if identity is not None:
                    seen.add(identity)
        return result

    def canonical_uuid(self, record, game):
        if record is None:
            return None
        identity = self.identity(record, game)
        for candidate in self._candidates(game):
            if candidate is record or (identity is not None and self.identity(candidate, game) == identity):
                return candidate.uuid
        return None

    def select(self, game_ids=None):
        settings = self.get_settings()
        result = {}
        if not settings['enabled']:
            return result
        wanted = {getShortGameId(g) for g in game_ids} if game_ids is not None else None
        for item in self.games():
            game = item['short_game_id']
            if wanted is not None and game not in wanted:
                continue
            config = settings['games'].get(item['short_game_id'], {})
            if not config.get('enabled', False):
                continue
            candidates = self._candidates(game)
            chosen = {self.canonical_uuid(self.manager.query_channel(uid), game)
                      for uid in config.get('account_uuids', [])}
            records = [record for record in candidates if record.uuid in chosen]
            records.sort(key=lambda record: (-record.last_login_time, record.uuid))
            if settings['limit_enabled']:
                records = records[:settings['recent_limit']]
            result[game] = records
        return result

    def account_label(self, record, expired=False):
        channel = record.channel_name
        if channel == 'myapp' and str(record.uuid).removeprefix('idv-').startswith('qq-'):
            channel = 'myapp_qq'
        known = {'huawei': '华为', 'oppo': 'OPPO', 'nearme_vivo': 'vivo', 'xiaomi_app': '小米', 'bilibili_sdk': '哔哩哔哩', 'uc_platform': '九游', '360_assistant': '360', '4399': '4399', 'm4399': '4399', 'honor_sdk': '荣耀', 'myapp_qq': 'QQ', 'myapp': '微信'}
        original_display = self.manager._manual_channel_name(channel, self.record_game(record)) or channel
        display = known.get(channel, re.sub(r'账号$', '', original_display).strip())
        name = str(record.name or record.uuid)
        label = f'{display} {name}'
        if not self._manual(record):
            return label + '（临时）'
        return label + ('（已过期）' if expired else '')
