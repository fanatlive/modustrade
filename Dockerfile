FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN DEBUG=1 DB_ENGINE=sqlite python manage.py collectstatic --noinput
RUN useradd --create-home app && chown -R app:app /app
USER app
EXPOSE 8000
CMD ["waitress-serve", "--listen=0.0.0.0:8000", "config.wsgi:application"]
