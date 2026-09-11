# LinkedIn sourcing worker — headed Chromium under Xvfb (LinkedIn is far less suspicious of a headed browser).
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy
RUN apt-get update && apt-get install -y --no-install-recommends xvfb && rm -rf /var/lib/apt/lists/*
WORKDIR /srv
COPY worker/requirements.txt worker/requirements.txt
RUN pip install --no-cache-dir -r worker/requirements.txt
COPY worker worker
ENV LINKEDIN_PROFILE_DIR=/data/linkedin-profile HEADLESS=0 PYTHONUNBUFFERED=1
VOLUME ["/data"]
CMD ["xvfb-run", "-a", "-s", "-screen 0 1280x860x24", "python", "-m", "worker", "run"]
