import hashlib
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, StrictStr

from env.environment import Environment


class ScanResult(BaseModel):
    md5_hash: StrictStr
    file_name: StrictStr
    file_extension: StrictStr
    directory: StrictStr
    last_indexed_at: datetime


class Scanner:
    __block_size: int

    def __init__(self, block_size: int = 4096):
        self.__block_size = block_size

    def scan_directory(self, path: Path) -> [ScanResult]:
        return self.scan_files(path.rglob('*'))

    def scan_files(self, files: [Path]) -> [ScanResult]:
        result = []
        considered_file_extensions = Environment().get_scanning_file_extensions()
        for f in files:
            f_path = Path(f)
            if not f_path.is_dir() and f_path.exists() and f_path.suffix.lower() in considered_file_extensions:
                md5_hash = self.md5_hash(str(f_path))
                result.append(ScanResult(
                    md5_hash=md5_hash,
                    file_name=f_path.name,
                    file_extension=f_path.suffix,
                    directory=str(f_path.parent),
                    last_indexed_at=datetime.now(),
                ))

        return result

    def md5_hash(self, path):
        hasher = hashlib.md5()
        with open(path, "rb") as file:
            for block in iter(lambda: file.read(self.__block_size), b""):
                hasher.update(block)
        return hasher.hexdigest()
