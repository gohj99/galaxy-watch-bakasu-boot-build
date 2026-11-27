import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from find_source_links import (Client, SamsungSearchParser, cached_archive_items, discover_archive_items, discover_rows, main, normalize_item,
                               read_dependencies, resolve, select_packages, summary,
                               validate_inputs)


BASE = 'SM-L305U_Opensource.zip'
DEVICE = 'SM-L315F_EUR_Opensource_dts.zip'
UPDATE = ('SM-L305U_Opensource_L305USQS3BZF4_L300XXS2BZF4_L300ZCS2BZF4_'
          'L305FXXS3BZF4_L305NKOS3BZF4_L310XXS2BZF4_L310ZCS2BZF4_'
          'L3150ZCS3BZF4_L315FXXS3BZF4_L315NKOS3BZF4_L315USQS3BZF4.zip')
OLDER = UPDATE.replace('BZF4', 'BZE1')
ROW = {'upload_id': '14120', 'models': ['SM-L305U', 'SM-L315F'],
       'versions': ['L305USQU3BZB4', 'L315FXXU3BZB4'],
       'filenames': [BASE, DEVICE, OLDER, UPDATE],
       'search_url': 'https://opensource.samsung.com/uploadSearch?searchValue=L315FXXS3BZF4'}


def metadata(*names):
    return {'metadata': {'identifier': 'samsung-opensource-14120'},
            'files': [{'name': name, 'source': 'original', 'size': '495466'} for name in names]}


def dependency(_):
    return {'readmes': [{'name': 'kernel.txt', 'base_versions': ['L305USQU3BZB4']}],
            'base_versions': ['L305USQU3BZB4']}


class SourceLinkTests(unittest.TestCase):
    def setUp(self):
        self.catalog = metadata(BASE, DEVICE, 'SM-L300_Opensource_dts.zip', OLDER, UPDATE)

    def select(self, firmware='L315FXXS3BZF4', row=ROW, catalog=None):
        return select_packages('SM-L315F', firmware, catalog or self.catalog, row, dependency)

    def test_watch7_expected_links_and_dependency_order(self):
        result = self.select()
        prefix = 'https://archive.org/download/samsung-opensource-14120/'
        self.assertEqual(result['source_urls'], ';'.join(prefix + name for name in (BASE, DEVICE, UPDATE)))
        self.assertEqual([p['role'] for p in result['packages']], ['base', 'device', 'update'])
        self.assertEqual(result['base_versions'], ['L305USQU3BZB4'])

    def test_cached_archive_index_is_used_before_online_discovery(self):
        index = {'schema_version': 2, 'generated_at': '2026-01-01T00:00:00+00:00',
                 'item_count': 1, 'items': [{'archive_item': 'samsung-opensource-14120',
                 'models': ['SM-L315F'], 'firmware_versions': ['L315FXXS3BZF4'],
                 'files': [{'name': UPDATE, 'size': 1}]}]}
        with patch('find_source_links.read_index', return_value=index):
            items, loaded = cached_archive_items('SM-L315F', 'L315FXXS3BZF4')
        self.assertEqual(items, ['samsung-opensource-14120'])
        self.assertEqual(loaded['generated_at'], index['generated_at'])

    def test_cached_archive_index_invalidates_when_firmware_is_absent(self):
        index = {'schema_version': 2, 'generated_at': '2026-01-01T00:00:00+00:00',
                 'item_count': 1, 'items': [{'archive_item': 'samsung-opensource-14120',
                 'models': ['SM-L315F'], 'firmware_versions': ['L315FXXS3BZF4'],
                 'files': [{'name': UPDATE, 'size': 1}]}]}
        with patch('find_source_links.read_index', return_value=index):
            items, _ = cached_archive_items('SM-L315F', 'L315FXXS3BZZ9')
        self.assertEqual(items, [])

    def test_cached_index_resolves_without_online_archive_lookup(self):
        files = [
            {'name': name, 'source': 'original', 'size': '495466'}
            for name in (BASE, DEVICE, UPDATE)
        ]
        index = {'schema_version': 2, 'generated_at': '2026-01-01T00:00:00+00:00',
                 'item_count': 1, 'items': [{'archive_item': 'samsung-opensource-14120',
                 'models': ['SM-L315F'], 'firmware_versions': ['L315FXXS3BZF4'], 'files': files}]}

        class OfflineClient:
            def read(self, *_args, **_kwargs):
                raise AssertionError('cache hit should not query Samsung or Archive.org metadata')
            def metadata(self, _item):
                raise AssertionError('cache hit should not download Archive.org metadata')

        report = {}
        with patch('find_source_links.cached_archive_items', return_value=(['samsung-opensource-14120'], index)), \
                patch('find_source_links.read_dependencies', side_effect=lambda _client, _item, entry: dependency(entry)):
            result = resolve(OfflineClient(), 'SM-L315F', 'L315FXXS3BZF4', '', report)
        self.assertEqual(result['archive_item'], 'samsung-opensource-14120')
        self.assertEqual(report['discovery']['method'], 'local_archive_index')

    def test_previous_build_does_not_select_newer_patch(self):
        result = self.select('L315FXXS3BZE1')
        self.assertEqual([p['name'] for p in result['packages']], [BASE, DEVICE, OLDER])

    def test_base_build_needs_no_update(self):
        result = self.select('L315FXXU3BZB4')
        self.assertEqual([p['name'] for p in result['packages']], [BASE, DEVICE])

    def test_complete_firmware_token_required(self):
        with self.assertRaisesRegex(ValueError, '精确匹配'):
            self.select('L315FXXS3BZF', row=None)

    def test_unknown_build_does_not_fall_back_to_latest(self):
        with self.assertRaisesRegex(ValueError, '精确匹配'):
            self.select('L315FXXS3BZZ9')

    def test_explicit_item_matches_update_without_samsung(self):
        result = self.select(row=None)
        self.assertEqual(result['packages'][-1]['name'], UPDATE)
        with self.assertRaisesRegex(ValueError, '精确匹配'):
            self.select('L315FXXU3BZB4', row=None)

    def test_combined_model_dts_alias(self):
        name = 'SM-L315U_L3150_Opensource_dts.zip'
        for model, version in [('SM-L315U', 'L315USQS3BZF4'), ('SM-L3150', 'L3150ZCS3BZF4')]:
            result = select_packages(model, version, metadata(BASE, name, UPDATE), None, dependency)
            self.assertEqual(result['packages'][1]['name'], name)

    def test_missing_or_private_target_dts_is_rejected(self):
        for catalog in (metadata(BASE, UPDATE), metadata(BASE, DEVICE, UPDATE)):
            if len(catalog['files']) == 3:
                catalog['files'][1]['private'] = 'true'
            with self.assertRaisesRegex(ValueError, '缺少目标型号'):
                self.select(catalog=catalog)

    def test_overlay_alone_is_not_a_complete_package(self):
        with self.assertRaisesRegex(ValueError, '完整基础包'):
            self.select(catalog=metadata(DEVICE, UPDATE))

    def test_multiple_firmware_matches_fail(self):
        with self.assertRaisesRegex(ValueError, '多个候选'):
            self.select(catalog=metadata(BASE, DEVICE, UPDATE, 'SM-L315F_Opensource_L315FXXS3BZF4_diff.zip'))

    def test_readme_dependency_can_select_between_base_packages(self):
        result = self.select(catalog=metadata(BASE, 'SM-L700_Opensource.zip', DEVICE, UPDATE))
        self.assertEqual(result['packages'][0]['name'], BASE)

    def test_inconsistent_base_dependencies_fail(self):
        def inconsistent(entry):
            return {'readmes': [], 'base_versions': ['L305USQU3BZB4' if entry['name'] == DEVICE else 'L305USQU1AXFA']}
        with self.assertRaisesRegex(ValueError, '不同的基础版本'):
            select_packages('SM-L315F', 'L315FXXS3BZF4', self.catalog, ROW, inconsistent)

    def test_readme_base_must_belong_to_official_row(self):
        with self.assertRaisesRegex(ValueError, '不在该三星条目'):
            self.select(row=ROW | {'versions': []})

    def test_samsung_search_ignores_mobile_duplicates_and_other_models(self):
        document = '''<table><tr>
          <td class="col-model" title="SM-L305U&#10;SM-L315F">SM-L305U<br>SM-L315F</td>
          <td class="col-version" title="L305USQU3BZB4&#10;L315FXXU3BZB4">L305USQU3BZB4</td>
          <td class="col-desc" title="''' + UPDATE + '''">''' + DEVICE + '''</td>
          <td><a href="javascript:showSrcDownPop('14120');">Download</a></td></tr>
          <tr class="m-column"><td><a href="javascript:showSrcDownPop('14120');">Download</a></td></tr>
          <tr><td class="col-model">SM-L315FN</td><td class="col-desc">''' + UPDATE + '''</td>
          <td><a href="javascript:showSrcDownPop('999');">Download</a></td></tr></table>'''
        parser = SamsungSearchParser()
        parser.feed(document)
        self.assertEqual(len(parser.rows), 2)
        class FakeClient:
            def read(self, _):
                return document.encode()
        rows, errors = discover_rows(FakeClient(), 'SM-L315F', 'L315FXXS3BZF4')
        self.assertEqual([r['upload_id'] for r in rows], ['14120'])
        self.assertEqual(errors, [])

    def test_unreadable_candidate_is_not_silently_ignored(self):
        class FakeClient:
            def metadata(self, _):
                raise RuntimeError('HTTP 503')
        report = {}
        with self.assertRaisesRegex(ValueError, '未能唯一解析'):
            resolve(FakeClient(), 'SM-L315F', 'L315FXXS3BZF4', '14120', report)
        self.assertEqual(report['candidate_errors'][0]['archive_item'], 'samsung-opensource-14120')

    def test_http_520_retries_and_recovers(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'recovered'
        error = urllib.error.HTTPError('https://opensource.samsung.com/uploadSearch', 520, 'origin error', {}, None)
        self.addCleanup(error.close)
        with patch('find_source_links.urllib.request.urlopen', side_effect=[error, error, response]) as request, \
                patch('find_source_links.time.sleep') as pause:
            self.assertEqual(Client().read(error.url), b'recovered')
        self.assertEqual(request.call_count, 3)
        self.assertEqual(pause.call_count, 2)

    def test_http_404_is_not_retried(self):
        error = urllib.error.HTTPError('https://archive.org/missing', 404, 'not found', {}, None)
        self.addCleanup(error.close)
        with patch('find_source_links.urllib.request.urlopen', side_effect=error) as request, \
                patch('find_source_links.time.sleep') as pause:
            with self.assertRaisesRegex(RuntimeError, 'HTTP 404'):
                Client().read(error.url)
        self.assertEqual(request.call_count, 1)
        pause.assert_not_called()

    def test_samsung_520_automatically_resolves_live_archive_hint(self):
        catalog = self.catalog
        class FakeClient:
            def read(self, _):
                raise RuntimeError('HTTP 520 Samsung origin error')
            def metadata(self, item):
                self.item = item
                return catalog
        report = {}
        client = FakeClient()
        with patch('find_source_links.read_dependencies', side_effect=lambda *args: dependency(args[-1])):
            result = resolve(client, 'SM-L315F', 'L315FXXS3BZF4', '', report)
        self.assertEqual(client.item, 'samsung-opensource-14120')
        self.assertEqual([p['name'] for p in result['packages']], [BASE, DEVICE, UPDATE])
        self.assertEqual(report['discovery']['method'], 'archive_fallback')
        self.assertIn('520', report['discovery']['samsung_error'])
        rendered = summary(report | {'model': 'SM-L315F', 'firmware': 'L315FXXS3BZF4',
                                    'result': result, 'status': 'success'})
        self.assertIn('已自动通过 Archive.org', rendered)
        self.assertIn('```text\n' + result['source_urls'], rendered)

    def test_unknown_firmware_does_not_reuse_hint_links(self):
        catalog = self.catalog
        class FakeClient:
            def read(self, _):
                raise RuntimeError('HTTP 520')
            def metadata(self, _):
                return catalog
            def archive_items(self):
                return ['samsung-opensource-14120', 'samsung-opensource-12345']
            def archive_files(self, _):
                return catalog['files']
        report = {}
        with self.assertRaisesRegex(ValueError, '没有精确匹配'):
            resolve(FakeClient(), 'SM-L315F', 'L315FXXS3BZZ9', '', report)
        self.assertTrue(report['archive_scan']['complete'])
        self.assertEqual(report['archive_scan']['checked_items'], 1)

    def test_no_hint_scans_collection_and_finds_exact_firmware(self):
        catalog = self.catalog
        class FakeClient:
            def read(self, _):
                raise RuntimeError('HTTP 520')
            def archive_items(self):
                return ['samsung-opensource-10000', 'samsung-opensource-14120']
            def archive_files(self, item):
                return catalog['files'] if item.endswith('14120') else metadata(BASE)['files']
            def metadata(self, _):
                return catalog
        report = {}
        with patch('find_source_links.archive_hint_items', return_value=[]), \
                patch('find_source_links.read_dependencies', side_effect=lambda *args: dependency(args[-1])):
            result = resolve(FakeClient(), 'SM-L315F', 'L315FXXS3BZF4', '', report)
        self.assertTrue(report['archive_scan']['complete'])
        self.assertEqual(report['archive_scan']['checked_items'], 2)
        self.assertEqual(result['archive_item'], 'samsung-opensource-14120')

    def test_scan_rejects_partial_results_even_after_a_match(self):
        catalog = self.catalog
        class FakeClient:
            def archive_items(self):
                return ['samsung-opensource-14120', 'samsung-opensource-10000']
            def archive_files(self, item):
                if item.endswith('10000'):
                    raise RuntimeError('HTTP 503')
                return catalog['files']
        report = {}
        with self.assertRaisesRegex(ValueError, '扫描未完成'):
            discover_archive_items(FakeClient(), 'L315FXXS3BZF4', report, set())
        self.assertFalse(report['archive_scan']['complete'])
        self.assertEqual(report['archive_scan']['error_count'], 1)
        self.assertEqual(report['archive_scan']['matches'], ['samsung-opensource-14120'])

    def test_scan_budget_exhaustion_is_not_reported_as_no_match(self):
        class FakeClient:
            def archive_items(self):
                return ['samsung-opensource-14120']
            def archive_files(self, _):
                raise AssertionError('budget exhausted before any request')
        report = {}
        with patch('find_source_links.ARCHIVE_SCAN_SECONDS', 0):
            with self.assertRaisesRegex(ValueError, '扫描未完成'):
                discover_archive_items(FakeClient(), 'L315FXXS3BZF4', report, set())
        self.assertEqual(report['archive_scan']['checked_items'], 0)

    def test_incomplete_collection_listing_is_rejected(self):
        class FakeClient(Client):
            def read(self, _):
                return json.dumps({'response': {'numFound': 2,
                                   'docs': [{'identifier': 'samsung-opensource-14120'}]}}).encode()
        with self.assertRaisesRegex(ValueError, '条目列表不完整'):
            FakeClient().archive_items()

    def test_collection_listing_is_paginated(self):
        class FakeClient(Client):
            def read(self, url):
                page = int(parse_qs(urlsplit(url).query)['page'][0])
                identifiers = ['samsung-opensource-', 'samsung-opensource-1', 'samsung-opensource-2',
                               'samsung-opensource-3']
                start = (page - 1) * 2
                docs = [{'identifier': value} for value in identifiers[start:start + 2]]
                return json.dumps({'response': {'numFound': 4, 'docs': docs}}).encode()

        with patch('find_source_links.ARCHIVE_ITEM_PAGE_SIZE', 2):
            self.assertEqual(FakeClient().archive_items(), ['samsung-opensource-3',
                                                            'samsung-opensource-2',
                                                            'samsung-opensource-1'])

    def test_small_overlay_zip_readme_and_hash_checked(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as z:
            z.writestr('L315FXXU3BZB4_kernel.txt', '1. Download and unzip the kernel source of L305USQU3BZB4.\n')
            z.writestr('kernel.tar.gz', b'not executed or unpacked')
        data = buffer.getvalue()
        class FakeClient:
            def read(self, url, limit):
                return data
        entry = {'name': DEVICE, 'size': str(len(data)), 'sha1': hashlib.sha1(data).hexdigest()}
        result = read_dependencies(FakeClient(), 'samsung-opensource-14120', entry)
        self.assertEqual(result['base_versions'], ['L305USQU3BZB4'])
        with self.assertRaisesRegex(ValueError, '校验值'):
            read_dependencies(FakeClient(), 'samsung-opensource-14120', entry | {'sha1': '0' * 40})

    def test_inputs_and_item_override(self):
        self.assertEqual(validate_inputs(' sm-l315f ', ' l315fxxs3bzf4 '), ('SM-L315F', 'L315FXXS3BZF4'))
        for value in ('14120', 'samsung-opensource-14120', 'https://archive.org/details/samsung-opensource-14120'):
            self.assertEqual(normalize_item(value), 'samsung-opensource-14120')
        for model, firmware in [('SM-L315F\n::error::oops', 'L315FXXS3BZF4'), ('SM-L315F', 'L305USQS3BZF4')]:
            with self.assertRaises(ValueError):
                validate_inputs(model, firmware)
        with self.assertRaises(ValueError):
            normalize_item('https://example.com/details/samsung-opensource-14120')

    def test_summary_has_copyable_urls_and_readme_evidence(self):
        result = self.select()
        rendered = summary({'model': 'SM-L315F', 'firmware': 'L315FXXS3BZF4', 'status': 'success',
                            'result': result, 'discovery': {'rows': [ROW]}})
        self.assertIn('```text\n' + result['source_urls'] + '\n```', rendered)
        self.assertIn('L305USQU3BZB4', rendered)
        self.assertIn('完整基础包', rendered)

    def test_failure_writes_summary_and_removes_stale_urls(self):
        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / 'out'
            out.mkdir()
            (out / 'source-urls.txt').write_text('old URLs')
            step_summary = Path(folder) / 'github-summary.md'
            with patch.object(sys, 'argv', ['find_source_links.py', '--model', 'bad', '--firmware', 'bad', '--output', str(out)]), \
                    patch.dict(os.environ, {'GITHUB_STEP_SUMMARY': str(step_summary)}):
                self.assertEqual(main(), 1)
            self.assertFalse((out / 'source-urls.txt').exists())
            self.assertEqual(json.loads((out / 'source-links.json').read_text(encoding='utf-8'))['status'], 'failed')
            self.assertIn('解析失败', step_summary.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
