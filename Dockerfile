# Same base image the scorer labelled with (its Python and SQLite decide which sqlglot tests run).
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.lock /app/requirements.lock
RUN pip install --no-deps -r /app/requirements.lock
# sqlglot at the pinned commit, from its own repository.
ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz /tmp/sqlglot.tar.gz
RUN mkdir -p /app/sq && tar xzf /tmp/sqlglot.tar.gz -C /app/sq --strip-components=1 && rm /tmp/sqlglot.tar.gz
COPY selector.py analyze.py patchinfo.py harness.py tpcfp.py tpcres.py state.py index.bin tpcduck.json /app/br/
RUN python -m compileall -q /app/sq/sqlglot /app/sq/tests /app/br && chmod -R a+rX /app
ENV PYTHONPATH=/app/sq SKIP_INTEGRATION=0 BR_REPO=/app/sq BR_DATA=/data
WORKDIR /app/sq
CMD ["python", "/app/br/selector.py"]
