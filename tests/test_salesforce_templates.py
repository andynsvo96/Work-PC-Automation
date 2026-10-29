from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from flask import Flask

from routes.salesforce_template_routes import register_salesforce_template_routes
from salesforce_templates import SalesforceTemplateStore, TemplateConflict, TemplateLibraryError, TemplateNotFound


SOURCE = '<html><head><title></title></head><body><p>Hello &rsquo; [REQUEST]</p><a href="https://example.test/?a=1&amp;b=2">Invoice</a><img src="https://example.test/logo.png" alt="Logo"></body></html>\n'


class TemplateFixtures:
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.capture = self.root / "captures" / "2026-10-27" / "email_templates.json"
        self.capture.parent.mkdir(parents=True)
        self.snapshot = {
            "[AUTO] First": {"name": "[AUTO] First", "salesforce_id": "template_a", "subject": "Order #[ORDER-NUMBER]", "body_html": SOURCE},
            "[AUTO] Second": {"name": "[AUTO] Second", "salesforce_id": "template_b", "subject": "Other order", "body_html": "<p>Other body</p>"},
        }
        self.capture.write_text(json.dumps(self.snapshot), encoding="utf-8")
        self.path = self.root / "salesforce_templates.json"
        self.store = SalesforceTemplateStore(self.path, self.root / "captures")

class TemplateStoreTests(TemplateFixtures, unittest.TestCase):
    def test_initialize_preserves_source_without_editing_capture(self):
        before = self.capture.read_bytes()
        rows = self.store.list_templates()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["body_html"], SOURCE)
        self.assertEqual(self.capture.read_bytes(), before)
        self.assertEqual(set(rows[0]), {"id", "name", "subject", "body_html", "revision"})
        self.assertNotIn("revision", json.loads(self.path.read_text())["templates"][0])

    def test_rename_and_save_keep_identity_other_templates_and_backup(self):
        first, second = self.store.list_templates()
        before = self.path.read_bytes()
        changed = {**first, "name": "[AUTO] Renamed", "subject": "Updated title", "body_html": SOURCE + "<p>New paragraph</p>"}
        saved = self.store.save_template(first["id"], changed)
        self.assertEqual(saved["id"], first["id"])
        self.assertNotEqual(saved["revision"], first["revision"])
        reopened = SalesforceTemplateStore(self.path).list_templates()
        self.assertIn(saved, reopened)
        self.assertIn(second, reopened)
        self.assertEqual(self.path.with_suffix(".json.bak").read_bytes(), before)
        self.assertEqual(json.loads(self.capture.read_text()), self.snapshot)

    def test_saving_unchanged_fields_does_not_rewrite_or_backup(self):
        first = self.store.list_templates()[0]
        with mock.patch.object(self.store, "_write") as write:
            self.assertEqual(self.store.save_template(first["id"], first), first)
        write.assert_not_called()

    def test_external_change_is_not_overwritten_by_stale_tab(self):
        first = self.store.list_templates()[0]
        manual = json.loads(self.path.read_text())
        manual["templates"][0]["subject"] = "Changed outside this tab"
        self.path.write_text(json.dumps(manual), encoding="utf-8")
        before = self.path.read_bytes()
        with self.assertRaises(TemplateConflict):
            self.store.save_template(first["id"], {**first, "subject": "Stale edit"})
        self.assertEqual(self.path.read_bytes(), before)

    def test_corrupt_active_file_is_not_replaced_from_capture(self):
        self.store.list_templates()
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(TemplateLibraryError):
            self.store.list_templates()
        self.assertEqual(self.path.read_text(), "{broken")

    def test_validation_never_changes_disk(self):
        first, second = self.store.list_templates()
        before = self.path.read_bytes()
        for changes in ({"name": "  "}, {"subject": "one\ntwo"}, {"body_html": ""},
                        {"name": second["name"].lower()}, {"revision": ""}):
            with self.subTest(changes=changes), self.assertRaises(TemplateLibraryError):
                self.store.save_template(first["id"], {**first, **changes})
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_replace_retains_original_and_cleans_temporary_files(self):
        first = self.store.list_templates()[0]
        before = self.path.read_bytes()
        import os
        real_replace = os.replace

        def replace(source, target):
            if Path(target) == self.path:
                raise OSError("simulated disk failure")
            return real_replace(source, target)

        with mock.patch("salesforce_templates.os.replace", side_effect=replace):
            with self.assertRaises(OSError):
                self.store.save_template(first["id"], {**first, "subject": "New"})
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.with_suffix(".json.bak").read_bytes(), before)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_simultaneous_edits_to_different_templates_are_both_kept(self):
        rows = self.store.list_templates()
        ready = threading.Barrier(2)

        def save(row):
            ready.wait(timeout=5)
            return self.store.save_template(row["id"], {**row, "subject": row["subject"] + " edited"})

        with ThreadPoolExecutor(max_workers=2) as pool:
            saved = list(pool.map(save, rows))
        self.assertCountEqual(self.store.list_templates(), saved)

    def test_missing_library_returns_empty_without_creating_placeholder_data(self):
        store = SalesforceTemplateStore(self.root / "missing.json")
        self.assertEqual(store.list_templates(), [])
        self.assertFalse(store.path.exists())

    def test_worker_lookup_uses_exact_id_or_case_insensitive_name_and_latest_saved_content(self):
        first = self.store.get_template(" [auto] FIRST ")
        self.assertEqual(self.store.get_template("template_a"), first)
        saved = self.store.save_template(first['id'], {**first, 'subject': 'Latest saved subject'})
        self.assertEqual(SalesforceTemplateStore(self.path).get_template('[AUTO] First'), saved)
        for reference in ('First', '[AUTO] Fir', ''):
            with self.subTest(reference=reference), self.assertRaises(TemplateNotFound):
                self.store.get_template(reference)

    def test_renames_keep_worker_reference_bound_to_the_same_id(self):
        first, second = self.store.list_templates()
        renamed = self.store.save_template(first['id'], {**first, 'name': '[AUTO] Renamed'})
        again = self.store.save_template(first['id'], {**renamed, 'name': '[AUTO] Final name'})
        for reference in (first['name'], renamed['name'], again['name'], first['id']):
            self.assertEqual(SalesforceTemplateStore(self.path).get_template(reference), again)
        with self.assertRaisesRegex(TemplateLibraryError, 'before a rename'):
            self.store.save_template(second['id'], {**second, 'name': first['name']})

    def test_ambiguous_aliases_and_missing_local_data_never_choose_another_template(self):
        self.store.list_templates()
        document = json.loads(self.path.read_text())
        document['templates'][1]['previous_names'] = ['[auto] first']
        self.path.write_text(json.dumps(document), encoding='utf-8')
        with self.assertRaises(TemplateLibraryError):
            self.store.get_template('[AUTO] First')
        with self.assertRaises(TemplateNotFound):
            SalesforceTemplateStore(self.root / 'absent.json').get_template('[AUTO] First')

    def test_create_copy_uses_new_identity_without_aliases_and_preserves_source(self):
        original, other = self.store.list_templates()
        renamed = self.store.save_template(original['id'], {**original, 'name': '[AUTO] Renamed'})
        before = self.path.read_bytes()
        copy = self.store.create_template({**renamed, 'name': '[AUTO] Renamed (copy)', 'previous_names': [original['name']]})
        self.assertNotEqual(copy['id'], original['id'])
        self.assertTrue(copy['id'].startswith('local_'))
        self.assertEqual(copy['subject'], renamed['subject'])
        self.assertEqual(copy['body_html'], SOURCE)
        self.assertEqual(self.store.get_template(original['name']), renamed)
        self.assertEqual(self.store.get_template(other['id']), other)
        self.assertNotIn('previous_names', next(row for row in json.loads(self.path.read_text())['templates'] if row['id']==copy['id']))
        self.assertEqual(self.path.with_suffix('.json.bak').read_bytes(), before)
        self.assertEqual(json.loads(self.capture.read_text()), self.snapshot)

    def test_create_rejects_name_collisions_including_previous_names_and_invalid_data(self):
        first = self.store.list_templates()[0]
        self.store.save_template(first['id'], {**first, 'name': 'Renamed'})
        before = self.path.read_bytes()
        for values in (None, [], {**first, 'name': first['name'].lower()}, {**first, 'name': 'RENAMED'},
                       {**first, 'name': 'New', 'body_html': ''}):
            with self.subTest(values=values), self.assertRaises(TemplateLibraryError):
                self.store.create_template(values)
            self.assertEqual(self.path.read_bytes(), before)

    def test_delete_checks_revision_and_keeps_last_template_deleted_across_reload(self):
        rows = self.store.list_templates()
        first = rows[0]
        edited = self.store.save_template(first['id'], {**first, 'subject': 'Edited'})
        before = self.path.read_bytes()
        with self.assertRaises(TemplateConflict):
            self.store.delete_template(first['id'], first)
        for values in (None, {}, [], {'revision': ''}):
            with self.subTest(values=values), self.assertRaises(TemplateLibraryError):
                self.store.delete_template(first['id'], values)
        self.assertEqual(self.path.read_bytes(), before)
        self.store.delete_template(first['id'], edited)
        self.assertEqual(self.path.with_suffix('.json.bak').read_bytes(), before)
        with self.assertRaises(TemplateNotFound):
            self.store.get_template(first['name'])
        self.store.delete_template(rows[1]['id'], rows[1])
        self.assertEqual(SalesforceTemplateStore(self.path, self.root/'captures').list_templates(), [])
        self.assertEqual(json.loads(self.path.read_text())['templates'], [])
        with self.assertRaises(TemplateNotFound):
            self.store.delete_template(first['id'], first)
        new = self.store.create_template({'name': 'Start again', 'subject': 'Title', 'body_html': SOURCE})
        self.assertEqual(self.store.list_templates(), [new])

    def test_create_and_delete_write_failure_never_change_the_active_library(self):
        first = self.store.list_templates()[0]
        before = self.path.read_bytes()
        import os
        real_replace = os.replace

        def replace(source, target):
            if Path(target) == self.path:
                raise OSError('simulated disk failure')
            return real_replace(source, target)

        with mock.patch('salesforce_templates.os.replace', side_effect=replace):
            with self.assertRaises(OSError):
                self.store.create_template({**first, 'name': 'New copy'})
            with self.assertRaises(OSError):
                self.store.delete_template(first['id'], first)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.with_suffix('.json.bak').read_bytes(), before)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_create_enforces_the_library_limit(self):
        document = {'schema_version': 1, 'templates': [
            {'id': 'id_'+str(index), 'name': 'Template '+str(index), 'subject': 'Title', 'body_html': SOURCE}
            for index in range(500)
        ]}
        self.path.write_text(json.dumps(document), encoding='utf-8')
        with self.assertRaisesRegex(TemplateLibraryError, 'at most 500'):
            self.store.create_template({'name': 'One too many', 'subject': 'Title', 'body_html': SOURCE})


class TemplateApiTests(TemplateFixtures, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.app = Flask(__name__)
        register_salesforce_template_routes(self.app, store=self.store)
        self.client = self.app.test_client()

    def test_list_edit_and_stale_save(self):
        rows = self.client.get("/api/salesforce-templates").get_json()["templates"]
        first = rows[0]
        response = self.client.post("/api/salesforce-templates/template_a", json={**first, "subject": "New title"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["template"]["subject"], "New title")
        response = self.client.post("/api/salesforce-templates/template_a", json={**first, "subject": "Stale"})
        self.assertEqual(response.status_code, 409)

    def test_invalid_payload_and_missing_template(self):
        first = self.store.list_templates()[0]
        for payload in (None, [], {"body_html": SOURCE}, {**first, "subject": "bad\nsubject"}):
            with self.subTest(payload=payload):
                response = self.client.post("/api/salesforce-templates/template_a", json=payload)
                self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/salesforce-templates/missing", json=first)
        self.assertEqual(response.status_code, 404)

    def test_write_failure_keeps_draft_error_readable(self):
        first = self.store.list_templates()[0]
        with mock.patch.object(self.store, "save_template", side_effect=OSError("private filesystem details")):
            response = self.client.post("/api/salesforce-templates/template_a", json=first)
        self.assertEqual(response.status_code, 500)
        self.assertIn("draft is still open", response.get_json()["message"])
        self.assertNotIn("private filesystem", response.get_data(as_text=True))

    def test_create_and_remove_routes_preserve_the_library_and_conflict_checks(self):
        original = self.store.list_templates()
        response = self.client.post('/api/salesforce-templates', json={'name': 'New template', 'subject': 'Title', 'body_html': SOURCE})
        self.assertEqual(response.status_code, 201)
        created = response.get_json()['template']
        path = '/api/salesforce-templates/'+created['id']
        self.assertEqual(len(self.client.get('/api/salesforce-templates').get_json()['templates']), 3)
        self.store.save_template(created['id'], {**created, 'subject': 'Changed elsewhere'})
        self.assertEqual(self.client.delete(path, json=created).status_code, 409)
        current = self.store.get_template(created['id'])
        self.assertEqual(self.client.delete(path, json=current).status_code, 200)
        self.assertEqual(self.client.delete(path, json=current).status_code, 404)
        self.assertEqual(self.store.list_templates(), original)

    def test_invalid_create_and_delete_requests_do_not_write(self):
        first = self.store.list_templates()[0]
        before = self.path.read_bytes()
        for values in (None, [], {}, {**first, 'name': first['name'].upper()}):
            self.assertEqual(self.client.post('/api/salesforce-templates', json=values).status_code, 400)
        for values in (None, [], {}):
            self.assertEqual(self.client.delete('/api/salesforce-templates/'+first['id'], json=values).status_code, 400)
        self.assertEqual(self.path.read_bytes(), before)
        with mock.patch.object(self.store, 'create_template', side_effect=OSError('private details')):
            response = self.client.post('/api/salesforce-templates', json=first)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('private details', response.get_data(as_text=True))
        with mock.patch.object(self.store, 'delete_template', side_effect=OSError('private details')):
            response = self.client.delete('/api/salesforce-templates/'+first['id'], json=first)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn('private details', response.get_data(as_text=True))


class TemplateAppSecurityTests(unittest.TestCase):
    def test_routes_require_the_existing_pin_csrf_and_version_checks(self):
        import server
        from app_security import create_app_security
        config = create_app_security("123456")
        with mock.patch.object(server, "APP_PIN_REQUIRED", True), \
                mock.patch.object(server, "app_security_config", config), \
                mock.patch.object(server, "app_security_initialization_error", None), \
                mock.patch.dict(server.app.config, {"SECRET_KEY": config.session_secret, "SESSION_COOKIE_SECURE": False}), \
                mock.patch.object(server, "_automation_version_block_reason", return_value=None), \
                mock.patch.object(server.salesforce_template_store, "list_templates", return_value=[]) as read, \
                mock.patch.object(server.salesforce_template_store, "save_template", return_value={}) as save:
            client = server.app.test_client()
            self.assertEqual(client.get("/api/salesforce-templates").status_code, 401)
            self.assertEqual(client.post("/api/salesforce-templates/example", json={}).status_code, 401)
            read.assert_not_called()
            save.assert_not_called()
            with client.session_transaction() as session:
                session["app_authenticated"] = True
                session["csrf_token"] = "fixture-token"
            self.assertEqual(client.get("/api/salesforce-templates").status_code, 200)
            self.assertEqual(client.post("/api/salesforce-templates/example", json={}).status_code, 403)
            save.assert_not_called()
            with mock.patch.object(server, "_automation_version_block_reason", return_value="fixture lock"):
                response = client.post("/api/salesforce-templates/example", json={},
                                       headers={"X-CSRF-Token": "fixture-token"})
                self.assertEqual(response.status_code, 409)
                save.assert_not_called()
            response = client.post("/api/salesforce-templates/example", json={},
                                   headers={"X-CSRF-Token": "fixture-token"})
            self.assertEqual(response.status_code, 200)
            save.assert_called_once()

    def test_create_and_remove_require_pin_csrf_and_version_checks_without_blocking_list_get(self):
        import server
        from app_security import create_app_security
        config = create_app_security('123456')
        with mock.patch.object(server,'APP_PIN_REQUIRED',True), \
                mock.patch.object(server,'app_security_config',config), \
                mock.patch.object(server,'app_security_initialization_error',None), \
                mock.patch.dict(server.app.config,{'SECRET_KEY':config.session_secret,'SESSION_COOKIE_SECURE':False}), \
                mock.patch.object(server,'_automation_version_block_reason',return_value=None), \
                mock.patch.object(server.salesforce_template_store,'list_templates',return_value=[]), \
                mock.patch.object(server.salesforce_template_store,'create_template',return_value={}) as create, \
                mock.patch.object(server.salesforce_template_store,'delete_template',return_value={}) as delete:
            client=server.app.test_client()
            requests=[('POST','/api/salesforce-templates',create,201),('DELETE','/api/salesforce-templates/example',delete,200)]
            for method,path,operation,success in requests:
                self.assertEqual(client.open(path,method=method,json={}).status_code,401)
                operation.assert_not_called()
            with client.session_transaction() as session:
                session['app_authenticated']=True
                session['csrf_token']='fixture-token'
            self.assertEqual(client.get('/api/salesforce-templates').status_code,200)
            for method,path,operation,success in requests:
                self.assertEqual(client.open(path,method=method,json={}).status_code,403)
                with mock.patch.object(server,'_automation_version_block_reason',return_value='fixture lock'):
                    response=client.open(path,method=method,json={},headers={'X-CSRF-Token':'fixture-token'})
                    self.assertEqual(response.status_code,409)
                    operation.assert_not_called()
                self.assertEqual(client.open(path,method=method,json={},headers={'X-CSRF-Token':'fixture-token'}).status_code,success)
                operation.assert_called_once()


if __name__ == "__main__":
    unittest.main()
