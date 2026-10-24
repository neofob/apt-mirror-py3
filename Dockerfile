# Default image for apt-mirror-py: a Debian cron daemon that runs the
# apt mirror update every 6 hours.
#
# Build:
#   docker build -t apt-mirror-py3 .
# Run (persist the mirror spool):
#   docker run -d --name apt-mirror -v my-mirror:/var/spool/apt-mirror apt-mirror-py3
#
# The crond daemon itself must run as root: Debian's cron requires
# /var/run/crond.pid and a root-owned cronspool. The cron job, and
# everything it runs and writes (apt-mirror, tee, cron.log), runs as
# uid:gid 1000:1000 (user "aptmirror").

FROM debian:trixie-slim

# python3 + python3-venv: the virtual environment (venv pip via ensurepip).
# cron + tini: the scheduler and its init wrapper.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        cron \
        python3 \
        python3-venv \
        tini \
    && rm -rf /var/lib/apt/lists/*

# The unprivileged mirror user (uid:gid 1000:1000).
RUN groupadd --gid 1000 aptmirror \
    && useradd --uid 1000 --gid aptmirror --no-create-home --shell /usr/sbin/nologin aptmirror

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

# Default mirror config.
COPY mirror.list /etc/apt/mirror.list

# Mirror update every 6 hours (00:00, 06:00, 12:00, 18:00). The job and
# its output run as aptmirror (uid:gid 1000:1000).
RUN printf '%s\n' \
        'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin' \
        '0 */6 * * * aptmirror /usr/local/bin/apt-mirror | tee -a /var/spool/apt-mirror/var/cron.log' \
    > /etc/cron.d/apt-mirror \
    && chmod 644 /etc/cron.d/apt-mirror

# crond in the foreground under tini; tini reaps cron's per-job children.
ENTRYPOINT ["/usr/bin/tini", "-v", "--", "/usr/sbin/cron", "-f"]
