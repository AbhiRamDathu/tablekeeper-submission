FROM python:3.12-slim

# The graded network is `--internal`, so nothing can be fetched at run time. Everything the
# service needs has to be in the image. `tzdata` supplies the IANA database that `zoneinfo`
# needs: the slim base image has no /usr/share/zoneinfo, and the spec's correctness tests span
# America/New_York and Europe/Berlin, so a service that cannot resolve them would fail.
RUN pip install --no-cache-dir "tzdata>=2024.1"

WORKDIR /srv
COPY app ./app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

EXPOSE 8080

# No dependency on source files, volume, port or network address: the whole service is these
# five modules plus the standard library.
CMD ["python", "-m", "app.main"]