# syntax=docker/dockerfile:1
FROM python:3.14-slim AS build
RUN python -m venv /opt/venv
WORKDIR /src
COPY pyproject.toml README.md ./
COPY oasis ./oasis
RUN --mount=type=cache,target=/root/.cache/pip /opt/venv/bin/pip install .

FROM python:3.14-slim
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1
RUN useradd --system --uid 1000 --home-dir /config oasis && mkdir /config && chown oasis /config
USER oasis
VOLUME /config
EXPOSE 4533
HEALTHCHECK --interval=30s --timeout=5s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4533/rest/ping')"]
ENTRYPOINT ["oasis", "-c", "/config/oasis.toml"]
CMD ["serve"]
