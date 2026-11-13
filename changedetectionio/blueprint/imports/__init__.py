from flask import Blueprint, flash, make_response, redirect, render_template, request, url_for
from flask_babel import gettext
from loguru import logger

from changedetectionio.auth_decorator import login_optionally_required
from changedetectionio.store import ChangeDetectionStore


def _read_watch_config_payload():
    """Extract the watch-config JSON text from either an uploaded file or a pasted form field.

    Returns (raw_text, error_redirect): error_redirect is a redirect response when the
    upload itself could not be read, otherwise None.
    """
    upload = request.files.get('watch_config_file')
    if upload and upload.filename:
        try:
            return upload.read().decode('utf-8', errors='replace'), None
        except OSError as e:
            logger.error(f"Could not read watch config upload: {e}")
            flash(str(e), 'error')
            return '', redirect(url_for('imports.import_page'))

    # Also accept pasted JSON so the page works without a file picker.
    return request.values.get('watch_config_json') or '', None


def export_watch_config_response(datastore):
    """Build the downloadable watch-config JSON response (view logic, kept out of the blueprint factory)."""
    import json as json_module

    from changedetectionio.blueprint.imports.watch_config import build_export_document

    document = build_export_document(datastore)
    response = make_response(json_module.dumps(document, indent=2, ensure_ascii=False))
    response.headers['Content-Type'] = 'application/json; charset=utf-8'
    response.headers['Content-Disposition'] = 'attachment; filename="watch-config.json"'
    return response


def import_watch_config_response(datastore):
    """Handle a watch-config JSON upload: update by URL, create the rest, report each failure on its own."""
    from changedetectionio.blueprint.imports.watch_config import (
        WatchConfigImportError,
        import_watch_config,
        parse_export_document,
    )

    raw_text, error_redirect = _read_watch_config_payload()
    if error_redirect:
        return error_redirect

    if not raw_text.strip():
        flash(
            gettext("No watch config file selected, choose a .json export file to import."), 'error'
        )
        return redirect(url_for('imports.import_page'))

    try:
        entries = parse_export_document(raw_text)
    except WatchConfigImportError as e:
        flash(str(e), 'error')
        return redirect(url_for('imports.import_page'))

    import_watch_config(datastore=datastore, entries=entries, flash=flash)
    return redirect(url_for('watchlist.index'))


def construct_blueprint(datastore: ChangeDetectionStore, update_q, queuedWatchMetaData):
    import_blueprint = Blueprint('imports', __name__, template_folder="templates")

    @import_blueprint.route("/export/watch-config.json", methods=['GET'])
    @login_optionally_required
    def export_watch_config():
        """Download every watch's portable configuration (URL, selectors, filters, notifications) as JSON."""
        return export_watch_config_response(datastore)

    @import_blueprint.route("/import/watch-config", methods=['POST'])
    @login_optionally_required
    def import_watch_config_page():
        """Import a watch-config JSON export (matched by URL, per-entry failures, no batch rollback)."""
        return import_watch_config_response(datastore)

    @import_blueprint.route("/import", methods=['GET', 'POST'])
    @login_optionally_required
    def import_page():
        remaining_urls = []
        from changedetectionio import forms
#
        if request.method == 'POST':
#            from changedetectionio import worker_pool

            from changedetectionio.blueprint.imports.importer import (
                import_distill_io_json,
                import_url_list,
                import_xlsx_custom,
                import_xlsx_wachete,
            )

            # URL List import
            if request.values.get('urls') and len(request.values.get('urls').strip()):
                # Import and push into the queue for immediate update check
                from changedetectionio import processors
                importer_handler = import_url_list()
                importer_handler.run(data=request.values.get('urls'), flash=flash, datastore=datastore, processor=request.values.get('processor', processors.get_default_processor()))
                logger.debug(f"Imported {len(importer_handler.new_uuids)} new UUIDs")
                # Dont' add to queue because scheduler can see that they haven't been checked and will add them to the queue
#                for uuid in importer_handler.new_uuids:
#                    worker_pool.queue_item_async_safe(update_q, queuedWatchMetaData.PrioritizedItem(priority=1, item={'uuid': uuid}))

                if len(importer_handler.remaining_data) == 0:
                    return redirect(url_for('watchlist.index'))
                else:
                    remaining_urls = importer_handler.remaining_data

            # Distill.io import
            if request.values.get('distill-io') and len(request.values.get('distill-io').strip()):
                # Import and push into the queue for immediate update check
                d_importer = import_distill_io_json()
                d_importer.run(data=request.values.get('distill-io'), flash=flash, datastore=datastore)
                # Dont' add to queue because scheduler can see that they haven't been checked and will add them to the queue
#                for uuid in importer_handler.new_uuids:
#                    worker_pool.queue_item_async_safe(update_q, queuedWatchMetaData.PrioritizedItem(priority=1, item={'uuid': uuid}))


            # XLSX importer
            if request.files and request.files.get('xlsx_file'):
                file = request.files['xlsx_file']

                if request.values.get('file_mapping') == 'wachete':
                    w_importer = import_xlsx_wachete()
                    w_importer.run(data=file, flash=flash, datastore=datastore)
                else:
                    w_importer = import_xlsx_custom()
                    # Building mapping of col # to col # type
                    map = {}
                    for i in range(10):
                        c = request.values.get(f"custom_xlsx[col_{i}]")
                        v = request.values.get(f"custom_xlsx[col_type_{i}]")
                        if c and v:
                            map[int(c)] = v

                    w_importer.import_profile = map
                    w_importer.run(data=file, flash=flash, datastore=datastore)

                # Dont' add to queue because scheduler can see that they haven't been checked and will add them to the queue
#                for uuid in importer_handler.new_uuids:
#                    worker_pool.queue_item_async_safe(update_q, queuedWatchMetaData.PrioritizedItem(priority=1, item={'uuid': uuid}))


        # Could be some remaining, or we could be on GET
        form = forms.importForm(formdata=request.form if request.method == 'POST' else None)
        output = render_template("import.html",
                                form=form,
                                import_url_list_remaining="\n".join(remaining_urls),
                                original_distill_json=''
                                )
        return output

    return import_blueprint
