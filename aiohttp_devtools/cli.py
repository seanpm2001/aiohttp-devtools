import sys
import traceback
from typing import Any

import click
from aiohttp.web import run_app

from . import __version__
from .exceptions import AiohttpDevException
from .logs import main_logger, setup_logging
from .runserver import INFER_HOST
from .runserver import runserver as _runserver
from .runserver import serve_static

_dir_existing = click.Path(exists=True, dir_okay=True, file_okay=False)
_file_dir_existing = click.Path(exists=True, dir_okay=True, file_okay=True)
_dir_may_exist = click.Path(dir_okay=True, file_okay=False, writable=True, resolve_path=True)


@click.group()
@click.version_option(__version__, "-V", "--version", prog_name="aiohttp-devtools")
def cli() -> None:
    pass


verbose_help = 'Enable verbose output.'
livereload_help = ('Whether to inject livereload.js into html page footers to autoreload on changes. '
                   'env variable AIO_LIVERELOAD')


@cli.command()
@click.argument('path', type=_dir_existing, required=True)
@click.option('--livereload/--no-livereload', envvar='AIO_LIVERELOAD', default=True, help=livereload_help)
@click.option('-p', '--port', default=8000, type=int)
@click.option('-v', '--verbose', is_flag=True, help=verbose_help)
def serve(path: str, livereload: bool, port: int, verbose: bool) -> None:
    """
    Serve static files from a directory.
    """
    setup_logging(verbose)
    run_app(**serve_static(static_path=path, livereload=livereload, port=port))


static_help = "Path of static files to serve, if excluded static files aren't served. env variable: AIO_STATIC_STATIC"
root_help = ('Root directory project used to qualify other paths. '
             'This can be used to watch a parent directory for changes in a more complex application. '
             'env variable: AIO_ROOT')
static_url_help = 'URL path to serve static files from, default "/static/". env variable: AIO_STATIC_URL'
host_help = ('host used when referencing livereload and static files, if blank host is taken from the request header '
             'with default of localhost. env variable AIO_HOST')
app_factory_help = ('name of the app factory to create an aiohttp.web.Application with, if missing default app-factory '
                    'names are tried. This can be either a function with signature '
                    '"def create_app(loop): -> Application" or "def create_app(): -> Application" '
                    'or just an instance of aiohttp.Application. env variable AIO_APP_FACTORY')
port_help = 'Port to serve app from, default 8000. env variable: AIO_PORT'
aux_port_help = 'Port to serve auxiliary app (reload and static) on, default port + 1. env variable: AIO_AUX_PORT'


# defaults are all None here so default settings are defined in one place: DEV_DICT validation
@cli.command()
@click.argument('app-path', envvar='AIO_APP_PATH', type=_file_dir_existing, required=False)
@click.option('-s', '--static', 'static_path', envvar='AIO_STATIC_PATH', type=_dir_existing, help=static_help)
@click.option('--root', 'root_path', envvar='AIO_ROOT', type=_dir_existing, help=root_help)
@click.option('--static-url', envvar='AIO_STATIC_URL', help=static_url_help)
@click.option('--livereload/--no-livereload', envvar='AIO_LIVERELOAD', default=None, help=livereload_help)
@click.option('--host', default=INFER_HOST, help=host_help)
@click.option('--app-factory', 'app_factory_name', envvar='AIO_APP_FACTORY', help=app_factory_help)
@click.option('-p', '--port', 'main_port', envvar='AIO_PORT', type=click.INT, help=port_help)
@click.option('--aux-port', envvar='AIO_AUX_PORT', type=click.INT, help=aux_port_help)
@click.option('-v', '--verbose', is_flag=True, help=verbose_help)
@click.argument('project_args', nargs=-1)
def runserver(**config: Any) -> None:
    """
    Run a development server for an aiohttp apps.

    Takes one argument "app-path" which should be a path to either a directory containing a recognized default file
    ("app.py" or "main.py") or to a specific file. Defaults to the environment variable "AIO_APP_PATH" or ".".

    The app path is run directly, see the "--app-factory" option for details on how an app is loaded from a python
    module.
    """
    active_config = {k: v for k, v in config.items() if v is not None}
    setup_logging(config['verbose'])
    # Rewrite argv for the application.
    sys.argv[1:] = active_config.pop('project_args')
    try:
        run_app(**_runserver(**active_config))
    except AiohttpDevException as e:
        if config['verbose']:
            tb = click.style(traceback.format_exc().strip('\n'), fg='white', dim=True)
            main_logger.warning('AiohttpDevException traceback:\n%s', tb)
        main_logger.error('Error: %s', e)
        sys.exit(2)
