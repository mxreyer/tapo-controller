FROM python:3.12-slim

# APP_PORT is honoured by both the server and the healthcheck below. It exists
# because network_mode: host puts us straight onto the host's port space, where
# 8080 is often already taken.
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 APP_PORT=8080

# openssh-client is needed to shut down server-backed plugs gracefully.
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s \
  CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('APP_PORT','8080'))"

# Shell form so $APP_PORT expands at runtime; exec so uvicorn stays PID 1 and
# receives the stop signal directly.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${APP_PORT:-8080}"]
