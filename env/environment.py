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

    def get_trash_dir_name(self) -> str:
        """Single folder name for the trash directory, created directly
        under ROOT_DIR. Must not be empty, contain a path separator, or be
        '.'/'..' — raises ValueError otherwise (validated at startup so a
        bad value fails fast)."""
        name = self.loadEnvironmentVariable("TRASH_DIR_NAME", ".trash")
        if not name or '/' in name or '\\' in name or name in ('.', '..'):
            raise ValueError(f'Invalid TRASH_DIR_NAME: {name!r}')
        return name

    def get_trash_dir(self) -> Path:
        return Path(self.get_root_dir()) / self.get_trash_dir_name()

    def get_task_poll_interval_ms(self) -> int:
        return int(self.loadEnvironmentVariable("TASK_POLL_INTERVAL_MS", "5000"))

    def get_google_maps_api_key(self) -> str:
        # Browser-side Maps JavaScript API key. Served to the frontend via /config
        # (it is always visible in the browser; protection is HTTP-referrer + API
        # restriction on the key, not secrecy). Empty string disables the maps.
        return self.loadEnvironmentVariable("GOOGLE_MAPS_API_KEY", "")

    def get_google_maps_map_id(self) -> str:
        # Cloud Map ID required for Advanced Markers (custom HTML pins/badges).
        return self.loadEnvironmentVariable("GOOGLE_MAPS_MAP_ID", "")

    def get_worker_pool_size(self) -> int:
        return int(self.loadEnvironmentVariable("WORKER_POOL_SIZE", "4"))

    def get_ffmpeg_threads(self) -> int:
        # Decoding threads per ffmpeg/ffprobe invocation. ffmpeg defaults to
        # one thread per core, which multiplies badly with concurrent jobs.
        return int(self.loadEnvironmentVariable("FFMPEG_THREADS", "2"))

    def get_heavy_job_concurrency(self) -> int:
        # Global ceiling on concurrent CPU-heavy jobs (ffmpeg previews, raw
        # decoding, …), independent of the worker/task pool sizes (#71).
        return int(self.loadEnvironmentVariable("HEAVY_JOB_CONCURRENCY", "2"))

    def get_process_niceness(self) -> int:
        # os.nice() delta applied to ffmpeg/ffprobe/exiftool child processes
        # so they yield CPU to the rest of the system under load.
        return int(self.loadEnvironmentVariable("PROCESS_NICENESS", "10"))

    def get_cpu_temp_limit_c(self) -> float:
        # Heavy jobs pause while the CPU is at/above this temperature (°C).
        # 0 disables the temperature check (e.g. no readable sensor).
        return float(self.loadEnvironmentVariable("CPU_TEMP_LIMIT_C", "85"))

    def get_load_avg_limit(self) -> float:
        # Heavy jobs pause while the 1-minute load average is at/above this
        # value. 0 disables the check. Defaults to the CPU count.
        default = str(float(os.cpu_count() or 1))
        return float(self.loadEnvironmentVariable("LOAD_AVG_LIMIT", default))

    def get_db_pool_size(self) -> int:
        return int(self.loadEnvironmentVariable("DB_POOL_SIZE", "5"))

    def get_db_max_overflow(self) -> int:
        return int(self.loadEnvironmentVariable("DB_MAX_OVERFLOW", "10"))

    def get_scanning_file_extensions(self) -> [str]:
        return list(self.get_media_type_map().keys())

    def get_media_type_map(self) -> dict[str, str]:
        # media_type here only decides a file's *extension-based* family
        # (video/photo, 360 or not); #79 refines it with real metadata once
        # the file is actually probed (scanner/media_type.py). .dng moved
        # out of MEDIA_TYPE_360_PHOTO and into MEDIA_TYPE_PHOTO: it's a real
        # RAW format other cameras write too, so it's metadata-checked
        # (Insta360's Make tag) instead of being blindly 360 just because an
        # Insta360 camera also happens to use it. .insp/.insv stay 360 —
        # Insta360-proprietary, no usable metadata to check at all.
        mapping = {}
        defaults = {
            "MEDIA_TYPE_VIDEO": ("video", ".mov,.mp4"),
            "MEDIA_TYPE_PHOTO": ("photo", ".jpg,.jpeg,.rw2,.dng"),
            "MEDIA_TYPE_360_VIDEO": ("360_video", ".insv"),
            "MEDIA_TYPE_360_PHOTO": ("360_photo", ".insp"),
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
