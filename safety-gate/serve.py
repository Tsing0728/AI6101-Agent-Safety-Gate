#!/usr/bin/env python3
"""启动安全门模型服务。默认 stub 引擎；模型训好后切 hf 引擎。"""

from gate_server.app import create_app
from gate_server.config import load_config


def main() -> None:
    import uvicorn

    cfg = load_config()
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()

