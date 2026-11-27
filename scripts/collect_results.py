#!/usr/bin/env python3
"""Collect only public build results; produce a commit-addressed Actions summary."""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import tarfile
from pathlib import Path

from common import sha256, write_json


def compressed_boot(source: Path) -> bytes:
    import lz4.frame

    original = source.read_bytes()
    compressed = lz4.frame.compress(
        original, block_size=lz4.frame.BLOCKSIZE_MAX1MB, block_linked=False,
        content_checksum=True, block_checksum=False, store_size=True,
    )
    if lz4.frame.decompress(compressed) != original:
        raise ValueError(f'{source.name}.lz4 does not decompress to {source.name}')
    return compressed


def write_ap_tar(compressed: bytes, destination: Path) -> None:
    """Write a tar archive containing only the supplied boot.img.lz4 stream."""
    entry = tarfile.TarInfo('boot.img.lz4')
    entry.size = len(compressed)
    entry.mode = 0o644
    entry.mtime = 0
    entry.uid = entry.gid = 0
    entry.uname = entry.gname = ''
    with tarfile.open(destination, mode='w') as archive:
        archive.addfile(entry, io.BytesIO(compressed))


def verified_boot_report(source: Path, manifest: Path) -> dict | None:
    if source.is_file() and manifest.is_file():
        report = json.loads(manifest.read_text())
        if isinstance(report.get('verification', {}).get('signed'), bool):
            return report
    return None


def collect(work: Path, destination: Path, summary: Path | None = None,
            ap_tar_output: Path | None = None):
    destination.mkdir(parents=True, exist_ok=True)
    reports = ['source-manifest.json', 'bakasu.json', 'prepare.json', 'config-check.json',
               'compile.json', 'module-trust.json', 'abi-symbols-gw8.json']
    for name in reports:
        if (work / name).is_file():
            shutil.copyfile(work / name, destination / name)
    for name in ['stock-boot.json', 'stock.config', 'stock-module-keys.pem']:
        if (work / 'stock' / name).is_file():
            # These are recovered public certificates only, never private keys.
            output_name = 'stock-module-keys.txt' if name.endswith('.pem') else name
            shutil.copyfile(work / 'stock' / name, destination / output_name)
    for name in ['Module.symvers', 'System.map', '.config']:
        if (work / 'out' / name).is_file():
            shutil.copyfile(work / 'out' / name, destination / ('kernel.config' if name == '.config' else name))
    image = work / 'out/arch/arm64/boot/Image'
    if image.is_file():
        shutil.copyfile(image, destination / 'Image')
    bakasu = json.loads((work / 'bakasu.json').read_text()) if (work / 'bakasu.json').exists() else {}
    source_boot = work / 'boot.img'
    manifest_name = 'boot-manifest.json'
    companion_manifest_name = 'boot-unsign-manifest.json'
    boot = verified_boot_report(source_boot, work / manifest_name)
    images = {}
    mode = 'incomplete'
    if boot is not None:
        signed = boot['verification']['signed']
        boot_name = 'boot.img' if signed else 'boot-unsign.img'
        mode = 'signed' if signed else 'unsigned'
        images[boot_name] = (source_boot, manifest_name, boot)
        if signed:
            companion = work / 'boot-unsign.img'
            report = verified_boot_report(companion, work / companion_manifest_name)
            if report is not None:
                if report['verification']['signed']:
                    raise ValueError('boot-unsign.img verification report marks it as signed')
                images[companion.name] = (companion, companion_manifest_name, report)

    # Remove compression outputs left by a previous collection in this directory.
    for name in ('boot.img', 'boot-unsign.img'):
        if name not in images:
            (destination / name).unlink(missing_ok=True)
        (destination / f'{name}.lz4').unlink(missing_ok=True)
        (destination / f'{name}.lz4.tar').unlink(missing_ok=True)
    if ap_tar_output is not None:
        ap_tar_output.unlink(missing_ok=True)
    used_manifests = {name for _, name, _ in images.values()}
    for name in (manifest_name, companion_manifest_name):
        if name not in used_manifests:
            (destination / name).unlink(missing_ok=True)

    text = ['# Galaxy Watch BakaSU boot', '',
            f'- BakaSU repository: `{bakasu.get("repository", "not resolved")}`',
            f'- BakaSU commit: `{bakasu.get("commit", "not resolved")}`',
            f'- Result: **{mode}**']
    for name, (source, report_name, report) in images.items():
        published_boot = destination / name
        shutil.copyfile(source, published_boot)
        report['output'] = name
        write_json(destination / report_name, report)
        text.append(f'- `{name}` SHA-256: `{sha256(published_boot)}`')
        if mode == 'signed' and name == 'boot.img':
            compressed = compressed_boot(published_boot)
            lz4_path = destination / 'boot.img.lz4'
            lz4_path.write_bytes(compressed)
            text.append(f'- `{lz4_path.name}` SHA-256: `{sha256(lz4_path)}`')
            if ap_tar_output is not None:
                ap_tar_output.parent.mkdir(parents=True, exist_ok=True)
                write_ap_tar(compressed, ap_tar_output)
                text.append(f'- Separate artifact `{ap_tar_output.name}` SHA-256: `{sha256(ap_tar_output)}`')
    if not images:
        text.append('- boot SHA-256: `not produced`')
    text.append('')
    if 'boot-unsign.img' in images:
        text.extend(['Unsigned build (not recommended): no new AVB metadata or signature; '
                     'the original Samsung tail and recovered module certificates are preserved; '
                     'zero-padded to the original boot file size.', ''])
    if summary:
        with summary.open('a') as stream:
            stream.write('\n'.join(text))
    (destination / 'BUILD.txt').write_text('\n'.join(text), encoding='utf-8')
    hashes = {p.name: sha256(p) for p in destination.iterdir() if p.is_file() and p.name != 'SHA256SUMS'}
    (destination / 'SHA256SUMS').write_text(''.join(f'{value}  {name}\n' for name, value in sorted(hashes.items())))
    if os.environ.get('GITHUB_OUTPUT'):
        commit = bakasu.get('commit', 'unresolved')[:12]
        suffix = {'signed': '', 'unsigned': '-unsign', 'incomplete': '-incomplete'}[mode]
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            stream.write(f'artifact_name=boot-{commit}{suffix}-{os.environ.get("GITHUB_RUN_ID", "local")}\n')
            stream.write(f'ap_tar={str(mode == "signed" and ap_tar_output is not None and ap_tar_output.is_file()).lower()}\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--ap-tar-output', type=Path)
    args = parser.parse_args()
    collect(args.work, args.output,
            Path(os.environ['GITHUB_STEP_SUMMARY']) if os.environ.get('GITHUB_STEP_SUMMARY') else None,
            args.ap_tar_output)
