from os import getenv
from copy import deepcopy

from changedetectionio.strtobool import strtobool
from changedetectionio.blueprint.menu_modes import MENU_SIDEBAR_ACTIONMODES_DEFAULT
from changedetectionio.blueprint.rss import RSS_FORMAT_TYPES, RSS_CONTENT_FORMAT_DEFAULT
from changedetectionio.model.Tags import TagsDict

from changedetectionio.notification import (
    default_notification_body,
    default_notification_format,
    default_notification_title,
)

# Equal to or greater than this number of FilterNotFoundInResponse exceptions will trigger a filter-not-found notification
_FILTER_FAILURE_THRESHOLD_ATTEMPTS_DEFAULT = 6
DEFAULT_SETTINGS_HEADERS_USERAGENT='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/87.0.4280.66 Safari/537.36'


def _env_retention_default(name, default, kind):
    """A tolerant env default, so one bad value can't stop the app from importing."""
    value = getenv(name)
    if value is None or not str(value).strip():
        return default
    try:
        return kind(str(value).strip())
    except ValueError:
        return default


class model(dict):
    base_config = {
            'note': "Hello! If you change this file manually, please be sure to restart your changedetection.io instance!",
            'watching': {},
            'settings': {
                'headers': {
                },
                'requests': {
                    'extra_proxies': [], # Configurable extra proxies via the UI
                    'extra_browsers': [],  # Configurable extra proxies via the UI
                    'jitter_seconds': 0,
                    'proxy': None, # Preferred proxy connection
                    'time_between_check': {'weeks': None, 'days': None, 'hours': 3, 'minutes': None, 'seconds': None},
                    'timeout': int(getenv("DEFAULT_SETTINGS_REQUESTS_TIMEOUT", "45")),  # Default 45 seconds
                    'workers': int(getenv("DEFAULT_SETTINGS_REQUESTS_WORKERS", "5")),  # Number of threads, lower is better for slow connections
                    'default_ua': {
                        'html_requests': getenv("DEFAULT_SETTINGS_HEADERS_USERAGENT", DEFAULT_SETTINGS_HEADERS_USERAGENT),
                        'html_webdriver': None,
                    }
                },
                'application': {
                    # Custom notification content
                    'all_paused': False,
                    'all_muted': False,
                    'api_access_token_enabled': True,
                    'base_url' : None,
                    'empty_pages_are_a_change': False,
                    'fetch_backend': getenv("DEFAULT_FETCH_BACKEND", "html_requests"),
                    'filter_failure_notification_threshold_attempts': _FILTER_FAILURE_THRESHOLD_ATTEMPTS_DEFAULT,
                    'global_ignore_text': [], # List of text to ignore when calculating the comparison checksum
                    'global_subtractive_selectors': [],
                    'history_snapshot_max_length': None,
                    # Snapshot retention: keep the newest N snapshots and nothing older than N days,
                    # always keeping the current version, the version before the latest change, and
                    # any pinned snapshot. See changedetectionio/snapshot_retention.py. The env vars
                    # are applied in __init__ below (and only to a fresh install's defaults; the
                    # UI/settings file wins after that).
                    'snapshot_retention_enabled': True,
                    'snapshot_retention_keep_last': 30,
                    'snapshot_retention_keep_days': 30,
                    'ignore_whitespace': True,
                    'ignore_status_codes': False, #@todo implement, as ternary.
                    'ssim_threshold': '0.96',  # Default SSIM threshold for screenshot comparison
                    'notification_body': default_notification_body,
                    'notification_format': default_notification_format,
                    'notification_title': default_notification_title,
                    'notification_urls': [], # Apprise URL list
                    'pager_size': 50,
                    'password': False,
                    'render_anchor_tag_content': False,
                    'rss_access_token': None,
                    'rss_content_format': RSS_CONTENT_FORMAT_DEFAULT,
                    'rss_template_type': 'system_default',
                    'rss_template_override': None,
                    'rss_diff_length': 5,
                    'rss_hide_muted_watches': True,
                    'rss_reader_mode': False,
                    'scheduler_timezone_default': None,  # Default IANA timezone name
                    'schema_version' : 0,
                    'shared_diff_access': False,
                    'strip_ignored_lines': False,
                    'tags': None,  # Initialized in __init__ with real datastore_path
                    # All LLM settings now live nested under application.llm.* (post-migration update_31).
                    # Defaults come from LLMSettings.model_validate({}) at read time —
                    # no need to pre-seed an empty {} here.
                    'webdriver_delay': None , # Extra delay in seconds before extracting text
                    'ui': {
                        'use_page_title_in_list': True,
                        'use_share_watch': False,  # "Share watch" link is a power-user feature, off by default
                        'open_diff_in_new_tab': True,
                        'socket_io_enabled': True,
                        'favicons_enabled': True,
                        'timeago_format': 'long',  # 'long' = "1 minute ago", 'short' = "1m ago"
                        'sidebar_mode': MENU_SIDEBAR_ACTIONMODES_DEFAULT,  # one of blueprint.menu_modes.MENU_SIDEBAR_ACTIONMODES
                    },
                }
            }
        }

    def __init__(self, *arg, datastore_path=None, **kw):
        super(model, self).__init__(*arg, **kw)
        # Capture any tags data passed in before base_config overwrites the structure
        existing_tags = self.get('settings', {}).get('application', {}).get('tags') or {}
        # CRITICAL: deepcopy to avoid sharing mutable objects between instances
        self.update(deepcopy(self.base_config))
        # Env overrides are read here, not in base_config: the class attribute is evaluated once
        # at import, so a value set later (tests, a restarted process with a different env) would
        # never take effect.
        self['settings']['application'].update({
            'snapshot_retention_enabled': _env_retention_default('SNAPSHOT_RETENTION_ENABLED', True, strtobool),
            'snapshot_retention_keep_last': _env_retention_default('SNAPSHOT_RETENTION_KEEP_LAST', 30, int),
            'snapshot_retention_keep_days': _env_retention_default('SNAPSHOT_RETENTION_KEEP_DAYS', 30, int),
        })
        # TagsDict requires the real datastore_path at runtime (cannot be set at class-definition time)
        if datastore_path is None:
            raise ValueError("App.model() requires 'datastore_path' keyword argument")
        self['settings']['application']['tags'] = TagsDict(existing_tags, datastore_path=datastore_path)


def parse_headers_from_text_file(filepath):
    headers = {}
    with open(filepath, 'r', encoding='utf-8') as f:
        for l in f.readlines():
            l = l.strip()
            if not l.startswith('#') and ':' in l:
                (k, v) = l.split(':', 1)  # Split only on the first colon
                headers[k.strip()] = v.strip()

    return headers