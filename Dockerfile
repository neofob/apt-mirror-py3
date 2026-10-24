# Default image for apt-mirror-py: a Debian cron daemon that runs the
# apt mirror update every 6 hours.
#
# Build:
#   docker build -t apt-mirror-py3 .
# Run (persist the mirror spool):
#   docker run -d --name apt-mirror -v my-mirror:/var/spool/apt-mirror apt-mirror-py3
#
# The crond daemon itself must run as root: cronie's crond requires
# /run/crond.pid. The cron job, and everything it runs and writes
# (apt-mirror, tee, cron.log), runs as uid:gid 1000:1000 (user
# "aptmirror").

FROM debian:trixie-slim

# python3 + python3-venv: the virtual environment (venv pip via ensurepip).
# cronie + tini: the scheduler and its init wrapper.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        cronie \
        python3 \
        python3-venv \
        tini \
    && rm -rf /var/lib/apt/lists/*

# The unprivileged mirror user (uid:gid 1000:1000). cronie chdirs each job
# into its user's home directory, so the home must exist or every job
# fails with "chdir failed".
RUN groupadd --gid 1000 aptmirror \
    && useradd --uid 1000 --gid aptmirror --create-home --shell /usr/sbin/nologin aptmirror

# Install this repository into a virtual environment. Only the package,
# the shim, and the pyproject are copied: tests/ has an __init__.py and
# would otherwise break pip's flat-layout package auto-discovery.
COPY apt_mirror/ /opt/apt-mirror/apt_mirror/
COPY apt-mirror.py /opt/apt-mirror/
COPY pyproject.toml /opt/apt-mirror/
RUN python3 -m venv /opt/apt-mirror/venv \
    && /opt/apt-mirror/venv/bin/pip install -q /opt/apt-mirror \
    && rm -rf /opt/apt-mirror/build /opt/apt-mirror/dist /opt/apt-mirror/*.egg-info \
    && find /opt/apt-mirror -name __pycache__ -type d -exec rm -rf {} +

# /usr/local/bin/apt-mirror: runs the repository shim with the venv
# interpreter (the shim's #!/usr/bin/env python3 shebang would otherwise
# resolve to the system python3, which does not see the venv site-packages).
# With no arguments it reads the default /etc/apt/mirror.list.
RUN printf '%s\n' \
        '#!/bin/sh' \
        'export PYTHONDONTWRITEBYTECODE=1' \
        'exec /opt/apt-mirror/venv/bin/python /opt/apt-mirror/apt-mirror.py "$@"' \
    > /usr/local/bin/apt-mirror \
    && chmod 755 /usr/local/bin/apt-mirror

# Mirror spool (base_path in mirror.list), owned by the mirror user.
RUN mkdir -p /var/spool/apt-mirror/mirror \
             /var/spool/apt-mirror/skel \
             /var/spool/apt-mirror/var \
    && chown -R 1000:1000 /var/spool/apt-mirror

# Default mirror config (the repository ships this as an example; an
# operator can override it at build time or by mounting /etc/apt/).
COPY mirror.list.example /etc/apt/mirror.list

# Mirror update every 6 hours (00:00, 06:00, 12:00, 18:00). The job and
# its output run as aptmirror (uid:gid 1000:1000). cron.d files must not
# be group/other-writable, hence the explicit 0644.
COPY crontab /etc/cron.d/apt-mirror
RUN chmod 0644 /etc/cron.d/apt-mirror

# crond (cronie) in the foreground under tini; tini reaps crond's
# per-job children. -x load logs job-file load events to stderr (the
# container log).
ENTRYPOINT ["/usr/bin/tini", "-v", "--", "/usr/sbin/crond", "-n", "-x", "load"]
