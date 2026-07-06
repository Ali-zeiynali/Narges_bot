import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


_CONFIGURED_KEY: tuple[str, str] | None = None


class WindowsSafeRotatingFileHandler(RotatingFileHandler):
    def doRollover(self) -> None:
        try:
            super().doRollover()
        except PermissionError:
            if self.stream:
                self.stream.close()
                self.stream = None
            if not self.delay:
                self.stream = self._open()


def setup_logging(log_file: str, log_level: str) -> None:
    global _CONFIGURED_KEY
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    key = (str(log_path.resolve()), log_level)
    if _CONFIGURED_KEY == key and logging.getLogger().handlers:
        return

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = WindowsSafeRotatingFileHandler(
        log_path,
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        handlers=[file_handler, stream_handler],
        force=True,
    )
    _CONFIGURED_KEY = key
