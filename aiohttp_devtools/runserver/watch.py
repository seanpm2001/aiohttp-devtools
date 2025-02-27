import asyncio
import os
import signal
import sys
from multiprocessing import Process
from pathlib import Path
from typing import AsyncIterator, Iterable, Optional, Tuple, Union

from aiohttp import ClientSession, web
from watchfiles import awatch

from ..exceptions import AiohttpDevException
from ..logs import rs_dft_logger as logger
from .config import Config
from .serve import WS, serve_main_app, src_reload


class WatchTask:
    _app: web.Application
    _task: "asyncio.Task[None]"

    def __init__(self, path: Union[Path, str]):
        self._path = path

    async def start(self, app: web.Application) -> None:
        self._app = app
        self.stopper = asyncio.Event()
        self._awatch = awatch(self._path, stop_event=self.stopper)
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        raise NotImplementedError()

    async def close(self, *args: object) -> None:
        if self._task:
            self.stopper.set()
            if self._task.done():
                self._task.result()
            self._task.cancel()

    async def cleanup_ctx(self, app: web.Application) -> AsyncIterator[None]:
        await self.start(app)
        yield
        await self.close(app)


class AppTask(WatchTask):
    template_files = '.html', '.jinja', '.jinja2'

    def __init__(self, config: Config):
        self._config = config
        self._reloads = 0
        self._session: Optional[ClientSession] = None
        self._runner = None
        assert self._config.watch_path
        super().__init__(self._config.watch_path)

    async def _run(self, live_checks: int = 20) -> None:
        assert self._app is not None

        self._session = ClientSession()
        try:
            self._start_dev_server()

            static_path = str(self._app['static_path'])

            def is_static(changes: Iterable[Tuple[object, str]]) -> bool:
                return all(str(c[1]).startswith(static_path) for c in changes)

            async for changes in self._awatch:
                self._reloads += 1
                if any(f.endswith('.py') for _, f in changes):
                    logger.debug('%d changes, restarting server', len(changes))
                    self._stop_dev_server()
                    self._start_dev_server()
                    await self._src_reload_when_live(live_checks)
                elif len(changes) == 1 and is_static(changes):
                    # a single (static) file has changed, reload a single file.
                    await src_reload(self._app, changes.pop()[1])
                else:
                    # reload all pages
                    await src_reload(self._app)
        except Exception as exc:
            logger.exception(exc)
            await self._session.close()
            raise AiohttpDevException('error running dev server')

    async def _src_reload_when_live(self, checks: int = 20) -> None:
        assert self._app is not None and self._session is not None

        if self._app[WS]:
            url = 'http://localhost:{.main_port}/?_checking_alive=1'.format(self._config)
            logger.debug('checking app at "%s" is running before prompting reload...', url)
            for i in range(checks):
                await asyncio.sleep(0.1)
                try:
                    async with self._session.get(url):
                        pass
                except OSError as e:
                    logger.debug('try %d | OSError %d app not running', i, e.errno)
                else:
                    logger.debug('try %d | app running, reloading...', i)
                    await src_reload(self._app)
                    return

    def _start_dev_server(self) -> None:
        act = 'Start' if self._reloads == 0 else 'Restart'
        logger.info('%sing dev server at http://%s:%s ●', act, self._config.host, self._config.main_port)

        try:
            tty_path = os.ttyname(sys.stdin.fileno())
        except OSError:  # pragma: no branch
            # fileno() always fails with pytest
            tty_path = '/dev/tty'
        except AttributeError:
            # on windows, without a windows machine I've no idea what else to do here
            tty_path = None

        self._process = Process(target=serve_main_app, args=(self._config, tty_path))
        self._process.start()

    def _stop_dev_server(self) -> None:
        if self._process.is_alive():
            logger.debug('stopping server process...')
            if self._process.pid:
                os.kill(self._process.pid, signal.SIGINT)
            self._process.join(5)
            if self._process.exitcode is None:
                logger.warning('process has not terminated, sending SIGKILL')
                self._process.kill()
                self._process.join(1)
            else:
                logger.debug('process stopped')
        else:
            logger.warning('server process already dead, exit code: %s', self._process.exitcode)

    async def close(self, *args: object) -> None:
        self.stopper.set()
        self._stop_dev_server()
        if self._session is None:
            raise RuntimeError("Object not started correctly before calling .close()")
        await asyncio.gather(super().close(), self._session.close())


class LiveReloadTask(WatchTask):
    async def _run(self) -> None:
        async for changes in self._awatch:
            if len(changes) > 1:
                await src_reload(self._app)
            else:
                await src_reload(self._app, changes.pop()[1])
