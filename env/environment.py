import os
from pathlib import Path

from sqlalchemy.engine.url import make_url


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

    def get_version(self) -> str:
        # APP_VERSION is baked into the Docker image at build time from the CI
        # git tag (see .github/workflows). For local dev it falls back to the
        # version declared in pyproject.toml, then to "dev".
        env_version = self.loadEnvironmentVariable("APP_VERSION")
        if env_version:
            return env_version
        try:
            import tomllib
            pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
            with pyproject.open("rb") as f:
                return tomllib.load(f)["project"]["version"]
        except Exception:
            return "dev"

    def get_database_url(self) -> str:
        url = make_url(self.loadEnvironmentVariable("DB_URL")).set(
            username=self.loadEnvironmentVariable("DB_USER"),
            password=self.loadEnvironmentVariable("DB_PASSWORD"),
        )
        return url.render_as_string(hide_password=False)

    def get_owner_database_url(self) -> str:
        url = make_url(self.loadEnvironmentVariable("DB_URL")).set(
            username=self.loadEnvironmentVariable("DB_OWNER_USER"),
            password=self.loadEnvironmentVariable("DB_OWNER_PASSWORD"),
        )
        return url.render_as_string(hide_password=False)

    def get_root_dir(self) -> str:
        raw = self.loadEnvironmentVariable("ROOT_DIR", "/mnt/user/footage")
        return str(Path(raw).resolve())

    def get_task_poll_interval_ms(self) -> int:
        return int(self.loadEnvironmentVariable("TASK_POLL_INTERVAL_MS", "5000"))

    def get_worker_pool_size(self) -> int:
        return int(self.loadEnvironmentVariable("WORKER_POOL_SIZE", "4"))

    def get_db_pool_size(self) -> int:
        return int(self.loadEnvironmentVariable("DB_POOL_SIZE", "5"))

    def get_db_max_overflow(self) -> int:
        return int(self.loadEnvironmentVariable("DB_MAX_OVERFLOW", "10"))

    def get_scanning_file_extensions(self) -> [str]:
        return list(self.get_media_type_map().keys())

    def get_media_type_map(self) -> dict[str, str]:
        mapping = {}
        defaults = {
            "MEDIA_TYPE_VIDEO": ("video", ".mov,.mp4"),
            "MEDIA_TYPE_PHOTO": ("photo", ".jpg,.jpeg,.rw2"),
            "MEDIA_TYPE_360_VIDEO": ("360_video", ".insv"),
            "MEDIA_TYPE_360_PHOTO": ("360_photo", ".insp,.dng"),
        }
        for env_var, (media_type, default) in defaults.items():
            raw = self.loadEnvironmentVariable(env_var, default)
            for ext in raw.lower().split(","):
                ext = ext.strip()
                if ext:
                    mapping[ext] = media_type
        return mapping

    def get_browser_hidden_extensions(self) -> list[str]:
        raw = self.loadEnvironmentVariable("BROWSER_HIDDEN_EXTENSIONS", ".xmp,.acr,.psd,.lrv,.identifier")
        return [e.strip().lower() for e in raw.split(",") if e.strip()]
