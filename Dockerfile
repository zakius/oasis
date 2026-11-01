# syntax=docker/dockerfile:1
FROM python:3.14-slim AS build
# PyICU builds against Debian's ICU, which the final image carries (libicu76).
RUN apt-get update && apt-get install -y --no-install-recommends g++ pkg-config libicu-dev && rm -rf /var/lib/apt/lists/*
RUN python -m venv /opt/venv
WORKDIR /src
COPY pyproject.toml README.md ./
COPY oasis ./oasis
RUN --mount=type=cache,target=/root/.cache/pip /opt/venv/bin/pip install .

FROM python:3.14-slim
# yt-dlp needs node to solve YouTube's JS challenge; ffmpeg transcodes for clients that ask for format= or maxBitRate=,
# and ffmpeg and ffprobe decode audio for dedupe.
COPY --from=node:24-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=mwader/static-ffmpeg:7.1 /ffmpeg /ffprobe /usr/local/bin/
# unar unpacks RAR and 7z downloads; flac recompresses and checks FLACs (quality.py); libicu76 romanizes (romanize.py).
RUN apt-get update && apt-get install -y --no-install-recommends unar flac libicu76 && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1
RUN groupadd --gid 1000 oasis && useradd --uid 1000 --gid 1000 --home-dir /config oasis && mkdir /config && chown oasis:oasis /config
USER oasis
VOLUME /config
EXPOSE 4533
HEALTHCHECK --interval=30s --timeout=5s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4533/rest/ping')"]
ENTRYPOINT ["oasis", "-c", "/config/oasis.toml"]
CMD ["serve"]
