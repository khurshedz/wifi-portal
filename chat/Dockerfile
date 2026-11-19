FROM python:3.12-slim
RUN useradd -m -u 1000 user
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=user app.py info.py visits.py access.py backup.py ./
COPY --chown=user static ./static
USER user
ENV DB_PATH=/tmp/chat.db COOKIE_SECURE=1 PYTHONUNBUFFERED=1
EXPOSE 7860
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-7860} --proxy-headers --forwarded-allow-ips=*"]
