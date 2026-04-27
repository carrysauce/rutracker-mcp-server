FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

# Railway injects PORT at runtime; default to 8000 for local use
ENV PORT=8000

EXPOSE 8000

CMD ["sh", "-c", "python server.py --transport streamable-http --host 0.0.0.0 --port ${PORT}"]
