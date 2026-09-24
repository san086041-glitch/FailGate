FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY warden ./warden
RUN pip install --no-cache-dir .

EXPOSE 8080
CMD ["warden", "serve", "--host", "0.0.0.0", "--port", "8080"]
