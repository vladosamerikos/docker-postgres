#!/usr/bin/env python3
"""Configure and start the PostgreSQL Docker template."""
import argparse
from contextlib import contextmanager
import datetime as dt
from decimal import Decimal, InvalidOperation
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time

import postgres_cli as shared

VERSIONS = tuple(str(version) for version in range(10, 19))
SERVICES = {'postgres-none', 'postgres-host', 'postgres-lan'}


class Terminal:
    def __init__(self):
        try:
            self.input = open('/dev/tty', 'r', encoding='utf-8')
            self.handle = open('/dev/tty', 'w', encoding='utf-8', buffering=1)
        except OSError as error:
            raise shared.ToolError('Setup requires an interactive terminal.') from error

    def ask(self, text, default=None):
        self.handle.write(text + (f' [{default}]' if default is not None else '') + ': ')
        self.handle.flush()
        response = self.input.readline()
        if not response:
            raise EOFError
        value = response.rstrip('\r\n').strip()
        return value or default or ''

    def choose(self, text, default, validator):
        while True:
            value = self.ask(text, default)
            try:
                validator(value)
                return value
            except (shared.ToolError, ValueError) as error:
                shared.message(str(error), 'WARN', self.handle)

    def yes(self, text, default=False):
        return self.choose(text + ' (yes/no)', 'yes' if default else 'no', validate_yes).lower() in ('yes', 'y')

    def close(self):
        self.handle.close()
        self.input.close()


def validate_yes(value):
    if value.lower() not in ('yes', 'no', 'y', 'n'):
        raise shared.ToolError('Enter yes or no.')


def validate_name(value):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}', value):
        raise shared.ToolError('Use 1-63 letters, digits, dots, underscores or hyphens, starting with a letter or digit.')


def validate_initial_database(value):
    validate_name(value)
    if value in ('template0', 'template1'):
        raise shared.ToolError('Choose postgres or a new database name; template databases cannot be used for application connections.')


def validate_hostname(value):
    if len(value) > 63 or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', value):
        raise shared.ToolError('Enter a Docker hostname of up to 63 letters, digits, dots or hyphens.')


def validate_admin(value):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', value) or value.startswith('pg_'):
        raise shared.ToolError('Use 1-63 letters, digits or underscores; avoid the reserved pg_ prefix.')


def validate_lan_host(value):
    if not value:
        return
    try:
        ipaddress.ip_address(value.strip('[]'))
    except ValueError:
        validate_hostname(value)


def memory_bytes(value):
    match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(b|k|kb|m|mb|g|gb)?', value.lower())
    if not match:
        raise shared.ToolError('Enter memory such as 512M or 4G.')
    return int(Decimal(match[1]) * {'b': 1, 'k': 1024, 'm': 1024**2, 'g': 1024**3}.get((match[2] or 'b')[0], 1))


def validate_memory(value):
    if memory_bytes(value) < 16 * 1024**2:
        raise shared.ToolError('Memory must be at least 16M.')


def cpu_value(value):
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise shared.ToolError('Enter a positive CPU count, such as 0.5 or 2.') from error
    if not result.is_finite() or result <= 0:
        raise shared.ToolError('CPU count must be positive.')
    return result


def openssl_password():
    result = subprocess.run(['openssl', 'rand', '-hex', '32'], capture_output=True, text=True)
    if result.returncode or not re.fullmatch(r'[0-9a-f]{64}', result.stdout.strip()):
        raise shared.ToolError('OpenSSL could not generate an administrator password.')
    return result.stdout.strip()


def environment():
    return {key: value for key, value in os.environ.items()
            if not key.startswith(('POSTGRES_', 'HOST_POSTGRES_', 'COMPOSE_'))
            and key not in ('CONTAINER_NAME', 'storecredentials', 'STORE_CREDENTIALS')}


def docker(*args, check=True, **kwargs):
    process = subprocess.run(['docker', *args], capture_output=True, text=True, env=environment(), **kwargs)
    if check and process.returncode:
        raise shared.ToolError('Docker command failed: ' + process.stderr.strip()[-1500:])
    return process


def compose(root, env_file, *args, check=True):
    return docker('compose', '--project-directory', str(root), '--env-file', str(env_file),
                  '-f', str(root / 'docker-compose.yml'), *args, check=check)


def inspect_container(name):
    result = docker('container', 'inspect', name, check=False)
    return json.loads(result.stdout)[0] if result.returncode == 0 else None


def require_owned_container(container, project, root=None):
    if container:
        labels = container['Config'].get('Labels') or {}
        if (labels.get('com.docker.compose.project') != project or labels.get('com.docker.compose.service') not in SERVICES
                or root is not None and labels.get('com.docker.compose.project.working_dir') != str(root)):
            raise shared.ToolError('That container belongs to another installation; choose another name.')


def data_exists(root):
    data = root / 'data'
    if data.is_symlink():
        raise shared.ToolError('The data path must not be a symlink.')
    try:
        return data.exists() and any(data.iterdir())
    except PermissionError:
        # PostgreSQL data is often mode 700 and owned by the container's UID.
        return True


def validate_port(value):
    if not value.isdigit() or not 1 <= int(value) <= 65535:
        raise shared.ToolError('Port must be between 1 and 65535.')


def port_available(mode, port, own_container=None):
    validate_port(str(port))
    port = str(int(port))
    host = '127.0.0.1' if mode == 'host' else '0.0.0.0'
    # Check all Docker mappings, including NAT-only publishing without a proxy.
    running = docker('ps', '-q').stdout.split()
    own_binding = False
    if running:
        containers = json.loads(docker('inspect', *running).stdout)
        for container in containers:
            for entries in (container.get('NetworkSettings', {}).get('Ports') or {}).values():
                for entry in entries or []:
                    if entry['HostPort'] == port:
                        ip = entry.get('HostIp', '0.0.0.0')
                        overlaps = host == '0.0.0.0' or ip in ('0.0.0.0', '::', '', host)
                        if overlaps:
                            if own_container and container['Id'] == own_container['Id']:
                                own_binding = True
                            else:
                                return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, int(port)))
            return True
        except OSError:
            # Recheck native conflicts after recreation releases the old mapping.
            return own_binding


def render_env(source, updates):
    """Preserve comments/custom keys and original quoting of unchanged passwords."""
    pending = {}
    for key, value in updates.items():
        if any(char in value for char in "'\n\r\x00"):
            raise shared.ToolError(f'Unsupported setup value for {key}.')
        pending[key] = key + "='" + value + "'"
    lines = []
    for line in source.splitlines():
        match = re.match(r'\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
        key = match[1] if match else None
        if key in updates:
            if key in pending:
                lines.append(pending.pop(key))
        else:
            lines.append(line)
    lines.extend(pending.values())
    return '\n'.join(lines) + '\n'


def private_write(path, content):
    """Atomic owner-only publication. Never truncate an existing symlink."""
    if path.is_symlink():
        raise shared.ToolError(f'Refusing symlink: {path.name}')
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise shared.ToolError(f'{path.name} must be an ordinary file owned by the current user without hard links.')
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.postgres-setup-', delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content.encode())
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def setup_lock(root):
    path = root / '.setup.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise shared.ToolError('Unsafe setup lock file.')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise shared.ToolError('Another setup is already running in this directory.') from error
        yield
    finally:
        os.close(fd)


def wait_ready(config, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        container = inspect_container(config.container)
        if container and container['State'].get('Health', {}).get('Status') == 'healthy':
            pg = shared.Postgres(config)
            if str(pg.version_num // 10000) != re.match(r'\d+', config.values['POSTGRES_VERSION'])[0]:
                raise shared.ToolError('Running server version does not match .env; no migration is performed.')
            return pg
        if container and container['State'].get('Status') in ('exited', 'dead'):
            raise shared.ToolError('PostgreSQL exited before becoming healthy. Check docker compose logs.')
        time.sleep(1)
    raise shared.ToolError(f'PostgreSQL did not become healthy within {timeout} seconds. Check docker compose logs.')


def gather(root, terminal, values, initialized, container, forced_version):
    updates = {}
    if initialized:
        version = values.get('POSTGRES_VERSION', '')
        if not re.fullmatch(r'(?:1[0-8])(?:\.\d+)*(?:-[A-Za-z0-9_.-]+)?', version):
            raise shared.ToolError('Installed POSTGRES_VERSION must identify PostgreSQL 10-18. Setup does not migrate data.')
        if forced_version and forced_version != re.match(r'\d+', version)[0]:
            raise shared.ToolError('Changing the version of an existing installation is not allowed.')
        shared.message(f'Existing PostgreSQL {version}: version and administrator credentials are preserved.')
    else:
        default = re.match(r'\d+', values.get('POSTGRES_VERSION', '18'))
        version = forced_version or terminal.choose('PostgreSQL major version (10-18)', default[0] if default and default[0] in VERSIONS else '18',
                                                    lambda value: None if value in VERSIONS else invalid('Choose PostgreSQL 10 through 18.'))
        if terminal.yes('Use the official Alpine image', True):
            version += '-alpine'
        updates['POSTGRES_VERSION'] = version
        updates['POSTGRES_USER'] = terminal.choose('Administrator username', values.get('POSTGRES_USER') or 'postgres', validate_admin)
        updates['POSTGRES_DB'] = terminal.choose('Initial database', values.get('POSTGRES_DB') or 'postgres', validate_initial_database)
        if not values.get('POSTGRES_PASSWORD'):
            updates['POSTGRES_PASSWORD'] = openssl_password()
            shared.message('Generated a 256-bit administrator password with OpenSSL. It will be saved in .env.')
    major = re.match(r'\d+', version)[0]
    defaults = {**values, **updates}
    if not initialized:
        for key in ('POSTGRES_HOSTNAME', 'CONTAINER_NAME', 'COMPOSE_PROJECT_NAME'):
            if not defaults.get(key) or re.fullmatch(r'postgres1[0-8]', defaults[key]):
                defaults[key] = 'postgres' + major
        if not defaults.get('HOST_POSTGRES_PORT') or re.fullmatch(r'541[0-8]', defaults['HOST_POSTGRES_PORT']):
            defaults['HOST_POSTGRES_PORT'] = '54' + major
    host_default = defaults.get('POSTGRES_HOSTNAME') or 'postgres' + major
    updates['POSTGRES_HOSTNAME'] = terminal.choose('Docker hostname', host_default, validate_hostname)
    project = defaults.get('COMPOSE_PROJECT_NAME') or 'postgres' + major
    def check_project(value):
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', value):
            raise shared.ToolError('Use lowercase letters, digits, underscores or hyphens.')
        ids = docker('ps', '-a', '-q', '--filter', 'label=com.docker.compose.project=' + value).stdout.split()
        for existing_id in ids:
            require_owned_container(inspect_container(existing_id), value, root)
    if not initialized:
        project = terminal.choose('Compose project name', project, check_project)
    else:
        check_project(project)
    updates['COMPOSE_PROJECT_NAME'] = project
    def check_container_name(name):
        validate_name(name)
        current = inspect_container(name)
        require_owned_container(current, project, root)
        if current and (not container or current['Id'] != container['Id']):
            raise shared.ToolError('That container name is already in use by another installation.')
    updates['CONTAINER_NAME'] = terminal.choose('Container name', defaults.get('CONTAINER_NAME') or updates['POSTGRES_HOSTNAME'], check_container_name)
    shared.message('Network modes: none = Docker only; host = 127.0.0.1; lan = 0.0.0.0.')
    default_mode = values.get('POSTGRES_PORT_MODE', 'host')
    if default_mode not in ('none', 'host', 'lan'):
        default_mode = 'host'
    mode = terminal.choose('Network mode (none/host/lan)', default_mode, lambda value: None if value in ('none', 'host', 'lan') else invalid('Choose none, host or lan.'))
    updates.update(POSTGRES_PORT_MODE=mode, COMPOSE_PROFILES=mode, COMPOSE_FILE='docker-compose.yml')
    port_default = defaults.get('HOST_POSTGRES_PORT') or '54' + major
    if mode != 'none':
        def check_port(value):
            if not port_available(mode, value, container):
                raise shared.ToolError('That port is unavailable. Choose another port; previous answers are preserved.')
        updates['HOST_POSTGRES_PORT'] = terminal.choose('External PostgreSQL port', port_default, check_port)
    else:
        updates['HOST_POSTGRES_PORT'] = port_default
    updates['POSTGRES_LAN_HOST'] = terminal.choose('Server LAN IP/hostname (optional)', values.get('POSTGRES_LAN_HOST', ''), validate_lan_host) if mode == 'lan' else values.get('POSTGRES_LAN_HOST', '')
    default_store = shared.boolean(values.get('storecredentials', values.get('STORE_CREDENTIALS', 'false')))
    updates['storecredentials'] = str(terminal.yes('Store credentials in .credentials', default_store)).lower()
    updates['POSTGRES_NETWORK_NAME'] = terminal.choose('Docker network name', values.get('POSTGRES_NETWORK_NAME') or project + '_network', validate_name)
    maximum_cpu = int(docker('info', '--format', '{{.NCPU}}').stdout)
    def check_cpu(value):
        if cpu_value(value) > maximum_cpu:
            raise shared.ToolError(f'CPU limit cannot exceed the Docker host count ({maximum_cpu}).')
    updates['POSTGRES_LIMIT_CPUS'] = terminal.choose('CPU limit', values.get('POSTGRES_LIMIT_CPUS') or str(min(2, maximum_cpu)), check_cpu)
    updates['POSTGRES_LIMIT_MEMORY'] = terminal.choose('Memory limit (e.g. 4G, 512M)', values.get('POSTGRES_LIMIT_MEMORY') or '4G', validate_memory)
    def check_cpu_reservation(value):
        if cpu_value(value) > cpu_value(updates['POSTGRES_LIMIT_CPUS']):
            raise shared.ToolError('CPU reservation cannot exceed its limit.')
    updates['POSTGRES_RESERVATION_CPUS'] = terminal.choose('CPU reservation', values.get('POSTGRES_RESERVATION_CPUS') or str(min(Decimal('0.5'), cpu_value(updates['POSTGRES_LIMIT_CPUS']))), check_cpu_reservation)
    def check_memory_reservation(value):
        validate_memory(value)
        if memory_bytes(value) > memory_bytes(updates['POSTGRES_LIMIT_MEMORY']):
            raise shared.ToolError('Memory reservation cannot exceed its limit.')
    memory_default = values.get('POSTGRES_RESERVATION_MEMORY') or '200M'
    if memory_bytes(memory_default) > memory_bytes(updates['POSTGRES_LIMIT_MEMORY']):
        memory_default = updates['POSTGRES_LIMIT_MEMORY']
    updates['POSTGRES_RESERVATION_MEMORY'] = terminal.choose('Memory reservation', memory_default, check_memory_reservation)
    return updates


def invalid(text):
    raise shared.ToolError(text)


def run_setup(root, terminal, forced_version=None):
    env_file = root / '.env'
    example = root / '.env.example'
    if env_file.is_symlink():
        raise shared.ToolError('The .env path must not be a symlink.')
    source_path = env_file if env_file.exists() else example
    if not source_path.exists():
        raise shared.ToolError('This template must contain .env or .env.example.')
    source = source_path.read_text()
    values = shared.load_dotenv(source_path)
    old_container = inspect_container(values.get('CONTAINER_NAME') or 'postgres' + values.get('POSTGRES_VERSION', '18'))
    project = values.get('COMPOSE_PROJECT_NAME') or 'postgres' + values.get('POSTGRES_VERSION', '18')
    local_state = bool(data_exists(root) or (env_file.exists() and values.get('POSTGRES_PASSWORD')))
    try:
        require_owned_container(old_container, project, root)
    except shared.ToolError:
        if local_state:
            raise
        old_container = None  # A fresh template may choose a different, unused name.
    initialized = bool(old_container or local_state)
    if initialized and not env_file.exists():
        raise shared.ToolError('Existing data/container requires its original .env. Restore it before running setup.')
    if initialized and not values.get('POSTGRES_PASSWORD'):
        raise shared.ToolError('Existing data/container found but POSTGRES_PASSWORD is missing. Restore the current administrator password in .env before setup; no password was changed.')
    if old_container:
        image = old_container['Config']['Image']
        image_major = re.search(r':(\d+)', image)
        env_major = re.match(r'\d+', values.get('POSTGRES_VERSION', ''))
        if image_major and env_major and image_major[1] != env_major[0]:
            raise shared.ToolError('Container image and POSTGRES_VERSION disagree; setup will not change the database version.')
    if old_container and old_container['State']['Running']:
        # Verify credentials before stopping a running installation.
        pg = shared.Postgres(shared.Config(env_file, environ={}))
        if str(pg.version_num // 10000) != re.match(r'\d+', values.get('POSTGRES_VERSION', ''))[0]:
            raise shared.ToolError('Existing server version differs from .env; no migration is performed.')
    updates = gather(root, terminal, values, initialized, old_container, forced_version)
    generated = 'POSTGRES_PASSWORD' in updates
    previous_source = env_file.read_text() if env_file.exists() else None
    previous_running = bool(old_container and old_container['State']['Running'])
    while True:
        draft = render_env(source, updates)
        with tempfile.NamedTemporaryFile(dir=root, prefix='.postgres-setup-', suffix='.env', delete=False) as handle:
            pending = Path(handle.name)
            handle.write(draft.encode())
        try:
            config = shared.Config(pending, environ={})
            resolved = compose(root, pending, 'config', '--format', 'json')
            definition = json.loads(resolved.stdout)
            active = list(definition.get('services', {}))
            if active != ['postgres-' + config.mode]:
                raise shared.ToolError('Compose must select exactly the configured PostgreSQL profile; this template is incompatible.')
            expected = definition['services'][active[0]]
            pgdata = expected.get('environment', {}).get('PGDATA')
            if pgdata != '/var/lib/postgresql/template-data':
                raise shared.ToolError('This setup requires the template PGDATA path /var/lib/postgresql/template-data.')
            if initialized and old_container:
                env = dict(entry.split('=', 1) for entry in old_container['Config'].get('Env', []) if '=' in entry)
                if env.get('PGDATA', pgdata) != pgdata:
                    raise shared.ToolError('Existing PGDATA differs from this template. Keep the original mount layout; setup will not move data.')
            if config.store:
                os.close(shared.open_credentials(config))
            print('\nPostgreSQL setup\n' + '-' * 55)
            for label, value in [('Directory', str(root)), ('Version', config.values['POSTGRES_VERSION']),
                                 ('Administrator', config.admin), ('Hostname', config.hostname),
                                 ('Container', config.container), ('Network mode', config.mode),
                                 ('External port', config.port if config.mode != 'none' else 'not published'),
                                 ('Credentials', str(config.store).lower()), ('CPU limit', config.values['POSTGRES_LIMIT_CPUS']),
                                 ('Memory limit', config.values['POSTGRES_LIMIT_MEMORY'])]:
                print(f'  {label:<18} {value}')
            if not terminal.yes('Apply configuration and start PostgreSQL', True):
                shared.message('Cancelled. Existing configuration and containers were not changed.')
                return {'ok': True, 'cancelled': True}
            stopped = False
            published = False
            try:
                shared.message('Pulling the latest image for the selected PostgreSQL tag...')
                compose(root, pending, 'pull')
                # Stop the old profile before switching modes to release its published port.
                if old_container:
                    compose(root, env_file, 'down')
                    stopped = True
                if config.mode != 'none' and not port_available(config.mode, config.port):
                    raise shared.ToolError('The selected port became unavailable before startup.')
                private_write(env_file, draft)
                published = True
                shared.message('Starting PostgreSQL with Docker Compose...')
                compose(root, env_file, 'up', '-d', '--force-recreate')
                live = wait_ready(config)
            except (shared.ToolError, OSError, subprocess.SubprocessError, KeyboardInterrupt) as error:
                shared.message('Startup failed. Restoring the previous configuration.', 'WARN')
                if published:
                    compose(root, env_file, 'down', check=False)
                    # A fresh installation keeps its generated .env for a safe retry.
                    if initialized and previous_source is not None:
                        private_write(env_file, previous_source)
                if previous_running and stopped:
                    recovery = compose(root, env_file, 'up', '-d', check=False)
                    if recovery.returncode:
                        raise shared.ToolError('Startup and recovery failed. .env was restored; inspect docker compose logs before retrying.') from error
                if isinstance(error, KeyboardInterrupt):
                    raise
                shared.message(str(error), 'ERROR')
                if not terminal.yes('Retry startup without losing previous answers', True):
                    raise shared.ToolError('Setup did not start the new configuration. Existing data was preserved.') from error
                if config.mode != 'none':
                    def retry_port(value):
                        if not port_available(config.mode, value, inspect_container(values.get('CONTAINER_NAME', '')) if previous_running else None):
                            raise shared.ToolError('That port is unavailable; try another.')
                    updates['HOST_POSTGRES_PORT'] = terminal.choose('External PostgreSQL port', updates.get('HOST_POSTGRES_PORT', config.port), retry_port)
                continue
            if generated and config.store:
                shared.store_credentials(config, {'ok': True, 'action': 'setup-admin', 'username': config.admin,
                    'password': config.password, 'database': config.database,
                    'connections': shared.connection_strings(config, config.database, config.admin, config.password),
                    'created_at': dt.datetime.now(dt.timezone.utc).isoformat()})
            shared.message(f'PostgreSQL {live.version} is ready.', 'OK')
            shared.message(f'Configuration saved to {env_file} (mode 600).')
            print(f'\nManage: {root / "scripts/manage.sh"}\nBackup: {root / "scripts/backup.sh"}')
            return {'ok': True, 'version': live.version, 'container': config.container}
        finally:
            pending.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--version', choices=VERSIONS)
    parser.add_argument('--no-color', action='store_true')
    args = parser.parse_args()
    if args.no_color:
        os.environ['NO_COLOR'] = '1'
    terminal = None
    try:
        for command in ('docker', 'openssl'):
            if not shutil.which(command):
                raise shared.ToolError(f'{command} is required.')
        docker('compose', 'version', timeout=30)
        docker('info', timeout=30)
        root = args.root.expanduser().resolve()
        terminal = Terminal()
        with setup_lock(root):
            run_setup(root, terminal, args.version)
        return 0
    except (shared.ToolError, OSError, ValueError, subprocess.SubprocessError) as error:
        shared.message(str(error), 'ERROR')
        return 1
    except (KeyboardInterrupt, EOFError):
        shared.message('Cancelled.', 'WARN')
        return 130
    finally:
        if terminal:
            terminal.close()


if __name__ == '__main__':
    sys.exit(main())
