"""
Watch configuration import/export as portable JSON.

Unlike the full backup (which carries snapshots, history and machine-local UUIDs),
this transfer format only contains the *configuration* needed to rebuild the same
monitors on another instance: the URL, selectors/filters and notification setup.

Import semantics (see import_watch_config()):
  - Watches are matched by URL (case-insensitive).
  - An existing match is updated in place - no duplicate watch is created.
  - A missing URL is created as a new watch.
  - One bad entry never aborts the batch: every failure is recorded per URL and
    reported back, successful entries are kept (no whole-batch rollback).
"""

import copy
import json

from flask_babel import gettext
from loguru import logger

# Bumped only when the on-disk/on-wire shape changes incompatibly.
WATCH_CONFIG_EXPORT_VERSION = 1
WATCH_CONFIG_EXPORT_TYPE = 'changedetectionio-watch-config'

# User-settable configuration that makes sense to move between machines.
# Deliberately excludes runtime state (history, checksums, counters, last error,
# view state) and machine-local pointers (proxy ids, tag UUIDs - tags travel by
# title, see _tag_titles_for_watch()).
WATCH_CONFIG_FIELDS = [
    'body',
    'browser_steps',
    'conditions',
    'conditions_match_logic',
    'check_unique_lines',
    'extract_lines_containing',
    'extract_text',
    'fetch_backend',
    'filter_failure_notification_send',
    'filter_text_added',
    'filter_text_removed',
    'filter_text_replaced',
    'follow_price_changes',
    'has_ldjson_price_data',
    'headers',
    'history_snapshot_max_length',
    'ignore_status_codes',
    'ignore_text',
    'in_stock_only',
    'include_filters',
    'link_to_open',
    'llm_change_summary',
    'llm_change_summary_mode',
    'llm_intent',
    'method',
    'notification_body',
    'notification_format',
    'notification_muted',
    'notification_screenshot',
    'notification_title',
    'notification_urls',
    'paused',
    'price_change_threshold_percent',
    'processor',
    'remove_duplicate_lines',
    'sort_text_alphabetically',
    'strip_ignored_lines',
    'subtractive_selectors',
    'text_should_not_be_present',
    'time_between_check',
    'time_between_check_use_default',
    'time_schedule_limit',
    'title',
    'track_ldjson_price_data',
    'trim_text_whitespace',
    'trigger_text',
    'use_page_title_in_list',
    'webdriver_delay',
    'webdriver_js_execute_code',
]

# Fields the API/UI treat as read-only or that are derived state - an import file
# must never be able to set them even if someone hand-edits one in.
_NEVER_IMPORTABLE = frozenset(
    {
        'check_count',
        'consecutive_filter_failures',
        'content-type',
        'date_created',
        'fetch_time',
        'last_checked',
        'last_changed',
        'last_error',
        'last_notification_error',
        'last_viewed',
        'newest_history_key',
        'notification_alert_count',
        'page_title',
        'previous_md5',
        'proxy',
        'remote_server_reply',
        'tag',
        'tags',
        'uuid',
        'viewed',
        'history',
    }
)


class WatchConfigImportError(Exception):
    """Raised for problems that make the whole import document unprocessable."""


def _tag_titles_for_watch(watch, datastore):
    """Tag titles attached to a watch, in a stable (sorted) order. [] when untagged."""
    titles = []
    tags = datastore.data['settings']['application'].get('tags', {})
    for tag_uuid in watch.get('tags') or []:
        tag = tags.get(tag_uuid)
        if tag and tag.get('title'):
            titles.append(tag['title'])
    return sorted(titles)


def serialize_watch(watch, datastore):
    """One portable config dict for a single watch (no runtime/state fields)."""
    entry = {'url': watch.get('url', '')}

    title = watch.get('title')
    if title:
        entry['title'] = title

    tag_titles = _tag_titles_for_watch(watch, datastore)
    if tag_titles:
        entry['tags'] = tag_titles

    for field in WATCH_CONFIG_FIELDS:
        if field in watch and field not in entry:
            entry[field] = copy.deepcopy(watch[field])

    return entry


def build_export_document(datastore):
    """The full export payload: metadata plus one entry per watch, sorted by URL."""
    watches = datastore.data.get('watching', {})
    entries = [serialize_watch(watch, datastore) for watch in watches.values()]
    entries.sort(key=lambda e: (e.get('url') or '').lower())

    return {
        'type': WATCH_CONFIG_EXPORT_TYPE,
        'version': WATCH_CONFIG_EXPORT_VERSION,
        'watch_count': len(entries),
        'watches': entries,
    }


def parse_export_document(raw_text):
    """Parse and structurally validate an uploaded export document.

    Returns the list of watch entry dicts. Raises WatchConfigImportError for any
    problem that prevents processing the document at all (bad JSON, wrong shape).
    Per-entry problems are left to import_watch_config() so they don't sink the
    rest of the batch.
    """
    if isinstance(raw_text, bytes):
        raw_text = raw_text.decode('utf-8', errors='replace')

    try:
        document = json.loads(raw_text)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise WatchConfigImportError(gettext("Unable to read JSON file, was it broken?")) from e

    # A bare top-level list is friendlier for hand-written files.
    if isinstance(document, list):
        entries = document
    elif isinstance(document.get('watches'), list):
        entries = document['watches']
    else:
        raise WatchConfigImportError(
            gettext("JSON structure looks invalid: expected an object with a 'watches' list.")
        )

    if not entries:
        raise WatchConfigImportError(gettext("No watches found in the import file."))

    return entries


def find_watch_uuid_for_url(datastore, url):
    """UUID of the watch whose URL matches (case-insensitive), or None."""
    needle = url.lower()
    for uuid, watch in datastore.data['watching'].items():
        if watch.get('url', '').lower() == needle:
            return uuid
    return None


def _sanitize_entry(entry):
    """Split a raw entry into (url, safe_extras, tag_titles, unknown_fields).

    Only known configuration fields survive; state/read-only fields and anything
    unrecognised are reported back instead of being silently stored.
    """
    unknown = sorted(
        k
        for k in entry.keys()
        if k not in WATCH_CONFIG_FIELDS and k not in ('url', 'title', 'tags')
    )

    extras = {}
    for field in WATCH_CONFIG_FIELDS:
        if field in entry:
            extras[field] = copy.deepcopy(entry[field])

    if entry.get('title'):
        extras['title'] = entry['title']

    raw_tags = entry.get('tags') or []
    if isinstance(raw_tags, str):
        tag_titles = [t.strip() for t in raw_tags.split(',') if t.strip()]
    elif isinstance(raw_tags, list):
        tag_titles = [str(t).strip() for t in raw_tags if str(t).strip()]
    else:
        tag_titles = []

    return extras, tag_titles, unknown


def _validate_processor_and_fetcher(extras):
    """Reject unknown processor/fetcher names. Returns an error string or None."""
    if extras.get('processor'):
        from changedetectionio.processors import available_processors

        known_processors = [name for name, _description in available_processors()]
        if extras['processor'] not in known_processors:
            return gettext("Unknown processor '%(name)s'.", name=extras['processor'])

    if extras.get('fetch_backend'):
        from changedetectionio.content_fetchers import available_fetchers

        known_fetchers = [name for name, _description in available_fetchers()]
        backend = extras['fetch_backend']
        if (
            backend != 'system'
            and backend not in known_fetchers
            and not backend.startswith('extra_browser_')
        ):
            return gettext("Unknown fetch backend '%(name)s'.", name=backend)

    return None


def _validate_notifications(extras):
    """Reject invalid Apprise URLs. Returns an error string or None."""
    if 'notification_urls' not in extras:
        return None

    from wtforms import ValidationError

    from changedetectionio.api.Notifications import validate_notification_urls

    try:
        validate_notification_urls(extras.get('notification_urls') or [])
    except ValidationError as e:
        return str(e)
    return None


def _validate_entry(url, extras):
    """Run the same boundary checks the API/UI use. Returns an error string or None."""
    from changedetectionio.api.operations import ApiError, validate_against_spec
    from changedetectionio.api.Watch import validate_jinja2_templates, validate_time_schedule_limit
    from changedetectionio.model.Watch import validate_time_between_check_required
    from changedetectionio.validate_url import is_safe_valid_url

    if not url or not isinstance(url, str):
        return gettext("Entry is missing a 'url' field.")

    url = url.strip()
    if not is_safe_valid_url(url):
        return gettext("Invalid or unsupported URL.")

    # An unknown processor name would later be used to build a config filename, so reject
    # it at the boundary instead of storing it (same check the REST import endpoint runs).
    error = _validate_processor_and_fetcher(extras)
    if error:
        return error

    error = validate_jinja2_templates(dict(extras, url=url))
    if error:
        return error

    error = validate_time_schedule_limit(extras)
    if error:
        return error

    error = validate_time_between_check_required(extras)
    if error:
        return error

    error = _validate_notifications(extras)
    if error:
        return error

    # Full type/length/enum check against the same OpenAPI schema the REST API enforces.
    # Unknown fields were already stripped in _sanitize_entry(), so this only sees known fields.
    try:
        validate_against_spec(dict(extras, url=url), 'CreateWatch')
    except ApiError as e:
        return e.message

    return None


def import_watch_config(datastore, entries, flash=None):
    """Import watch configuration entries.

    For each entry:
      * existing URL (case-insensitive) -> its configuration is updated, nothing
        duplicated;
      * new URL -> a watch is created;
      * any error -> recorded against that URL and skipped, the rest of the batch
        continues. There is deliberately no whole-batch rollback.

    Returns a result dict:
      {
        'created': [uuids], 'updated': [uuids],
        'errors': [{'url', 'message'}], 'ignored_unknown': [(url, [fields])],
      }
    """
    result = {
        'created': [],
        'updated': [],
        'errors': [],
        'ignored_unknown': [],
    }

    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            result['errors'].append(
                {
                    'url': f"#{position}",
                    'message': gettext("Entry is not a JSON object, it was skipped."),
                }
            )
            continue

        url = (
            (entry.get('url') or '').strip()
            if isinstance(entry.get('url'), str)
            else entry.get('url')
        )
        label = url or f"#{position}"

        try:
            extras, tag_titles, unknown_fields = _sanitize_entry(entry)
            if unknown_fields:
                result['ignored_unknown'].append((label, unknown_fields))

            error = _validate_entry(url, extras)
            if error:
                result['errors'].append({'url': label, 'message': error})
                continue

            existing_uuid = find_watch_uuid_for_url(datastore, url)
            if existing_uuid:
                _apply_update(datastore, existing_uuid, extras)
                if tag_titles:
                    _apply_tags(datastore, existing_uuid, tag_titles)
                result['updated'].append(existing_uuid)
                logger.info(f"Watch config import: updated existing watch {existing_uuid} ({url})")
            else:
                if datastore.watch_limit_reached():
                    result['errors'].append(
                        {
                            'url': label,
                            'message': datastore.watch_limit_message(),
                        }
                    )
                    continue

                new_uuid = datastore.add_watch(url=url, extras=extras, save_immediately=True)
                if not new_uuid:
                    result['errors'].append(
                        {
                            'url': label,
                            'message': gettext("Watch could not be created, it was skipped."),
                        }
                    )
                    continue
                if tag_titles:
                    _apply_tags(datastore, new_uuid, tag_titles)
                result['created'].append(new_uuid)
                logger.info(f"Watch config import: created watch {new_uuid} ({url})")

        except Exception as e:
            # Per-item isolation: one broken entry must never abort the others.
            logger.exception(f"Watch config import failed for {label}: {e}")
            result['errors'].append({'url': label, 'message': str(e)})

    if flash is not None:
        _flash_summary(flash, result)

    return result


def _apply_update(datastore, uuid, extras):
    """Merge imported configuration onto an existing watch, keeping its identity/state."""
    watch = datastore.data['watching'][uuid]

    # Never let an import overwrite the identity fields or runtime state.
    extras = {k: v for k, v in extras.items() if k not in _NEVER_IMPORTABLE}
    watch.update(extras)
    watch.ensure_valid_time_between_check()
    watch.commit()


def _apply_tags(datastore, uuid, tag_titles):
    """Resolve tag titles to tag UUIDs (creating missing groups) and attach them."""
    tag_uuids = []
    for title in tag_titles:
        tag_uuid = datastore.tag_uuid_for_title(title) or datastore.add_tag(title)
        if tag_uuid:
            tag_uuids.append(tag_uuid)

    if tag_uuids:
        watch = datastore.data['watching'][uuid]
        watch['tags'] = sorted(set(tag_uuids))
        watch.commit()


def _flash_summary(flash, result):
    """Push a human-readable summary, plus one error flash per failed entry."""
    created = len(result['created'])
    updated = len(result['updated'])
    failed = len(result['errors'])

    flash(
        gettext(
            "Watch config import finished: %(created)d created, %(updated)d updated, %(failed)d failed.",
            created=created,
            updated=updated,
            failed=failed,
        )
    )

    for error in result['errors']:
        flash(
            gettext(
                "Import failed for %(url)s: %(message)s", url=error['url'], message=error['message']
            ),
            'error',
        )

    for url, fields in result['ignored_unknown']:
        flash(
            gettext(
                "Ignored unknown field(s) %(fields)s for %(url)s.",
                fields=', '.join(fields),
                url=url,
            ),
            'error',
        )
