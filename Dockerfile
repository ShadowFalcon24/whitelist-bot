FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the bot code
COPY . .

# Make data folder if not mounted
RUN mkdir -p /app/data

CMD ["python", "main.py"]
