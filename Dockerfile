# Use Python slim image for smaller footprint
FROM python:3.11-slim

# Install AWS CLI and other dependencies
RUN apt-get update && apt-get install -y \
    curl \
    unzip \
    && curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" \
    && unzip awscliv2.zip \
    && ./aws/install \
    && rm -rf awscliv2.zip aws \
    && apt-get remove -y curl unzip \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY github_metrics.py .
COPY prometheus/prometheus.yml /etc/prometheus/prometheus.yml

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV AWS_DEFAULT_REGION=${AWS_REGION}

# Create non-root user
RUN useradd -m -r metrics
RUN chown -R metrics:metrics /app
USER metrics

# Expose Prometheus metrics port
EXPOSE 8000

# Start the metrics collector
CMD ["python", "github_metrics.py"]