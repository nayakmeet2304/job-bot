FROM python:3.13-slim

# Install Chromium and dependencies for Selenium
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-sandbox \
    fonts-liberation \
    libappindicator3-1 \
    libasound2 \
    libatk1.0-0 \
    libatspi2.0-0 \
    libcups2 \
    libdbus-1-3 \
    libgdk-pixbuf2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libxss1 \
    xdg-utils \
    && rm -rf /var/lib/apt/lists/*

# Set work directory
WORKDIR /app

# Copy files
COPY requirements.txt bot.py .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Run bot
CMD ["python", "bot.py"]
