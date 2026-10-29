FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Europe/Moscow
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN mkdir -p /srv/data/uploads
ENV DB_PATH=/srv/data/pretenzii.db UPLOAD_DIR=/srv/data/uploads
EXPOSE 8000
# Один воркер: в процессе живут планировщик напоминаний и Telegram-бот
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
