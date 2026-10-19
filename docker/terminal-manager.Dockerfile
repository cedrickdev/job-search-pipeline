FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir fastapi uvicorn pydantic 'docker>=7.1,<8' 'websockets>=15,<16'
COPY terminal_manager ./terminal_manager

EXPOSE 8788
CMD ["uvicorn", "terminal_manager.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8788"]
