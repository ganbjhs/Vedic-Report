# The Telegram bots — one image, two entry points.
#
# No browser, no Playwright, no Chromium: these containers only speak HTTP to
# Report Maker and to api.telegram.org, so the image is a few tens of MB and
# starts in a second. That is the whole point of moving off WhatsApp
# automation, where a bot meant a second Chromium competing for the same RAM as
# the capture engine.
#
#   report bot   CMD ["python", "bot.py"]        (the default)
#   splitter     CMD ["python", "splitter.py"]   (set in docker-compose.yml)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py splitter.py client.py ./

# Long polling: outbound only. No port is published and none is needed.
CMD ["python", "bot.py"]
