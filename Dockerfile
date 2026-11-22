FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Run unprivileged. /data is created here so a new named volume inherits this owner.
RUN useradd --system --uid 10001 quickshot && mkdir -p /data && chown quickshot /data
USER quickshot
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
