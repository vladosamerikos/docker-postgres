# PostgreSQL Docker boilerplate

A Docker Compose boilerplate for running PostgreSQL 10–18, with persistent data, configurable networking and resource limits. Helper commands make it easier to create databases and users, manage permissions, transfer ownership and make backups.

## Setup

Requires Docker Compose, Python 3.9+ and OpenSSL; remote installation also uses Git.

From a cloned repository:

```sh
./setup.sh
```

Or install into `postgres<VERSION>` in the current directory:

```sh
curl -fsSL https://raw.githubusercontent.com/vladosamerikos/docker-postgres/main/setup.sh | bash
```

Setup asks for the version, administrator, hostname, networking, credential storage and resource limits. Enter accepts defaults. It generates a password with OpenSSL, saves `.env` and starts PostgreSQL. Unavailable ports can be retried without losing previous answers.

Run setup again to reconfigure an existing installation while preserving its version, administrator credentials and data in `data/`. Failed recreation restores the previous configuration and restarts a previously running service. Major-version upgrades require a separate migration.

For manual configuration, copy `.env.example` to `.env`, set `POSTGRES_PASSWORD`, then run:

```sh
docker compose up -d
docker compose logs -f
docker compose down
```

## Configuration

Edit `.env` to configure the image, credentials, hostname, port and CPU/RAM limits. `POSTGRES_PORT_MODE` selects the network mode:

| Mode | Published port |
| --- | --- |
| `none` | Docker network only |
| `host` | `127.0.0.1:HOST_POSTGRES_PORT` |
| `lan` | `0.0.0.0:HOST_POSTGRES_PORT` |

Set `POSTGRES_LAN_HOST` to the server IP/hostname for LAN connection strings. Run `docker compose down` before changing modes manually. Changing initialization credentials in `.env` does not update existing PostgreSQL roles.

`storecredentials=true` saves generated/rotated credentials as plaintext JSON Lines in `.credentials` (permissions `600`). Keep it private; deleting users does not remove previous entries.

## Commands

Scripts find their own `.env`, so they work from `scripts/` or any working directory. Use `--env-file PATH` for another configuration; supplied file paths are relative to your working directory.

```sh
cd scripts
./manage.sh                                      # Interactive menu
./manage.sh interactive create-user              # One guided operation
./manage.sh --help
./manage.sh create-db -d app -u app_writer -r crw  # Database + user + generated password
./manage.sh create-user -d app -u app_reader -r cr
./manage.sh list-users
./manage.sh list-dbs
./manage.sh show-permissions -d app -u app_writer
./manage.sh grant -d app -u app_writer -r a
./manage.sh revoke -d app -u app_writer -r w
./manage.sh set-permissions -d app -u app_writer -r cr --yes
./manage.sh change-password -d app -u app_writer   # Generate a new password
./manage.sh transfer-ownership -d app --owner app_writer
./manage.sh delete-user -u app_reader --yes
./manage.sh delete-db -d retired_app --yes
./login-as-root.sh
./login-as-root.sh -d postgres --eval 'SELECT version();'
```

`create-user` also creates the database if missing. `create-db -d app` creates only a database; add `--with-user` to generate a username, or `-u NAME` to choose one. Omit the password to generate it, or supply `--password VALUE` / `--password-stdin`:

```sh
# APP_PASSWORD contains the supplied password.
printf '%s\n' "$APP_PASSWORD" | ./manage.sh create-user -d app -u app_custom -r crw --password-stdin
```

CLI commands return JSON with the operation summary, generated credentials and connection URIs under `connections.localhost`, `connections.lan` and `connections.postgres_network` (`null` when unavailable). Errors return `ok: false` and a nonzero exit status. Login with `--eval` returns raw SQL output.

Permissions default to `crw`: `c` connect, `r` read, `w` write, `a` create objects, `s` server superuser. Named forms such as `connect,read` also work. Grants cover existing and future objects in `public`; use `--schema NAME` or `--schema all`, and repeat `--creator ROLE` for additional roles that create future objects. New schemas need a new grant.

New databases restrict `PUBLIC` access; use `harden-db -d NAME --yes` to apply that policy to an existing database. Revoking direct permissions does not remove access inherited through ownership, `PUBLIC` or role membership. Superuser access applies to the entire server.

Ownership transfers affect the database and its former owner's supported objects within it; the new owner must exist. Use `--database-only` to change only database ownership, or `--from-owner ROLE` to select another object owner. Reapply future-object grants after changing creators. User deletion refuses owned objects: transfer them first.

## Backups

```sh
./backup.sh export -d app --output app.dump
./backup.sh export -d app --path /srv/backups --output app.dump
./backup.sh import -d app_copy --file ../backups/app.dump --create --yes
./backup.sh import -d app_copy --file ../backups/app.dump --clean --yes
./backup.sh export-globals
```

Exports create private custom archives in `backups/` by default and never overwrite files. Import requires `--yes` and restores in one transaction. `--create` creates a missing database; add `--owner ROLE` to choose its owner. `--clean` replaces matching archive objects. Original ownership and permissions are omitted unless `--preserve-permissions` is supplied and the original roles exist.

`export-globals` saves roles and tablespaces, including password hashes, as SQL for manual recovery with `psql`. Restore trusted dumps into the same major or a newer compatible version. Restore extensions requiring superuser access as the administrator.
