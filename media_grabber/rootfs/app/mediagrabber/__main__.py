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
        svc.vpn.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    logging.getLogger("mediagrabber").info("Media Grabber listening on :%d (NAS %s)",
                                           cfg.listen_port, cfg.nas_path)
    serve(create_app(svc), host="0.0.0.0", port=cfg.listen_port, threads=8, ident=None)


if __name__ == "__main__":
    main()
