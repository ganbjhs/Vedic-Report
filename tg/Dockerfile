# Report bot — Telegram front door.
#
# No browser, no Playwright, no Chromium: this container only speaks HTTP to
# Report Maker and to api.telegram.org, so it is a few tens of MB and starts in
# a second. That is the whole point of moving off WhatsApp automation.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py client.py ./

# Long polling: outbound only. No port is published and none is needed.
CMD ["python", "bot.py"]
