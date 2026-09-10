FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home bot
COPY --chown=bot:bot . .
RUN mkdir -p /data/documents && chown -R bot:bot /data
USER bot
EXPOSE 8000
CMD ["sh", "-c", "uvicorn run_webhook:app --host 0.0.0.0 --port ${PORT}"]
