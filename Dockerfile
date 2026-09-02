FROM python:3.11-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt pymysql "psycopg[binary]"
COPY . .
ENV DATA_DIR=/srv/data
VOLUME ["/srv/data"]
EXPOSE 8000
CMD gunicorn -w 2 -b 0.0.0.0:${PORT:-8000} --timeout 60 wsgi:app
