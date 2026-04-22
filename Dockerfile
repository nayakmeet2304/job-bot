FROM python:3.13-slim

# Install Chrome and dependencies for Selenium
RUN apt-get update && apt-get install -y \
    chromium-browser \
    chromium \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Set work directory
WORKDIR /app

# Copy files
COPY requirements.txt bot.py .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Run bot
CMD ["python", "bot.py"]
