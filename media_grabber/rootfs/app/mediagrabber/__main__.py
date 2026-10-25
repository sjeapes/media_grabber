import logging
import signal
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

    def stop(*_):
        svc.browser.stop()
        svc.vpn.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    loc = svc.storage.resolve()
    logging.getLogger("mediagrabber").info(
        "Media Grabber listening on :%d — storage: %s", cfg.listen_port,
        loc["path"] if not loc["error"] else loc["error"])
    serve(create_app(svc), host="0.0.0.0", port=cfg.listen_port, threads=16, ident=None)


if __name__ == "__main__":
    main()
