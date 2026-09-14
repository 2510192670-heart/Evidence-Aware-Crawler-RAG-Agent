"""Run the loopback fixtures on the default loopback port."""

import uvicorn

from .app import DEFAULT_PORT, app


def main():
    uvicorn.run(app, host='127.0.0.1', port=DEFAULT_PORT)


if __name__ == '__main__':
    main()
