FROM python:3.12-slim

WORKDIR /app

COPY bot.py core.py store.py tgapi.py main.py /app/
RUN useradd --system --uid 10001 --home /app sigma \
    && mkdir -p /data \
    && chown -R sigma:sigma /app /data

USER sigma
ENV DATA_DIR=/data
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

CMD ["python", "/app/bot.py"]
