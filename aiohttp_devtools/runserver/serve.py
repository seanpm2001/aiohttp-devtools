import asyncio
import contextlib
import json
import mimetypes
import sys
from errno import EADDRINUSE
from pathlib import Path
from typing import Any, Iterator, Optional

from aiohttp import WSMsgType, web
from aiohttp.hdrs import LAST_MODIFIED, CONTENT_LENGTH
from aiohttp.typedefs import Handler
from aiohttp.web_exceptions import HTTPNotFound, HTTPNotModified
from aiohttp.web_urldispatcher import StaticResource
from yarl import URL

from ..exceptions import AiohttpDevException
from ..logs import rs_aux_logger as aux_logger
from ..logs import rs_dft_logger as dft_logger
from ..logs import setup_logging
from .config import AppFactory, Config
from .log_handlers import AccessLogger
from .utils import MutableValue

LIVE_RELOAD_HOST_SNIPPET = '\n<script src="http://{}:{}/livereload.js"></script>\n'
LIVE_RELOAD_LOCAL_SNIPPET = b'\n<script src="/livereload.js"></script>\n'
HOST = '0.0.0.0'


def _set_static_url(app: web.Application, url: str) -> None:
    app["static_root_url"] = MutableValue(url)
    for subapp in app._subapps:
        _set_static_url(subapp, url)


def _change_static_url(app: web.Application, url: str) -> None:
    app["static_root_url"].change(url)
    for subapp in app._subapps:
        _change_static_url(subapp, url)


def modify_main_app(app: web.Application, config: Config) -> None:
    """
    Modify the app we're serving to make development easier, eg.
    * modify responses to add the livereload snippet
    * set ``static_root_url`` on the app (for use with aiohttp-jinja2)
    """
    app._debug = True
    dft_logger.debug('livereload enabled: %s', '✓' if config.livereload else '✖')

    def get_host(request: web.Request) -> str:
        if config.infer_host:
            return request.headers.get('host', 'localhost').split(':', 1)[0]
        else:
            return config.host

    if config.livereload:
        async def on_prepare(request: web.Request, response: web.StreamResponse) -> None:
            if (not isinstance(response, web.Response)
                    or not isinstance(response.body, bytes)  # No support for Payload
                    or request.path.startswith("/_debugtoolbar")
                    or "text/html" not in response.content_type):
                return
            lr_snippet = LIVE_RELOAD_HOST_SNIPPET.format(get_host(request), config.aux_port)
            dft_logger.debug("appending live reload snippet '%s' to body", lr_snippet)
            response.body += lr_snippet.encode()
            response.headers[CONTENT_LENGTH] = str(len(response.body))
        app.on_response_prepare.append(on_prepare)

    static_path = config.static_url.strip('/')
    if config.infer_host and config.static_path is not None:
        # we set the app key even in middleware to make the switch to production easier and for backwards compat.
        @web.middleware
        async def static_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
            static_url = 'http://{}:{}/{}'.format(get_host(request), config.aux_port, static_path)
            dft_logger.debug('setting app static_root_url to "%s"', static_url)
            _change_static_url(request.app, static_url)
            return await handler(request)

        app.middlewares.insert(0, static_middleware)

    if config.static_path is not None:
        static_url = 'http://{}:{}/{}'.format(config.host, config.aux_port, static_path)
        dft_logger.debug('settings app static_root_url to "%s"', static_url)
        _set_static_url(app, static_url)


async def check_port_open(port: int, delay: float = 1) -> None:
    loop = asyncio.get_running_loop()
    # the "s = socket.socket; s.bind" approach sometimes says a port is in use when it's not
    # this approach replicates aiohttp so should always give the same answer
    for i in range(5, 0, -1):
        try:
            server = await loop.create_server(asyncio.Protocol, host=HOST, port=port)
        except OSError as e:
            if e.errno != EADDRINUSE:
                raise
            dft_logger.warning('port %d is already in use, waiting %d...', port, i)
            await asyncio.sleep(delay)
        else:
            server.close()
            await server.wait_closed()
            return
    raise AiohttpDevException('The port {} is already is use'.format(port))


@contextlib.contextmanager
def set_tty(tty_path: Optional[str]) -> Iterator[None]:
    try:
        if not tty_path:
            # to match OSError from open
            raise OSError()
        with open(tty_path) as tty:
            sys.stdin = tty
            yield
    except OSError:
        # either tty_path is None (windows) or opening it fails (eg. on pycharm)
        yield


def serve_main_app(config: Config, tty_path: Optional[str]) -> None:
    with set_tty(tty_path):
        setup_logging(config.verbose)
        app_factory = config.import_app_factory()
        if sys.version_info >= (3, 11):
            with asyncio.Runner() as runner:
                app_runner = runner.run(create_main_app(config, app_factory))
                try:
                    runner.run(start_main_app(app_runner, config.main_port))
                    runner.get_loop().run_forever()
                except KeyboardInterrupt:
                    pass
                finally:
                    with contextlib.suppress(asyncio.TimeoutError, KeyboardInterrupt):
                        runner.run(app_runner.cleanup())
        else:
            loop = asyncio.new_event_loop()
            runner = loop.run_until_complete(create_main_app(config, app_factory))
            try:
                loop.run_until_complete(start_main_app(runner, config.main_port))
                loop.run_forever()
            except KeyboardInterrupt:  # pragma: no cover
                pass
            finally:
                with contextlib.suppress(asyncio.TimeoutError, KeyboardInterrupt):
                    loop.run_until_complete(runner.cleanup())


async def create_main_app(config: Config, app_factory: AppFactory) -> web.AppRunner:
    app = await config.load_app(app_factory)
    modify_main_app(app, config)

    await check_port_open(config.main_port)
    return web.AppRunner(app, access_log_class=AccessLogger)


async def start_main_app(runner: web.AppRunner, port: int) -> None:
    await runner.setup()
    site = web.TCPSite(runner, host=HOST, port=port, shutdown_timeout=0.1)
    await site.start()


WS = 'websockets'


async def src_reload(app: web.Application, path: Optional[str] = None) -> int:
    """
    prompt each connected browser to reload by sending websocket message.

    :param path: if supplied this must be a path relative to app['static_path'],
        eg. reload of a single file is only supported for static resources.
    :return: number of sources reloaded
    """
    cli_count = len(app[WS])
    if cli_count == 0:
        return 0

    is_html = None
    if path:
        path = str(Path(app['static_url']) / Path(path).relative_to(app['static_path']))
        is_html = mimetypes.guess_type(path)[0] == 'text/html'

    reloads = 0
    aux_logger.debug('prompting source reload for %d clients', cli_count)
    for ws, url in app[WS]:
        if path and is_html and path not in {url, url + '.html', url.rstrip('/') + '/index.html'}:
            aux_logger.debug('skipping reload for client at %s', url)
            continue
        aux_logger.debug('reload client at %s', url)
        data = {
            'command': 'reload',
            'path': path or url,
            'liveCSS': True,
            'liveImg': True,
        }
        try:
            await ws.send_str(json.dumps(data))
        except RuntimeError as e:
            # eg. "RuntimeError: websocket connection is closing"
            aux_logger.error('Error broadcasting change to %s, RuntimeError: %s', path or url, e)
        else:
            reloads += 1

    if reloads:
        s = '' if reloads == 1 else 's'
        aux_logger.info('prompted reload of %s on %d client%s', path or 'page', reloads, s)
    return reloads


async def cleanup_aux_app(app: web.Application) -> None:
    aux_logger.debug('closing %d websockets...', len(app[WS]))
    await asyncio.gather(*(ws.close() for ws, _ in app[WS]))


def create_auxiliary_app(
        *, static_path: Optional[str], static_url: str = "/", livereload: bool = True) -> web.Application:
    app = web.Application()
    app[WS] = set()
    app.update(
        static_path=static_path,
        static_url=static_url,
    )
    app.on_shutdown.append(cleanup_aux_app)

    if livereload:
        lr_path = Path(__file__).resolve().parent / 'livereload.js'
        app['livereload_script'] = lr_path.read_bytes()
        app.router.add_route('GET', '/livereload.js', livereload_js)
        app.router.add_route('GET', '/livereload', websocket_handler)
        aux_logger.debug('enabling livereload on auxiliary app')

    if static_path:
        route = CustomStaticResource(
            static_url.rstrip('/'),
            static_path + '/',
            name='static-router',
            add_tail_snippet=livereload,
            follow_symlinks=True
        )
        app.router.register_resource(route)

    return app


async def livereload_js(request: web.Request) -> web.Response:
    if request.if_modified_since:
        raise HTTPNotModified()

    lr_script = request.app['livereload_script']
    return web.Response(body=lr_script, content_type='application/javascript',
                        headers={LAST_MODIFIED: 'Fri, 01 Jan 2016 00:00:00 GMT'})

WS_TYPE_LOOKUP = {k.value: v for v, k in WSMsgType.__members__.items()}


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(timeout=0.01)
    url = None
    await ws.prepare(request)

    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError as e:
                aux_logger.error('JSON decode error: %s', str(e))
                await ws.close()
            else:
                command = data['command']
                if command == 'hello':
                    if 'http://livereload.com/protocols/official-7' not in data['protocols']:
                        aux_logger.error('live reload protocol 7 not supported by client %s', msg.data)
                        await ws.close()
                    else:
                        handshake = {
                            'command': 'hello',
                            'protocols': [
                                'http://livereload.com/protocols/official-7',
                            ],
                            'serverName': 'livereload-aiohttp',
                        }
                        await ws.send_str(json.dumps(handshake))
                elif command == 'info':
                    aux_logger.debug('browser connected: %s', data)
                    url = '/' + data['url'].split('/', 3)[-1]
                    request.app[WS].add((ws, url))
                else:
                    aux_logger.error('Unknown ws message %s', msg.data)
                    await ws.close()
        elif msg.type == WSMsgType.ERROR:
            aux_logger.error('ws connection closed with exception %s', ws.exception())
        else:
            aux_logger.error('unknown websocket message type %s, data: %s', WS_TYPE_LOOKUP[msg.type], msg.data)
            await ws.close()

    if url is None:
        aux_logger.warning('browser disconnected, appears no websocket connection was made')
    else:
        aux_logger.debug('browser disconnected')
        request.app[WS].remove((ws, url))
    return ws


class CustomStaticResource(StaticResource):
    def __init__(self, *args: Any, add_tail_snippet: bool = False, **kwargs: Any):
        self._add_tail_snippet = add_tail_snippet
        super().__init__(*args, **kwargs)
        self._show_index = True

    def modify_request(self, request: web.Request) -> None:
        """
        Apply common path conventions eg. / > /index.html, /foobar > /foobar.html
        """
        filename = URL.build(path=request.match_info['filename'], encoded=True).path
        raw_path = self._directory.joinpath(filename)
        try:
            filepath = raw_path.resolve(strict=True)
        except FileNotFoundError:
            try:
                html_file = raw_path.with_name(raw_path.name + '.html').resolve().relative_to(self._directory)
            except (FileNotFoundError, ValueError):
                pass
            else:
                request.match_info['filename'] = str(html_file)
        else:
            if filepath.is_dir():
                index_file = filepath / 'index.html'
                if index_file.exists():
                    try:
                        request.match_info['filename'] = str(index_file.relative_to(self._directory))
                    except ValueError:
                        # path is not not relative to self._directory
                        pass

    def _insert_footer(self, response: web.StreamResponse) -> web.StreamResponse:
        if not isinstance(response, web.FileResponse) or not self._add_tail_snippet:
            return response

        filepath = response._path
        ct, encoding = mimetypes.guess_type(str(response._path))
        if ct != 'text/html':
            return response

        with filepath.open('rb') as f:
            body = f.read() + LIVE_RELOAD_LOCAL_SNIPPET

        resp = web.Response(body=body, content_type="text/html")
        # Mypy bug: https://github.com/python/mypy/issues/11892
        resp.last_modified = filepath.stat().st_mtime  # type: ignore[assignment]
        return resp

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        self.modify_request(request)
        try:
            response = await super()._handle(request)
            response = self._insert_footer(response)
        except HTTPNotModified:
            raise
        except HTTPNotFound:
            # TODO include list of files in 404 body
            _404_msg = '404: Not Found\n'
            response = web.Response(body=_404_msg.encode(), status=404, content_type='text/plain')
        else:
            # Inject CORS headers to allow webfonts to load correctly
            response.headers['Access-Control-Allow-Origin'] = '*'

        # Add no-cache header to avoid browser caching in local development.
        response.headers["Cache-Control"] = "no-cache"

        return response
