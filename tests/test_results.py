import io
import hashlib
import json
import re
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import lz4.frame

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from collect_results import collect
from common import sha256


class ResultsTests(unittest.TestCase):
    def test_artifact_allowlist_excludes_private_keys_and_marks_unsigned(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            work, output = root / 'work', root / 'dist'
            (work / 'out/certs').mkdir(parents=True)
            (work / 'out/certs/signing_key.pem').write_text('PRIVATE MODULE KEY')
            (work / 'avb-key.pem').write_text('PRIVATE AVB KEY')
            (work / 'boot.img').write_bytes(b'boot')
            (work / 'bakasu.json').write_text(json.dumps({'commit': 'a' * 40, 'repository': 'test'}))
            (work / 'boot-manifest.json').write_text(json.dumps({'verification': {'signed': False}}))
            github_output = root / 'github-output.txt'
            with patch.dict('os.environ', {'GITHUB_OUTPUT': str(github_output), 'GITHUB_RUN_ID': '123'}):
                collect(work, output, root / 'summary.md', root / 'AP_20261127_123456.tar.md5')
            self.assertEqual(github_output.read_text(),
                             'artifact_name=boot-aaaaaaaaaaaa-unsign-123\nap_tar_md5=false\n')
            self.assertIn('unsigned', (output / 'BUILD.txt').read_text())
            self.assertIn('BUILD.txt', (output / 'SHA256SUMS').read_text())
            self.assertIn('boot-unsign.img', (output / 'SHA256SUMS').read_text())
            self.assertNotIn('.lz4', (output / 'SHA256SUMS').read_text())
            self.assertFalse((output / 'boot-unsign.img.lz4').exists())
            self.assertFalse((output / 'boot.img.lz4.tar').exists())
            self.assertFalse((root / 'AP.tar.md5').exists())
            for path in output.rglob('*'):
                if path.is_file():
                    self.assertNotIn(b'PRIVATE', path.read_bytes())

    def test_failed_early_build_still_has_summary(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            work, output = root / 'work', root / 'dist'
            work.mkdir()
            output.mkdir()
            (work / 'boot.img').write_bytes(b'no verification manifest')
            (output / 'boot.img').write_bytes(b'previous build')
            (output / 'boot.img.lz4').write_bytes(b'previous build')
            (output / 'boot.img.lz4.tar').write_bytes(b'previous build')
            (root / 'AP_20261127_123456.tar.md5').write_bytes(b'previous build')
            (root / 'AP.tar').write_bytes(b'previous build')
            collect(work, output, ap_tar_md5_output=root / 'AP_20261127_123456.tar.md5')
            self.assertIn('incomplete', (output / 'BUILD.txt').read_text())
            self.assertFalse(list(output.glob('*.img*')))
            self.assertFalse((root / 'AP_20261127_123456.tar.md5').exists())
            self.assertFalse((root / 'AP.tar').exists())

    def test_lz4_and_checksums_cover_each_published_boot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / 'dist'
            for signed in (True, False):
                work = root / f'work-{signed}'
                work.mkdir()
                # Include nonzero content and the final footer/padding to catch
                # accidentally compressing Image or a pre-signing boot instead.
                payload = bytes(range(256)) * 4096
                unsigned_boot = payload + bytes(64)
                expected = {'boot-unsign.img': unsigned_boot}
                if signed:
                    expected['boot.img'] = payload + b'AVBf' + bytes(60)
                    (work / 'boot-unsign.img').write_bytes(unsigned_boot)
                    (work / 'boot-unsign-manifest.json').write_text(json.dumps({'verification': {'signed': False}}))
                primary = 'boot.img' if signed else 'boot-unsign.img'
                (work / 'boot.img').write_bytes(expected[primary])
                (work / 'boot-manifest.json').write_text(json.dumps({'verification': {'signed': signed}}))
                ap_tar_md5 = root / 'AP_20261127_123456.tar.md5'
                collect(work, output, ap_tar_md5_output=ap_tar_md5)
                published = {p.name for p in output.iterdir()
                             if p.name.endswith('.img') or p.name.endswith('.img.lz4')}
                expected_published = set(expected)
                if signed:
                    expected_published.add('boot.img.lz4')
                self.assertEqual(published, expected_published)
                report = json.loads((output / 'boot-manifest.json').read_text())
                self.assertEqual(report['output'], primary)
                self.assertIs(report['verification']['signed'], signed)
                companion_report = output / 'boot-unsign-manifest.json'
                if signed:
                    report = json.loads(companion_report.read_text())
                    self.assertEqual(report['output'], 'boot-unsign.img')
                    self.assertIs(report['verification']['signed'], False)
                else:
                    self.assertFalse(companion_report.exists())
                checksums = {name: digest for digest, name in
                             (line.split('  ', 1) for line in (output / 'SHA256SUMS').read_text().splitlines())}
                for name, final in expected.items():
                    self.assertEqual((output / name).read_bytes(), final)
                    self.assertEqual(checksums[name], sha256(output / name))
                if signed:
                    lz4_path = output / 'boot.img.lz4'
                    compressed = lz4_path.read_bytes()
                    self.assertEqual(lz4.frame.decompress(compressed), expected['boot.img'])
                    info = lz4.frame.get_frame_info(compressed)
                    self.assertEqual(info['content_size'], len(expected['boot.img']))
                    self.assertTrue(info['content_checksum'])
                    self.assertFalse(info['block_linked'])
                    self.assertFalse(info['block_checksum'])
                    self.assertEqual(checksums['boot.img.lz4'], sha256(lz4_path))
                    raw = ap_tar_md5.read_bytes()
                    footer_at = raw.index(b'Show the build information\n')
                    raw_tar = raw[:footer_at]
                    footer = raw[footer_at:].decode()
                    with tarfile.open(fileobj=io.BytesIO(raw_tar), mode='r:') as archive:
                        self.assertEqual(archive.getnames(), ['boot.img.lz4'])
                        tar_compressed = archive.extractfile('boot.img.lz4').read()
                    self.assertEqual(tar_compressed, compressed)
                    self.assertNotIn('AP.tar', checksums)
                    self.assertIn(f'{ap_tar_md5.name}` raw AP.tar MD5: `{hashlib.md5(raw_tar).hexdigest()}`',
                                  (output / 'BUILD.txt').read_text())
                    expected_tar_name = ap_tar_md5.name.removesuffix('.md5')
                    match = re.search(r'original_tar_file_size:(\d+)\n([0-9a-f]{32})  (.+\.tar)\n$', footer)
                    self.assertIsNotNone(match)
                    self.assertEqual(int(match.group(1)), len(raw_tar))
                    self.assertEqual(match.group(2), hashlib.md5(raw_tar).hexdigest())
                    self.assertEqual(match.group(3), expected_tar_name)
                else:
                    self.assertFalse((output / 'boot.img.lz4').exists())
                    self.assertFalse(ap_tar_md5.exists())


if __name__ == '__main__':
    unittest.main()
