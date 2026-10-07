from __future__ import annotations

import struct
from pathlib import Path

from controller.card_art import CardArtStore, find_sts2_pck
from controller.card_catalog import load_card_catalog


def _write_test_pck(path: Path) -> bytes:
    webp = b'RIFF' + struct.pack('<I', 4) + b'WEBP'
    texture = bytes(56) + webp
    newline = chr(10)
    import_data = newline.join([
        '[remap]',
        '',
        'path="res://.godot/imported/bash.ctex"',
        '',
    ]).encode('utf-8')
    resources = [
        ('.godot/imported/bash.ctex', texture),
        ('images/packed/card_portraits/ironclad/bash.png.import', import_data),
    ]

    base_offset = 112
    payload = bytearray()
    entries = []
    for name, value in resources:
        entries.append((name, len(payload), len(value)))
        payload.extend(value)
    directory_offset = base_offset + len(payload)
    directory = bytearray(struct.pack('<I', len(entries)))
    for name, offset, size in entries:
        encoded = name.encode('utf-8') + bytes(1)
        encoded += bytes((-len(encoded)) % 4)
        directory.extend(struct.pack('<I', len(encoded)))
        directory.extend(encoded)
        directory.extend(struct.pack('<QQ', offset, size))
        directory.extend(bytes(16))
        directory.extend(struct.pack('<I', 0))

    header = bytearray(base_offset)
    header[:4] = b'GDPC'
    struct.pack_into('<IIIII', header, 4, 3, 4, 5, 1, 2)
    struct.pack_into('<QQ', header, 24, base_offset, directory_offset)
    path.write_bytes(header + payload + directory)
    return webp


def test_card_art_store_reads_embedded_webp(tmp_path):
    pck = tmp_path / 'SlayTheSpire2.pck'
    expected = _write_test_pck(pck)

    result = CardArtStore(pck).get('CARD.BASH')

    assert result == (expected, 'image/webp')


def test_card_art_discovery_accepts_explicit_pack(tmp_path, monkeypatch):
    pck = tmp_path / 'SlayTheSpire2.pck'
    _write_test_pck(pck)
    monkeypatch.setenv('STS2_PCK_PATH', str(pck))

    assert find_sts2_pck() == pck.resolve()


def test_card_catalog_includes_bilingual_descriptions():
    root = Path(__file__).resolve().parents[1]
    bash = next(card for card in load_card_catalog(root, 'IRONCLAD') if card['id'] == 'BASH')

    assert bash['name_en'] == 'Bash'
    assert bash['name_zh'] == '痛击'
    assert bash['description_en'] == 'Deal 8 damage.\nApply 2 Vulnerable.'
    assert bash['description_zh'] == '造成8点伤害。\n给予2层易伤。'
    assert bash['description_en_upgraded'] == 'Deal 10 damage.\nApply 3 Vulnerable.'
    assert bash['description_zh_upgraded'] == '造成10点伤害。\n给予3层易伤。'
    assert bash['upgradable'] is True


def test_card_catalog_resolves_all_visible_placeholders():
    root = Path(__file__).resolve().parents[1]
    cards = load_card_catalog(root, 'IRONCLAD')

    descriptions = [
        str(card.get(field) or '')
        for card in cards
        for field in ('description_en', 'description_zh',
                      'description_en_upgraded', 'description_zh_upgraded')
    ]
    assert not any('{' in description or '}' in description for description in descriptions)

    whirlwind = next(card for card in cards if card['id'] == 'WHIRLWIND')
    assert whirlwind['cost'] == 'X'
    assert '5' in whirlwind['description_en']
    assert '8' in whirlwind['description_en_upgraded']
