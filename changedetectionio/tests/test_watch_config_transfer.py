#!/usr/bin/env python3
"""Tests for the portable watch-config JSON import/export (blueprint/imports/watch_config.py).

Covers:
  - export shape and the runtime-state fields it must never leak
  - import creates new watches with full config (selectors, filters, notifications)
  - re-importing the same document updates by URL instead of duplicating
  - per-entry failures are reported without rolling back successful entries
  - tag titles survive an export -> import round-trip (no UUIDs travel in the file)
"""

import io
import json

from flask import url_for

from .util import delete_all_watches


def _add_configured_watch(datastore, url, **extras):
    extras.setdefault('include_filters', ['.product-price'])
    extras.setdefault('subtractive_selectors', ['.ads'])
    extras.setdefault('ignore_text', ['Advertisement'])
    extras.setdefault('notification_urls', ['json://example.com/notify'])
    extras.setdefault('notification_title', 'Price changed for {{ watch_title }}')
    return datastore.add_watch(url=url, extras=extras)


def test_export_watch_config_document_shape(
    client, live_server, measure_memory_usage, datastore_path
):
    datastore = live_server.app.config['DATASTORE']
    _add_configured_watch(datastore, 'https://example.com/product-1')

    res = client.get(url_for('imports.export_watch_config'))
    assert res.status_code == 200
    assert res.content_type.startswith('application/json')
    assert 'attachment; filename="watch-config.json"' in res.headers.get('Content-Disposition', '')

    document = json.loads(res.data)
    assert document['type'] == 'changedetectionio-watch-config'
    assert document['version'] == 1
    assert document['watch_count'] == 1
    entry = document['watches'][0]

    # The configuration the user asked for must travel with the export
    assert entry['url'] == 'https://example.com/product-1'
    assert entry['include_filters'] == ['.product-price']
    assert entry['subtractive_selectors'] == ['.ads']
    assert entry['ignore_text'] == ['Advertisement']
    assert entry['notification_urls'] == ['json://example.com/notify']
    assert entry['notification_title'] == 'Price changed for {{ watch_title }}'

    # Runtime/machine state must never be part of a portable config
    for forbidden in (
        'uuid',
        'history',
        'last_checked',
        'last_changed',
        'previous_md5',
        'check_count',
        'viewed',
        'last_error',
        'proxy',
        'tags',
    ):
        assert forbidden not in entry, f"{forbidden} must not be exported"

    # Round-trip parse of the exact bytes we served
    from changedetectionio.blueprint.imports.watch_config import parse_export_document

    assert len(parse_export_document(res.data)) == 1

    delete_all_watches(client)


def test_export_includes_tags_by_title(client, live_server, measure_memory_usage, datastore_path):
    datastore = live_server.app.config['DATASTORE']
    tag_uuid = datastore.add_tag(title="Export group")
    uuid = _add_configured_watch(datastore, 'https://example.com/tagged')
    datastore.update_watch(uuid, {'tags': [tag_uuid]})

    document = json.loads(client.get(url_for('imports.export_watch_config')).data)
    entry = document['watches'][0]
    # Tag UUIDs are machine-local; titles are the portable representation
    assert entry['tags'] == ['Export group']
    assert tag_uuid not in json.dumps(document)

    delete_all_watches(client)


def test_import_creates_new_watches_from_json(
    client, live_server, measure_memory_usage, datastore_path
):
    datastore = live_server.app.config['DATASTORE']
    payload = {
        'type': 'changedetectionio-watch-config',
        'version': 1,
        'watches': [
            {
                'url': 'https://example.com/new-watch',
                'title': 'Imported watch',
                'include_filters': ['.main'],
                'subtractive_selectors': ['.banner'],
                'ignore_text': ['noise'],
                'notification_urls': ['json://example.com/hook'],
                'notification_muted': True,
            }
        ],
    }

    res = client.post(
        url_for('imports.import_watch_config_page'),
        data={'watch_config_file': (io.BytesIO(json.dumps(payload).encode()), 'watch-config.json')},
        content_type='multipart/form-data',
        follow_redirects=True,
    )
    assert res.status_code == 200
    assert b'1 created' in res.data
    assert b'0 updated' in res.data

    watches = datastore.data['watching']
    assert len(watches) == 1
    watch = next(iter(watches.values()))
    assert watch['url'] == 'https://example.com/new-watch'
    assert watch['title'] == 'Imported watch'
    assert watch['include_filters'] == ['.main']
    assert watch['subtractive_selectors'] == ['.banner']
    assert watch['ignore_text'] == ['noise']
    assert watch['notification_urls'] == ['json://example.com/hook']
    assert watch['notification_muted'] is True

    delete_all_watches(client)


def test_import_updates_existing_watch_by_url_without_duplicate(
    client, live_server, measure_memory_usage, datastore_path
):
    datastore = live_server.app.config['DATASTORE']
    original_uuid = _add_configured_watch(
        datastore,
        'https://example.com/dup',
        title='Before import',
        include_filters=['.old-selector'],
    )

    payload = {
        'watches': [
            {
                'url': 'https://example.com/dup',
                'title': 'After import',
                'include_filters': ['.new-selector'],
                'notification_urls': ['json://example.com/new-hook'],
            }
        ]
    }

    res = client.post(
        url_for('imports.import_watch_config_page'),
        data={'watch_config_file': (io.BytesIO(json.dumps(payload).encode()), 'watch-config.json')},
        content_type='multipart/form-data',
        follow_redirects=True,
    )

    # Updated, not created - still exactly one watch with the same UUID
    assert b'0 created' in res.data
    assert b'1 updated' in res.data
    assert len(datastore.data['watching']) == 1
    watch = datastore.data['watching'][original_uuid]
    assert watch['title'] == 'After import'
    assert watch['include_filters'] == ['.new-selector']
    assert watch['notification_urls'] == ['json://example.com/new-hook']

    delete_all_watches(client)


def test_import_url_match_is_case_insensitive(
    client, live_server, measure_memory_usage, datastore_path
):
    datastore = live_server.app.config['DATASTORE']
    original_uuid = _add_configured_watch(datastore, 'https://example.com/CasePath')

    payload = {'watches': [{'url': 'HTTPS://EXAMPLE.COM/casepath', 'title': 'Updated'}]}
    client.post(
        url_for('imports.import_watch_config_page'),
        data={'watch_config_file': (io.BytesIO(json.dumps(payload).encode()), 'watch-config.json')},
        content_type='multipart/form-data',
        follow_redirects=True,
    )

    assert len(datastore.data['watching']) == 1
    assert original_uuid in datastore.data['watching']
    assert datastore.data['watching'][original_uuid]['title'] == 'Updated'

    delete_all_watches(client)


def test_import_reports_per_entry_failures_without_rollback(
    client, live_server, measure_memory_usage, datastore_path
):
    datastore = live_server.app.config['DATASTORE']
    payload = {
        'watches': [
            # Good entry - must be kept even though a later entry fails
            {'url': 'https://example.com/good', 'title': 'Good one'},
            # Bad URL - reported per entry, does not abort the batch
            {'url': 'not-a-valid-url', 'title': 'Broken one'},
            # Another good entry proving the batch kept going after the failure
            {'url': 'https://example.com/good-two', 'title': 'Good two'},
        ]
    }

    res = client.post(
        url_for('imports.import_watch_config_page'),
        data={'watch_config_file': (io.BytesIO(json.dumps(payload).encode()), 'watch-config.json')},
        content_type='multipart/form-data',
        follow_redirects=True,
    )

    assert b'2 created' in res.data
    assert b'1 failed' in res.data
    # The failure is reported against the offending URL
    assert b'not-a-valid-url' in res.data
    assert b'Import failed for' in res.data

    urls = sorted(w['url'] for w in datastore.data['watching'].values())
    assert urls == ['https://example.com/good', 'https://example.com/good-two']

    delete_all_watches(client)


def test_import_invalid_json_does_not_touch_datastore(
    client, live_server, measure_memory_usage, datastore_path
):
    datastore = live_server.app.config['DATASTORE']
    _add_configured_watch(datastore, 'https://example.com/keep')
    before = set(datastore.data['watching'].keys())

    res = client.post(
        url_for('imports.import_watch_config_page'),
        data={'watch_config_file': (io.BytesIO(b'{this is not json'), 'watch-config.json')},
        content_type='multipart/form-data',
        follow_redirects=True,
    )

    assert b'Unable to read JSON' in res.data
    assert set(datastore.data['watching'].keys()) == before

    delete_all_watches(client)


def test_import_round_trip_export_then_import_on_empty_datastore(
    client, live_server, measure_memory_usage, datastore_path
):
    datastore = live_server.app.config['DATASTORE']
    _add_configured_watch(datastore, 'https://example.com/round-trip', title='Round trip')
    export_bytes = client.get(url_for('imports.export_watch_config')).data

    delete_all_watches(client)
    assert len(datastore.data['watching']) == 0

    res = client.post(
        url_for('imports.import_watch_config_page'),
        data={'watch_config_file': (io.BytesIO(export_bytes), 'watch-config.json')},
        content_type='multipart/form-data',
        follow_redirects=True,
    )

    assert b'1 created' in res.data
    watch = next(iter(datastore.data['watching'].values()))
    assert watch['url'] == 'https://example.com/round-trip'
    assert watch['title'] == 'Round trip'
    assert watch['include_filters'] == ['.product-price']
    assert watch['notification_urls'] == ['json://example.com/notify']

    delete_all_watches(client)


def test_check_interval_survives_round_trip(
    client, live_server, measure_memory_usage, datastore_path
):
    """A custom check interval has historically been mangled by migrations - guard it here."""
    datastore = live_server.app.config['DATASTORE']
    custom_interval = {'weeks': 1, 'days': 2, 'hours': 3, 'minutes': 4, 'seconds': 5}
    datastore.add_watch(
        url='https://example.com/interval',
        extras={
            'time_between_check_use_default': False,
            'time_between_check': custom_interval,
        },
    )

    export_bytes = client.get(url_for('imports.export_watch_config')).data
    entry = json.loads(export_bytes)['watches'][0]
    assert entry['time_between_check'] == custom_interval
    assert entry['time_between_check_use_default'] is False

    delete_all_watches(client)
    client.post(
        url_for('imports.import_watch_config_page'),
        data={'watch_config_file': (io.BytesIO(export_bytes), 'watch-config.json')},
        content_type='multipart/form-data',
        follow_redirects=True,
    )

    watch = next(iter(datastore.data['watching'].values()))
    assert watch['time_between_check'] == custom_interval
    assert watch['time_between_check_use_default'] is False

    delete_all_watches(client)


def test_import_accepts_pasted_json_without_file(
    client, live_server, measure_memory_usage, datastore_path
):
    datastore = live_server.app.config['DATASTORE']
    payload = json.dumps({'watches': [{'url': 'https://example.com/pasted'}]})

    res = client.post(
        url_for('imports.import_watch_config_page'),
        data={'watch_config_json': payload},
        follow_redirects=True,
    )

    assert res.status_code == 200
    assert len(datastore.data['watching']) == 1
    assert next(iter(datastore.data['watching'].values()))['url'] == 'https://example.com/pasted'

    delete_all_watches(client)


def test_import_accepts_bare_top_level_list(
    client, live_server, measure_memory_usage, datastore_path
):
    datastore = live_server.app.config['DATASTORE']
    payload = json.dumps([{'url': 'https://example.com/bare-list'}])

    res = client.post(
        url_for('imports.import_watch_config_page'),
        data={'watch_config_file': (io.BytesIO(payload.encode()), 'watch-config.json')},
        content_type='multipart/form-data',
        follow_redirects=True,
    )

    assert b'1 created' in res.data
    assert next(iter(datastore.data['watching'].values()))['url'] == 'https://example.com/bare-list'

    delete_all_watches(client)


def test_import_wrong_field_type_is_reported_per_entry(
    client, live_server, measure_memory_usage, datastore_path
):
    datastore = live_server.app.config['DATASTORE']
    payload = {
        'watches': [
            # include_filters must be an array; a string fails the OpenAPI schema check
            {'url': 'https://example.com/bad-types', 'include_filters': 'not-a-list'},
            {'url': 'https://example.com/good-after-bad-types'},
        ]
    }

    res = client.post(
        url_for('imports.import_watch_config_page'),
        data={'watch_config_file': (io.BytesIO(json.dumps(payload).encode()), 'watch-config.json')},
        content_type='multipart/form-data',
        follow_redirects=True,
    )

    assert b'1 created' in res.data
    assert b'1 failed' in res.data
    assert b'https://example.com/bad-types' in res.data
    urls = sorted(w['url'] for w in datastore.data['watching'].values())
    assert urls == ['https://example.com/good-after-bad-types']

    delete_all_watches(client)


def test_import_without_any_payload_flashes_error(
    client, live_server, measure_memory_usage, datastore_path
):
    res = client.post(
        url_for('imports.import_watch_config_page'),
        data={},
        follow_redirects=True,
    )
    assert b'No watch config file selected' in res.data


def test_import_recreates_tags_from_titles(
    client, live_server, measure_memory_usage, datastore_path
):
    datastore = live_server.app.config['DATASTORE']
    payload = {
        'watches': [
            {
                'url': 'https://example.com/with-tags',
                'tags': ['Migrated group', 'Second group'],
            }
        ]
    }

    client.post(
        url_for('imports.import_watch_config_page'),
        data={'watch_config_file': (io.BytesIO(json.dumps(payload).encode()), 'watch-config.json')},
        content_type='multipart/form-data',
        follow_redirects=True,
    )

    watch = next(iter(datastore.data['watching'].values()))
    tags = datastore.data['settings']['application']['tags']
    titles = sorted(tags[uuid]['title'] for uuid in watch['tags'])
    assert titles == ['Migrated group', 'Second group']

    delete_all_watches(client)


def test_import_unknown_fields_are_reported_not_stored(
    client, live_server, measure_memory_usage, datastore_path
):
    datastore = live_server.app.config['DATASTORE']
    payload = {
        'watches': [
            {
                'url': 'https://example.com/unknown-fields',
                'include_filters': ['.ok'],
                'uuid': '11111111-1111-4111-8111-111111111111',
                'previous_md5': 'deadbeef',
                'totally_made_up': 'x',
            }
        ]
    }

    res = client.post(
        url_for('imports.import_watch_config_page'),
        data={'watch_config_file': (io.BytesIO(json.dumps(payload).encode()), 'watch-config.json')},
        content_type='multipart/form-data',
        follow_redirects=True,
    )

    # The watch itself was still created with the valid field
    assert b'1 created' in res.data
    assert b'Ignored unknown field' in res.data
    watch = next(iter(datastore.data['watching'].values()))
    assert watch['uuid'] != '11111111-1111-4111-8111-111111111111'
    assert watch.get('previous_md5') in (False, None)
    assert 'totally_made_up' not in watch
    assert watch['include_filters'] == ['.ok']

    delete_all_watches(client)


def test_import_unknown_processor_and_fetcher_are_rejected_per_entry(
    client, live_server, measure_memory_usage, datastore_path
):
    datastore = live_server.app.config['DATASTORE']
    payload = {
        'watches': [
            {'url': 'https://example.com/bad-processor', 'processor': 'not-a-real-processor'},
            {'url': 'https://example.com/bad-fetcher', 'fetch_backend': 'html_magic'},
            # A valid entry in the same batch must still go through
            {'url': 'https://example.com/still-good'},
        ]
    }

    res = client.post(
        url_for('imports.import_watch_config_page'),
        data={'watch_config_file': (io.BytesIO(json.dumps(payload).encode()), 'watch-config.json')},
        content_type='multipart/form-data',
        follow_redirects=True,
    )

    assert b'1 created' in res.data
    assert b'2 failed' in res.data
    assert b'Unknown processor' in res.data
    assert b'Unknown fetch backend' in res.data

    urls = sorted(w['url'] for w in datastore.data['watching'].values())
    assert urls == ['https://example.com/still-good']

    delete_all_watches(client)


def test_import_mixed_create_and_update_in_one_batch(
    client, live_server, measure_memory_usage, datastore_path
):
    datastore = live_server.app.config['DATASTORE']
    existing_uuid = _add_configured_watch(datastore, 'https://example.com/existing', title='Old')

    payload = {
        'watches': [
            {'url': 'https://example.com/existing', 'title': 'New'},
            {'url': 'https://example.com/brand-new', 'title': 'Fresh'},
        ]
    }
    res = client.post(
        url_for('imports.import_watch_config_page'),
        data={'watch_config_file': (io.BytesIO(json.dumps(payload).encode()), 'watch-config.json')},
        content_type='multipart/form-data',
        follow_redirects=True,
    )

    assert b'1 created' in res.data
    assert b'1 updated' in res.data
    assert len(datastore.data['watching']) == 2
    assert datastore.data['watching'][existing_uuid]['title'] == 'New'

    delete_all_watches(client)
