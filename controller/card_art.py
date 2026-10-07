from __future__ import annotations

import os
import struct
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

from controller.deck_profile import normalize_card_id


_GAME_DIRECTORY = Path('steamapps') / 'common' / 'Slay the Spire 2'


def find_sts2_pck() -> Optional[Path]:
    configured = os.environ.get('STS2_PCK_PATH')
    candidates = []
    if configured:
        candidates.append(Path(configured).expanduser())

    steam_roots = [
        Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Steam',
        Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Steam',
        Path.home() / '.steam' / 'steam',
        Path.home() / '.local' / 'share' / 'Steam',
    ]
    for steam_root in steam_roots:
        candidates.append(steam_root / _GAME_DIRECTORY)
        library_file = steam_root / 'steamapps' / 'libraryfolders.vdf'
        if not library_file.is_file():
            continue
        try:
            lines = library_file.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            continue
        for line in lines:
            fields = line.split('"')
            if len(fields) >= 4 and fields[1] == 'path':
                root = fields[3].replace(chr(92) * 2, chr(92))
                candidates.append(Path(root) / _GAME_DIRECTORY)

    for candidate in candidates:
        path = candidate / 'SlayTheSpire2.pck' if candidate.is_dir() else candidate
        if path.is_file():
            return path.resolve()
    return None


class CardArtStore:
    def __init__(self, pck_path: Optional[Path] = None):
        self.pck_path = pck_path
        self._entries: Optional[Dict[str, Tuple[int, int]]] = None
        self._imports: Dict[str, str] = {}
        self._base_offset = 0
        self._cache: Dict[str, Tuple[bytes, str]] = {}
        self._lock = threading.RLock()

    @classmethod
    def discover(cls) -> 'CardArtStore':
        return cls(find_sts2_pck())

    @property
    def available(self) -> bool:
        return self.pck_path is not None

    def get(self, card_id: str) -> Optional[Tuple[bytes, str]]:
        key = normalize_card_id(card_id)
        if not key or not self.pck_path:
            return None
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            self._load_index()
            import_path = self._imports.get(key)
            if import_path is None:
                return None
            import_text = self._read_entry(import_path).decode('utf-8')
            marker = 'path="res://'
            start = import_text.find(marker)
            if start < 0:
                return None
            start += len(marker)
            end = import_text.find('"', start)
            if end < 0:
                return None
            texture = self._read_entry(import_text[start:end])
            image = self._embedded_image(texture)
            if image is not None:
                self._cache[key] = image
            return image

    def _load_index(self) -> None:
        if self._entries is not None:
            return
        if self.pck_path is None:
            self._entries = {}
            return
        entries: Dict[str, Tuple[int, int]] = {}
        imports: Dict[str, str] = {}
        with self.pck_path.open('rb') as stream:
            header = stream.read(40)
            if len(header) != 40 or header[:4] != b'GDPC':
                raise ValueError('Unsupported STS2 resource pack')
            self._base_offset = struct.unpack_from('<Q', header, 24)[0]
            directory_offset = struct.unpack_from('<Q', header, 32)[0]
            stream.seek(directory_offset)
            file_count_data = stream.read(4)
            if len(file_count_data) != 4:
                raise ValueError('Invalid STS2 resource directory')
            file_count = struct.unpack('<I', file_count_data)[0]
            if file_count > 1_000_000:
                raise ValueError('Invalid STS2 resource count')
            for _ in range(file_count):
                raw_length = stream.read(4)
                if len(raw_length) != 4:
                    raise ValueError('Truncated STS2 resource directory')
                path_length = struct.unpack('<I', raw_length)[0]
                if path_length < 1 or path_length > 1_048_576:
                    raise ValueError('Invalid STS2 resource path')
                path = stream.read(path_length).rstrip(bytes(1)).decode('utf-8')
                metadata = stream.read(36)
                if len(metadata) != 36:
                    raise ValueError('Truncated STS2 resource entry')
                offset, size = struct.unpack_from('<QQ', metadata)
                entries[path] = (offset, size)
                prefix = 'images/packed/card_portraits/'
                if path.startswith(prefix) and path.endswith('.png.import'):
                    card_key = Path(path[:-7]).stem.upper()
                    imports.setdefault(card_key, path)
        self._entries = entries
        self._imports = imports

    def _read_entry(self, path: str) -> bytes:
        if self._entries is None or self.pck_path is None:
            raise KeyError(path)
        offset, size = self._entries[path]
        with self.pck_path.open('rb') as stream:
            stream.seek(self._base_offset + offset)
            value = stream.read(size)
        if len(value) != size:
            raise ValueError(f'Truncated STS2 resource: {path}')
        return value

    @staticmethod
    def _embedded_image(texture: bytes) -> Optional[Tuple[bytes, str]]:
        riff = texture.find(b'RIFF', 32)
        if riff >= 0 and texture[riff + 8:riff + 12] == b'WEBP':
            length = struct.unpack_from('<I', texture, riff + 4)[0] + 8
            if riff + length <= len(texture):
                return texture[riff:riff + length], 'image/webp'
        png = texture.find(bytes.fromhex('89504e470d0a1a0a'), 32)
        if png >= 0:
            return texture[png:], 'image/png'
        jpeg = texture.find(bytes.fromhex('ffd8ff'), 32)
        if jpeg >= 0:
            return texture[jpeg:], 'image/jpeg'
        return None
