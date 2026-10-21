import logging
import sys

from waitress import serve

from . import config
from .web import Services, create_app


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load()
    svc = Services(cfg)
    svc.start()
    loc = svc.storage.resolve()
    logging.getLogger("photomanager").info(
        "Photo Manager listening on :%d — NAS: %s", cfg.listen_port,
        loc["path"] if not loc["error"] else loc["error"])
    serve(create_app(svc), host="0.0.0.0", port=cfg.listen_port, threads=8, ident=None)


if __name__ == "__main__":
    main()
