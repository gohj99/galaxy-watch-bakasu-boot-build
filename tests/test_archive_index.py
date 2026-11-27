import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from archive_index import build_index, file_identity, validate_index


class ArchiveIndexTests(unittest.TestCase):
    def test_valid_index(self):
        value = {'schema_version': 2, 'generated_at': '2026-01-01T00:00:00+00:00',
                 'item_count': 1, 'items': [{'archive_item': 'samsung-opensource-1',
                 'models': ['SM-L315F'], 'firmware_versions': ['L315FXXS3BZF4'],
                 'files': [{'name': 'source.zip', 'size': 1, 'source': 'original'}]}]}
        self.assertIs(validate_index(value), value)

    def test_extracts_models_and_firmware_versions(self):
        models, versions = file_identity(
            'SM-L315U_L3150_Opensource_L305USQS3BZB4_L315FXXS3BZF4.zip')
        self.assertEqual(models, ['SM-L3150', 'SM-L315U'])
        self.assertEqual(versions, ['L305USQS3BZB4', 'L315FXXS3BZF4'])

    def test_rejects_duplicate_items(self):
        value = {'schema_version': 2, 'generated_at': 'now', 'item_count': 2,
                 'items': [{'archive_item': 'samsung-opensource-1', 'models': [], 'firmware_versions': [], 'files': []},
                           {'archive_item': 'samsung-opensource-1', 'models': [], 'firmware_versions': [], 'files': []}]}
        with self.assertRaises(ValueError):
            validate_index(value)

    def test_build_index_records_only_public_original_zips_and_derives_fields(self):
        class FakeClient:
            def archive_items(self):
                return ['samsung-opensource-2', 'samsung-opensource-1']

            def archive_files(self, item):
                if item.endswith('-1'):
                    return [
                        {'name': 'SM-L315U_L3150_Opensource_L315FXXS3BZF4.zip',
                         'source': 'original', 'size': '42', 'sha1': 'abc'},
                        {'name': 'private.zip', 'source': 'original', 'private': 'true'},
                        {'name': 'readme.txt', 'source': 'original'},
                        {'name': 'other.zip', 'source': 'derivative'},
                    ]
                return []

        index = build_index(FakeClient(), '2026-01-01T00:00:00+00:00', workers=1)
        first, second = index['items']
        self.assertEqual(first['archive_item'], 'samsung-opensource-2')
        self.assertEqual(first['models'], [])
        self.assertEqual(second['models'], ['SM-L3150', 'SM-L315U'])
        self.assertEqual(second['firmware_versions'], ['L315FXXS3BZF4'])
        self.assertEqual([file['name'] for file in second['files']],
                         ['SM-L315U_L3150_Opensource_L315FXXS3BZF4.zip'])

    def test_build_index_fails_instead_of_returning_partial_data(self):
        class FakeClient:
            def archive_items(self):
                return ['samsung-opensource-1']

            def archive_files(self, _item):
                raise RuntimeError('HTTP 503')

        with self.assertRaisesRegex(RuntimeError, '未生成新索引'):
            build_index(FakeClient(), '2026-01-01T00:00:00+00:00', workers=1)


if __name__ == '__main__':
    unittest.main()
