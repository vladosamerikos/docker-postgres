#!/usr/bin/env python3
"""Commands for the PostgreSQL Docker template."""
import argparse
from contextlib import contextmanager, nullcontext
import datetime as dt
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import quote

TEMPLATE_ROOT = Path(__file__).resolve().parents[2]
SYSTEM_DATABASES = {'postgres', 'template0', 'template1'}

class ToolError(Exception):
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details


def emit(value):
    print(json.dumps(value, ensure_ascii=True, separators=(',', ':')))


def message(text, level='INFO', stream=None):
    stream = stream or sys.stderr
    color = stream.isatty() and 'NO_COLOR' not in os.environ and os.environ.get('TERM') != 'dumb'
    colors = {'INFO': '36', 'OK': '32', 'ERROR': '31', 'WARN': '33'}
    label = f'\033[{colors.get(level, "36")}m{level:<5}\033[0m' if color else f'{level:<5}'
    print(f'{label} {text}', file=stream)


def load_dotenv(path):
    """Read values without sourcing executable shell code or splitting passwords."""
    values = {}
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError as error:
        raise ToolError(f'Cannot read configuration file: {path}') from error
    for number, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)', line)
        if not match:
            raise ToolError(f'Invalid .env assignment on line {number}.')
        key, value = match.groups()
        literal = value.startswith("'")
        if value.startswith(('"', "'")):
            delimiter = value[0]
            closing = None
            for index in range(1, len(value)):
                backslashes = len(value[:index]) - len(value[:index].rstrip('\\'))
                if value[index] == delimiter and (delimiter == "'" or backslashes % 2 == 0):
                    closing = index
                    break
            if closing is None or (value[closing + 1:].strip() and not value[closing + 1:].lstrip().startswith('#')):
                raise ToolError(f'Invalid quoted value on .env line {number}.')
            value = value[1:closing]
            if not literal:
                escapes = {'n': '\n', 'r': '\r', 't': '\t', '\\': '\\', '"': '"'}
                value = re.sub(r'\\([nrt\\"])', lambda item: escapes[item[1]], value)
        else:
            value = re.split(r'\s+#', value, maxsplit=1)[0].rstrip()
        if not literal:
            def expand(item):
                name, default = item[1], item[2]
                return os.environ.get(name, values.get(name, '')) or (default or '')
            value = re.sub(r'\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)',
                           lambda item: expand(item) if item[1] else os.environ.get(item[3], values.get(item[3], '')), value)
        values[key] = value
    return values


def boolean(value):
    value = str(value).lower()
    if value not in ('true', 'false', '1', '0', 'yes', 'no', 'on', 'off'):
        raise ToolError('STORE_CREDENTIALS must be true or false.')
    return value in ('true', '1', 'yes', 'on')


def open_credentials(config):
    """Never follow symlinks; enforce an owner-only regular file."""
    try:
        fd = os.open(config.credentials, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != os.getuid() or file_stat.st_nlink != 1:
                raise ToolError('.credentials must be an ordinary file owned by the current user, without hard links.')
            os.fchmod(fd, 0o600)
            return fd
        except (OSError, ToolError):
            os.close(fd)
            raise
    except OSError as error:
        raise ToolError('Cannot securely open .credentials; check its path and permissions.') from error


def store_credentials(config, record):
    record['credentials_stored'] = True
    try:
        with os.fdopen(open_credentials(config), 'a', encoding='utf-8') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            handle.write(json.dumps(record, ensure_ascii=True) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
    except (OSError, ToolError) as error:
        record['credentials_stored'] = False
        raise ToolError('PostgreSQL operation succeeded but credentials could not be saved.', details=record) from error


class Config:
    def __init__(self, env_file=None, environ=None):
        environ = os.environ if environ is None else environ
        self.path = Path(env_file or environ.get('POSTGRES_ENV_FILE', TEMPLATE_ROOT / '.env')).expanduser().resolve()
        self.root = self.path.parent
        self.values = load_dotenv(self.path)
        self.values.update(environ)
        for key in ('CONTAINER_NAME', 'POSTGRES_USER', 'POSTGRES_PASSWORD'):
            if not self.values.get(key):
                raise ToolError(f'Missing required configuration: {key}')
        self.container = self.values['CONTAINER_NAME']
        self.admin = identifier(self.values['POSTGRES_USER'])
        self.password = validate_password(self.values['POSTGRES_PASSWORD'])
        self.database = identifier(self.values.get('POSTGRES_DB', 'postgres'))
        self.hostname = self.values.get('POSTGRES_HOSTNAME', self.container)
        self.port = self.values.get('HOST_POSTGRES_PORT', '5432')
        if not self.port.isdigit() or not 1 <= int(self.port) <= 65535:
            raise ToolError('HOST_POSTGRES_PORT must be between 1 and 65535.')
        self.mode = self.values.get('POSTGRES_PORT_MODE', 'host')
        if self.mode not in ('none', 'host', 'lan'):
            raise ToolError('POSTGRES_PORT_MODE must be none, host or lan.')
        self.store = boolean(self.values.get('storecredentials', self.values.get('STORE_CREDENTIALS', 'false')))
        self.credentials = self.root / '.credentials'


def identifier(value):
    if not value or len(value.encode('utf-8')) > 63 or re.search(r'[\x00-\x1f\x7f]', value):
        raise ToolError('Names must contain 1-63 UTF-8 bytes without control characters.')
    return value


def database_name(value, protect=False):
    identifier(value)
    if protect and value in SYSTEM_DATABASES:
        raise ToolError('Modifying postgres, template0 or template1 is not allowed by this command.')
    return value


def validate_password(value):
    if not value or any(char in value for char in ('\x00', '\n', '\r')):
        raise ToolError('Passwords must be nonempty and cannot contain NUL or line breaks.')
    return value


def qi(value):
    return '"' + identifier(value).replace('"', '""') + '"'


def ql(value):
    """Explicit escape literals work even if standard_conforming_strings is off."""
    if '\x00' in value:
        raise ToolError('SQL values cannot contain NUL.')
    return "E'" + value.replace('\\', '\\\\').replace("'", "''") + "'"


def conninfo(database):
    # Quoting prevents names such as x=y from becoming libpq connection options.
    return "dbname='" + database.replace('\\', '\\\\').replace("'", "\\'") + "'"


def permission_flags(value):
    aliases = {'connect': 'c', 'read': 'r', 'write': 'w', 'create': 'a', 'superuser': 's'}
    value = value.lower()
    if ',' in value or value in aliases:
        try:
            value = ''.join(aliases[item.strip()] for item in value.split(','))
        except KeyError as error:
            raise ToolError('Use c/r/w/a/s or connect,read,write,create,superuser.') from error
    if not re.fullmatch('[crwas]+', value):
        raise ToolError('Use c/r/w/a/s or connect,read,write,create,superuser.')
    return ''.join(letter for letter in 'crwas' if letter in value)


def connection_strings(config, database, username=None, password=None):
    auth = quote(username, safe='') if username else ''
    if password is not None:
        auth += ':' + quote(password, safe='')
    auth = auth + '@' if auth else ''
    def uri(host, port):
        if ':' in host and not host.startswith('['):
            host = '[' + host + ']'
        return f'postgresql://{auth}{host}:{port}/{quote(database, safe="")}'
    lan_host = config.values.get('POSTGRES_LAN_HOST', '')
    return {'localhost': uri('127.0.0.1', config.port) if config.mode != 'none' else None,
            'lan': uri(lan_host, config.port) if config.mode == 'lan' and lan_host else None,
            'postgres_network': uri(config.hostname, '5432')}


class Postgres:
    def __init__(self, config):
        self.config = config
        if not shutil.which('docker'):
            raise ToolError('Docker CLI is required.')
        probe = subprocess.run(['docker', 'inspect', '--format', '{{.State.Running}}', config.container],
                               capture_output=True, text=True, timeout=30)
        if probe.returncode or probe.stdout.strip() != 'true':
            raise ToolError(f'Container {config.container!r} is not running, or Docker is inaccessible.')
        status = self.rows("SELECT current_setting('server_version_num')::integer AS number, current_setting('server_version') AS version, (SELECT rolsuper FROM pg_roles WHERE rolname=current_user) AS superuser")[0]
        self.version_num, self.version = status['number'], status['version']
        if self.version_num < 100000:
            raise ToolError('PostgreSQL 10 or newer is required.')
        if not status['superuser']:
            raise ToolError('POSTGRES_USER must be a superuser for administration.')

    def tool_command(self, tool, options):
        # Read the password through stdin, keeping it out of host/container argv.
        shell = 'IFS= read -r PGPASSWORD || exit 1; export PGPASSWORD; export PGCONNECT_TIMEOUT=10 PGCLIENTENCODING=UTF8; exec "$@"'
        return ['docker', 'exec', '-i', self.config.container, 'sh', '-c', shell, 'sh', tool,
                '--host=127.0.0.1', '--port=5432', '--username=' + self.config.admin, '--no-password', *options]

    def run_tool(self, tool, options, payload=b'', source=None, output=None):
        command = self.tool_command(tool, options)
        with tempfile.TemporaryFile() as staged:
            staged.write((self.config.password + '\n').encode())
            if source is not None:
                shutil.copyfileobj(source, staged)
            else:
                staged.write(payload)
            staged.seek(0)
            # Output is streamed to disk for backups; large dumps are never held in RAM.
            process = subprocess.run(command, stdin=staged, stdout=output or subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        if process.returncode:
            # SQL diagnostics can contain passwords or evaluated SQL; expose no raw source.
            diagnostic = process.stderr.decode(errors='replace')
            state = re.search(r'(?:ERROR|FATAL):\s+([0-9A-Z]{5}):', diagnostic)
            suffix = f' SQLSTATE {state[1]}.' if state else ''
            raise ToolError(f'{tool} failed (exit {process.returncode}).{suffix} Check credentials, permissions, object dependencies and container health.')
        if process.stderr:
            message(f'{tool} returned server notices or warnings.', 'WARN')
        return process.stdout or b''

    @contextmanager
    def private_file(self, data):
        probe = subprocess.run(['docker', 'exec', self.config.container, 'mktemp', '/tmp/postgres-tools.XXXXXXXX'],
                               capture_output=True, text=True)
        if probe.returncode:
            raise ToolError('Cannot create a temporary file in the container.')
        remote = probe.stdout.strip()
        try:
            result = subprocess.run(['docker', 'exec', '-i', self.config.container, 'sh', '-c',
                                     'cat > "$1" && chmod 600 "$1"', 'sh', remote], input=data, capture_output=True)
            if result.returncode:
                raise ToolError('Cannot write a temporary file in the container.')
            yield remote
        finally:
            subprocess.run(['docker', 'exec', self.config.container, 'rm', '-f', '--', remote], capture_output=True)

    def sql(self, sql, database='postgres'):
        return self.run_tool('psql', ['-X', '-q', '-t', '-A', '-v', 'ON_ERROR_STOP=1',
                                     '-v', 'VERBOSITY=verbose', '--dbname=' + conninfo(database)],
                             payload=sql.encode()).decode().strip()

    def rows(self, select, database='postgres'):
        return json.loads(self.sql('SELECT COALESCE(json_agg(row_to_json(t)), \'[]\'::json) FROM (' + select + ') t;', database))

    def transaction(self, statements, database):
        return self.sql('BEGIN;\n' + '\n'.join(statements) + '\nCOMMIT;', database)

    def role_exists(self, role):
        return self.sql(f'SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname={ql(role)});') == 't'

    def db_exists(self, database):
        return self.sql(f'SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname={ql(database)});') == 't'

    def require_role(self, role):
        identifier(role)
        if not self.role_exists(role):
            raise ToolError(f'User does not exist: {role}')

    def require_db(self, database):
        database_name(database)
        if not self.db_exists(database):
            raise ToolError(f'Database does not exist: {database}')

    def schemas(self, database, schema):
        names = [row['name'] for row in self.rows("SELECT nspname AS name FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema' ORDER BY 1", database)]
        if schema == 'all':
            return names
        identifier(schema)
        if schema not in names:
            raise ToolError(f'User schema does not exist: {schema}')
        return [schema]

    def creator_roles(self, database, names, extra=None):
        # Defaults belong to the actual object creator, not the grantee.
        roles = {self.config.admin}
        roles.update(row['owner'] for row in self.rows(
            'SELECT pg_get_userbyid(datdba) AS owner FROM pg_database WHERE datname=' + ql(database)))
        clause = ','.join(ql(name) for name in names)
        if names:
            roles.update(row['owner'] for row in self.rows(
                'SELECT DISTINCT pg_get_userbyid(c.relowner) AS owner FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN (' + clause + ')', database))
            roles.update(row['owner'] for row in self.rows(
                'SELECT pg_get_userbyid(nspowner) AS owner FROM pg_namespace WHERE nspname IN (' + clause + ')', database))
        # pg_database_owner is an implicit group, never the current role creating a table.
        roles.discard('pg_database_owner')
        if extra:
            for role in extra:
                self.require_role(role)
                roles.add(role)
        return sorted(roles)


def permission_sql(pg, database, username, flags, schema='public', creators=None, revoke=False):
    names = pg.schemas(database, schema)
    creators = pg.creator_roles(database, names, creators)
    verb, direction = ('REVOKE', 'FROM') if revoke else ('GRANT', 'TO')
    user = qi(username)
    statements = []
    if 's' in flags:
        statements.append(f'ALTER ROLE {user} ' + ('NOSUPERUSER;' if revoke else 'SUPERUSER;'))
    if 'c' in flags:
        statements.append(f'{verb} CONNECT ON DATABASE {qi(database)} {direction} {user};')
    tables = []
    if 'r' in flags:
        tables.append('SELECT')
    if 'w' in flags:
        tables += ['INSERT', 'UPDATE', 'DELETE']
    for name in names:
        ns = qi(name)
        if not revoke and any(letter in flags for letter in 'rwa'):
            statements.append(f'GRANT USAGE ON SCHEMA {ns} TO {user};')
        if 'a' in flags:
            statements.append(f'{verb} CREATE ON SCHEMA {ns} {direction} {user};')
        for privileges, kind in [(', '.join(tables), 'TABLES'),
                                 ('USAGE, SELECT' if 'w' in flags else '', 'SEQUENCES')]:
            if privileges:
                statements.append(f'{verb} {privileges} ON ALL {kind} IN SCHEMA {ns} {direction} {user};')
                for role in creators:
                    statements.append(f'ALTER DEFAULT PRIVILEGES FOR ROLE {qi(role)} IN SCHEMA {ns} {verb} {privileges} ON {kind} {direction} {user};')
    return statements, names, creators


def reset_permissions_sql(pg, database, username, schema, creators=None):
    names = pg.schemas(database, schema)
    roles = pg.creator_roles(database, names, creators)
    user = qi(username)
    statements = [f'REVOKE CONNECT ON DATABASE {qi(database)} FROM {user};',
                  f'ALTER ROLE {user} NOSUPERUSER;']
    for name in names:
        ns = qi(name)
        statements.append(f'REVOKE ALL ON SCHEMA {ns} FROM {user};')
        for kind in ('TABLES', 'SEQUENCES'):
            statements.append(f'REVOKE ALL ON ALL {kind} IN SCHEMA {ns} FROM {user};')
            statements.extend(f'ALTER DEFAULT PRIVILEGES FOR ROLE {qi(role)} IN SCHEMA {ns} REVOKE ALL ON {kind} FROM {user};' for role in roles)
    return statements


def harden_sql(database, admin, owner):
    return [f'REVOKE CONNECT, TEMPORARY ON DATABASE {qi(database)} FROM PUBLIC;',
            'REVOKE CREATE ON SCHEMA public FROM PUBLIC;',
            f'GRANT CONNECT, TEMPORARY ON DATABASE {qi(database)} TO {qi(admin)};',
            f'GRANT CONNECT, TEMPORARY ON DATABASE {qi(database)} TO {qi(owner)};',
            f'GRANT USAGE, CREATE ON SCHEMA public TO {qi(owner)};']


def create_database(pg, database, owner, encoding='UTF8'):
    pg.sql(f'CREATE DATABASE {qi(database)} OWNER {qi(owner)} ENCODING {ql(encoding)} TEMPLATE template0;')
    try:
        statements = harden_sql(database, pg.config.admin, owner)
        if pg.version_num < 150000:
            statements.append(f'ALTER SCHEMA public OWNER TO {qi(owner)};')
        pg.transaction(statements, database)
    except ToolError:
        pg.sql(f'DROP DATABASE {qi(database)};')
        raise


def protected_user(pg, username):
    identifier(username)
    if username in (pg.config.admin, 'postgres') or username.startswith('pg_'):
        raise ToolError('Modifying the configured administrator or a system role is not allowed.')


def password_for(args):
    if args.password is not None and args.password_stdin:
        raise ToolError('Use either --password or --password-stdin, not both.')
    if args.password_stdin:
        password = sys.stdin.readline().rstrip('\n')
    else:
        password = args.password if args.password is not None else secrets.token_urlsafe(24)
    return validate_password(password)


def require_yes(args):
    if not args.yes:
        raise ToolError('This operation requires --yes; no changes were made.')


def transfer_ownership(pg, args):
    database = database_name(args.database, protect=True)
    pg.require_db(database)
    pg.require_role(args.owner)
    old = pg.sql(f'SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname={ql(database)};')
    old = args.from_owner or old
    pg.require_role(old)
    new = args.owner
    statements = []
    counts = {}
    # REASSIGN OWNED would also change shared objects in other databases.
    namespace = "n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'"
    extension = "NOT EXISTS(SELECT 1 FROM pg_depend d WHERE d.classid=%s::regclass AND d.objid=%s AND d.deptype='e')"
    routine_kind = "CASE p.prokind WHEN 'p' THEN 'PROCEDURE' WHEN 'a' THEN 'AGGREGATE' ELSE 'FUNCTION' END" if pg.version_num >= 110000 else "CASE WHEN p.proisagg THEN 'AGGREGATE' ELSE 'FUNCTION' END"
    if not args.database_only and old != new:
        for key, query in [
            ('schemas', f"SELECT 'ALTER SCHEMA ' || quote_ident(n.nspname) || ' OWNER TO ' || quote_ident({ql(new)}) || ';' AS sql FROM pg_namespace n WHERE {namespace} AND n.nspowner={ql(qi(old))}::regrole AND {extension % (ql('pg_namespace'), 'n.oid')}"),
            ('relations', f"SELECT 'ALTER ' || CASE c.relkind WHEN 'r' THEN 'TABLE' WHEN 'p' THEN 'TABLE' WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW' WHEN 'f' THEN 'FOREIGN TABLE' END || ' ' || quote_ident(n.nspname) || '.' || quote_ident(c.relname) || ' OWNER TO ' || quote_ident({ql(new)}) || ';' AS sql FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE {namespace} AND c.relowner={ql(qi(old))}::regrole AND c.relkind IN ('r','p','S','v','m','f') AND {extension % (ql('pg_class'), 'c.oid')} ORDER BY CASE WHEN c.relkind='S' THEN 1 ELSE 0 END, c.oid"),
            ('routines', f"SELECT 'ALTER ' || {routine_kind} || ' ' || quote_ident(n.nspname) || '.' || quote_ident(p.proname) || '(' || pg_get_function_identity_arguments(p.oid) || ') OWNER TO ' || quote_ident({ql(new)}) || ';' AS sql FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE {namespace} AND p.proowner={ql(qi(old))}::regrole AND {extension % (ql('pg_proc'), 'p.oid')}"),
            ('types', f"SELECT 'ALTER ' || CASE WHEN t.typtype='d' THEN 'DOMAIN' ELSE 'TYPE' END || ' ' || quote_ident(n.nspname) || '.' || quote_ident(t.typname) || ' OWNER TO ' || quote_ident({ql(new)}) || ';' AS sql FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace LEFT JOIN pg_class c ON c.oid=t.typrelid WHERE {namespace} AND t.typowner={ql(qi(old))}::regrole AND (t.typtype IN ('d','e','r') OR (t.typtype='c' AND c.relkind='c')) AND {extension % (ql('pg_type'), 't.oid')}"),
            ('large_objects', f"SELECT 'ALTER LARGE OBJECT ' || oid || ' OWNER TO ' || quote_ident({ql(new)}) || ';' AS sql FROM pg_largeobject_metadata WHERE lomowner={ql(qi(old))}::regrole")]:
            rows = pg.rows(query, database)
            statements.extend(row['sql'] for row in rows)
            counts[key] = len(rows)
    statements += [f'ALTER DATABASE {qi(database)} OWNER TO {qi(new)};',
                   f'GRANT USAGE, CREATE ON SCHEMA public TO {qi(new)};']
    pg.transaction(statements, database)
    return {'ok': True, 'action': 'transfer-ownership', 'database': database,
            'previous_owner': old, 'owner': new, 'objects_selected': counts,
            'connections': connection_strings(pg.config, database, new)}


def manage(pg, args):
    action = args.command
    if action == 'status':
        return {'ok': True, 'action': action, 'version': pg.version, 'version_num': pg.version_num,
                'container': pg.config.container, 'storecredentials': pg.config.store}
    if action == 'list-dbs':
        return {'ok': True, 'action': action, 'databases': pg.rows("SELECT datname AS name, pg_get_userbyid(datdba) AS owner, pg_size_pretty(pg_database_size(oid)) AS size FROM pg_database WHERE NOT datistemplate ORDER BY datname")}
    if action == 'list-users':
        users = pg.rows("SELECT rolname AS username, rolsuper AS superuser, rolcreatedb AS create_database, rolcreaterole AS create_role FROM pg_roles WHERE rolcanlogin ORDER BY rolname")
        return {'ok': True, 'action': action, 'users': users}
    if action == 'show-permissions':
        database = database_name(args.database)
        pg.require_db(database)
        pg.require_role(args.username)
        user = ql(args.username)
        return {'ok': True, 'action': action, 'database': database, 'username': args.username,
                'connect': pg.sql(f"SELECT has_database_privilege({user}, {ql(database)}, 'CONNECT');") == 't',
                'schemas': pg.rows(f"SELECT nspname AS name, has_schema_privilege({user}, oid, 'USAGE') AS usage, has_schema_privilege({user}, oid, 'CREATE') AS create FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema' ORDER BY 1", database),
                'tables': pg.rows(f"SELECT n.nspname AS schema, c.relname AS name, has_table_privilege({user}, c.oid, 'SELECT') AS read, has_table_privilege({user}, c.oid, 'INSERT') AS insert, has_table_privilege({user}, c.oid, 'UPDATE') AS update, has_table_privilege({user}, c.oid, 'DELETE') AS delete FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p','v','m','f') AND n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' ORDER BY 1,2", database)}
    if action == 'transfer-ownership':
        return transfer_ownership(pg, args)
    if action in ('create-user', 'create-db'):
        database = database_name(args.database, protect=True)
        username = args.username
        if action == 'create-user' or args.with_user:
            username = username or 'user_' + secrets.token_hex(6)
        if not username and (args.password is not None or args.password_stdin or args.permissions):
            raise ToolError('Use --with-user or --username to supply a password or permissions to create-db.')
        flags = permission_flags(args.permissions or 'crw') if username else None
        password = password_for(args) if username else None
        if username:
            protected_user(pg, username)
            if pg.role_exists(username):
                raise ToolError(f'User already exists: {username}')
        owner = getattr(args, 'owner', None) or pg.config.admin
        if owner != username:
            pg.require_role(owner)
        exists = pg.db_exists(database)
        if action == 'create-db' and exists:
            raise ToolError(f'Database already exists: {database}')
        if not exists and getattr(args, 'schema', 'public') not in ('public', 'all'):
            raise ToolError('A new database starts with the public schema; create custom schemas before granting permissions.')
        if exists and username:
            pg.schemas(database, args.schema)
        if getattr(args, 'creator', None):
            for role in args.creator:
                pg.require_role(role)
        if username and pg.config.store:
            os.close(open_credentials(pg.config))
        user_created = False
        db_created = False
        try:
            if username:
                pg.sql(f'CREATE ROLE {qi(username)} LOGIN PASSWORD {ql(password)};')
                user_created = True
            if not exists:
                create_database(pg, database, owner, getattr(args, 'encoding', 'UTF8'))
                db_created = True
            names, creators = [], []
            if username:
                statements, names, creators = permission_sql(pg, database, username, flags, args.schema, args.creator)
                pg.transaction(statements, database)
        except ToolError as error:
            # Only undo resources created by this invocation. Grants are transactional.
            try:
                if db_created:
                    pg.sql(f'DROP DATABASE {qi(database)};')
                if user_created:
                    pg.sql(f'DROP ROLE {qi(username)};')
            except ToolError:
                raise ToolError('Creation failed and cleanup could not complete.',
                                details={'database': database, 'username': username,
                                         'database_created': db_created, 'user_created': user_created}) from error
            raise
        record = {'ok': True, 'action': action, 'database': database, 'username': username, 'password': password,
                  'permissions': flags, 'schemas': names, 'default_privilege_creators': creators,
                  'database_created': db_created, 'user_created': user_created, 'owner': owner if db_created else None,
                  'credentials_stored': False, 'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                  'connections': connection_strings(pg.config, database, username, password)}
        if username and pg.config.store:
            store_credentials(pg.config, record)
        return record
    if action in ('grant', 'revoke', 'set-permissions'):
        database = database_name(args.database)
        pg.require_db(database)
        protected_user(pg, args.username)
        pg.require_role(args.username)
        flags = permission_flags(args.permissions)
        if action == 'set-permissions':
            require_yes(args)
        statements = reset_permissions_sql(pg, database, args.username, args.schema, args.creator) if action == 'set-permissions' else []
        sql, names, creators = permission_sql(pg, database, args.username, flags, args.schema, args.creator, revoke=action == 'revoke')
        pg.transaction(statements + sql, database)
        return {'ok': True, 'action': action, 'database': database, 'username': args.username,
                'permissions': flags, 'schemas': names, 'default_privilege_creators': creators,
                'note': 'Direct grants only. Ownership, PUBLIC and role memberships can provide additional permissions.'}
    if action == 'harden-db':
        require_yes(args)
        database = database_name(args.database, protect=True)
        pg.require_db(database)
        owner = pg.sql(f'SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname={ql(database)};')
        pg.transaction(harden_sql(database, pg.config.admin, owner), database)
        return {'ok': True, 'action': action, 'database': database}
    if action == 'change-password':
        protected_user(pg, args.username)
        pg.require_role(args.username)
        if args.database:
            pg.require_db(args.database)
        password = password_for(args)
        if pg.config.store:
            os.close(open_credentials(pg.config))
        pg.sql(f'ALTER ROLE {qi(args.username)} PASSWORD {ql(password)};')
        record = {'ok': True, 'action': action, 'username': args.username, 'password': password,
                  'credentials_stored': False, 'created_at': dt.datetime.now(dt.timezone.utc).isoformat()}
        if args.database:
            record.update(database=args.database, connections=connection_strings(pg.config, args.database, args.username, password))
        if pg.config.store:
            store_credentials(pg.config, record)
        return record
    if action == 'delete-db':
        require_yes(args)
        database = database_name(args.database, protect=True)
        pg.require_db(database)
        # Block reconnects before dropping; FORCE is unavailable on PostgreSQL 10-12.
        pg.sql(f'ALTER DATABASE {qi(database)} ALLOW_CONNECTIONS false;')
        try:
            pg.sql(f'SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname={ql(database)} AND pid<>pg_backend_pid();')
            pg.sql(f'DROP DATABASE {qi(database)};')
        except ToolError:
            pg.sql(f'ALTER DATABASE {qi(database)} ALLOW_CONNECTIONS true;')
            raise
        return {'ok': True, 'action': action, 'database': database}
    if action == 'delete-user':
        require_yes(args)
        protected_user(pg, args.username)
        pg.require_role(args.username)
        # Recheck ownership in each database before DROP OWNED can remove anything.
        owned = f"EXISTS(SELECT 1 FROM pg_shdepend WHERE refclassid='pg_authid'::regclass AND refobjid={ql(qi(args.username))}::regrole AND deptype='o')"
        if pg.sql(f'SELECT {owned};') == 't':
            raise ToolError('User owns objects. Transfer ownership or remove those objects before deleting the user.')
        databases = pg.rows('SELECT datname AS name, datallowconn AS allowed FROM pg_database WHERE NOT datistemplate')
        if any(not row['allowed'] for row in databases):
            raise ToolError('Cannot clean grants while a database has connections disabled.')
        for row in databases:
            pg.transaction([f"DO $guard$ BEGIN IF {owned} THEN RAISE EXCEPTION 'Role owns objects'; END IF; END $guard$;",
                            f'DROP OWNED BY {qi(args.username)} RESTRICT;'], row['name'])
        pg.sql(f'DROP ROLE {qi(args.username)};')
        return {'ok': True, 'action': action, 'username': args.username}
    raise ToolError('Unknown management command.')


def backup(pg, args):
    if args.command in ('export', 'export-globals'):
        database = database_name(args.database) if args.command == 'export' else None
        if database:
            pg.require_db(database)
        directory = Path(args.path or pg.config.root / 'backups').expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        stem = re.sub('[^A-Za-z0-9_.-]', '_', database or 'globals')[:63]
        extension = '.dump' if database else '.sql'
        filename = args.output or f'{stem}_{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%S}_{secrets.token_hex(3)}{extension}'
        if not filename or filename in ('.', '..') or Path(filename).name != filename:
            raise ToolError('--output must be a filename; use --path for the directory.')
        path = directory / filename
        if path.exists() or path.is_symlink():
            raise ToolError('Backup output already exists; choose another filename.')
        tool = 'pg_dump' if database else 'pg_dumpall'
        options = ['--format=custom', '--dbname=' + conninfo(database)] if database else ['--globals-only']
        message(f'Exporting {database or "global roles and tablespaces"} to {path}')
        with tempfile.NamedTemporaryFile(dir=directory, prefix='.postgres-backup-', delete=False) as handle:
            staging = Path(handle.name)
            try:
                pg.run_tool(tool, options, output=handle)
                handle.flush()
                os.fsync(handle.fileno())
                os.link(staging, path)
            finally:
                staging.unlink(missing_ok=True)
        return {'ok': True, 'action': args.command, 'database': database, 'file': str(path),
                'format': 'custom' if database else 'sql', 'version': pg.version, 'bytes': path.stat().st_size}
    require_yes(args)
    database = database_name(args.database, protect=True)
    path = Path(args.file).expanduser().resolve()
    if not path.is_file():
        raise ToolError(f'Backup file does not exist: {path}')
    # Validate the archive with the container's matching pg_restore before any mutation.
    with path.open('rb') as handle:
        toc = pg.run_tool('pg_restore', ['--list'], source=handle)
    exists = pg.db_exists(database)
    if not exists and not args.create:
        raise ToolError('Target database does not exist; use --create or create it first.')
    if args.owner and not args.create:
        raise ToolError('--owner requires --create.')
    if args.owner and exists:
        raise ToolError('--owner only applies to a newly created database.')
    owner = args.owner or pg.config.admin
    pg.require_role(owner)
    created = False
    if not exists:
        create_database(pg, database, owner)
        created = True
    options = ['--dbname=' + conninfo(database), '--exit-on-error', '--single-transaction']
    if not args.preserve_permissions:
        options += ['--no-owner', '--no-privileges']
        if args.owner:
            options += ['--role=' + owner]
    if args.clean:
        options += ['--clean', '--if-exists']
    message(f'Restoring {path} into {database}')
    # PostgreSQL 10's plpgsql comment belongs to the administrator, not the restore role.
    filtered = None
    if args.owner and not args.preserve_permissions and pg.version_num < 110000:
        filtered = b'\n'.join(line for line in toc.splitlines() if b' COMMENT - EXTENSION plpgsql ' not in line) + b'\n'
    try:
        with pg.private_file(filtered) if filtered is not None else nullcontext(None) as remote:
            restore_options = options + (['--use-list=' + remote] if remote else [])
            with path.open('rb') as handle:
                pg.run_tool('pg_restore', restore_options, source=handle)
    except ToolError:
        if created:
            pg.sql(f'DROP DATABASE {qi(database)};')
        raise
    return {'ok': True, 'action': 'import', 'database': database, 'database_created': created,
            'file': str(path), 'preserve_permissions': args.preserve_permissions}


class Parser(argparse.ArgumentParser):
    def error(self, text):
        raise ToolError(text + ' (use --help for usage)')


def parser_for(tool):
    common = Parser(add_help=False)
    # Suppressed defaults let common options appear BEFORE or AFTER a subcommand.
    common.add_argument('--env-file', default=argparse.SUPPRESS, help='Configuration file (default: template .env)')
    common.add_argument('--no-color', action='store_true', default=argparse.SUPPRESS)
    parser = Parser(prog={'manage': 'manage.sh', 'backup': 'backup.sh', 'login': 'login-as-root.sh'}[tool], parents=[common],
                    description='PostgreSQL 10+ tools. CLI commands return JSON; all paths are independent of the working directory.')
    def password_options(command):
        command.add_argument('-p', '--password', help='Omit to generate; prefer --password-stdin for automation')
        command.add_argument('--password-stdin', action='store_true', help='Read one password line from stdin')
    def privileges(command, required=False):
        command.add_argument('-r', '--permissions', required=required, help='c=connect r=read w=write a=create s=superuser (default: crw)')
        command.add_argument('--schema', default='public', help='Schema name or all existing user schemas (default: public)')
        command.add_argument('--creator', action='append', help='Additional role creating future objects; repeat as needed')
    if tool == 'manage':
        commands = parser.add_subparsers(dest='command', parser_class=Parser)
        def command(name, **kwargs):
            return commands.add_parser(name, parents=[common], **kwargs)
        command('tui', help='Open the interactive menu (default with no arguments)')
        guided = command('interactive', help='Run one guided operation')
        guided.add_argument('operation', choices=['create-user', 'create-db', 'grant', 'revoke', 'set-permissions',
                                                  'transfer-ownership', 'delete-user', 'delete-db', 'change-password', 'harden-db', 'show-permissions'])
        command('status', help='Show server version and configuration')
        command('list-dbs', aliases=['list-databases'], help='List databases and owners')
        command('list-users', help='List login roles and server privileges')
        show = command('show-permissions', help='Show effective privileges including PUBLIC and memberships')
        show.add_argument('-d', '--database', required=True)
        show.add_argument('-u', '--username', required=True)
        for action in ('create-user', 'create-db'):
            item = command(action, help='Create a user/database and print JSON connection strings')
            item.add_argument('-d', '--database', required=True)
            item.add_argument('-u', '--username', help='Username (generated for create-user or --with-user)')
            password_options(item)
            privileges(item)
            if action == 'create-db':
                item.add_argument('--with-user', action='store_true')
                item.add_argument('--owner', help='Existing role or the new username; default: configured administrator')
                item.add_argument('--encoding', default='UTF8')
            else:
                item.set_defaults(with_user=False)
        for action in ('grant', 'revoke', 'set-permissions'):
            item = command(action, help='Manage direct grants and future-object privileges')
            item.add_argument('-d', '--database', required=True)
            item.add_argument('-u', '--username', required=True)
            privileges(item, required=True)
            if action == 'set-permissions':
                item.add_argument('-y', '--yes', action='store_true')
        owner = command('transfer-ownership', aliases=['change-owner'], help='Transfer database and its old owner objects locally, without affecting other databases')
        owner.add_argument('-d', '--database', required=True)
        owner.add_argument('--owner', required=True)
        owner.add_argument('--from-owner', help='Object owner to transfer (default: current database owner)')
        owner.add_argument('--database-only', action='store_true')
        for action in ('delete-user', 'delete-db', 'harden-db'):
            item = command(action, help='Requires --yes; delete-user refuses owned objects')
            item.add_argument('-u', '--username', required=True) if action == 'delete-user' else item.add_argument('-d', '--database', required=True)
            item.add_argument('-y', '--yes', action='store_true')
        change = command('change-password', help='Rotate a password and optionally store credentials')
        change.add_argument('-u', '--username', required=True)
        change.add_argument('-d', '--database')
        password_options(change)
    elif tool == 'backup':
        commands = parser.add_subparsers(dest='command', required=True, parser_class=Parser)
        for action in ('export', 'export-globals'):
            item = commands.add_parser(action, parents=[common], help='Export a private archive; globals exports roles/tablespaces as SQL')
            if action == 'export':
                item.add_argument('-d', '--database', required=True)
            item.add_argument('-p', '--path', help='Output directory (default: template-root/backups)')
            item.add_argument('-o', '--output', help='Filename; existing files are never overwritten')
        item = commands.add_parser('import', parents=[common], aliases=['restore'], help='Restore a custom archive in one transaction; requires --yes')
        item.add_argument('-d', '--database', required=True)
        item.add_argument('-f', '--file', required=True)
        item.add_argument('--create', action='store_true', help='Create target database if absent')
        item.add_argument('--owner', help='Owner of new target database (--create)')
        item.add_argument('--clean', action='store_true', help='Drop matching objects before restoring')
        item.add_argument('--preserve-permissions', action='store_true', help='Restore original ownership and ACLs; referenced roles must exist')
        item.add_argument('-y', '--yes', action='store_true')
    else:
        parser.add_argument('-d', '--database', help='Database (default: POSTGRES_DB)')
        parser.add_argument('--eval', dest='code', help='Execute SQL without opening an interactive shell')
    return parser


def prompt(text, default=None, secret=False):
    label = text + (f' [{default}]' if default is not None else '') + ': '
    value = getpass.getpass(label) if secret else input(label)
    return value if value else default


def choose_user_database(pg):
    databases = [row['name'] for row in pg.rows('SELECT datname AS name FROM pg_database WHERE NOT datistemplate AND datallowconn ORDER BY datname')
                 if row['name'] not in SYSTEM_DATABASES]
    print('\nDatabase\n 0. Create a new database')
    for index, name in enumerate(databases, 1):
        print(f'{index:>2}. {name}')
    while True:
        selection = prompt('Choose a database number') or ''
        if selection.isascii() and selection.isdigit() and 0 <= int(selection) <= len(databases):
            index = int(selection)
            if index:
                return databases[index - 1]
            break
        message('Choose one of the listed database numbers.', 'WARN')
    while True:
        try:
            database = database_name(prompt('New database name') or '', protect=True)
        except ToolError as error:
            message(str(error), 'WARN')
            continue
        if not pg.db_exists(database):
            return database
        message('That database already exists; enter a new name.', 'WARN')


def guided(pg, action):
    values = [action]
    if action != 'delete-user':
        values += ['-d', choose_user_database(pg) if action == 'create-user' else prompt('Database') or '']
    if action in ('create-user', 'create-db', 'grant', 'revoke', 'set-permissions', 'delete-user', 'change-password', 'show-permissions'):
        user = prompt('Username (blank generates one)' if action in ('create-user', 'create-db') else 'Username')
        if user:
            values += ['-u', user]
        if action in ('create-user', 'create-db', 'change-password'):
            if action == 'create-db' and not user and prompt('Create a generated user? y/n', 'n').lower() == 'y':
                values += ['--with-user']
                user = 'generated'
            if action != 'create-db' or user:
                password = prompt('Password (blank generates one)', secret=True)
                if password:
                    values += ['--password', password]
        if action in ('create-user', 'grant', 'revoke', 'set-permissions') or action == 'create-db' and user:
            message('Permissions: c connect, r read, w write, a create, s server superuser')
            values += ['-r', prompt('Permissions', 'crw')]
    if action == 'transfer-ownership':
        values += ['--owner', prompt('New owner') or '']
    if action in ('delete-user', 'delete-db', 'set-permissions', 'harden-db'):
        if prompt('Confirm changes? y/n', 'n').lower() != 'y':
            return {'ok': True, 'action': action, 'cancelled': True}
        values += ['--yes']
    args = parser_for('manage').parse_args(values)
    if getattr(args, 'permissions', None) and 's' in permission_flags(args.permissions):
        message('Superuser grants full control over the entire server.', 'WARN')
        if prompt('Grant server superuser? y/n', 'n').lower() != 'y':
            return {'ok': True, 'action': action, 'cancelled': True}
    return manage(pg, args)


def render_result(record):
    """Readable terminal presentation; machine-readable output stays in CLI mode."""
    if record.get('cancelled'):
        message('Operation cancelled.')
        return
    message(record.get('action', 'Operation').replace('-', ' ').capitalize() + ' completed.', 'OK')
    for key, columns in [('users', [('username', 'Username'), ('superuser', 'Superuser'), ('create_database', 'Create DB'), ('create_role', 'Create role')]),
                         ('databases', [('name', 'Database'), ('owner', 'Owner'), ('size', 'Size')])]:
        if key in record:
            rows = record[key]
            widths = [max([len(label)] + [len(str(row.get(name, ''))) for row in rows]) for name, label in columns]
            line = lambda values: '  '.join(str(value).ljust(width) for value, width in zip(values, widths))
            print(line(label for _, label in columns))
            print(line('-' * width for width in widths))
            for row in rows:
                print(line(row.get(name, '') for name, _ in columns))
            if not rows:
                print('No entries found.')
            return
    labels = {'database': 'Database', 'username': 'Username', 'password': 'Password', 'permissions': 'Permissions',
              'owner': 'Owner', 'previous_owner': 'Previous owner', 'file': 'Backup file', 'bytes': 'Bytes',
              'credentials_stored': 'Credentials saved'}
    for key, label in labels.items():
        if key in record and record[key] is not None:
            print(f'  {label:<18} {record[key]}')
    for key in ('schemas', 'tables'):
        if record.get(key) and isinstance(record[key][0], dict):
            print('\n' + key.capitalize())
            for row in record[key]:
                print('  ' + ', '.join(f'{name}: {value}' for name, value in row.items()))
    if record.get('connections'):
        print('\nConnection strings')
        for label, value in record['connections'].items():
            if value is not None:
                print(f'  {label:<18} {value}')
    if record.get('note'):
        message(record['note'], 'WARN')


def tui(pg):
    actions = [('List users', 'list-users'), ('List databases', 'list-dbs'), ('Inspect user privileges', 'show-permissions'), ('Create user', 'create-user'),
               ('Create database', 'create-db'), ('Grant privileges', 'grant'), ('Revoke privileges', 'revoke'),
               ('Set privileges', 'set-permissions'), ('Transfer ownership', 'transfer-ownership'),
               ('Change password', 'change-password'), ('Delete user', 'delete-user'), ('Delete database', 'delete-db'),
               ('Harden existing database', 'harden-db'), ('Export backup', 'backup-export'), ('Restore backup', 'backup-import')]
    while True:
        print(f'\nPostgreSQL Manager | {pg.version} | {pg.config.container}\n' + '-' * 60)
        for index, (label, _) in enumerate(actions, 1):
            print(f'{index:>2}. {label}')
        print(' 0. Exit')
        selection = prompt('Choose an action', '0')
        if selection == '0':
            return
        try:
            index = int(selection) - 1
            if not 0 <= index < len(actions):
                raise ValueError
            action = actions[index][1]
            if action.startswith('list-'):
                result = manage(pg, parser_for('manage').parse_args([action]))
            elif action.startswith('backup-'):
                values = ['export' if action == 'backup-export' else 'import', '-d', prompt('Database') or '']
                if action == 'backup-import':
                    values += ['--file', prompt('Backup file') or '', '--create']
                    if prompt('Confirm restore? y/n', 'n').lower() != 'y':
                        continue
                    values += ['--yes']
                result = backup(pg, parser_for('backup').parse_args(values))
            else:
                result = guided(pg, action)
            render_result(result)
        except (ToolError, OSError, ValueError) as error:
            message(str(error) or 'Invalid selection.', 'ERROR')
        prompt('Press ENTER to return to the menu')


def login(pg, args):
    database = database_name(args.database or pg.config.database)
    if args.code is not None:
        print(pg.sql(args.code, database))
        return
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ToolError('Interactive login requires a terminal; use --eval for automation.')
    message(f'Opening psql as {pg.config.admin} in {pg.config.container}')
    # A private pgpass file leaves stdin free for the interactive terminal.
    escape = lambda value: value.replace('\\', '\\\\').replace(':', '\\:')
    pgpass = ':'.join(escape(value) for value in ('127.0.0.1', '5432', database, pg.config.admin, pg.config.password)) + '\n'
    with pg.private_file(pgpass.encode()) as remote:
        result = subprocess.run(['docker', 'exec', '-it', '-e', 'PGPASSFILE=' + remote, pg.config.container,
                                 'psql', '-X', '--host=127.0.0.1', '--username=' + pg.config.admin,
                                 '--dbname=' + conninfo(database), '--no-password'])
        if result.returncode:
            raise ToolError(f'psql exited with status {result.returncode}.')


def main():
    interactive = False
    try:
        if sys.version_info < (3, 9):
            raise ToolError('Python 3.9 or newer is required.')
        tool = sys.argv[1]
        arguments = sys.argv[2:]
        if arguments == ['help'] or tool == 'backup' and not arguments:
            arguments = ['--help']
        args = parser_for(tool).parse_args(arguments)
        if getattr(args, 'no_color', False):
            os.environ['NO_COLOR'] = '1'
        if tool == 'manage':
            args.command = {'list-databases': 'list-dbs', 'change-owner': 'transfer-ownership'}.get(args.command, args.command or 'tui')
            interactive = args.command in ('tui', 'interactive')
            if interactive and (not sys.stdin.isatty() or not sys.stdout.isatty()):
                interactive = False
                raise ToolError('Interactive mode requires a terminal; use a CLI subcommand for automation.')
        if tool == 'backup' and args.command == 'restore':
            args.command = 'import'
        pg = Postgres(Config(getattr(args, 'env_file', None)))
        if tool == 'manage':
            if args.command == 'tui':
                tui(pg)
            elif args.command == 'interactive':
                render_result(guided(pg, args.operation))
            else:
                emit(manage(pg, args))
        elif tool == 'backup':
            emit(backup(pg, args))
        else:
            login(pg, args)
        return 0
    except (ToolError, OSError, ValueError, subprocess.SubprocessError) as error:
        message(str(error), 'ERROR')
        if not interactive:
            record = {'ok': False, 'error': str(error)}
            if getattr(error, 'details', None):
                record['result'] = error.details
            emit(record)
        return 1
    except (KeyboardInterrupt, EOFError):
        message('Cancelled.', 'WARN')
        return 130


if __name__ == '__main__':
    sys.exit(main())
