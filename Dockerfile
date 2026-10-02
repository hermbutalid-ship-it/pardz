# Use a lightweight base image for Python
FROM python:3.11-slim

# Set working directory inside the container
WORKDIR /app

# Copy requirement listings first to maximize Docker layer caching
COPY requirements.txt .

# Install dependencies smoothly
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of your application code
COPY main.py .

# Command to execute your bot script
CMD ["python", "main.py"]
