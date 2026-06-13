import hashlib
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, StrictStr

from env.environment import Environment
from tasks.workerpool import parallel_map


class ScanResult(BaseModel):
    md5_hash: StrictStr
    file_name: StrictStr
    file_extension: StrictStr
    media_type: StrictStr | None
    directory: StrictStr
    last_indexed_at: datetime


class Scanner:
    __block_size: int

    def __init__(self, block_size: int = 4096):
        self.__block_size = block_size

    def scan_directory(self, path: Path) -> [ScanResult]:
        return self.scan_files(path.rglob('*'))

    def scan_files(self, files: [Path]) -> [ScanResult]:
        env = Environment()
        considered_file_extensions = env.get_scanning_file_extensions()
        media_type_map = env.get_media_type_map()

        candidates = []
        for f in files:
            f_path = Path(f)
            if f_path.name.startswith('._'):  # macOS AppleDouble sidecars, hidden in the browser too
                continue
            if not f_path.is_dir() and f_path.exists() and f_path.suffix.lower() in considered_file_extensions:
                candidates.append(f_path)

        # Hashing is I/O bound (reading whole files, often large videos over the
        # network), so fan it out across the shared worker pool. Order is preserved.
        hashes = parallel_map(candidates, lambda p: self.md5_hash(str(p)))

        indexed_at = datetime.now()
        return [
            ScanResult(
                md5_hash=md5_hash,
                file_name=f_path.name,
                file_extension=f_path.suffix,
                media_type=media_type_map.get(f_path.suffix.lower()),
                directory=str(f_path.parent),
                last_indexed_at=indexed_at,
            )
            for f_path, md5_hash in zip(candidates, hashes)
        ]

    def md5_hash(self, path):
        hasher = hashlib.md5()
        with open(path, "rb") as file:
            for block in iter(lambda: file.read(self.__block_size), b""):
                hasher.update(block)
        return hasher.hexdigest()
