# fb-watcher as braveduck's Facebook scanner, for braveduck's deploy/hosted
# (compose profile "facebook"). Microsoft's Playwright image has Chromium
# and its libraries; the Playwright version matches requirements.txt's.
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt playwright==1.64.1
COPY *.py ./
# Login profile, seen.db and config.json live in the volume.
ENV FB_WATCHER_DATA=/data PYTHONUNBUFFERED=1
RUN mkdir -p /data && chown pwuser:pwuser /data
VOLUME /data
USER pwuser
ENTRYPOINT ["python", "watcher.py"]
CMD ["serve-braveduck"]
