FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY autoheal.py .
ENV PYTHONUNBUFFERED=1
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD python -c "import os; assert os.path.exists('/app/autoheal.py')" || exit 1
CMD ["python", "autoheal.py"]
