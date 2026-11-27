"""Read and build the local index of public Samsung source ZIP archives."""
from __future__ import annotations

import json
import re
from pathlib import Path


INDEX_SCHEMA_VERSION = 2
INDEX_PATH = Path(__file__).resolve().parents[1] / 'data/archive-source-index.json'
ITEM_RE = re.compile(r'samsung-opensource-\d+')
MODEL_RE = re.compile(r'SM-[A-Z0-9]{3,16}')
TOKEN_RE = re.compile(r'[A-Z][A-Z0-9]{7,39}')


def file_identity(name: str) -> tuple[list[str], list[str]]:
    """Extract Samsung model and complete firmware tokens from a ZIP name."""
    upper = name.upper()
    models = sorted(set(MODEL_RE.findall(upper)))
    package_prefix = re.split(r'_OPENSOURCE', upper, maxsplit=1)[0]
    for token in package_prefix.split('_')[1:]:
        if re.fullmatch(r'[A-Z]\d{3,4}[A-Z0-9]{0,2}', token):
            models.append('SM-' + token)
    models = sorted(set(models))
    versions = []
    for token in re.split(r'[^A-Z0-9-]+', upper):
        if token.startswith('SM-') or not TOKEN_RE.fullmatch(token) or not any(c.isdigit() for c in token):
            continue
        versions.append(token)
    return models, sorted(set(versions))


def validate_index(data: object) -> dict:
    if not isinstance(data, dict) or data.get('schema_version') != INDEX_SCHEMA_VERSION:
        raise ValueError('Archive.org 本地索引版本无效。')
    items = data.get('items')
    if not isinstance(items, list) or data.get('item_count') != len(items):
        raise ValueError('Archive.org 本地索引条目数量无效。')
    seen = set()
    for item in items:
        if (not isinstance(item, dict) or not isinstance(item.get('archive_item'), str)
                or not ITEM_RE.fullmatch(item['archive_item'])
                or item['archive_item'] in seen or not isinstance(item.get('files'), list)
                or not isinstance(item.get('models'), list)
                or not isinstance(item.get('firmware_versions'), list)):
            raise ValueError('Archive.org 本地索引条目格式无效。')
        seen.add(item['archive_item'])
        if any(not isinstance(value, str) for value in item['models'] + item['firmware_versions']):
            raise ValueError('Archive.org 本地索引型号或固件版本格式无效。')
        for entry in item['files']:
            if (not isinstance(entry, dict) or not isinstance(entry.get('name'), str)
                    or not entry['name'].lower().endswith('.zip')):
                raise ValueError('Archive.org 本地索引文件格式无效。')
    if not isinstance(data.get('generated_at'), str):
        raise ValueError('Archive.org 本地索引缺少生成时间。')
    return data


def read_index(path: Path = INDEX_PATH) -> dict:
    try:
        return validate_index(json.loads(path.read_text(encoding='utf-8')))
    except json.JSONDecodeError as error:
        raise ValueError('Archive.org 本地索引 JSON 无法解析。') from error


def build_index(client, generated_at: str, workers: int = 16) -> dict:
    """Fetch every collection file list; fail as a whole if any item is unreadable."""
    from concurrent.futures import ThreadPoolExecutor

    item_ids = client.archive_items()
    records = {}
    errors = []

    def inspect(item_id):
        files = client.archive_files(item_id)
        original_zips = [entry for entry in files
                         if entry.get('source') == 'original'
                         and entry.get('name', '').lower().endswith('.zip')
                         and str(entry.get('private', '')).lower() not in ('true', '1')]
        indexed_files = [
            {'name': entry['name'], 'size': int(entry.get('size', 0)),
             'sha1': entry.get('sha1'), 'md5': entry.get('md5'), 'source': 'original'}
            for entry in original_zips
        ]
        models, firmware_versions = set(), set()
        for entry in indexed_files:
            found_models, found_versions = file_identity(entry['name'])
            models.update(found_models)
            firmware_versions.update(found_versions)
        return item_id, {'models': sorted(models), 'firmware_versions': sorted(firmware_versions),
                         'files': indexed_files}

    with ThreadPoolExecutor(max_workers=workers) as executor:
        for offset in range(0, len(item_ids), workers):
            batch = item_ids[offset:offset + workers]
            for item_id, record, error in executor.map(_capture_item_error, batch,
                                                       [inspect] * len(batch)):
                if error is None:
                    records[item_id] = record
                else:
                    errors.append({'archive_item': item_id, 'error': str(error)})
            print(f'Archive.org 已索引 {min(offset + len(batch), len(item_ids))}/{len(item_ids)} 个条目。',
                  flush=True)
    if errors:
        example = errors[0]
        raise RuntimeError(f"{len(errors)} 个 Archive.org 条目无法读取；未生成新索引。"
                           f" 首个错误 {example['archive_item']}: {example['error']}")
    items = [{'archive_item': item_id, **records[item_id]} for item_id in item_ids]
    result = {'schema_version': INDEX_SCHEMA_VERSION, 'generated_at': generated_at,
              'item_count': len(items), 'items': items}
    return validate_index(result)


def _capture_item_error(item_id, inspect):
    try:
        found, files = inspect(item_id)
        return found, files, None
    except Exception as error:
        return item_id, [], error
