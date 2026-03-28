FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY filter_config.json ./
COPY accounts.json ./

CMD ["python", "run_all.py"]
