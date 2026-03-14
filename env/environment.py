import os
from pathlib import Path


class Environment:
    @staticmethod
    def loadEnvironmentVariable(name, fallback=None) -> str:
        value = os.environ.get(name)
        result = value if value is not None else fallback
        return result

    def get_log_level(self) -> str:
        return self.loadEnvironmentVariable("LOG_LEVEL", "INFO")

    def get_server_host(self) -> str:
        return self.loadEnvironmentVariable("SERVER_HOST", "0.0.0.0")

    def get_server_port(self) -> int:
        return int(self.loadEnvironmentVariable("SERVER_PORT", "8051"))

    def get_release_name(self) -> str:
        return self.loadEnvironmentVariable("RELEASE_NAME", "local")

    def get_db_path(self) -> str:
        return self.loadEnvironmentVariable("DB_PATH", "/backup/footage_archive.sqlite")

    def get_root_dir(self) -> str:
        raw = self.loadEnvironmentVariable("ROOT_DIR", "/mnt/user/footage")
        return str(Path(raw).resolve())

    def get_task_poll_interval_ms(self) -> int:
        return int(self.loadEnvironmentVariable("TASK_POLL_INTERVAL_MS", "5000"))

    def get_scanning_file_extensions(self) -> [str]:
        return (
            self.loadEnvironmentVariable("SCANNING_FILE_EXTENSIONS", ".mov")
            .lower()
            .split(",")
        )
