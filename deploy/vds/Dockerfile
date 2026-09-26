# Docker Hub rate-limits anonymous pulls (429); Google's mirror serves the same official image.
FROM mirror.gcr.io/library/python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# Modulbank and zakupki.gov.ru use certificates of the Russian Mintsifry CA, absent from Debian's store.
COPY deploy/vds/certs/*.crt /usr/local/share/ca-certificates/
RUN update-ca-certificates
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python manage.py collectstatic --noinput
CMD ["gunicorn", "mysite.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--timeout", "600"]
