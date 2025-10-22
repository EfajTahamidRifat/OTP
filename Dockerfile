# Use official Python image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy files
COPY . .

# Install system dependencies for Playwright
RUN apt-get update && apt-get install -y wget gnupg curl && \
    pip install --no-cache-dir -r requirements.txt && \
    playwright install --with-deps chromium

# Expose no ports (bot uses Telegram polling)
ENV PYTHONUNBUFFERED=1

# Run the bot
CMD ["python", "main.py"]
